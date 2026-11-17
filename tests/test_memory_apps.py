# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""App-memory grouping, the growth tracker, and the panel's 360 px contract.

The grouping/tracker tests are widget-free logic against records shaped exactly
like walker output. The panel tests at the bottom build the real AppMemoryPanel
(no display needed): the environment is isolated before any gi import (memory
GSettings backend, throwaway XDG dirs), the same pattern as the UI test modules.
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_TMP = Path(tempfile.mkdtemp(prefix="asusius-memory-apps-"))
for _sub in ("config", "data"):
    (_TMP / _sub).mkdir()
os.environ["GSETTINGS_BACKEND"] = "memory"  # before anything imports gi
_XDG_KEYS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME")
_OLD_XDG = {k: os.environ.get(k) for k in _XDG_KEYS}


def setUpModule():
    """XDG dirs are scoped to this module's tests (never at import time)."""
    os.environ["XDG_CONFIG_HOME"] = str(_TMP / "config")
    os.environ["XDG_DATA_HOME"] = str(_TMP / "data")


def tearDownModule():
    for _k, _v in _OLD_XDG.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")  # noqa: E402
gi.require_version("Adw", "1")  # noqa: E402

from gi.repository import Gtk  # noqa: E402

from asusius.telemetry.app_memory import (MAX_GROUPS, MAX_MEMBERS, AppMemRow,  # noqa: E402
                                          AppMemoryTracker, group_apps)
from asusius.telemetry.procs import Proc  # noqa: E402
from asusius.ui.app_memory import ROW_LIMIT, AppMemoryPanel  # noqa: E402

OWN_PID = 4242
OWN_APP = "io.example.Own"
LONG_APP = "com.overlong." + "label" * 30  # an overlong app id like the fake tree's


def make_proc(pid, start, app=None, mem=0, partial=False, ppid=1, unit=None):
    """One walker-shaped record (a real slotted Proc, fields set by hand)."""
    p = Proc(pid, start)
    p.ppid = ppid
    p.app = app
    p.unit = unit
    p.mem = mem
    p.rss = mem
    p.mem_partial = partial
    p.name = f"p{pid}"
    return p


class Grouping(unittest.TestCase):
    def test_direct_app_id_groups_without_hops(self):
        procs = [make_proc(10, 1, app="a.b"), make_proc(11, 1, app="a.b"),
                 make_proc(12, 1, app="c.d")]
        groups = group_apps(procs, OWN_PID, OWN_APP)
        self.assertEqual(sorted(groups), ["a.b", "c.d"])
        self.assertEqual([p.pid for p in groups["a.b"]], [10, 11])

    def test_parent_hop_fills_a_missing_app_id(self):
        parent = make_proc(10, 1, app="a.b", ppid=1)
        child = make_proc(11, 1, app=None, ppid=10)
        grand = make_proc(12, 1, app=None, ppid=11)
        groups = group_apps([parent, child, grand], OWN_PID, OWN_APP)
        self.assertEqual([p.pid for p in groups["a.b"]], [10, 11, 12])

    def test_hop_walks_at_most_eight_ancestors(self):
        chain = [make_proc(100 + i, 1, app=None, ppid=99 + i) for i in range(1, 11)]
        root = make_proc(100, 1, app="deep.app", ppid=1)
        procs = [root] + [p for p in chain if p.pid != 100]
        # 101..110: pid 109 is 9 hops above 100's child 101 — beyond the limit
        groups = group_apps(procs, OWN_PID, OWN_APP)
        self.assertEqual(len(groups["deep.app"]), 9)  # root + 8 hop descendants

    def test_hop_never_walks_through_init_or_absent_parents(self):
        orphan = make_proc(30, 1, app=None, ppid=1)  # init is not a hop
        missing = make_proc(31, 1, app=None, ppid=999)
        groups = group_apps([orphan, missing], OWN_PID, OWN_APP)
        self.assertEqual(groups, {})

    def test_own_process_becomes_the_own_app(self):
        own = make_proc(OWN_PID, 7, app=None)
        kid = make_proc(OWN_PID + 1, 7, app=None, ppid=OWN_PID)
        groups = group_apps([own, kid], OWN_PID, OWN_APP)
        self.assertEqual(groups, {OWN_APP: [own, kid]})

    def test_ungrouped_processes_are_left_out(self):
        procs = [make_proc(10, 1, app=None), make_proc(11, 1, app="a.b")]
        self.assertEqual(group_apps(procs, OWN_PID, OWN_APP), {"a.b": [procs[1]]})


