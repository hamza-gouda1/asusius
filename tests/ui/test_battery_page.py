# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""The battery page over a stub hub: sections, selector, history merge and fallbacks.

The environment is isolated before any gi import (memory GSettings backend, XDG dirs
in a throwaway tempdir) so the owner's dconf is never touched. The page is built for
real on a stub window; its collaborators are doubles: the hub records the
power_page_shown/hidden calls and serves canned history through the contract's token
guard (the latest request per (dev_key, range_s) wins, stale ones are dropped), and
the chart is a recording stand-in for C's ui.history_chart.HistoryChart.
"""
import os
import tempfile  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import Mock, patch  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="asusius-ui-battery-page-"))
for _sub in ("config", "data", "state"):
    (_TMP / _sub).mkdir()
os.environ["GSETTINGS_BACKEND"] = "memory"  # before anything imports gi
_XDG_KEYS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")
_OLD_XDG = {k: os.environ.get(k) for k in _XDG_KEYS}


def setUpModule():
    """XDG dirs are scoped to this module's tests: import-time state here must never
    reach another module's tests (and vice versa)."""
    for key, sub in zip(_XDG_KEYS, ("config", "data", "state")):
        os.environ[key] = str(_TMP / sub)


def tearDownModule():
    for key, value in _OLD_XDG.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from asusius.core import Telemetry  # noqa: E402
from asusius.telemetry.upower import UpowerDeviceInfo  # noqa: E402
from asusius.ui.pages.base import a11y_name  # noqa: E402
from asusius.ui.pages.battery import (  # noqa: E402
    AGGREGATE_KEY,
    EMPTY_TEXT,
    RANGES,
    SERIES_FIELD,
    BatteryPage,
    aggregate_history,
    battery_weight,
    build_bands,
    device_kind,
    device_name,
    device_subtitle,
    history_key,
    level_text,
    merge_series,
    state_text,
    summary_lines,
)

HISTORY_SERIES = ("rate", "charge", "voltage")
BAT0 = "/org/freedesktop/UPower/devices/battery_BAT0"
BAT1 = "/org/freedesktop/UPower/devices/battery_BAT1"
MOUSE = "/org/freedesktop/UPower/devices/mouse_mouse"
UPS = "/org/freedesktop/UPower/devices/ups_ups"


def pump(seconds: float = 0.25) -> None:
    """Run the default main context until the stub hub's idle deliveries and the
    worker-pool callbacks (run_bg) have landed."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not ctx.iteration(False):
            time.sleep(0.005)


# -- collaborators ---------------------------------------------------------------------------


class StubHub:
    """C's LiveHub seam: canned inventory + history with the contract §5 token guard."""

    def __init__(self, info=None):
        self.info = info or {}
        self.shown: list = []
        self.hidden: list = []
        self.requests: list = []
        self.responses: dict = {}
        self.tokens: dict = {}
        self.dropped = 0
        self._next_token = 0

    def power_page_shown(self, page):
        self.shown.append(page)

    def power_page_hidden(self, page):
        self.hidden.append(page)

    def request_power_history(self, dev_key, range_s, done, error):
        self.requests.append((dev_key, range_s))
        self._next_token += 1
        key = (dev_key, range_s)
        token = self.tokens[key] = self._next_token
        GLib.idle_add(self._deliver, token, key, self.responses.get(key), done, error)

    def _deliver(self, token, key, resp, done, error):
        if self.tokens.get(key) != token:
            self.dropped += 1  # stale: silently dropped before done/error fire (§5)
            return GLib.SOURCE_REMOVE
        if isinstance(resp, Exception):
            error(resp)
        else:
            done(list(resp or []))
        return GLib.SOURCE_REMOVE


class StubCtl:
    """Controller stand-in feeding the preserved charge-limit and health reads."""

    def __init__(self):
        self.limit = 80

    def get_battery_threshold(self):
        return self.limit, None

    def read(self, cap):
        return SimpleNamespace(health_percent=91.0, energy_full_uwh=50_000_000,
                               energy_full_design_uwh=55_000_000, cycle_count=120), None

    def set_battery_threshold(self, v):
        self.limit = v

    def one_shot_full_charge(self):
        pass


class ChartStub:
    """Recording stand-in for C's HistoryChart (set_data / set_empty_text)."""

    def __init__(self):
        self.data = []  # (t_end, span_s, series, states)
        self.empty = []

    def set_data(self, t_end, span_s, series, states):
        self.data.append((t_end, span_s, series, states))

    def set_empty_text(self, text):
        self.empty.append(text)


def make_win(hub):
    return SimpleNamespace(hub=hub, ctl=StubCtl(), caps={}, caps_error=None, fake=True,
                           toast_error=Mock(), run_write=Mock(), profile=None)


