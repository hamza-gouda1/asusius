# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""M4 power integration: fake transport -> UpowerClient -> LiveHub, end to end.

No real D-Bus and no GTK main loop: LiveHub is constructed with GLib available and
an explicitly injected fake UPower client, its telemetry timer is never started
(ticks are driven with collect_now) and pending worker-pool callbacks are delivered
by iterating the default GLib main context (the same pattern the QA harness uses).
Also here: the sysfs dGPU probe gate (a suspended or unreadable GPU is never woken
by nvidia-smi) and the RAPL wrap/reset handling in _rapl_power.
"""
import os
import shutil
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from gi.repository import GLib

from asusius.backends.fake import build_fake_tree
from asusius.backends.sysfs import SysfsBackend
from asusius.telemetry.fake import (
    UPOWER_AC,
    UPOWER_BAT0,
    UPOWER_DUP,
    UPOWER_SYSFS_BAT0,
    FakeUpowerTransport,
    fake_upower_client,
    fake_upower_transport,
    upower_battery_history,
    upower_battery_props,
)
from asusius.telemetry.upower import UpowerClient, UpowerDeviceInfo
from asusius.ui.live import POWER_MAX_ERRORS, LiveHub


def pump_until(cond, timeout_s: float = 5.0) -> bool:
    """Dispatch the default GLib context until cond() holds (worker -> main loop)."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout_s
    while not cond():
        if time.monotonic() >= deadline:
            return False
        ctx.iteration(False)
        time.sleep(0.005)
    return True


def drain(seconds: float = 0.5) -> None:
    """Let queued main-loop callbacks run (and prove nothing more arrives)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        GLib.MainContext.default().iteration(False)
        time.sleep(0.005)


class FakePage:
    """The seam the battery page codes against: hub -> page on the main loop."""

    def __init__(self) -> None:
        self.inventories: list = []

    def on_power_inventory(self, devices, status_text) -> None:
        self.inventories.append((list(devices), status_text))


class HubTestCase(unittest.TestCase):
    """LiveHub against a fake tree with an injected fake UPower client."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = build_fake_tree(Path(self._tmp.name) / "root")
        self._old_state = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(Path(self._tmp.name) / "state")
        self.addCleanup(self._restore_state)
        self.transport = FakeUpowerTransport()
        self.hub = LiveHub(fake_root=str(self.root),
                           upower=UpowerClient(self.transport))
        self.transport.add(UPOWER_AC, Type=1, Model="AC", NativePath="/sys/devices/AC")
        self.transport.add(UPOWER_BAT0, **upower_battery_props())
        self.transport.set_history(UPOWER_BAT0, "charge",
                                   upower_battery_history()["charge"])
        self.transport.set_history(UPOWER_BAT0, "rate", upower_battery_history()["rate"])
        self.transport.set_history(UPOWER_BAT0, "voltage",
                                   upower_battery_history()["voltage"])
        self.page = FakePage()
        self.addCleanup(self.hub.close)

    def _restore_state(self) -> None:
        if self._old_state is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self._old_state


class FakeFixtureTests(unittest.TestCase):
    def test_client_builder_scenarios(self):
        for scenario, kinds in (("populated", {"laptop", "line_power"}),
                                ("short", {"laptop", "line_power"}),
                                ("empty", {"laptop", "line_power"}),
                                ("peripheral", {"laptop", "peripheral", "line_power"}),
                                ("multi", {"laptop", "line_power"}),
                                ("duplicate", {"laptop", "line_power"}),
                                ("ups", {"laptop", "ups", "line_power"})):
            client = fake_upower_client(scenario)
            devices, status = client.refresh_inventory()
            self.assertEqual(status, f"UPower: {len(devices)} devices", scenario)
            self.assertEqual({d.kind for d in devices}, kinds, scenario)
        with self.assertRaises(ValueError):
            fake_upower_transport("nope")

    def test_populated_history_shape(self):
        rows = upower_battery_history()
        self.assertEqual(sorted(rows), ["charge", "rate", "voltage"])
        for series, rows_ in rows.items():
            ts = [t for t, _v, _s in rows_]
            self.assertEqual(ts, sorted(ts), series)  # oldest first
            self.assertGreater(len(ts), 100, series)
            states = {s for _t, _v, s in rows_}
            self.assertEqual(states, {1, 2}, series)  # charging + discharging
        # one gap: a stretch with no rows at all, mid-history
        charge = rows["charge"]
        gaps = [b[0] - a[0] for a, b in zip(charge, charge[1:]) if b[0] - a[0] > 600]
        self.assertEqual(len(gaps), 1, "expected exactly one gap")
        self.assertEqual(charge[-1][0], int(1_810_000_000))  # newest point = base_t

    def test_short_history(self):
        rows = upower_battery_history(max_points=5)
        for series, rows_ in rows.items():
            self.assertEqual(len(rows_), 5, series)


