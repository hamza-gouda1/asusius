# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""The rebuilt Memory page: summary states, cursor semantics, seams and lifecycle.

Widget tests construct the real page over a stub hub (a real MemHistory ring fed by
push/commit, Mock hub seams) — no display cycle is needed beyond widget construction,
and the suite is skipped on headless CI like the other GTK-in-tests modules. The stub
hub is the integration contract: hub.mem_store / mem_page_shown / mem_page_hidden /
request_processes / app_memory are exactly the seams the page consumes via getattr.
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

_TMP = Path(tempfile.mkdtemp(prefix="asusius-ui-memory-page-"))
for _sub in ("config", "data"):
    (_TMP / _sub).mkdir()
os.environ["GSETTINGS_BACKEND"] = "memory"  # before anything imports gi
_XDG_KEYS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME")
_OLD_XDG = {k: os.environ.get(k) for k in _XDG_KEYS}


def setUpModule():
    """XDG dirs are scoped to this module's tests (fresh per module, never at import)."""
    os.environ["XDG_CONFIG_HOME"] = str(_TMP / "config")
    os.environ["XDG_DATA_HOME"] = str(_TMP / "data")


def tearDownModule():
    for _k, _v in _OLD_XDG.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk, Gtk, Pango  # noqa: E402

from asusius.telemetry import fmt  # noqa: E402
from asusius.telemetry.memory_history import MemHistory  # noqa: E402
from asusius.telemetry.ring import Store  # noqa: E402
from asusius.ui.charts import Chart, Line, value_at  # noqa: E402
from asusius.ui.pages.memory import (  # noqa: E402
    BREAKDOWN_LINES, DEFAULT_RANGE, MemoryPage, USAGE_LINES, ddr_text, zram_line)

GIB = 1 << 30

try:  # C's panel widget lands in a parallel phase; the shell shows a placeholder before
    from asusius.ui import app_memory  # noqa: F401

    _NO_APP_MEMORY_MODULE = False
except ImportError:
    _NO_APP_MEMORY_MODULE = True


def mem_dict(i: int = 0, **over) -> dict:
    """One info["mem"]-shaped dict; per-sample growth on the volatile fields."""
    m = dict(total=16 * GIB, in_use=8 * GIB + 1000 * i, available=8 * GIB - 1000 * i,
             free=1 * GIB, buff_cache=2 * GIB, shmem=GIB, slab=384 << 20,
             slab_reclaimable=256 << 20, slab_unreclaimable=128 << 20,
             dirty=4096, writeback=0, committed=44 * GIB, commit_limit=30 * GIB,
             swap_total=23 * GIB, swap_used=112 << 20, swap_cached=5 << 20,
             page_tables=87 << 20, kernel_stack=24 << 20,
             anon=5 * GIB + 100 * i, file=3 * GIB, zram_any=False,
             psi={"some": {"avg10": 0.12, "avg60": 0.4, "avg300": 0.9},
                  "full": {"avg10": 0.01, "avg60": 0.2, "avg300": 0.3}},
             psi_reason=None)
    m.update(over)
    return m


def feed(mh: MemHistory, n: int = 30, **over) -> float:
    """Commit n ring samples at 1 Hz; returns the ring's last_t."""
    for i in range(n):
        mh.commit(1.0 + i, mem_dict(i, **over))
    return 1.0 + n - 1


def make_hub(**kw) -> SimpleNamespace:
    mem = kw.pop("mem", None)
    info = kw.pop("info", None)
    seams = kw.pop("seams", True)
    feed_n = kw.pop("feed_n", 30)
    mh = mem if mem is not None else MemHistory()
    if seams and feed_n and mem is None:
        feed(mh, feed_n)  # a caller-supplied ring arrives exactly as it was fed
    hub = SimpleNamespace(
        store=Store(300), interval_s=0.5,
        sampler=SimpleNamespace(mem=SimpleNamespace(zswap_enabled=lambda: False)),
        info={"mem": info if info is not None else mem_dict()},
        mem_store=mh if seams else None, mem_page_shown=Mock(), mem_page_hidden=Mock(),
        request_processes=Mock(),
        app_memory=SimpleNamespace(reset=Mock(), ranking=Mock(return_value=[]),
                                   coverage="ok", baseline_count=0, group_count=0))
    return hub


def make_page(hub=None, **kw) -> MemoryPage:
    page = MemoryPage(SimpleNamespace(hub=hub if hub is not None else make_hub(**kw),
                                      ctl=None, caps=None))
    page.update()
    return page


def txt(lab: Gtk.Label) -> str:
    return lab.get_label().replace("\u00a0", " ")


def draw_chart(chart) -> None:
    """Render one frame off-screen: the draw-level assertions run the real _draw."""
    import cairo
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 140)
    chart._draw(chart, cairo.Context(surf), 300, 140)