def build_page(win, chart=None) -> BatteryPage:
    if chart is None:
        return BatteryPage(win)
    with patch.object(BatteryPage, "_make_chart", lambda self: chart):
        return BatteryPage(win)


# -- fixtures -------------------------------------------------------------------------------------


def laptop(path=BAT0, **kw):
    kw.setdefault("type_text", "Laptop battery")
    return UpowerDeviceInfo(object_path=path, kind="laptop", **kw)


def peripheral(path=MOUSE, **kw):
    kw.setdefault("type_text", "Peripheral")
    return UpowerDeviceInfo(object_path=path, kind="peripheral", **kw)


def sample_devices():
    """One laptop battery, one peripheral with no level, a UPS and the two kinds that
    must never become rows (AC line power and the DisplayDevice composite)."""
    return [
        laptop(native_path="/sys/class/power_supply/BAT0", model="ASUS Battery",
               percent=62.0, state="discharging", watts=8.3, voltage=15.4,
               energy_full=50.0),
        peripheral(model="Wireless Mouse", percent=None, state="discharging"),
        UpowerDeviceInfo(object_path=UPS, kind="ups", type_text="UPS", percent=100.0,
                         watts=0.0, voltage=230.0, state="fully-charged"),
        UpowerDeviceInfo(object_path="/org/freedesktop/UPower/devices/line_ac",
                         kind="line_power", type_text="AC adapter"),
        UpowerDeviceInfo(object_path="/org/freedesktop/UPower/devices/DisplayDevice",
                         kind="display", type_text="Display device"),
    ]


CHARGE = [(1000, 55.0, 2), (1100, 54.0, 2), (1200, 54.5, 1), (1300, float("nan"), 1),
          (1400, 56.0, 1)]
RATE = [(1000, 8.0, 2), (1100, 7.5, 2), (1200, 12.0, 1), (1300, 12.5, 1),
        (1400, 11.0, 1)]
VOLTAGE = [(1000, 15.4, 0), (1100, 15.3, 0), (1200, 15.5, 0), (1300, 15.6, 0),
           (1400, 15.5, 0)]
RANGE_24H = 24 * 3600


def history_response(series):
    return {"charge": CHARGE, "rate": RATE, "voltage": VOLTAGE}[series]


def battery_responses(path=BAT0, range_s=RANGE_24H):
    return {(history_key(path, s), range_s): history_response(s) for s in HISTORY_SERIES}


# -- pure helpers -------------------------------------------------------------------------------


class StateTextTests(unittest.TestCase):
    def test_raw_state_strings_map_to_concise_wording(self):
        for raw, want in (("charging", "Charging"), ("Discharging", "Discharging"),
                          ("Full", "Plugged in"), ("fully-charged", "Plugged in"),
                          ("Not charging", "Plugged in"), ("empty", "Empty"),
                          ("pending-charge", "Pending charge"),
                          ("pending-discharge", "Pending discharge")):
            self.assertEqual(state_text(raw), want, raw)

    def test_missing_and_unknown_states_read_as_dash(self):
        self.assertEqual(state_text(None), "–")
        self.assertEqual(state_text(""), "–")
        self.assertEqual(state_text("unknown"), "–")

    def test_raw_int_state_ids_fall_back_to_the_upower_table(self):
        self.assertEqual(state_text(2), "Discharging")
        self.assertEqual(state_text(1), "Charging")


