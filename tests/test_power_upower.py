# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""UPower client and Power.native_paths: fake transports only, no real D-Bus."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from asusius.telemetry.power import Power
from asusius.telemetry.upower import (
    KIND_BY_ID,
    MAX_TIMESPAN_S,
    MIN_RESOLUTION_S,
    GioUpowerTransport,
    UpowerClient,
    UpowerError,
    UpowerUnavailable,
    kind_label,
    state_text,
)

REPO = Path(__file__).resolve().parent.parent
BAT0_PATH = "/org/freedesktop/UPower/devices/battery_BAT0"
DISPLAY_PATH = "/org/freedesktop/UPower/devices/DisplayDevice"

# a full laptop battery as GetAll(org.freedesktop.UPower.Device) returns it
BATTERY_PROPS = {
    "NativePath": "/sys/devices/LNXSYSTM:00/LNXSYBUS:00/PNP0C0A:00/power_supply/BAT0",
    "Type": 2, "State": 2, "Percentage": 87.5, "EnergyRate": -12.34, "Voltage": 7.4,
    "Energy": 40.2, "EnergyFull": 50.0, "EnergyFullDesign": 60.0, "CycleCount": 12,
    "IsPresent": True, "Model": "X", "Vendor": "Y", "Serial": "SECRET-SERIAL",
}


class FakeTransport:
    """In-memory UpowerTransport: canned devices and history, a call log and
    per-method failure knobs. No D-Bus anywhere."""

    def __init__(self, devices=None, history=None, owner=True, fail=None):
        self.devices = devices or {}  # object path -> props (dict) or junk
        self.history = history or {}  # (object path, history type) -> raw rows
        self.owner = owner
        self.fail = fail or {}  # method name -> exception to raise
        self.calls: list[str] = []
        self.call_args: dict[str, tuple] = {}

    def _maybe_raise(self, method: str) -> None:
        exc = self.fail.get(method)
        if exc is not None:
            raise exc

    def name_has_owner(self, name: str) -> bool:
        self.calls.append("name_has_owner")
        self._maybe_raise("name_has_owner")
        return self.owner

    def call(self, dest: str, path: str, iface: str, method: str,
             sig: str | None = None, args: tuple = ()):
        self.calls.append(method)
        self.call_args[method] = args
        self._maybe_raise(method)
        if method == "EnumerateDevices":
            return (sorted(self.devices),)
        if method == "GetHistory":
            assert sig == "(suu)" and args[0] in ("charge", "rate", "voltage")
            return (list(self.history.get((path, args[0]), [])),)
        raise AssertionError(f"unexpected call {method}")

    def get_all(self, dest: str, path: str, iface: str):
        self.calls.append("get_all")
        self._maybe_raise(f"get_all {path}")
        props = self.devices.get(path)
        return dict(props) if isinstance(props, dict) else props


class KindStateTests(unittest.TestCase):
    def test_kind_mapping_is_the_authoritative_collapse(self):
        expect = {0: "unknown", 1: "line_power", 2: "laptop", 3: "ups", 4: "peripheral",
                  5: "peripheral", 6: "peripheral", 7: "peripheral", 8: "peripheral",
                  9: "peripheral", 10: "peripheral", 11: "unknown", 12: "peripheral",
                  13: "peripheral", 14: "peripheral", 15: "peripheral"}
        for kind_id, want in expect.items():
            self.assertEqual(KIND_BY_ID[kind_id], want, kind_id)
        for kind_id in (29, 99):  # unknown ids collapse to unknown
            self.assertEqual(KIND_BY_ID.get(kind_id, "unknown"), "unknown")

    def test_kind_labels(self):
        for kind, want in (("laptop", "Laptop battery"), ("ups", "UPS"),
                           ("display", "Display device"), ("line_power", "AC adapter"),
                           ("peripheral", "Peripheral"), ("unknown", "Unknown device"),
                           ("nope", "Unknown device")):
            self.assertEqual(kind_label(kind), want)

    def test_state_text_covers_the_upower_enum(self):
        expect = {0: "unknown", 1: "charging", 2: "discharging", 3: "empty",
                  4: "fully-charged", 5: "pending-charge", 6: "pending-discharge"}
        for state_id, want in expect.items():
            self.assertEqual(state_text(state_id), want)
        self.assertEqual(state_text(99), "unknown")
        self.assertEqual(state_text(-1), "unknown")