class HistoryEndToEndTests(HubTestCase):
    def test_per_series_request_returns_raw_rows(self):
        got, errors = [], []
        self.hub.request_power_history(f"{UPOWER_BAT0}|charge", 3600, got.append,
                                       errors.append)
        self.assertTrue(pump_until(lambda: got or errors), "no history delivered")
        self.assertEqual(errors, [])
        rows = got[0]
        self.assertEqual([(p.t, p.value, p.state) for p in rows],
                         upower_battery_history()["charge"])

    def test_bare_object_path_returns_all_three_series(self):
        got, errors = [], []
        self.hub.request_power_history(UPOWER_BAT0, 3600, got.append, errors.append)
        self.assertTrue(pump_until(lambda: got or errors), "no history delivered")
        self.assertEqual(errors, [])
        payload = got[0]
        self.assertEqual(sorted(payload), ["charge", "rate", "voltage"])
        expect = upower_battery_history()
        for series in ("charge", "rate", "voltage"):
            self.assertEqual([(p.t, p.value, p.state) for p in payload[series]],
                             expect[series])

    def test_stale_response_is_dropped_and_fresh_delivered(self):
        got, errors = [], []
        # two requests for the same (dev_key, range): the second supersedes the
        # first, so exactly one response may arrive whichever order the pool runs
        self.hub.request_power_history(f"{UPOWER_BAT0}|rate", 3600, got.append,
                                       errors.append)
        self.hub.request_power_history(f"{UPOWER_BAT0}|rate", 3600, got.append,
                                       errors.append)
        self.assertTrue(pump_until(lambda: got or errors), "no history delivered")
        drain()  # nothing else may arrive
        self.assertEqual(len(got), 1, "a stale response was delivered")
        self.assertEqual(errors, [])
        self.assertEqual([(p.t, p.value, p.state) for p in got[0]],
                         upower_battery_history()["rate"])

    def test_token_guard_is_per_device_and_range(self):
        got: list = []
        # different ranges for the same device are independent requests: both deliver
        self.hub.request_power_history(f"{UPOWER_BAT0}|charge", 3600,
                                       lambda r: got.append(("short", r)), None)
        self.hub.request_power_history(f"{UPOWER_BAT0}|charge", 7200,
                                       lambda r: got.append(("long", r)), None)
        self.assertTrue(pump_until(lambda: len(got) >= 2))
        self.assertEqual(sorted(tag for tag, _r in got), ["long", "short"])
        for _tag, rows in got:
            self.assertEqual([(p.t, p.value, p.state) for p in rows],
                             upower_battery_history()["charge"])

    def test_absent_service_uses_error_callback(self):
        got, errors = [], []
        self.transport.owner = False
        self.hub.request_power_history(f"{UPOWER_BAT0}|charge", 3600, got.append,
                                       errors.append)
        self.assertTrue(pump_until(lambda: got or errors))
        self.assertEqual(got, [])
        self.assertEqual([str(e) for e in errors], ["UPower not available"])
        self.assertEqual(self.transport.calls.count("GetHistory"), 0)  # no retry storm

    def test_split_history_key(self):
        for key, want in ((f"{UPOWER_BAT0}|rate", (UPOWER_BAT0, "rate")),
                          (f"{UPOWER_BAT0}|voltage", (UPOWER_BAT0, "voltage")),
                          (UPOWER_BAT0, (UPOWER_BAT0, None)),
                          ("aggregate:all", ("aggregate:all", None))):
            self.assertEqual(LiveHub._split_history_key(key), want, key)