class DeviceHelperTests(unittest.TestCase):
    def test_kinds_collapse_to_laptop_ups_peripheral_or_other(self):
        self.assertEqual(device_kind(laptop()), "laptop")
        self.assertEqual(device_kind(UpowerDeviceInfo(object_path=UPS, kind="ups")), "ups")
        self.assertEqual(device_kind(peripheral()), "peripheral")
        for kind in ("line_power", "display", "unknown"):
            self.assertEqual(device_kind(UpowerDeviceInfo(object_path="/x", kind=kind)),
                             "other", kind)
        # raw Kind ids (contract §2) map defensively too
        self.assertEqual(device_kind(SimpleNamespace(kind=2)), "laptop")
        self.assertEqual(device_kind(SimpleNamespace(kind=3)), "ups")
        self.assertEqual(device_kind(SimpleNamespace(kind=5)), "peripheral")
        self.assertEqual(device_kind(SimpleNamespace(kind=1)), "other")

    def test_device_name_prefers_model_then_type_then_sysfs(self):
        self.assertEqual(device_name(laptop(model="ASUS Battery")), "ASUS Battery")
        self.assertEqual(device_name(laptop()), "Laptop battery")
        self.assertEqual(
            device_name(laptop(type_text="", native_path="/sys/class/power_supply/BAT0")),
            "BAT0")
        self.assertEqual(device_name(UpowerDeviceInfo(object_path="/o/x")), "x")
        self.assertEqual(device_name(SimpleNamespace(object_path="", native_path="")),
                         "Battery")

    def test_serial_never_reaches_a_label(self):
        dev = laptop(model=None, native_path="", serial="GOLDEN-EYE-42")
        self.assertNotIn("GOLDEN-EYE", device_name(dev))
        self.assertNotIn("GOLDEN-EYE", device_subtitle(dev))
        self.assertNotIn("GOLDEN-EYE", level_text(dev))

    def test_level_text_distinguishes_missing_from_a_genuine_zero(self):
        self.assertEqual(level_text(peripheral(percent=None)), "Level unknown")
        self.assertEqual(level_text(peripheral(percent=0)), "0%")
        self.assertEqual(level_text(peripheral(percent=55)), "55%")  # coarse is fine

    def test_subtitle_joins_state_and_present_readings(self):
        self.assertEqual(device_subtitle(peripheral(state="charging")),
                         "Charging")
        self.assertEqual(device_subtitle(peripheral(state="charging", watts=4.25,
                                                    voltage=11.1)),
                         "Charging · 4.2 W · 11.10 V")

    def test_battery_weight_prefers_capacity_and_skips_zero(self):
        self.assertEqual(battery_weight(laptop(energy_full=50.0)), 50.0)
        self.assertEqual(battery_weight(laptop(energy=42.0)), 42.0)
        self.assertIsNone(battery_weight(laptop(energy_full=0.0)))
        self.assertIsNone(battery_weight(laptop()))

    def test_history_request_key_carries_the_series(self):
        for s in HISTORY_SERIES:
            self.assertEqual(history_key(BAT0, s), f"{BAT0}|{s}")
        self.assertEqual(RANGES, (("1h", 3600), ("6h", 21600), ("24h", 86400),
                                  ("7d", 7 * 86400)))
        self.assertEqual(SERIES_FIELD, {"rate": "watts", "charge": "percent",
                                        "voltage": "voltage"})


# -- history math ---------------------------------------------------------------------------------


class MergeSeriesTests(unittest.TestCase):
    def test_fields_merge_by_timestamp_and_state_comes_from_charge(self):
        rows = merge_series({
            "charge": [(1000, 55.0, "discharging"), (1100, None, "charging")],
            "rate": [(1000, 8.0, None), (1100, 9.0, None)],
            "voltage": [(1100, 15.4, None)],
        })
        self.assertEqual([r["t"] for r in rows], [1000, 1100])
        self.assertEqual(rows[0], {"t": 1000, "percent": 55.0, "watts": 8.0,
                                   "state": "discharging"})
        self.assertEqual(rows[1], {"t": 1100, "percent": None, "watts": 9.0,
                                   "voltage": 15.4, "state": "charging"})

    def test_unknown_series_names_are_ignored(self):
        self.assertEqual(merge_series({"junk": [(1, 1.0, None)]}), [])


class BuildBandsTests(unittest.TestCase):
    def test_state_transitions_become_labelled_tinted_bands(self):
        rows = [
            {"t": 1000, "state": "discharging"},
            {"t": 1100, "state": "discharging"},
            {"t": 1200, "state": "charging"},
            {"t": 1400, "state": "charging"},
        ]
        self.assertEqual(build_bands(rows), [
            (1000, 1100, "Discharging", (0.86, 0.64, 0.28)),
            (1200, 1400, "Charging", (0.34, 0.66, 0.44)),
        ])

    def test_unknown_states_do_not_open_a_band(self):
        rows = [{"t": 1000, "state": None}, {"t": 1100, "state": "fully-charged"}]
        self.assertEqual(build_bands(rows),
                         [(1100, 1100, "Fully charged", (0.44, 0.60, 0.84))])
        self.assertEqual(build_bands([]), [])


class SummaryLinesTests(unittest.TestCase):
    def test_latest_min_max_per_series(self):
        lines = summary_lines([
            {"t": 1, "percent": 50.0, "watts": 9.0},
            {"t": 2, "percent": None, "watts": 11.0},
            {"t": 3, "percent": 62.0, "watts": None, "voltage": 15.2},
        ])
        self.assertEqual(lines["percent"], (62.0, 50.0, 62.0))
        self.assertEqual(lines["watts"], (11.0, 9.0, 11.0))
        self.assertEqual(lines["voltage"], (15.2, 15.2, 15.2))

    def test_all_missing_series_are_absent(self):
        self.assertEqual(summary_lines([{"t": 1, "percent": None}]), {})
        self.assertEqual(summary_lines([]), {})