class InventoryTests(unittest.TestCase):
    def test_device_properties_map_onto_the_info_record(self):
        devices, status = UpowerClient(FakeTransport(
            devices={BAT0_PATH: BATTERY_PROPS})).refresh_inventory()
        self.assertEqual(status, "UPower: 1 device")
        d = devices[0]
        self.assertEqual(d.object_path, BAT0_PATH)
        self.assertEqual(d.native_path, BATTERY_PROPS["NativePath"])
        self.assertEqual(d.kind, "laptop")
        self.assertEqual(d.type_text, "Laptop battery")
        self.assertIs(d.present, True)
        self.assertEqual(d.percent, 87.5)
        self.assertEqual(d.state, "discharging")
        self.assertEqual(d.watts, 12.34)  # sign folded: a magnitude, like power.py
        self.assertEqual(d.voltage, 7.4)
        self.assertEqual((d.energy, d.energy_full, d.energy_design), (40.2, 50.0, 60.0))
        self.assertEqual(d.cycles, 12)
        self.assertEqual(d.model, "X")
        self.assertEqual(d.vendor, "Y")
        self.assertEqual(d.serial, "SECRET-SERIAL")  # carried, never shown or stored

    def test_phone_mouse_and_computer_kinds(self):
        for type_id, kind, label in ((5, "peripheral", "Peripheral"),
                                     (8, "peripheral", "Peripheral"),
                                     (3, "ups", "UPS"), (1, "line_power", "AC adapter"),
                                     (4, "peripheral", "Peripheral"),
                                     (11, "unknown", "Unknown device")):
            t = FakeTransport(devices={BAT0_PATH: {"Type": type_id}})
            (d,), status = UpowerClient(t).refresh_inventory()
            self.assertEqual((d.kind, d.type_text), (kind, label), type_id)
            self.assertEqual(status, "UPower: 1 device")

    def test_genuine_zero_percent_stays_zero(self):
        t = FakeTransport(devices={BAT0_PATH: {"Type": 2, "Percentage": 0, "State": 3}})
        (d,), _status = UpowerClient(t).refresh_inventory()
        self.assertIsNotNone(d.percent)
        self.assertEqual(d.percent, 0)
        self.assertEqual(d.state, "empty")

    def test_coarse_level_replaces_the_approximate_percentage(self):
        t = FakeTransport(devices={BAT0_PATH: {"Type": 17, "Percentage": 20,
                                              "BatteryLevel": 3, "Voltage": 0}})
        (d,), _status = UpowerClient(t).refresh_inventory()
        self.assertEqual((d.kind, d.coarse_level), ("peripheral", "Low"))
        self.assertIsNone(d.percent)
        self.assertIsNone(d.voltage)

    def test_composite_device_is_identified_by_path(self):
        t = FakeTransport(devices={DISPLAY_PATH: {"Type": 2, "PowerSupply": True}})
        (d,), _status = UpowerClient(t).refresh_inventory()
        self.assertEqual(d.kind, "display")

    def test_absent_coarse_and_sentinel_percent_is_none(self):
        for props in ({"Type": 2}, {"Type": 2, "Percentage": -1},
                      {"Type": 2, "Percentage": "87"},
                      {"Type": 2, "Percentage": float("nan")}, {"Type": 2, "State": 99}):
            t = FakeTransport(devices={BAT0_PATH: props})
            devices, _status = UpowerClient(t).refresh_inventory()
            self.assertEqual([d.percent for d in devices], [None], props)
            self.assertEqual([d.watts for d in devices], [None])  # missing, not a 0
        # over-100 reports are clamped into the 0-100 range
        t = FakeTransport(devices={BAT0_PATH: {"Type": 2, "Percentage": 120.5}})
        (d,), _status = UpowerClient(t).refresh_inventory()
        self.assertEqual(d.percent, 100.0)

    def test_empty_properties_are_skipped_as_unreadable(self):
        t = FakeTransport(devices={BAT0_PATH: {}})
        devices, status = UpowerClient(t).refresh_inventory()
        self.assertEqual(devices, [])
        self.assertEqual(status, "UPower: 0 devices, 1 unreadable")

    def test_sparse_properties_read_as_none(self):
        t = FakeTransport(devices={BAT0_PATH: {"Type": 2, "CycleCount": -1}})
        (d,), _status = UpowerClient(t).refresh_inventory()
        self.assertIsNone(d.present)
        self.assertIsNone(d.state)  # missing State property stays None, not "unknown"
        self.assertIsNone(d.voltage)
        self.assertIsNone(d.cycles)  # UPower's -1 "unknown" sentinel
        self.assertIsNone(d.model)
        self.assertEqual(d.native_path, "")  # "" when UPower gives none

    def test_absent_service_reads_as_not_available(self):
        t = FakeTransport(owner=False, devices={BAT0_PATH: BATTERY_PROPS})
        devices, status = UpowerClient(t).refresh_inventory()
        self.assertEqual((devices, status), ([], "UPower not available"))
        self.assertNotIn("EnumerateDevices", t.calls)  # never poked, no activation

    def test_transport_unavailable_reads_as_not_available(self):
        t = FakeTransport(fail={"name_has_owner": UpowerUnavailable("no gi")})
        devices, status = UpowerClient(t).refresh_inventory()
        self.assertEqual((devices, status), ([], "UPower not available"))

    def test_enumerate_failure_is_status_with_no_retry(self):
        t = FakeTransport(fail={"EnumerateDevices": UpowerError("bus exploded")})
        devices, status = UpowerClient(t).refresh_inventory()
        self.assertEqual(devices, [])
        self.assertEqual(status, "UPower query failed: bus exploded")
        self.assertEqual(t.calls.count("EnumerateDevices"), 1)  # one pass, no storm

    def test_unreadable_or_garbage_devices_are_skipped(self):
        other = "/org/freedesktop/UPower/devices/battery_BAT1"
        t = FakeTransport(devices={BAT0_PATH: BATTERY_PROPS, other: "not a dict"},
                          fail={f"get_all {other}": UpowerError("cannot read")})
        devices, status = UpowerClient(t).refresh_inventory()
        self.assertEqual([d.object_path for d in devices], [BAT0_PATH])
        self.assertEqual(status, "UPower: 1 device, 1 unreadable")