class InventoryTests(HubTestCase):
    def shown_cycle(self) -> None:
        """hidden + shown: one fresh inventory pass must land on the page."""
        before = len(self.page.inventories)
        self.hub.power_page_hidden(self.page)
        self.hub.power_page_shown(self.page)
        self.assertTrue(pump_until(lambda: len(self.page.inventories) > before),
                        "inventory never delivered")

    def test_inventory_delivered_on_show(self):
        self.hub.power_page_shown(self.page)
        self.assertTrue(pump_until(lambda: self.page.inventories))
        devices, status = self.page.inventories[-1]
        self.assertEqual(status, "UPower: 2 devices")
        self.assertEqual([d.object_path for d in devices],
                         sorted([UPOWER_BAT0, UPOWER_AC]))

    def test_sysfs_duplicate_kept_history_reachable(self):
        """native_path matching a sysfs battery dir still reaches the page: the
        object path is the only route to that battery's history, and the page
        renders one row per record, so nothing appears twice. Unmatched and
        empty-native_path records stay separate rows."""
        sysfs_bat = str(self.root / "sys/class/power_supply/BAT0")
        visible = self.hub._sysfs_visible([
            UpowerDeviceInfo(UPOWER_BAT0, native_path=UPOWER_SYSFS_BAT0, kind="laptop"),
            UpowerDeviceInfo("/dup", native_path=sysfs_bat, kind="laptop"),
            UpowerDeviceInfo("/anon", native_path="", kind="peripheral"),
            UpowerDeviceInfo("/other", native_path="/sys/other/BAT9", kind="ups"),
        ])
        self.assertEqual([d.object_path for d in visible],
                         [UPOWER_BAT0, "/dup", "/anon", "/other"])
        # the same through the delivery path: a fake UPower record pointing at the
        # fake tree's sysfs battery still reaches the page (history stays reachable)
        self.transport.add("/in-tree", **upower_battery_props(native_path=sysfs_bat))
        self.shown_cycle()
        paths = [d.object_path for d in self.page.inventories[-1][0]]
        self.assertIn("/in-tree", paths)  # native_path matches sysfs: still kept
        self.assertIn(UPOWER_BAT0, paths)  # unmatched native_path: kept
        self.assertIn(UPOWER_AC, paths)

    def test_repeated_native_device_is_deduplicated(self):
        self.transport.add(UPOWER_DUP, **upower_battery_props())
        self.shown_cycle()
        paths = [d.object_path for d in self.page.inventories[-1][0]]
        self.assertEqual(paths.count(UPOWER_BAT0), 1)
        self.assertNotIn(UPOWER_DUP, paths)

    def test_hotplug_removal_and_restart(self):
        self.shown_cycle()
        self.assertEqual(len(self.page.inventories[-1][0]), 2)  # battery + AC
        self.transport.remove(UPOWER_BAT0)  # unplugged/gone: inventory shrinks
        self.shown_cycle()
        self.assertEqual([d.object_path for d in self.page.inventories[-1][0]],
                         [UPOWER_AC])
        self.transport.owner = False  # service stopped
        self.shown_cycle()
        devices, status = self.page.inventories[-1]
        self.assertEqual((devices, status), ([], "UPower not available"))
        self.transport.owner = True  # service restarted
        self.shown_cycle()
        devices, status = self.page.inventories[-1]
        self.assertEqual(status, "UPower: 1 device")

    def test_poll_backs_off_and_caps_retries(self):
        self.hub._power_page = self.page
        for _ in range(POWER_MAX_ERRORS):
            self.hub._power_inventory_done(([], "UPower not available"))
        self.assertEqual(self.hub._power_errors, POWER_MAX_ERRORS)
        self.assertIsNotNone(self.hub._power_timer)  # bounded backoff, able to recover
        self.assertEqual(len(self.page.inventories), POWER_MAX_ERRORS)
        self.hub._power_inventory_done((
            [UpowerDeviceInfo(UPOWER_BAT0, kind="laptop")], "UPower: 1 device"))
        self.assertEqual(self.hub._power_errors, 0)  # success resets and re-arms
        self.assertIsNotNone(self.hub._power_timer)
        self.assertEqual(len(self.page.inventories), POWER_MAX_ERRORS + 1)