class AggregateHistoryTests(unittest.TestCase):
    def _per_dev(self, bat0_full=50.0, bat1_full=30.0):
        per_dev = {
            BAT0: {"charge": [(1000, 50.0, "discharging"), (1100, 49.0, "discharging")],
                   "rate": [(1000, 8.0, None), (1100, 8.0, None)],
                   "voltage": [(1000, 15.4, None), (1100, 15.4, None)]},
            BAT1: {"charge": [(1000, 80.0, "charging"), (1100, 80.0, "charging")],
                   "rate": [(1000, 5.0, None), (1100, 4.0, None)],
                   "voltage": [(1000, 12.6, None)]},
        }
        weights = {BAT0: bat0_full, BAT1: bat1_full}
        return per_dev, weights

    def test_energy_weighted_percent_and_voltage_watts_summed(self):
        per_dev, weights = self._per_dev()
        out = aggregate_history(per_dev, weights)
        self.assertEqual(out["charge"][0], (1000, 61.25, None))  # different states
        self.assertEqual(out["rate"][0], (1000, 13.0, None))
        self.assertAlmostEqual(out["voltage"][0][1], 14.35)  # (15.4·50+12.6·30)/80
        self.assertEqual(out["charge"][1], (1100, 60.625, None))
        self.assertEqual(out["rate"][1], (1100, 12.0, None))
        self.assertEqual(out["voltage"][1], (1100, None, None))

    def test_percent_falls_back_to_the_mean_without_energies(self):
        per_dev, _weights = self._per_dev()
        out = aggregate_history(per_dev, {BAT0: None, BAT1: None})
        self.assertEqual(out["charge"][0], (1000, 65.0, None))  # (50+80)/2
        self.assertEqual(out["voltage"][0], (1000, 14.0, None))  # (15.4+12.6)/2

    def test_every_battery_missing_at_a_timestamp_stays_a_gap(self):
        out = aggregate_history({BAT0: {"rate": [(1000, None, None)]}}, {BAT0: 50.0})
        self.assertEqual(out["rate"], [(1000, None, None)])
        self.assertEqual(out["charge"], [])
        self.assertEqual(out["voltage"], [])


# -- stub hub seam --------------------------------------------------------------------------------


class StubHubTests(unittest.TestCase):
    def test_latest_request_token_wins_and_stale_is_dropped(self):
        hub = StubHub()
        got, failed = [], []
        for _ in range(2):  # same (dev_key, range_s): the second replaces the first
            hub.request_power_history(BAT0, 86400, got.append, failed.append)
        pump(0.05)
        self.assertEqual(len(hub.requests), 2)
        self.assertEqual(len(got), 1, "the stale response must never fire done")
        self.assertEqual(failed, [])
        self.assertEqual(hub.dropped, 1)

    def test_errors_are_delivered_like_results(self):
        hub = StubHub()
        hub.responses[(BAT0, 3600)] = RuntimeError("bus gone")
        got, failed = [], []
        hub.request_power_history(BAT0, 3600, got.append, failed.append)
        pump(0.05)
        self.assertEqual(got, [])
        self.assertEqual(len(failed), 1)

    def test_shown_and_hidden_are_recorded(self):
        hub = StubHub()
        hub.power_page_shown("page")
        hub.power_page_hidden("page")
        self.assertEqual(hub.shown, ["page"])
        self.assertEqual(hub.hidden, ["page"])


# -- wiring (real page, stub window) -------------------------------------------------------------