class Tracker(unittest.TestCase):
    def tracker(self):
        return AppMemoryTracker(own_pid=OWN_PID, own_app_id=OWN_APP)

    def row(self, tracker, app_id, mode="size"):
        for r in tracker.ranking(mode):
            if r.app_id == app_id:
                return r
        raise AssertionError(f"no row {app_id}")

    def test_baseline_is_the_first_fully_valid_reading_and_growth_follows(self):
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app="a", mem=100)])
        r = self.row(t, "a")
        self.assertIsNone(r.growth_bytes)  # the baseline reading itself: no growth yet
        self.assertAlmostEqual(r.baseline_age_s, 0.0)
        self.assertFalse(r.is_new)
        t.observe(40.0, [make_proc(10, 1, app="a", mem=200)])
        r = self.row(t, "a")
        self.assertEqual(r.growth_bytes, 100)
        self.assertAlmostEqual(r.baseline_age_s, 30.0)

    def test_partial_reading_delays_the_baseline(self):
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app="a", mem=100, partial=True)])
        self.assertIsNone(self.row(t, "a").growth_bytes)
        self.assertEqual(t.baseline_count, 0)
        t.observe(20.0, [make_proc(10, 1, app="a", mem=150)])  # statm readable again
        t.observe(30.0, [make_proc(10, 1, app="a", mem=250)])
        self.assertEqual(self.row(t, "a").growth_bytes, 100)  # baseline was t=20
        self.assertAlmostEqual(self.row(t, "a").baseline_age_s, 10.0)

    def test_partial_group_is_marked_in_the_row(self):
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app="a", mem=100, partial=True)])
        self.assertTrue(self.row(t, "a").partial)
        t.observe(20.0, [make_proc(10, 1, app="a", mem=140)])
        self.assertFalse(self.row(t, "a").partial)

    def test_growth_skips_an_rss_fallback_without_losing_the_valid_baseline(self):
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app="a", mem=100)])
        t.observe(12.0, [make_proc(10, 1, app="a", mem=900, partial=True)])
        self.assertIsNone(self.row(t, "a").growth_bytes)
        t.observe(14.0, [make_proc(10, 1, app="a", mem=120)])
        self.assertEqual(self.row(t, "a").growth_bytes, 20)

    def test_group_new_since_opening_gets_the_new_badge_and_its_own_baseline(self):
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app="a", mem=100)])
        t.observe(20.0, [make_proc(10, 1, app="a", mem=110),
                         make_proc(11, 1, app="b", mem=500)])
        b = self.row(t, "b")
        self.assertTrue(b.is_new)  # absent from the opening snapshot
        self.assertIsNone(b.growth_bytes)  # its first reading is its own baseline
        t.observe(30.0, [make_proc(10, 1, app="a", mem=120),
                         make_proc(11, 1, app="b", mem=600)])
        self.assertEqual(self.row(t, "b").growth_bytes, 100)
        self.assertTrue(self.row(t, "b").is_new)  # still new since opening
        self.assertFalse(self.row(t, "a").is_new)

    def test_reused_pid_with_a_new_start_time_is_a_new_identity(self):
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app="a", mem=100)])
        # same PID, different start: no overlapping (pid, start) member, so the
        # old baseline can never compare — the group re-baselines and shows New
        t.observe(20.0, [make_proc(10, 2, app="a", mem=900)])
        r = self.row(t, "a")
        self.assertIsNone(r.growth_bytes)
        self.assertTrue(r.is_new)
        t.observe(30.0, [make_proc(10, 2, app="a", mem=950)])
        r = self.row(t, "a")  # growth resumes against the restart baseline
        self.assertEqual(r.growth_bytes, 50)
        self.assertAlmostEqual(r.baseline_age_s, 10.0)

    def test_full_member_churn_rebaselines_without_pid_reuse(self):
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app="a", mem=100),
                         make_proc(11, 1, app="a", mem=50)])
        t.observe(20.0, [make_proc(20, 1, app="a", mem=300)])  # all members replaced
        self.assertIsNone(self.row(t, "a").growth_bytes)
        self.assertTrue(self.row(t, "a").is_new)
        t.observe(30.0, [make_proc(20, 1, app="a", mem=350)])
        self.assertEqual(self.row(t, "a").growth_bytes, 50)

    def test_caps_stop_baseline_retention_and_surface_partial_coverage(self):
        t = self.tracker()
        procs = [make_proc(1000 + i, 1, app=f"app.{i:03d}", mem=i)
                 for i in range(MAX_GROUPS + 20)]
        t.observe(10.0, procs)  # one delivery: caps are per-baseline, not per-tick
        self.assertEqual(t.baseline_count, MAX_GROUPS)
        self.assertEqual(t.coverage, "partial")
        self.assertEqual(t.group_count, MAX_GROUPS + 20)
        # the current-size ranking is never truncated, only the retention is
        self.assertEqual(len(t.ranking()), MAX_GROUPS + 20)
        self.assertEqual(t.ranking()[0].app_id, f"app.{MAX_GROUPS + 19:03d}")
        self.assertIsNone(t.ranking()[0].growth_bytes)  # past the cap: no baseline
        self.assertEqual(t.ranking()[-1].app_id, "app.000")  # smallest, has a baseline

    def test_member_cap_also_surfaces_partial_coverage(self):
        t = self.tracker()
        per_group = MAX_MEMBERS // 100
        for i in range(100):  # 100 groups x 20 members = 2000 identities: fits
            t.observe(10.0, [make_proc(1000 + i * per_group + j, 1,
                                       app=f"g{i:02d}", mem=1)
                             for j in range(per_group)])
        self.assertEqual(t.coverage, "full")
        t.observe(20.0, [make_proc(9000 + j, 1, app="overflow", mem=1)
                         for j in range(MAX_MEMBERS)])
        self.assertEqual(t.coverage, "partial")
        self.assertEqual(t.baseline_count, 100)
        self.assertIsNone(self.row(t, "overflow").growth_bytes)

    def test_ranking_orders_by_size_then_app_id(self):
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app="zeta", mem=100),
                         make_proc(11, 1, app="alpha", mem=100),
                         make_proc(12, 1, app="mid", mem=500)])
        self.assertEqual([r.app_id for r in t.ranking()],
                         ["mid", "alpha", "zeta"])  # tie: app id wins

    def test_growth_ranking_puts_missing_growth_last_deterministically(self):
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app="z.b", mem=100),
                         make_proc(11, 1, app="a.b", mem=900),
                         make_proc(12, 1, app="m.b", mem=100)])
        t.observe(30.0, [make_proc(10, 1, app="z.b", mem=400),  # +300
                         make_proc(11, 1, app="a.b", mem=1000),  # +100
                         make_proc(12, 1, app="m.b", mem=130),  # +30
                         make_proc(13, 1, app="new.b", mem=50)])  # no baseline yet
        self.assertEqual([r.app_id for r in t.ranking("growth")],
                         ["z.b", "a.b", "m.b", "new.b"])
        self.assertTrue(t.ranking("growth")[-1].is_new)

    def test_rows_are_immutable_and_carry_the_full_shape(self):
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app="a", mem=100)])
        row = t.ranking()[0]
        self.assertIsInstance(row, AppMemRow)
        self.assertEqual((row.app_id, row.name_key, row.size_bytes, row.partial),
                         ("a", "a", 100, False))
        with self.assertRaises(AttributeError):
            row.size_bytes = 5  # named tuples are immutable: the aggregate cannot rot

    def test_reset_clears_baseline_and_new_state_for_a_fresh_round(self):
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app="a", mem=100)])
        t.observe(20.0, [make_proc(10, 1, app="a", mem=200),
                         make_proc(11, 1, app="late", mem=10)])
        t.reset()
        self.assertEqual((t.baseline_count, t.group_count, t.coverage), (0, 0, "full"))
        self.assertEqual(t.ranking(), [])
        t.observe(30.0, [make_proc(10, 1, app="a", mem=300)])
        r = self.row(t, "a")  # the pre-reset growth is gone; a new baseline starts
        self.assertIsNone(r.growth_bytes)
        self.assertFalse(r.is_new)  # "a" is in the new round's opening snapshot
        t.observe(40.0, [make_proc(10, 1, app="a", mem=320)])
        self.assertEqual(self.row(t, "a").growth_bytes, 20)

    def test_reset_between_rounds_reuses_the_same_tracker_object(self):
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app="a", mem=100)])
        t.reset()
        t.observe(20.0, [make_proc(11, 1, app="b", mem=500)])
        self.assertEqual([r.app_id for r in t.ranking()], ["b"])  # no stale aggregate
        self.assertEqual(t.member_count("a"), 0)

    def test_long_labels_pass_through_intact_and_rank_deterministically(self):
        long_id = "com.overlong." + "label" * 30
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app=long_id, mem=700),
                         make_proc(11, 1, app="short", mem=50)])
        rows = t.ranking()
        self.assertEqual(rows[0].app_id, long_id)  # never truncated in the data layer
        self.assertEqual(t.member_count(long_id), 1)
        self.assertAlmostEqual(rows[0].baseline_age_s, 0.0)

    def test_member_count_tracks_the_latest_delivery(self):
        t = self.tracker()
        t.observe(10.0, [make_proc(10, 1, app="a", mem=100),
                         make_proc(11, 1, app="a", mem=50)])
        self.assertEqual(t.member_count("a"), 2)
        t.observe(20.0, [make_proc(10, 1, app="a", mem=160)])
        self.assertEqual(t.member_count("a"), 1)
        self.assertEqual(t.group_count, 1)

    def test_launched_flag_follows_the_app_unit_rule(self):
        t = self.tracker()
        t.observe(10.0, [
            make_proc(10, 1, app="a.b", mem=100, unit="app-gnome-a.b-1.scope"),
            make_proc(11, 1, app="a.b", mem=40,
                      unit="user@1000.service/session.slice/x.service"),
            make_proc(12, 1, app="c.d", mem=50,
                      unit="user@1000.service/session.slice/y.service"),
        ])
        self.assertTrue(self.row(t, "a.b").launched)  # any member under app- counts
        self.assertFalse(self.row(t, "c.d").launched)  # session slice only: unrecognized

    def test_empty_first_delivery_does_not_become_the_opening_snapshot(self):
        t = self.tracker()
        t.observe(10.0, [])  # the walk returned nothing right after opening
        t.observe(20.0, [make_proc(10, 1, app="a", mem=100)])
        self.assertFalse(self.row(t, "a").is_new)  # a is in the real opening snapshot
        t.observe(30.0, [make_proc(10, 1, app="a", mem=120),
                         make_proc(11, 1, app="late", mem=10)])
        self.assertTrue(self.row(t, "late").is_new)  # later arrivals still are