class VoltageStoreTests(HubTestCase):
    def test_local_voltage_fallback_records_and_reads_back_a_verified_battery(self):
        state = Path(os.environ["XDG_STATE_HOME"]) / "asusius" / "voltage"
        bat = self.root / "sys/class/power_supply/BAT0"
        (bat / "serial_number").write_text("BAT-ID\n")
        self.transport.devices[UPOWER_BAT0].update(NativePath="BAT0", Serial="BAT-ID")
        self.hub.collect_now(time.monotonic())
        self.assertFalse(state.exists())
        self.hub.power_page_shown(self.page)
        self.assertTrue(pump_until(lambda: self.page.inventories))
        self.transport.set_history(UPOWER_BAT0, "voltage", [])
        got, errors = [], []
        key = f"{UPOWER_BAT0}|voltage"
        self.hub.request_power_history(key, 3600, got.append, errors.append)
        self.assertTrue(pump_until(lambda: list(state.glob("*.json"))))
        self.hub.request_power_history(key, 3600, got.append, errors.append)
        self.assertTrue(pump_until(lambda: len(got) == 2 or errors))
        self.assertEqual(errors, [])
        self.assertEqual(len(got[1]), 1)
        self.assertAlmostEqual(got[1][0].voltage, 15.82, places=2)
        self.assertNotIn("BAT-ID", next(state.glob("*.json")).read_text())

        # A replacement drive with the same name and model has another identity.
        (bat / "serial_number").write_text("REPLACED\n")
        self.transport.devices[UPOWER_BAT0]["Serial"] = "REPLACED"
        self.hub.power_page_hidden(self.page)
        prev = len(self.page.inventories)
        self.hub.power_page_shown(self.page)
        self.assertTrue(pump_until(lambda: len(self.page.inventories) > prev))
        self.hub.request_power_history(key, 3600, got.append, errors.append)
        self.assertTrue(pump_until(lambda: len(got) == 3 or errors))
        self.assertEqual(got[2], [])

    def test_missing_battery_reports_no_values_and_no_file(self):
        shutil.rmtree(self.root / "sys/class/power_supply")
        self.hub.power_page_shown(self.page)
        self.assertTrue(pump_until(lambda: self.page.inventories))
        self.hub.collect_now(time.monotonic())
        state = Path(os.environ["XDG_STATE_HOME"]) / "asusius" / "voltage"
        self.assertFalse(state.exists())  # missing = a gap, never a fabricated row