class HistoryTests(unittest.TestCase):
    def test_charge_triples_pass_through_with_the_right_type(self):
        rows = [(1000, 87.5, 2), (1060, 86.0, 2)]
        t = FakeTransport(history={(BAT0_PATH, "charge"): rows})
        pts = UpowerClient(t).get_history(BAT0_PATH, "charge", 3600, 30)
        self.assertEqual([(p.t, p.value, p.state) for p in pts], rows)
        self.assertEqual(t.call_args["GetHistory"], ("charge", 3600, 30))

    def test_rate_and_voltage_series_types(self):
        for series in ("rate", "voltage"):
            t = FakeTransport(history={(BAT0_PATH, series): [(1000, 1.5, 2)]})
            pts = UpowerClient(t).get_history(BAT0_PATH, series, 3600, 30)
            self.assertEqual([(p.t, p.value, p.state) for p in pts], [(1000, 1.5, 2)])
            self.assertEqual(t.call_args["GetHistory"], (series, 3600, 30))

    def test_timespan_and_resolution_are_clamped(self):
        t = FakeTransport(history={(BAT0_PATH, "rate"): []})
        pts = UpowerClient(t).get_history(BAT0_PATH, "rate", 10 * MAX_TIMESPAN_S, 5)
        self.assertEqual(pts, [])
        self.assertEqual(t.call_args["GetHistory"], ("rate", MAX_TIMESPAN_S, MIN_RESOLUTION_S))

    def test_display_device_object_path_works(self):
        t = FakeTransport(history={(DISPLAY_PATH, "charge"): [(1000, 55.0, 1)]})
        pts = UpowerClient(t).get_history(DISPLAY_PATH, "charge", 3600, 30)
        self.assertEqual([p.value for p in pts], [55.0])

    def test_older_uint_value_shape(self):  # a(uuu): value as uint
        t = FakeTransport(history={(BAT0_PATH, "charge"): [(1000, 87, 2), (1100, 80, 4)]})
        pts = UpowerClient(t).get_history(BAT0_PATH, "charge", 3600, 30)
        self.assertEqual([(p.t, p.value, p.state) for p in pts],
                         [(1000, 87.0, 2), (1100, 80.0, 4)])

    def test_older_pair_shape_reads_as_unknown_state(self):  # (t, value) only
        t = FakeTransport(history={(BAT0_PATH, "charge"): [(1000, 87.5), (1100, 80)]})
        pts = UpowerClient(t).get_history(BAT0_PATH, "charge", 3600, 30)
        self.assertEqual([(p.t, p.value, p.state) for p in pts],
                         [(1000, 87.5, 0), (1100, 80.0, 0)])

    def test_unusable_rows_are_dropped(self):
        t = FakeTransport(history={(BAT0_PATH, "charge"): [("x", 1, 2), (1000, 87.5, 2),
                                                    (), (5,), (None, 1.0, 2)]})
        pts = UpowerClient(t).get_history(BAT0_PATH, "charge", 3600, 30)
        self.assertEqual([(p.t, p.value, p.state) for p in pts], [(1000, 87.5, 2)])

    def test_history_transport_failure_raises(self):
        t = FakeTransport(fail={"GetHistory": UpowerError("bus exploded")})
        c = UpowerClient(t)
        with self.assertRaisesRegex(UpowerError, "bus exploded"):
            c.get_history(BAT0_PATH, "rate", 3600, 30)
        self.assertEqual(t.calls.count("GetHistory"), 1)  # no retry storm

    def test_history_service_absence_raises(self):
        t = FakeTransport(owner=False)
        c = UpowerClient(t)
        with self.assertRaisesRegex(UpowerError, "UPower not available"):
            c.get_history(BAT0_PATH, "rate", 3600, 30)
        self.assertNotIn("GetHistory", t.calls)

    def test_unknown_series_is_a_caller_bug(self):
        with self.assertRaises(ValueError):
            UpowerClient(FakeTransport()).get_history(BAT0_PATH, "power", 3600, 30)