class WiringTests(unittest.TestCase):
    """The real BatteryPage on a stub window; no LiveHub, no controller, no display
    dependency beyond GTK widget construction."""

    @classmethod
    def setUpClass(cls) -> None:
        if Gdk.Display.get_default() is None:
            raise unittest.SkipTest("GTK widget tests require a display")

    def setUp(self) -> None:
        self.enterContext(patch("asusius.ui.pages.battery.wall_time", return_value=1500.0))

    def test_sections_and_preserved_controls_render(self):
        hub = StubHub()
        rec = Mock(side_effect=a11y_name)
        with patch("asusius.ui.pages.battery.a11y_name", rec):
            page = build_page(make_win(hub))
        # status card
        self.assertEqual(page.card_big.get_label(), "–")
        self.assertEqual(page.card_w.get_label(), "–")
        self.assertEqual(page.card_v.get_label(), "–")
        # charge limit (preserved): presets, scale, a11y name, error row
        labels = []
        child = page.presets.get_first_child()
        while child is not None:
            labels.append(child.get_label())
            child = child.get_next_sibling()
        self.assertEqual(labels, ["60%", "80%", "100%"])
        named = [c for c in rec.call_args_list if c.args[0] is page.scale]
        self.assertTrue(named and named[0].args[1] == "Charge limit")
        named = [c for c in rec.call_args_list if c.args[0] is page.hist_selector]
        self.assertTrue(named and named[0].args[1] == "Battery history device")
        self.assertFalse(page.limit_err.get_visible())
        self.assertEqual(page.oneshot.get_title(), "Charge to 100% once")
        # health + Right now rows (preserved)
        for row, title in ((page.h_row, "Health"), (page.cap_row, "Capacity"),
                           (page.cyc_row, "Charge cycles"), (page.now_row, "Right now")):
            self.assertEqual(row.get_title(), title)
        # device groups start hidden, four range chips exist
        self.assertFalse(page.periph_group.get_visible())
        self.assertFalse(page.ups_group.get_visible())
        self.assertEqual(len(page.chip_buttons), 4)
        self.assertEqual([b.get_label() for b in page.chip_buttons],
                         ["1h", "6h", "24h", "7d"])
        self.assertEqual([page.chip_buttons[i].get_active() for i in range(4)],
                         [False, False, True, False])  # 24h is the default range
        # history: hidden until the inventory answers (the hub has the seam)
        self.assertFalse(page.hist_group.get_visible())

    def test_shown_and_hidden_drive_the_hub_and_the_preserved_reads_land(self):
        hub = StubHub()
        page = build_page(make_win(hub))
        page.on_shown()
        self.assertEqual(hub.shown, [page])
        pump()
        self.assertEqual(page.big.get_label(), "80%")  # preserved _got_limit wiring
        self.assertEqual(page.hint.get_label(), "Good balance of lifespan and runtime")
        self.assertEqual(page.h_val.get_label(), "91% · Excellent")  # preserved health
        self.assertEqual(page.cap_val.get_label(), "50.0 of 55.0 Wh")
        self.assertEqual(page.cyc_val.get_label(), "120")
        page.on_hidden()
        self.assertEqual(hub.hidden, [page])

    def test_card_tracks_telemetry_and_the_hub_voltage(self):
        hub = StubHub(info={"battery": {"voltage": 15.398}})
        page = build_page(make_win(hub))
        page.on_telemetry(Telemetry(battery_percent=62, battery_status="Discharging",
                                    battery_power_w=8.34))
        self.assertEqual(page.card_big.get_label(), "62%")
        self.assertEqual(page.card_state.get_label(), "Discharging")
        self.assertEqual(page.card_w.get_label(), "8.3 W")
        self.assertEqual(page.card_v.get_label(), "15.40 V")
        self.assertEqual(page.now_val.get_label(), "62% · discharging · 8.3 W")

    def test_card_keeps_genuine_zeros_and_dashes_missing_values(self):
        page = build_page(make_win(StubHub()))
        page.on_telemetry(Telemetry(battery_percent=0, battery_status="Discharging",
                                    battery_power_w=0.0))
        self.assertEqual(page.card_big.get_label(), "0%")
        self.assertEqual(page.card_w.get_label(), "0.0 W")
        page.on_telemetry(Telemetry(battery_percent=None, battery_status=None,
                                    battery_power_w=None))
        self.assertEqual(page.card_big.get_label(), "–")
        self.assertEqual(page.card_state.get_label(), "–")
        self.assertEqual(page.card_w.get_label(), "–")
        self.assertEqual(page.card_v.get_label(), "–")

    def test_inventory_populates_peripherals_ups_and_selector(self):
        hub = StubHub()
        page = build_page(make_win(hub))
        page.on_power_inventory(sample_devices(), "UPower: 5 devices")
        self.assertTrue(page.periph_group.get_visible())
        self.assertEqual([r.get_title() for r in page.periph_rows], ["Wireless Mouse"])
        self.assertIn("Discharging", page.periph_rows[0].get_subtitle())
        self.assertTrue(page.ups_group.get_visible())
        self.assertEqual([r.get_title() for r in page.ups_rows], ["UPS"])
        self.assertIn("Plugged in", page.ups_rows[0].get_subtitle())
        # system battery first, then peripherals, then the UPS; the excluded kinds
        # (line power, display composite) never become entries
        self.assertEqual([e["name"] for e in page._hist_entries],
                         ["ASUS Battery", "Wireless Mouse", "UPS"])
        self.assertEqual(page.hist_selector.get_selected(), 0)
        self.assertTrue(page.hist_group.get_visible())
        self.assertFalse(page.hist_note.get_visible())
        self.assertTrue(page.hist_head.get_visible())

    def test_peripheral_level_reads_when_percent_is_absent_or_coarse(self):
        self.assertEqual(level_text(peripheral(percent=None)), "Level unknown")
        self.assertEqual(level_text(peripheral(percent=55)), "55%")
        self.assertEqual(level_text(peripheral(percent=0)), "0%")

    def test_aggregate_row_appears_only_with_two_laptop_batteries(self):
        page = build_page(make_win(StubHub()))
        page.on_power_inventory([laptop(BAT0, model="One")], "")
        self.assertEqual([e["name"] for e in page._hist_entries], ["One"])
        page.on_power_inventory([laptop(BAT0, model="One"), laptop(BAT1, model="Two")], "")
        self.assertEqual([e["name"] for e in page._hist_entries],
                         ["One", "Two", "All batteries"])
        self.assertEqual(page._hist_entries[2]["key"], AGGREGATE_KEY)

    def test_duplicate_identity_renders_two_distinct_rows(self):
        page = build_page(make_win(StubHub()))
        page.on_power_inventory([laptop(BAT0, native_path="/sys/class/power_supply/BAT0",
                                        model="Same Model"),
                                 laptop(BAT1, native_path="/sys/class/power_supply/BAT1",
                                        model="Same Model")], "")
        self.assertEqual([e["name"] for e in page._hist_entries],
                         ["Same Model", "Same Model (BAT1)", "All batteries"])
        self.assertEqual(len({e["key"] for e in page._hist_entries}), 3)

    def test_history_needs_upower_note_when_the_service_is_absent(self):
        page = build_page(make_win(StubHub()))
        page.on_power_inventory([], "UPower not available")
        self.assertTrue(page.hist_group.get_visible())
        self.assertTrue(page.hist_note.get_visible())
        self.assertIn("History needs UPower", page.hist_note.get_label())
        self.assertEqual(page.hist_note.get_label(), "History needs UPower.")
        self.assertFalse(page.hist_head.get_visible())
        self.assertFalse(page.periph_group.get_visible())
        self.assertFalse(page.ups_group.get_visible())
        # an empty DropDown is not focusable (qa_audit focus-unreachable) and there
        # is nothing to select: the selector stays insensitive with no devices
        self.assertFalse(page.hist_selector.get_sensitive())

    def test_selector_becomes_sensitive_when_devices_arrive_and_again_when_empty(self):
        page = build_page(make_win(StubHub()))
        self.assertFalse(page.hist_selector.get_sensitive())  # pre-inventory
        page.on_power_inventory(sample_devices(), "UPower: 5 devices")
        self.assertTrue(page.hist_selector.get_sensitive())
        page.on_power_inventory([], "UPower not available")
        self.assertFalse(page.hist_selector.get_sensitive())

    def test_page_stays_usable_before_the_hub_grows_the_power_seam(self):
        win = make_win(SimpleNamespace(info={}))  # no power_page_shown / request_power_history
        page = build_page(win)
        self.assertTrue(page.hist_group.get_visible())
        self.assertIn("History needs UPower", page.hist_note.get_label())
        page.on_shown()  # no crash, nothing recorded anywhere
        page.on_telemetry(Telemetry(battery_percent=71, battery_status="Charging",
                                    battery_power_w=12.0))
        self.assertEqual(page.card_big.get_label(), "71%")
        page.on_hidden()

    def test_history_requests_all_three_series_for_the_selected_battery(self):
        hub = StubHub()
        chart = ChartStub()
        page = build_page(make_win(hub), chart=chart)
        hub.responses = battery_responses()
        page.on_power_inventory([laptop(BAT0)], "")
        self.assertEqual([r[0] for r in hub.requests],
                         [history_key(BAT0, s) for s in HISTORY_SERIES])
        self.assertEqual([r[1] for r in hub.requests], [RANGE_24H] * 3)
        pump()
        self.assertEqual(len(chart.data), 1)
        t_end, span, series, states = chart.data[0]
        self.assertEqual(t_end, 1500.0)
        self.assertEqual(span, RANGE_24H)
        self.assertEqual(series["percent"], [(1000.0, 55.0), (1100.0, 54.0),
                                             (1200.0, 54.5), (1300.0, None),
                                             (1400.0, 56.0)])
        self.assertEqual(series["watts"], [(1000.0, 8.0), (1100.0, 7.5), (1200.0, -12.0),
                                           (1300.0, -12.5), (1400.0, -11.0)])
        self.assertEqual(states, [(1000.0, 1100.0, "Discharging", (0.86, 0.64, 0.28)),
                                  (1200.0, 1400.0, "Charging", (0.34, 0.66, 0.44))])

    def test_summary_rows_read_latest_min_max_with_dashes_for_gaps(self):
        hub = StubHub()
        page = build_page(make_win(hub))  # no chart: pre-C the summary still fills
        hub.responses = battery_responses()
        page.on_power_inventory([laptop(BAT0)], "")
        pump()
        self.assertTrue(page.hist_summary.get_visible())
        self.assertEqual(page._summary_rows["percent"][1].get_label(),
                         "latest 56% · min 54% · max 56%")
        self.assertEqual(page._summary_rows["watts"][1].get_label(),
                         "latest -11.0 W · min -12.5 W · max 8.0 W")
        self.assertEqual(page._summary_rows["voltage"][1].get_label(),
                         "latest 15.50 V · min 15.30 V · max 15.60 V")
        self.assertFalse(page.hist_empty.get_visible())

    def test_history_outside_the_selected_range_does_not_fill_the_chart(self):
        page = build_page(make_win(StubHub()))
        with patch("asusius.ui.pages.battery.wall_time", return_value=100_000.0):
            page._show_history_rows([{"t": 1000.0, "percent": 80.0},
                                     {"t": 100_001.0, "percent": 77.0}])
        self.assertFalse(page.hist_chart_holder.get_visible())
        self.assertFalse(page.hist_summary.get_visible())
        self.assertTrue(page.hist_empty.get_visible())

    def test_inventory_poll_preserves_existing_history_and_summary(self):
        hub = StubHub()
        page = build_page(make_win(hub))
        hub.responses = battery_responses()
        page.on_power_inventory([laptop(BAT0)], "")
        pump()
        self.assertTrue(page.hist_summary.get_visible())
        page.on_power_inventory([laptop(BAT0, percent=22)], "")
        self.assertTrue(page.hist_summary.get_visible())
        self.assertTrue(page.hist_chart_holder.get_visible())
        self.assertEqual(len(hub.requests), 3)

    def test_all_failed_requests_report_a_fetch_failure(self):
        hub = StubHub()
        page = build_page(make_win(hub))
        hub.responses = {(history_key(BAT0, s), RANGE_24H): RuntimeError("bus gone")
                         for s in HISTORY_SERIES}
        page.on_power_inventory([laptop(BAT0)], "")
        pump()
        self.assertTrue(page.hist_empty.get_visible())
        self.assertEqual(page.hist_empty.get_label(), "Could not load battery history.")
        self.assertFalse(page.hist_chart_holder.get_visible())

    def test_range_chips_refetch_with_the_new_span_and_stay_exclusive(self):
        hub = StubHub()
        chart = ChartStub()
        page = build_page(make_win(hub), chart=chart)
        hub.responses = battery_responses(range_s=6 * 3600)
        page.on_power_inventory([laptop(BAT0)], "")  # initial fetch at 24h
        pump()
        page.chip_buttons[1].set_active(True)  # "6h"
        pump()
        self.assertEqual([b.get_active() for b in page.chip_buttons],
                         [False, True, False, False])
        self.assertEqual([r[1] for r in hub.requests[-3:]], [6 * 3600] * 3)
        self.assertEqual(chart.data[-1][1], 6 * 3600)

    def test_stale_requests_are_dropped_so_only_the_latest_generation_renders(self):
        hub = StubHub()
        chart = ChartStub()
        page = build_page(make_win(hub), chart=chart)
        hub.responses = battery_responses()
        page.on_power_inventory([laptop(BAT0)], "")  # first selection (generation 1)
        page._refresh_history(force=True)  # a newer request for the same device+range
        pump()
        self.assertEqual(hub.dropped, 3)  # the hub dropped the superseded triple
        self.assertEqual(len(chart.data), 1)

    def test_empty_history_shows_the_empty_state(self):
        hub = StubHub()
        chart = ChartStub()
        page = build_page(make_win(hub), chart=chart)
        hub.responses = {k: [] for k in battery_responses()}
        page.on_power_inventory([laptop(BAT0)], "")  # UPower answered: no usable points
        pump()
        self.assertEqual(chart.empty, [EMPTY_TEXT])
        self.assertEqual(chart.data, [])
        self.assertTrue(page.hist_empty.get_visible())
        self.assertFalse(page.hist_summary.get_visible())

    def test_a_failed_series_is_a_gap_not_a_zero(self):
        hub = StubHub()
        chart = ChartStub()
        page = build_page(make_win(hub), chart=chart)
        responses = battery_responses()
        responses[(history_key(BAT0, "charge"), RANGE_24H)] = RuntimeError("bus gone")
        hub.responses = responses
        page.on_power_inventory([laptop(BAT0)], "")
        pump()
        self.assertEqual(len(chart.data), 1)
        _t_end, _span, series, _states = chart.data[0]
        self.assertEqual(series["percent"], [(t, None) for t in
                                             (1000.0, 1100.0, 1200.0, 1300.0, 1400.0)])
        self.assertEqual(series["watts"][0], (1000.0, 8.0))
        self.assertFalse(page._summary_rows["percent"][0].get_visible())
        self.assertTrue(page._summary_rows["watts"][0].get_visible())
        self.assertEqual(page.hist_empty.get_label(), "Some history could not be loaded.")

    def test_all_batteries_aggregate_is_energy_weighted(self):
        hub = StubHub()
        chart = ChartStub()
        page = build_page(make_win(hub), chart=chart)
        bat0 = laptop(BAT0, model="One", energy_full=50.0)
        bat1 = laptop(BAT1, model="Two", energy_full=30.0)
        rows0 = {"charge": [(1000, 50.0, 2), (1100, 49.0, 2)],
                 "rate": [(1000, 8.0, 2), (1100, 8.0, 2)],
                 "voltage": [(1000, 15.4, 0), (1100, 15.4, 0)]}
        rows1 = {"charge": [(1000, 80.0, 1), (1100, 80.0, 1)],
                 "rate": [(1000, 5.0, 1), (1100, 4.0, 1)],
                 "voltage": [(1000, 12.6, 0)]}  # a gap on BAT1 at 1100
        for s in HISTORY_SERIES:
            hub.responses[(history_key(BAT0, s), RANGE_24H)] = rows0[s]
            hub.responses[(history_key(BAT1, s), RANGE_24H)] = rows1[s]
        page.on_power_inventory([bat0, bat1], "")
        idx = next(i for i, e in enumerate(page._hist_entries)
                   if e["key"] == AGGREGATE_KEY)
        page.hist_selector.set_selected(idx)  # -> "All batteries"
        pump()
        self.assertEqual(len(hub.requests), 9)  # 3 (first battery) + 3 series × 2
        # the single-battery round is superseded by the aggregate selection: the page
        # generation guard drops it, only the aggregate round renders
        self.assertEqual(len(chart.data), 1)
        _t_end, _span, series, states = chart.data[-1]
        self.assertEqual(series["percent"], [(1000.0, 61.25), (1100.0, 60.625)])
        self.assertEqual(series["watts"], [(1000.0, 3.0), (1100.0, 4.0)])
        self.assertEqual(series["voltage"], [(1000.0, 14.35), (1100.0, None)])
        self.assertEqual(states, [])
        self.assertEqual(page._summary_rows["watts"][1].get_label(),
                         "latest 4.0 W · min 3.0 W · max 4.0 W")

    def test_the_real_history_chart_receives_the_snapshot(self):
        """With C's ui.history_chart present, _make_chart builds the real widget and
        the same page flow feeds it (smoke: the widget holds the rendered snapshot)."""
        hub = StubHub()
        page = build_page(make_win(hub))  # no stub: the deferred import finds the chart
        self.assertIsNotNone(page._chart)
        self.assertIs(page.hist_chart_holder.get_first_child(), page._chart)
        hub.responses = battery_responses()
        page.on_power_inventory([laptop(BAT0)], "")
        pump()
        self.assertFalse(page._chart._empty)
        self.assertEqual(page._chart._t_end, 1500.0)
        self.assertEqual(page._chart._span_s, RANGE_24H)
        self.assertEqual(len(page._chart._states), 2)

    def test_selection_switch_back_refetches_without_rebuilding_noise(self):
        hub = StubHub()
        chart = ChartStub()
        page = build_page(make_win(hub), chart=chart)
        hub.responses = battery_responses()
        mouse = peripheral(percent=44.0)
        hub.responses.update(battery_responses(MOUSE))
        page.on_power_inventory([laptop(BAT0), mouse], "")
        pump()
        self.assertEqual(len(chart.data), 1)
        page.hist_selector.set_selected(1)  # the peripheral
        pump()
        self.assertEqual(len(chart.data), 2)
        self.assertEqual([r[0] for r in hub.requests[-3:]],
                         [history_key(MOUSE, s) for s in HISTORY_SERIES])
        # a repeated inventory must not refetch what is already on screen
        page.on_power_inventory([laptop(BAT0), mouse], "")
        pump()
        self.assertEqual(len(chart.data), 2)
        self.assertEqual(len(hub.requests), 6)

    def test_fit_width_preserves_the_limit_row_and_wraps_the_history(self):
        page = build_page(make_win(StubHub()))
        page.fit_width(3)
        self.assertEqual(page.top.get_orientation(), Gtk.Orientation.VERTICAL)
        self.assertEqual(page.presets.get_halign(), Gtk.Align.START)
        self.assertEqual(page.hist_head.get_orientation(), Gtk.Orientation.VERTICAL)
        self.assertEqual(page.range_chips.get_max_children_per_line(), 2)
        page.fit_width(0)
        self.assertEqual(page.top.get_orientation(), Gtk.Orientation.HORIZONTAL)
        self.assertEqual(page.presets.get_halign(), Gtk.Align.FILL)
        self.assertEqual(page.hist_head.get_orientation(), Gtk.Orientation.HORIZONTAL)
        self.assertEqual(page.range_chips.get_max_children_per_line(), 4)


if __name__ == "__main__":
    unittest.main()