class SysfsNoProbeTests(unittest.TestCase):
    """A suspended or unreadable NVIDIA dGPU is never woken by nvidia-smi."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = build_fake_tree(Path(self._tmp.name) / "root")
        self.calls: list = []

        def spy(cmd, **_kw):
            self.calls.append(cmd)
            return "46, 12.40, 3"

        patcher = unittest.mock.patch("asusius.backends.sysfs.run_cmd", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def backend(self) -> SysfsBackend:
        return SysfsBackend(self.root, use_nvidia_smi=True)

    def test_unreadable_runtime_state_is_not_probed(self):
        (self.root / "sys/bus/pci/devices/0000:01:00.0/power/runtime_status").unlink()
        self.assertIsNone(self.backend().get_telemetry().gpu_runtime)
        self.assertEqual(self.calls, [])

    def test_suspended_gpu_is_not_probed(self):
        (self.root / "sys/bus/pci/devices/0000:01:00.0/power/runtime_status").write_text(
            "suspended\n")
        self.assertEqual(self.backend().get_telemetry().gpu_runtime, "suspended")
        self.assertEqual(self.calls, [])

    def test_active_gpu_is_probed(self):
        t = self.backend().get_telemetry()
        self.assertEqual(t.gpu_runtime, "active")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "nvidia-smi")
        self.assertIn("--query-gpu=", " ".join(self.calls[0]))

    def test_no_pci_sysfs_cannot_establish_that_a_probe_is_safe(self):
        shutil.rmtree(self.root / "sys/bus/pci/devices")
        self.backend().get_telemetry()
        self.assertEqual(self.calls, [])


class RaplPowerTests(unittest.TestCase):
    """_rapl_power: wrap-aware with a known range, a gap without one."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = build_fake_tree(Path(self._tmp.name) / "root")

    def backend(self, now: float, range_uj: int | None = 2_000_000_000):
        if range_uj is not None:
            (self.root / "sys/class/powercap/intel-rapl:0/max_energy_range_uj").write_text(
                f"{range_uj}\n")
        b = SysfsBackend(self.root, use_nvidia_smi=False)
        patcher = unittest.mock.patch("asusius.backends.sysfs.time",
                                      SimpleNamespace(monotonic=lambda: now))
        patcher.start()
        self.addCleanup(patcher.stop)
        return b

    def energy(self, value: int) -> None:
        (self.root / "sys/class/powercap/intel-rapl:0/energy_uj").write_text(f"{value}\n")

    def test_wrap_with_known_range(self):
        b = self.backend(1000.5)
        self.energy(2_000_000_000)  # the counter sat at its ceiling...
        b._rapl_prev = (2_000_000_000, 1000.0)
        self.energy(500_000_000)    # ...and wrapped to 0.5e9
        # de = -1.5e9 < 0 -> + 2e9 range -> 0.5e9 uJ over 0.5 s = 1000.0 W
        self.assertEqual(b._rapl_power(), 1000.0)

    def test_wrap_without_range_is_a_gap(self):
        b = self.backend(1000.5, range_uj=None)  # no max_energy_range_uj in the tree
        b._rapl_prev = (1_000_000_000, 1000.0)
        self.energy(500_000_000)
        self.assertIsNone(b._rapl_power())

    def test_drop_past_one_wrap_is_a_gap(self):
        """A drop bigger than the known range (reboot/rearm or a wrong range file)
        is not wrap math: it stays a gap (consistent with telemetry.sensors)."""
        b = self.backend(1000.5, range_uj=500_000_000)
        b._rapl_prev = (1_000_000_000, 1000.0)
        self.energy(100_000)  # de + range is still < 0
        self.assertIsNone(b._rapl_power())

    def test_reset_within_one_wrap_reads_as_the_wrapped_delta(self):
        """A counter that restarted just above zero wraps to a small delta (the
        documented wrap math, as telemetry.sensors applies it)."""
        b = self.backend(1000.5)
        b._rapl_prev = (2_000_000_000, 1000.0)
        self.energy(100_000)
        self.assertEqual(b._rapl_power(), 0.2)  # 100000 uJ / 1e6 / 0.5 s

    def test_monotonic_counter(self):
        b = self.backend(1000.5)
        b._rapl_prev = (1_000_000_000, 1000.0)
        self.energy(1_100_000_000)
        self.assertEqual(b._rapl_power(), 200.0)  # 1e8 uJ / 1e6 / 0.5 s

    def test_mid_range_reset_is_a_gap(self):
        b = self.backend(1000.5)
        b._rapl_prev = (1_000_000_000, 1000.0)
        self.energy(100_000)
        self.assertIsNone(b._rapl_power())

    def test_short_interval_is_a_gap(self):
        b = self.backend(1000.1)  # the <0.2 s guard stays
        b._rapl_prev = (1_000_000_000, 1000.0)
        self.assertIsNone(b._rapl_power())

    def test_first_reading_is_a_gap(self):
        b = self.backend(1000.5)
        self.assertIsNone(b._rapl_power())


if __name__ == "__main__":
    unittest.main()