class PanelMinWidth(unittest.TestCase):
    """The real AppMemoryPanel stays inside a 360 px window with realistic rows.

    Names ellipsize and the size/growth pair is width-bounded, so each row line
    shrinks; the Size | Growth / Open Processes head wraps (FlowBox) instead of
    forcing a three-button single line that a narrow window would clip."""

    def panel_with_rows(self):
        class Hub:
            def __init__(self):
                self.procs_gen = 0
                self.listeners = []
                self.app_memory = AppMemoryTracker(own_pid=999999,
                                                   own_app_id="own.app")

            def request_processes(self, on, owner="processes"):
                pass

        hub = Hub()
        panel = AppMemoryPanel(SimpleNamespace(hub=hub, win=None))
        panel.set_open(True)
        procs = []
        for i, (app, mib) in enumerate(zip(
                ("com.demo.Firefox", "com.demo.Code", "com.demo.Steam",
                 "org.demo.Nautilus", LONG_APP, "com.demo.Cyberpunk2077"),
                (640, 420, 520, 5200, 88, 95))):
            p = make_proc(10 + i, 1, app=app, mem=mib * (1 << 20))
            p.unit = f"app-gnome-{app}-{10 + i}.scope"  # launched: recognized
            procs.append(p)
        hub.procs_gen += 1
        hub.app_memory.observe(10.0, procs)
        panel._on_hub(hub)  # the delivery refresh the mapped panel would run
        return panel

    def row_count(self, panel):
        n, c = 0, panel.rows_box.get_first_child()
        while c is not None:
            n += 1
            c = c.get_next_sibling()
        return n

    def test_minimum_width_stays_within_360(self):
        panel = self.panel_with_rows()
        mn = panel.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        self.assertGreater(mn, 0)
        self.assertLessEqual(mn, 360)

    def test_head_wraps_instead_of_forcing_three_buttons_on_one_line(self):
        panel = self.panel_with_rows()
        head = panel.get_first_child()
        self.assertIsInstance(head, Gtk.FlowBox)
        self.assertEqual(head.get_max_children_per_line(), 3)
        mn = head.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        self.assertGreater(mn, 0)
        self.assertLessEqual(mn, 220)  # the widest single cell, not a button row

    def test_five_recognized_rows_render(self):
        panel = self.panel_with_rows()
        self.assertEqual(self.row_count(panel), ROW_LIMIT)


if __name__ == "__main__":
    unittest.main()