class TransportShapeTests(unittest.TestCase):
    def test_gio_transport_classifies_errors_without_touching_gi(self):
        wrap = GioUpowerTransport._wrap
        unavailable = wrap(Exception("GDBus.Error:org.freedesktop.DBus.Error."
                                     "ServiceUnknown: The name was not provided"))
        self.assertIsInstance(unavailable, UpowerUnavailable)
        self.assertIsInstance(wrap(Exception("Timeout was reached")), UpowerError)
        other = wrap(Exception("something else"))
        self.assertIsInstance(other, UpowerError)
        self.assertNotIsInstance(other, UpowerUnavailable)


class ModuleScopeTests(unittest.TestCase):
    def test_modules_import_without_gi_or_ui(self):
        env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
        code = ("import sys"
                "; import asusius.telemetry.upower"
                "; import asusius.telemetry.power_history"
                "; import asusius.telemetry.power"
                "; from asusius.telemetry.upower import UpowerClient"
                "; UpowerClient()"  # default transport: constructs without gi
                "; print('gi' in sys.modules, 'asusius.ui' in sys.modules)")
        res = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, cwd=str(REPO), env=env)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "False False")


def build_power_tree(root: Path) -> None:
    def put(rel: str, text: str) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    put("sys/class/power_supply/BAT0/type", "Battery\n")
    put("sys/class/power_supply/BAT0/status", "Discharging\n")
    put("sys/class/power_supply/BAT0/capacity", "87\n")
    put("sys/class/power_supply/BAT0/voltage_now", "7400000\n")
    put("sys/class/power_supply/BAT0/current_now", "1200000\n")
    put("sys/class/power_supply/BAT0/energy_now", "40200000\n")
    put("sys/class/power_supply/BAT0/energy_full", "50000000\n")
    put("sys/class/power_supply/BAT0/energy_full_design", "60000000\n")
    put("sys/class/power_supply/BAT0/cycle_count", "12\n")
    put("sys/class/power_supply/BAT0/charge_control_end_threshold", "80\n")
    put("sys/class/power_supply/BAT0/model_name", "ASUS battery\n")
    put("sys/class/power_supply/BAT0/technology", "Li-ion\n")
    put("sys/class/power_supply/AC/type", "Mains\n")
    put("sys/class/power_supply/AC/online", "1\n")


class PowerNativePathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_native_paths_are_the_battery_dirs(self):
        build_power_tree(self.root)
        p = Power(root=str(self.root))
        self.assertEqual(p.native_paths, p.batteries)
        self.assertEqual(p.native_paths, [str(self.root / "sys/class/power_supply/BAT0")])

    def test_fast_path_keys_and_units_unchanged(self):
        build_power_tree(self.root)
        vals, info = {}, {}
        Power(root=str(self.root)).sample(0.0, vals, info)
        self.assertEqual(set(vals), {"bat.power", "bat.percent", "bat.voltage"})
        self.assertEqual(set(info["battery"]),
                         {"present", "ac_online", "name", "status", "percent", "watts",
                          "voltage", "energy_now", "energy_full", "energy_design",
                          "cycles", "limit", "model", "technology", "hours"})
        self.assertAlmostEqual(vals["bat.power"], 1200000 * 7400000 / 1e12)
        self.assertEqual(vals["bat.percent"], 87)
        self.assertAlmostEqual(vals["bat.voltage"], 7.4)
        b = info["battery"]
        self.assertIs(b["present"], True)
        self.assertIs(b["ac_online"], True)
        self.assertEqual(b["name"], "BAT0")
        self.assertEqual(b["status"], "Discharging")
        self.assertEqual(b["energy_now"], 40200000)  # raw sysfs units, preserved
        self.assertEqual(b["energy_full"], 50000000)
        self.assertEqual(b["energy_design"], 60000000)
        self.assertEqual(b["cycles"], 12)
        self.assertEqual(b["limit"], 80)
        self.assertAlmostEqual(b["hours"], 40.2 / (1200000 * 7400000 / 1e12))

    def test_no_battery_fast_path_unchanged(self):
        (self.root / "sys/class/power_supply/AC").mkdir(parents=True)
        (self.root / "sys/class/power_supply/AC/type").write_text("Mains\n")
        (self.root / "sys/class/power_supply/AC/online").write_text("1\n")
        vals, info = {}, {}
        Power(root=str(self.root)).sample(0.0, vals, info)
        self.assertEqual(vals, {})
        self.assertEqual(info["battery"], {"present": False, "ac_online": True})


if __name__ == "__main__":
    unittest.main()