def legend_buttons(page) -> list[Gtk.ToggleButton]:
    cols = page._legend_cols
    return [page.legend_box.get_child_at(i % cols, i // cols) for i in range(4)]


def kids(w) -> list:
    out, c = [], w.get_first_child()
    while c is not None:
        out.append(c)
        c = c.get_next_sibling()
    return out


class WidgetCase(unittest.TestCase):
    """GTK widget tests need a display; headless CI skips the module like its peers."""

    @classmethod
    def setUpClass(cls) -> None:
        if Gdk.Display.get_default() is None:
            raise unittest.SkipTest("GTK widget tests require a display")


class HelperTests(unittest.TestCase):
    """zram_line/ddr_text are carried over from the old page unchanged."""

    def test_zram_line_devices_join_and_zero_stays_zero(self):
        row = zram_line([{"device": "zram0", "orig": 1 << 30, "compr": 300 << 20,
                          "total": 320 << 20},
                         {"device": "zram1", "orig": 0, "compr": 0, "total": 0}])
        self.assertIn(f"zram0: {fmt.bytes_(1 << 30)} stored", row)
        self.assertIn("zram1: 0 B stored", row)  # a measured zero is a real zero

    def test_zram_line_unparsed_fields_and_empty_pool(self):
        self.assertEqual(zram_line([{"device": "zram0", "orig": None}]), "zram0: –")
        self.assertIsNone(zram_line(None))
        self.assertIsNone(zram_line([]))

    def test_ddr_text_summary_and_absence(self):
        mods = [{"slot": "DIMM0", "type": "DDR4", "speed_mts": 3200},
                {"slot": "DIMM1", "type": "DDR4", "speed_mts": 3200}]
        self.assertEqual(ddr_text(mods), "2 modules · DDR4 · 3200 MT/s — DIMM0, DIMM1")
        self.assertIsNone(ddr_text(None))
        self.assertIsNone(ddr_text([]))


class SummaryTests(WidgetCase):
    """Card states: measured zero, absent source and feature-off stay distinct."""

    def test_populated_cards(self):
        page = make_page()
        self.assertEqual(txt(page.cards["in_use"][0]), fmt.bytes_(8 * GIB))
        self.assertEqual(txt(page.cards["in_use"][1]), f"{fmt.pct(50)} of usable RAM")
        self.assertEqual(txt(page.cards["available"][1]), f"{fmt.bytes_(1 * GIB)} free")
        self.assertEqual(txt(page.cards["pressure"][0]), fmt.pct(0.12, 2))
        self.assertEqual(txt(page.cards["swap"][0]), fmt.bytes_(112 << 20))
        self.assertEqual(txt(page.cards["swap"][1]), f"of {fmt.bytes_(23 * GIB)} configured")

    def test_absent_estimates_show_dash_not_zero(self):
        page = make_page(info=mem_dict(total=None, available=None, in_use=None))
        self.assertEqual(txt(page.cards["in_use"][0]), "–")
        self.assertEqual(txt(page.cards["in_use"][1]), "–")
        self.assertEqual(page.sub.get_label(), "Total unavailable")

    def test_measured_zero_swap_usage_is_zero_bytes(self):
        page = make_page(info=mem_dict(swap_used=0))
        self.assertEqual(txt(page.cards["swap"][0]), "0 B")

    def test_no_swap_says_so(self):
        page = make_page(info=mem_dict(swap_total=0, swap_used=0))
        self.assertEqual(txt(page.cards["swap"][0]), "no swap configured")
        self.assertEqual(txt(page.cards["swap"][1]), "")

    def test_psi_absent_shows_the_reason(self):
        page = make_page(info=mem_dict(psi=None, psi_reason="not available: no /proc/pressure"))
        self.assertEqual(txt(page.cards["pressure"][0]), "–")
        self.assertIn("no /proc/pressure", txt(page.psi_note))

    def test_psi_some_line_missing_shows_dash_with_metric_caption(self):
        page = make_page(info=mem_dict(psi={"full": {"avg10": 0.01}}))
        self.assertEqual(txt(page.cards["pressure"][0]), "–")
        self.assertEqual(txt(page.cards["pressure"][1]), "PSI unavailable")

    def test_displayed_zero_pressure_keeps_the_kernel_average_label(self):
        page = make_page(info=mem_dict(psi={"some": {"avg10": 0.001}}))
        self.assertEqual(txt(page.cards["pressure"][0]), fmt.pct(0.001, 2))
        self.assertIn("10 s avg", txt(page.cards["pressure"][1]))

    def test_free_absent_leaves_the_available_context_dash(self):
        page = make_page(info=mem_dict(free=None))
        self.assertEqual(txt(page.cards["available"][1]), "–")


class ViewAndRangeTests(WidgetCase):
    """View/range segmented toggles: active states, chart lines, notes, defaults."""

    def test_defaults_are_breakdown_and_two_minutes(self):
        page = make_page()
        self.assertEqual(page.view, "breakdown")
        self.assertTrue(page.view_btns["breakdown"].get_active())
        self.assertFalse(page.view_btns["usage"].get_active())
        self.assertTrue(page.range_btns[DEFAULT_RANGE].get_active())
        self.assertFalse(page.range_btns[60].get_active())
        self.assertEqual([line.key for line in page.ram_chart.lines],
                         ["anon", "file", "shmem", "slab"])
        self.assertEqual(page.ram_chart.window_s, DEFAULT_RANGE)

    def test_view_switch_swaps_lines_stack_note_and_readout(self):
        page = make_page()
        page.view_btns["usage"].set_active(True)
        self.assertEqual(page.view, "usage")
        self.assertEqual([line.key for line in page.ram_chart.lines],
                         [line.key for line in USAGE_LINES])
        self.assertTrue(page.ram_chart.stacked)
        self.assertIn("without swapping", page.chart_note.get_label())
        self.assertEqual(sorted(page.readout_vals), ["available", "in_use"])
        self.assertTrue(page.view_btns["usage"].get_active())
        self.assertFalse(page.view_btns["breakdown"].get_active())
        page.view_btns["breakdown"].set_active(True)
        self.assertFalse(page.ram_chart.stacked)
        self.assertIn("file-backed LRU", page.chart_note.get_label())

    def test_usage_view_fixes_the_y_axis_to_total(self):
        page = make_page()
        page.view_btns["usage"].set_active(True)
        self.assertEqual(page.ram_chart.y_fixed, (0.0, 16.0))  # info total, in GiB
        page.view_btns["breakdown"].set_active(True)
        self.assertIsNone(page.ram_chart.y_fixed)

    def test_usage_view_without_a_total_keeps_a_free_axis(self):
        page = make_page(info=mem_dict(total=None))
        page.view_btns["usage"].set_active(True)
        self.assertIsNone(page.ram_chart.y_fixed)

    def test_range_change_updates_the_charts_and_clears_the_pin(self):
        page = make_page()
        page.qa_pin(page.ram_chart.store.last_t)
        page.range_btns[300].set_active(True)
        self.assertEqual(page.range_s, 300)
        for c in (page.ram_chart, page.psi_chart, page.swap_chart):
            self.assertEqual(c.window_s, 300)
        self.assertIsNone(page.pin_ts)
        self.assertTrue(page.range_btns[300].get_active())
        self.assertFalse(page.range_btns[DEFAULT_RANGE].get_active())

    def test_legend_toggles_filter_lines_and_never_all_off(self):
        page = make_page()
        btns = legend_buttons(page)
        self.assertEqual(len(btns), 4)
        self.assertTrue(all(b.get_active() for b in btns))
        btns[0].set_active(False)
        self.assertEqual([line.key for line in page.ram_chart.lines],
                         ["file", "shmem", "slab"])
        btns[1].set_active(False)
        btns[2].set_active(False)
        self.assertEqual(page._sel, {"slab"})
        btns[3].set_active(False)  # the last active line is refused
        self.assertEqual(page._sel, {"slab"})
        self.assertTrue(btns[3].get_active())

    def test_legend_selection_survives_a_view_round_trip(self):
        page = make_page()
        legend_buttons(page)[0].set_active(False)
        page.view_btns["usage"].set_active(True)
        page.view_btns["breakdown"].set_active(True)
        self.assertNotIn("anon", page._sel)
        self.assertEqual([line.key for line in page.ram_chart.lines],
                         ["file", "shmem", "slab"])


class CursorTests(WidgetCase):
    """selected = pin ?? hover ?? latest, mirroring the design preview exactly."""

    def _plotted(self, page):
        st = page.ram_chart.store
        page.ram_chart._plot = (6.0, 8.0, 200.0, 150.0, st.last_t - page.range_s, st.last_t)

    def _hover(self, page, frac: float) -> None:
        self._plotted(page)
        page._ram_motion(None, 6.0 + 200.0 * frac, 10.0)

    def test_defaults_show_the_latest_sample(self):
        page = make_page()
        self.assertEqual(page.sample_label.get_label(), "Latest sample")
        self.assertFalse(page.latest_btn.get_visible())
        self.assertFalse(page.sample_badge.get_visible())
        for c in (page.ram_chart, page.psi_chart, page.swap_chart):
            self.assertEqual(c.cursor_ts, page.ram_chart.store.last_t)

    def test_hover_moves_the_cursor_and_leave_returns_to_latest(self):
        page = make_page(hub=make_hub(feed_n=200))  # ring fills the whole 120 s window
        st = page.ram_chart.store
        self._hover(page, 0.5)
        self.assertEqual(page.hover_ts, 140.0)  # nearest sample to the plot midpoint
        self.assertEqual(txt(page.sample_label),
                         f"{fmt.duration(st.last_t - page.hover_ts)} ago")
        self.assertTrue(page.sample_badge.get_visible())
        self.assertEqual(page.ram_chart.cursor_ts, page.hover_ts)
        page._ram_leave()
        self.assertIsNone(page.hover_ts)
        self.assertEqual(page.sample_label.get_label(), "Latest sample")

    def test_hover_is_snapped_to_a_ring_sample(self):
        page = make_page(hub=make_hub(feed_n=200))
        st = page.ram_chart.store
        self._hover(page, 0.331)  # between samples: lands on the nearest one
        self.assertIn(page.hover_ts, [st.times[st.slot(k)] for k in st.index_range()])

    def test_hover_over_stale_geometry_selects_nothing(self):
        page = make_page()  # ring 1..30 inside a 120 s window: mostly empty space
        self._plotted(page)
        page._ram_motion(None, 6.0 + 10.0, 10.0)  # ts ~ -84: no sample within a tick
        self.assertIsNone(page.hover_ts)
        self.assertEqual(page.sample_label.get_label(), "Latest sample")
        self.assertFalse(page.sample_badge.get_visible())

    def test_touch_motion_never_hovers_but_tap_still_pins(self):
        page = make_page(hub=make_hub(feed_n=200))
        self._plotted(page)
        dev = SimpleNamespace(get_source=lambda: Gdk.InputSource.TOUCHSCREEN)
        ev = SimpleNamespace(get_device=lambda: dev)
        ctrl = SimpleNamespace(get_current_event=lambda: ev)
        page._ram_motion(ctrl, 6.0 + 100.0, 10.0)
        self.assertIsNone(page.hover_ts)  # preview parity: touch does not hover
        page._ram_click(None, 1, 6.0 + 100.0, 10.0)
        self.assertIsNotNone(page.pin_ts)  # tap-to-pin still works

    def test_hover_is_ignored_while_pinned(self):
        page = make_page(hub=make_hub(feed_n=200))
        page.qa_pin(page.ram_chart.store.last_t - 5.0)
        pinned = page.pin_ts
        self._hover(page, 0.9)
        self.assertEqual(page.pin_ts, pinned)
        self.assertEqual(page.ram_chart.cursor_ts, pinned)

    def test_click_pins_the_nearest_sample(self):
        page = make_page(hub=make_hub(feed_n=200))
        self._hover(page, 0.25)
        page._ram_click(None, 1, 6.0 + 50.0, 10.0)
        self.assertIsNotNone(page.pin_ts)
        self.assertTrue(page.latest_btn.get_visible())
        self.assertIn("Pinned · ", page.sample_label.get_label())

    def test_enter_pins_latest_and_escape_clears(self):
        page = make_page()
        st = page.ram_chart.store
        page._ram_key(None, Gdk.KEY_Return, 0, 0)
        self.assertEqual(page.pin_ts, st.last_t)
        self.assertEqual(page.sample_label.get_label(), "Pinned · latest")
        self.assertTrue(page.latest_btn.get_visible())
        page._ram_key(None, Gdk.KEY_Escape, 0, 0)
        self.assertIsNone(page.pin_ts)
        self.assertFalse(page.latest_btn.get_visible())
        self.assertEqual(page.sample_label.get_label(), "Latest sample")

    def test_arrows_step_one_ring_sample(self):
        page = make_page()
        st = page.ram_chart.store
        page._ram_key(None, Gdk.KEY_Left, 0, 0)  # not pinned: moves the hover
        self.assertEqual(page.hover_ts, st.times[st.slot(st.count - 2)])
        page._ram_key(None, Gdk.KEY_Right, 0, 0)
        self.assertEqual(page.hover_ts, st.last_t)
        page._ram_key(None, Gdk.KEY_Return, 0, 0)
        page._ram_key(None, Gdk.KEY_Left, 0, 0)
        self.assertEqual(page.pin_ts, st.times[st.slot(st.count - 2)])

    def test_left_clamps_at_the_visible_window_start(self):
        hub = make_hub(feed_n=200)  # ring 1.0 .. 200.0, window [80.0, 200.0]
        page = make_page(hub)
        st = page.ram_chart.store
        page.qa_pin(st.last_t - page.range_s)
        page._ram_key(None, Gdk.KEY_Left, 0, 0)
        self.assertEqual(page.pin_ts, st.last_t - page.range_s)  # and it stays there

    def test_a_pin_aging_out_of_the_ring_returns_to_latest(self):
        page = make_page()
        st = page.ram_chart.store  # samples 1.0 .. 30.0 in a 360-slot ring
        oldest = st.times[st.slot(0)]
        page.qa_pin(oldest)
        self.assertEqual(page.pin_ts, oldest)
        feed(st, 365)  # wraps the ring: the pinned sample has aged out entirely
        page.update()
        self.assertIsNone(page.pin_ts)
        self.assertEqual(page.sample_label.get_label(), "Latest sample")
        self.assertFalse(page.latest_btn.get_visible())

    def test_a_pin_aging_out_of_the_visible_range_returns_to_latest(self):
        hub = make_hub(feed_n=200)
        page = make_page(hub=hub)
        st = page.ram_chart.store
        page.qa_pin(st.last_t - page.range_s)
        st.commit(st.last_t + 1, mem_dict())
        page.update()
        self.assertIsNone(page.pin_ts)
        self.assertEqual(page.sample_label.get_label(), "Latest sample")

    def test_hover_into_a_gap_clears_the_previous_sample(self):
        page = make_page()
        self._hover(page, 0.9)
        self.assertIsNotNone(page.hover_ts)
        self._hover(page, 0.1)
        self.assertIsNone(page.hover_ts)
        self.assertEqual(page.sample_label.get_label(), "Latest sample")

    def test_a_pin_survives_a_view_switch(self):
        page = make_page()
        page.qa_pin(page.ram_chart.store.last_t - 7.0)
        pinned = page.pin_ts
        page.view_btns["usage"].set_active(True)
        self.assertEqual(page.pin_ts, pinned)
        self.assertEqual(page.ram_chart.cursor_ts, pinned)
        self.assertIn("Pinned · ", page.sample_label.get_label())

    def test_latest_button_clears_the_pin(self):
        page = make_page()
        page.qa_pin(page.ram_chart.store.last_t - 7.0)
        page.latest_btn.emit("clicked")
        self.assertIsNone(page.pin_ts)
        self.assertEqual(page.sample_label.get_label(), "Latest sample")

    def test_readout_shows_the_pinned_sample_values_and_missing_markers(self):
        mh = MemHistory()
        for i in range(10):
            mh.commit(1.0 + i, mem_dict(i, anon=None if i == 4 else 5 * GIB + 100 * i))
        page = make_page(mem=mh)
        st = page.ram_chart.store
        page.qa_pin(5.0)  # the anon gap
        self.assertEqual(page.readout_vals["anon"].get_label(), "–")
        page.qa_pin(3.0)
        self.assertEqual(txt(page.readout_vals["anon"]), fmt.bytes_(5 * GIB + 200))
        self.assertEqual(page.ram_chart.cursor_ts, 3.0)
        self.assertIsNone(value_at(st, "nope", 3.0, 1.0))  # unknown key: never raises


class A11yAndDetailsTests(WidgetCase):
    """Accessible toggles, the focusable chart and the technical details group."""

    def test_chart_is_focusable_labelled_and_has_the_key_controller(self):
        page = make_page()
        self.assertTrue(page.ram_chart.get_focusable())
        kinds = {type(c).__name__ for c in page.ram_chart.observe_controllers()}
        self.assertIn("EventControllerKey", kinds)
        self.assertIn("EventControllerMotion", kinds)
        self.assertIn("GestureClick", kinds)

    def test_view_toggles_carry_the_pressed_accessible_state(self):
        page = make_page()
        for btn in (*page.view_btns.values(), *page.range_btns.values()):
            self.assertEqual(btn.get_active(),
                             bool(btn.get_state_flags() & Gtk.StateFlags.CHECKED))
        page.view_btns["usage"].set_active(True)
        self.assertTrue(page.view_btns["usage"].get_active())
        self.assertFalse(page.view_btns["breakdown"].get_active())

    def test_details_rows_are_all_present(self):
        page = make_page()
        for key in ("total", "in_use", "available", "free", "buff_cache", "shmem",
                    "slab_r", "slab_u", "dirty", "committed", "page_tables",
                    "kernel_stack", "swap_cached", "dimm", "psi_some60",
                    "psi_some300", "psi_full60", "psi_full300"):
            self.assertIn(key, page.details.cells, key)

    def test_details_values_and_the_pressure_history_rows(self):
        page = make_page()
        self.assertEqual(txt(page.details.cells["committed"]),
                         f"{fmt.bytes_(44 * GIB)} of {fmt.bytes_(30 * GIB)}")
        self.assertEqual(txt(page.details.cells["page_tables"]), fmt.bytes_(87 << 20))
        self.assertEqual(txt(page.details.cells["psi_some300"]), fmt.pct(0.9, 2))
        self.assertEqual(txt(page.details.cells["psi_full60"]), fmt.pct(0.2, 2))

    def test_details_psi_rows_show_dash_when_psi_absent(self):
        page = make_page(info=mem_dict(psi=None, psi_reason="x"))
        for key in ("psi_some60", "psi_some300", "psi_full60", "psi_full300"):
            self.assertEqual(page.details.cells[key].get_label(), "–", key)

    def test_total_tooltip_conditional_ddr_text(self):
        page = make_page()
        self.assertIn("Speed, type and slot layout need root (DMI)",
                      page.details.boxes["total"].get_tooltip_text())
        ddr = {"modules": [{"slot": "DIMM0", "type": "DDR5", "speed_mts": 5600}]}
        page = make_page(info=mem_dict(ddr=ddr))
        self.assertEqual(page.details.boxes["total"].get_tooltip_text(),
                         ddr_text(ddr["modules"]))

    def test_committed_vm_is_a_plain_row_without_alarm_language(self):
        page = make_page()
        tip = page.details.boxes["committed"].get_tooltip_text()
        self.assertIn("separate measurement", tip)

    def test_disclosures_open_and_relabel(self):
        page = make_page()
        self.assertFalse(page.app_reveal.get_reveal_child())
        self.assertFalse(page.det_reveal.get_reveal_child())
        page.det_toggle.set_active(True)
        self.assertTrue(page.det_reveal.get_reveal_child())
        self.assertEqual(page.det_toggle.get_label(), "Hide")
        page.comp_toggle.set_active(True)
        self.assertTrue(page.comp_reveal.get_reveal_child())

    def test_compressed_memory_rows_and_zswap_state(self):
        page = make_page(info=mem_dict(
            zram=[{"device": "zram0", "orig": 1 << 30, "compr": 300 << 20,
                   "total": 320 << 20}], zram_any=True))
        page.update()
        self.assertIn(f"zram0: {fmt.bytes_(1 << 30)} stored", txt(page.zram_val))
        off = make_page(info=mem_dict(zswap_pool=0, zswap_stored=0))
        self.assertEqual(txt(off.zswap_val), "off")  # disabled: not "not reported"
        page = make_page(info=mem_dict(zswap_pool=64 << 20, zswap_stored=16 << 20))
        self.assertIn(f"{fmt.bytes_(16 << 20)} in {fmt.bytes_(64 << 20)}",
                      txt(page.zswap_val))

    def test_layout_cards_stack_from_level_2(self):
        page = make_page()
        page.fit_header(0)
        self.assertEqual(page.duo.get_orientation(), Gtk.Orientation.HORIZONTAL)
        page.fit_header(2)
        self.assertEqual(page.duo.get_orientation(), Gtk.Orientation.VERTICAL)


class AppPanelTests(WidgetCase):
    """The shell: placeholder before integration, owner-tagged walks, no hidden polling."""

    def test_app_panel_body_starts_as_a_placeholder_and_is_built_on_open(self):
        page = make_page()
        self.assertFalse(page.app_reveal.get_reveal_child())  # collapsed by default
        self.assertEqual(page.app_widget.get_label(), "–")  # nothing heavy while closed
        if _NO_APP_MEMORY_MODULE:
            page.qa_set_app_panel(True)
            self.assertEqual(page.app_widget.get_label(), "–")  # module missing: stays
        else:
            from asusius.ui.app_memory import AppMemoryPanel
            page.qa_set_app_panel(True)
            self.assertIsInstance(page.app_widget, AppMemoryPanel)  # built on first open

    def test_opening_requests_walks_with_the_owner_tag_and_resets(self):
        hub = make_hub()
        page = make_page(hub)
        page.qa_set_app_panel(True)
        self.assertTrue(page.app_reveal.get_reveal_child())
        self.assertTrue(page._app_open)
        hub.request_processes.assert_called_once_with(True, "memory-apps")
        hub.app_memory.reset.assert_called_once_with()
        page.qa_set_app_panel(False)
        hub.request_processes.assert_called_with(False, "memory-apps")
        self.assertEqual(hub.request_processes.call_count, 2)
        self.assertFalse(page.app_reveal.get_reveal_child())

    def test_page_hidden_releases_the_walk_and_shown_re_requests(self):
        hub = make_hub()
        page = make_page(hub)
        page.qa_set_app_panel(True)
        hub.request_processes.reset_mock()
        hub.app_memory.reset.reset_mock()
        page.on_hidden()
        hub.request_processes.assert_called_once_with(False, "memory-apps")
        hub.mem_page_hidden.assert_called_once_with(page)
        hub.mem_page_shown.assert_not_called()
        page.on_shown()
        hub.mem_page_shown.assert_called_once_with(page)
        hub.request_processes.assert_called_with(True, "memory-apps")
        # contract §6.2: the baseline clears on close AND page leave, so a
        # hidden->shown round trip resets twice (release-clear + re-activation)
        self.assertEqual(hub.app_memory.reset.call_count, 2)

    def test_no_hidden_polling_from_construction_updates_or_hide(self):
        hub = make_hub()
        page = make_page(hub)  # construction + one update
        page.update()
        page.update()
        hub.request_processes.assert_not_called()
        hub.app_memory.reset.assert_not_called()
        hub.mem_page_shown.assert_not_called()
        hub.mem_page_hidden.assert_not_called()
        page.on_hidden()
        hub.request_processes.assert_not_called()  # the panel never opened: no release
        hub.mem_page_hidden.assert_called_once()

    def test_update_repoints_the_charts_when_the_ring_appears(self):
        hub = make_hub(seams=False)  # pre-integration: no hub.mem_store
        page = make_page(hub)
        self.assertIsNone(page.ram_chart.store)
        page.update()
        self.assertEqual(page.readout_vals["anon"].get_label(), "–")
        hub.mem_store = MemHistory()
        feed(hub.mem_store, 5)
        page.update()
        self.assertIs(page.ram_chart.store, hub.mem_store)
        self.assertNotEqual(page.readout_vals["anon"].get_label(), "–")


class ChartSeamTests(WidgetCase):
    """Chart store/sample_interval defaults keep every existing caller byte-identical."""

    def test_old_way_still_reads_the_shared_hub_store(self):
        st = Store(300)
        for i in range(30):  # 0.5 s cadence inside a 20 s window: one point per sample
            st.push(1.0 + i * 0.5, {"cpu.user": 10.0 + i})
        hub = SimpleNamespace(store=st, interval_s=0.5, info={})
        chart = Chart(hub, [Line("cpu.user", "user")], window_s=20, floor=0, monitor=True)
        self.assertIsNone(chart.store)
        self.assertIsNone(chart.sample_interval)
        self.assertIsNone(chart.cursor_ts)
        draw_chart(chart)
        self.assertEqual(len(chart._xs[0]), 30)  # the points came from hub.store

    def test_memory_charts_read_the_mem_ring_at_ring_cadence(self):
        mh = MemHistory()
        feed(mh, 10)
        page = make_page(hub=make_hub(mem=mh))
        for c in (*page._mem_charts,):
            self.assertIs(c.store, mh)
            self.assertEqual(c.sample_interval, 1.0)  # ring cadence, not hub.interval_s

    def test_line_pattern_defaults_dash_compat_and_ram_scale(self):
        self.assertIsNone(Line("k").pattern)
        self.assertFalse(Line("k").dash)
        self.assertEqual(Line("k", pattern=(6, 3), dash=True).pattern, (6, 3))
        self.assertEqual(BREAKDOWN_LINES[1].pattern, (6, 3))  # file cache: dashed gold
        self.assertIsNone(BREAKDOWN_LINES[0].pattern)  # anon: solid primary
        for line in (*BREAKDOWN_LINES, *USAGE_LINES):
            self.assertEqual(line.scale, 1 / GIB)  # bytes -> the chart's GiB axis

    def test_value_at_gap_missing_tolerance_and_none_store(self):
        st = Store(60)
        st.push(1.0, {"a": 5.0})
        st.push(2.0, {"a": None})  # an unreadable source: NaN gap
        st.push(3.0, {"a": 7.0})
        self.assertEqual(value_at(st, "a", 3.0, 1.0), 7.0)
        self.assertIsNone(value_at(st, "a", 2.0, 1.0))
        self.assertIsNone(value_at(st, "a", 50.0, 1.0))  # outside one interval of any
        self.assertIsNone(value_at(st, "b", 3.0, 1.0))
        self.assertIsNone(value_at(None, "a", 3.0, 1.0))

    def test_cursor_ts_api_and_undrawn_geometry(self):
        hub = SimpleNamespace(store=Store(60), interval_s=0.5, info={})
        chart = Chart(hub, [Line("k")])
        chart.set_cursor_ts(12.0)
        self.assertEqual(chart.cursor_ts, 12.0)
        self.assertIsNone(chart.ts_at_x(10.0))  # never drawn: no geometry to map


class RamChartDrawTests(WidgetCase):
    """Draw-level checks for GiB scaling and capacity composition."""

    def _ticks(self, page) -> list[float]:
        seen: list[float] = []

        def _fmt(v):
            seen.append(v)
            return "x"

        page.ram_chart.fmt = _fmt
        draw_chart(page.ram_chart)
        return seen

    def test_breakdown_ticks_are_gib_ranged(self):
        page = make_page()
        ticks = self._ticks(page)
        self.assertTrue(ticks)
        self.assertLess(max(ticks), 100.0)  # GiB — not 1e+09 GiB (raw bytes)
        self.assertEqual(min(ticks), 0.0)

    def test_usage_fixed_axis_ticks_are_gib_ranged(self):
        page = make_page()
        page.view_btns["usage"].set_active(True)
        ticks = self._ticks(page)
        self.assertTrue(ticks)
        self.assertEqual(min(ticks), 0.0)
        self.assertGreater(max(ticks), 8.0)  # spans toward the 16 GiB total...
        self.assertLessEqual(max(ticks), 16.0)  # ...in GiB, never raw bytes

    def test_usage_stack_fills_used_then_available_to_capacity(self):
        page = make_page()
        page.view_btns["usage"].set_active(True)
        draw_chart(page.ram_chart)
        used, capacity = page.ram_chart._ys
        self.assertTrue(used)
        for a, b in zip(used, capacity):
            self.assertGreater(a, 0.0)
            self.assertLess(a, b)
            self.assertAlmostEqual(b, 16.0, places=5)
        page.view_btns["breakdown"].set_active(True)
        self.assertFalse(page.ram_chart.stacked)


class SwapReadoutTests(WidgetCase):
    """The pressure card's swap row states agree with the summary card."""

    def test_no_swap_configured_is_not_a_zero_reading(self):
        mh = MemHistory()
        feed(mh, 3, swap_total=0, swap_used=0)
        page = make_page(mem=mh, info=mem_dict(swap_total=0, swap_used=0))
        self.assertEqual(txt(page.swap_used_val), "no swap configured")

    def test_configured_and_quiet_swap_reads_zero_bytes(self):
        mh = MemHistory()
        for i in range(5):
            mh.commit(1.0 + i, mem_dict(i, swap_used=0))
        page = make_page(mem=mh)
        self.assertEqual(txt(page.swap_used_val), "0 B")

    def test_configured_swap_reads_the_ring_value(self):
        page = make_page()
        self.assertEqual(txt(page.swap_used_val), fmt.bytes_(112 << 20))


class LifecycleTests(WidgetCase):
    """A hub without any of the new seams (parallel phase) never raises."""

    def test_bare_hub_construction_lifecycle_and_qa_hooks(self):
        hub = SimpleNamespace(store=Store(300), interval_s=0.5,
                              sampler=SimpleNamespace(), info={})
        page = make_page(hub)
        self.assertEqual(page.sub.get_label(), "No data yet")
        page.on_shown()
        page.on_hidden()
        page.qa_set_app_panel(True)
        page.qa_set_app_panel(False)
        page.qa_pin(1.0)
        self.assertEqual(page.sample_label.get_label(), "Latest sample")


class QaAuditGatesTests(WidgetCase):
    """The two qa_audit gates this page must hold: keyboard reachability and width."""

    INTERACTIVE = (Gtk.Button, Gtk.ToggleButton, Gtk.CheckButton, Gtk.Switch, Gtk.Entry,
                   Gtk.SearchEntry, Gtk.SpinButton, Gtk.DropDown, Gtk.Scale)

    @staticmethod
    def _ancestors(w) -> list:
        out, p = [], w.get_parent()
        while p is not None:
            out.append(p)
            p = p.get_parent()
        return out

    def _unreachable(self, root) -> list[str]:
        """qa_audit's focus-unreachable rule: can_focus and focusable on the widget
        and every ancestor (one non-focusable wrapper orphans everything inside)."""
        bad = []

        def visit(w):
            if isinstance(w, self.INTERACTIVE) and w.get_sensitive():
                if not (w.get_can_focus() and w.get_focusable()
                        and all(a.get_can_focus() for a in self._ancestors(w))):
                    bad.append(type(w).__name__)
            c = w.get_first_child()
            while c is not None:
                visit(c)
                c = c.get_next_sibling()

        visit(root)
        return bad

    def test_every_toggle_and_button_is_keyboard_reachable(self):
        page = make_page()
        page.view_btns["usage"].set_active(True)  # rebuilds the legend rows
        page.view_btns["breakdown"].set_active(True)
        page.qa_set_app_panel(True)  # C's panel widgets, once the module lands
        self.assertEqual(self._unreachable(page), [])

    def test_head_controls_wrap_and_the_page_fits_360px(self):
        page = make_page()
        page.update()
        page.fit_header(3)  # 360 px: everything stacks
        self.assertIsInstance(page.ram_controls, Gtk.FlowBox)  # wraps, never a fixed row
        self.assertLessEqual(page.measure(Gtk.Orientation.HORIZONTAL, -1)[0], 360)
        page.qa_set_app_panel(True)  # the widest state (app panel open) must fit too
        self.assertLessEqual(page.measure(Gtk.Orientation.HORIZONTAL, -1)[0], 360)

    def test_ram_controls_wrap_button_by_button_below_the_title(self):
        """The controls live on their own row under the title.

        Sharing the head row with an expanding title starved the wrapping controls
        down to their minimum width, and the resulting multi-row head was taller
        than the natural-height budget the duo had measured — the legend and the
        note stuck out of the panel vertically (qa: overflows-parent)."""
        page = make_page()
        self.assertIs(page.ram_controls.get_parent(), page.ram_panel)
        self.assertIsNot(page.ram_controls.get_parent(),
                         page.ram_panel.get_first_child())  # not inside the head row
        buttons = [c.get_child() for c in kids(page.ram_controls)]
        self.assertEqual(len(buttons), 5)
        for button in buttons:
            self.assertIsInstance(button, Gtk.ToggleButton)

    def test_readout_values_and_note_cannot_reflow_taller(self):
        """Row heights stay put: values are single-line at their natural width (never
        wrapped, never ellipsized — an essential reading is always fully visible) and
        the note's natural width is bounded."""
        page = make_page()
        self.assertEqual(page._legend_cols, 2)
        for lab in (*page.readout_vals.values(), page.swap_used_val):
            self.assertFalse(lab.get_wrap())
            self.assertEqual(lab.get_ellipsize(), Pango.EllipsizeMode.NONE)
            self.assertEqual(lab.get_width_chars(), -1)
        self.assertEqual(page.chart_note.get_max_width_chars(), 52)

    def test_wide_layout_still_goes_side_by_side(self):
        page = make_page()
        page.fit_header(0)
        self.assertEqual(page.duo.get_orientation(), Gtk.Orientation.HORIZONTAL)
