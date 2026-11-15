# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""The processes page logic against fake collaborators: no widgets, no display.

The environment is isolated before any gi import (memory GSettings backend, XDG
dirs in a throwaway tempdir) so the owner's dconf is never touched. The page is
never instantiated: unbound methods run against a SimpleNamespace page whose
list models (root store, tree, selection) are real Gio/GTK *models* — they carry
no widgets — so splicing, expansion and selection behave exactly as on screen.
run_bg and the GLib timers are patched throughout; the backend collaborators
(inspect_process, kill, set_priority) are mocked or captured, never run.
"""
import os
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

_TMP = Path(tempfile.mkdtemp(prefix="asusius-ui-proc-page-"))
for _sub in ("config", "data"):
    (_TMP / _sub).mkdir()
os.environ["GSETTINGS_BACKEND"] = "memory"  # before anything imports gi
_XDG_KEYS = ("XDG_CONFIG_HOME", "XDG_DATA_HOME")
_OLD_XDG = {k: os.environ.get(k) for k in _XDG_KEYS}


def setUpModule():
    """XDG dirs are scoped to this module's tests: import-time state here must never
    reach another module's tests (test_gestures_page compiles schemas into its own
    XDG_DATA_HOME and would otherwise see ours)."""
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

from gi.repository import Gio, Gtk, Pango  # noqa: E402

import asusius.ui.pages.processes as processes  # noqa: E402
from asusius.telemetry import fmt  # noqa: E402
from asusius.ui.pages.processes import (  # noqa: E402
    COLUMNS, SECTION_APPS, SECTION_PROCS, SYSTEM_KEY, InspectorDialog, ProcessesPage, Row,
    totals_for)

APP = "org.demo.Web"
MY_UID = os.getuid()


def proc(pid, start=None, name="proc", cmd=None, app=None, unit=None, cpu=None, mem=0,
         swap=None, io_r=None, io_w=None, gpu=None, vram=None, uid=MY_UID, kthread=False,
         ppid=1, threads=1):
    """One walker record (Proc is slotted, so a stand-in namespace is used)."""
    return SimpleNamespace(
        pid=pid, start=pid * 10 if start is None else start, ppid=ppid, name=name,
        cmd=f"/usr/bin/{name} --flag" if cmd is None else cmd, uid=uid, state="S",
        threads=threads, cpu=cpu, mem=mem, swap=swap, io_read=io_r, io_write=io_w,
        gpu=gpu, vram=vram, app=app, unit=unit, kthread=kthread, seen=1)


def web_procs():
    """Two processes of one app, one system process, one kernel thread."""
    return [proc(10, name="web", app=APP, unit=f"app-gnome-{APP}-1.scope", cpu=1.5),
            proc(11, name="web2", app=APP, unit=f"app-gnome-{APP}-2.scope", cpu=2.5),
            proc(20, name="svc", cpu=3.0),
            proc(2, name="kthreadd", cmd="", kthread=True)]


def bound(page, *names):
    """Bind ProcessesPage methods onto the stand-in so self.method(...) resolves."""
    for n in names:
        def make(n):
            def call(*a, **k):
                return getattr(ProcessesPage, n)(page, *a, **k)
            return call
        setattr(page, n, make(n))


def fake_page(procs=(), fake_hub=False, search="", root="/"):
    p = SimpleNamespace()
    p.hub = SimpleNamespace(procs=list(procs), procs_gen=1, fake=fake_hub, root=root,
                            want_processes=Mock())
    p.win = Mock()
    p.search = Mock()
    p.search.get_text.return_value = search
    p.summary = Mock()
    p.end_btn, p.kill_btn = Mock(), Mock()
    p.details_btn, p.collapse_btn = Mock(), Mock()
    p.rows, p.flat, p.apps = {}, {}, {}
    p._dindex, p._dindex_busy = {}, False
    p._quit_timer = 0
    p._aligned = True  # the header-alignment idle stays out of the tests
    p._gen = -1
    p._order = []
    p._shown = True
    p._rebuilding = False
    p._insp = None
    p._insp_token = 0
    p._insp_timer = 0
    p._insp_pending = None
    p._insp_target = None
    p._sec_kids = {SECTION_APPS: [], SECTION_PROCS: []}
    p._expanded = {SECTION_APPS: True, SECTION_PROCS: True}
    p.sections = {}
    for key, title in ((SECTION_APPS, "Apps (0)"), (SECTION_PROCS, "Processes (0)")):
        r = Row(key, "section")
        r.name, r.cmd = title, ""
        r.children = Gio.ListStore(item_type=Row)
        p.sections[key] = r
    p.root_store = Gio.ListStore(item_type=Row)
    p.tree = Gtk.TreeListModel.new(p.root_store, False, False, ProcessesPage._children)
    p.sel = Gtk.SingleSelection(model=p.tree, autoselect=False, can_unselect=True)
    p.sel.connect("notify::selected", lambda *_: ProcessesPage._selection_changed(p))
    p.cols = {k: Mock() for k, _t, _c, _tip in COLUMNS}
    p.cv = Mock()
    sorter = p.cv.get_sorter.return_value
    sorter.get_primary_sort_column.return_value = p.cols["pid"]
    sorter.get_primary_sort_order.return_value = Gtk.SortType.ASCENDING
    bound(p, "_selected_row", "_refusal", "_targets", "_sort", "_app_info", "_desktop_index",
          "_proc_row", "_fill_group", "_splice", "_update_titles", "_open_inspector",
          "_insp_arm", "_insp_fetch", "_insp_tick", "_insp_deliver", "_insp_stop_timer",
          "_insp_closed", "_insp_resume", "_insp_priority_done", "_insp_priority_error")
    return p


def bind_dialog(d, *names):
    for n in names:
        def make(n):
            def call(*a, **k):
                return getattr(InspectorDialog, n)(d, *a, **k)
            return call
        setattr(d, n, make(n))


def fake_dialog(page):
    d = SimpleNamespace(page=page, pid=20, start=1020, detail=None, gone=False,
                        stack=Mock(), main=Mock(), status=Mock(), cmd_row=Mock(),
                        spin=Mock(), apply_btn=Mock(), copy_btn=Mock(), stop_btn=Mock(),
                        force_btn=Mock(), vals={})
    d.spin.get_value.return_value = 0.0
    d._to_clipboard = Mock()
    for key, _cap in (*processes.INFO_ROWS, *processes.MEMORY_ROWS):
        d.vals[key] = Mock()
    d.vals["args"] = Mock()
    bind_dialog(d, "got_detail", "got_priority", "_mark_gone", "_set_stop_actions", "_set_val")
    return d


def labels(d):
    return {k: v.set_label.call_args.args[0].replace("\u00a0", " ")
            for k, v in d.vals.items() if v.set_label.call_args}


def summary_text(page):
    calls = page.summary.set_label.call_args_list
    return calls[-1].args[0].replace("\u00a0", " ") if calls else ""


def tree_items(page):
    return [page.tree.get_row(i).get_item() for i in range(page.tree.get_n_items())]


def tree_row(page, key):
    """The visible tree row holding `key`; positions shift as rows expand and collapse."""
    for i in range(page.tree.get_n_items()):
        tr = page.tree.get_row(i)
        if tr.get_item().key == key:
            return tr
    raise AssertionError(f"no visible tree row {key}")


def select(page, pid):
    for i, item in enumerate(tree_items(page)):
        if item.kind == "proc" and item.pid == pid:
            page.sel.set_selected(i)
            return item
    raise AssertionError(f"no proc row {pid}")


def ok_detail(**over):
    fields = dict(status="ok", pid=20, start=1020, comm="svc", name="svc",
                  argv0="/usr/bin/svc", args=["svc", "--one", "--two"], args_count=2,
                  uid=os.getuid(), user="me", ppid=1, state="S", threads=4, uptime_s=3661.0,
                  nice=0, app=None, unit="session.slice", cgroup="/user.slice/user.slice",
                  rss=4096, hwm=8192, swap=1024, private=2048, shared=512)
    fields.update(over)
    return SimpleNamespace(**fields)


class Totals(unittest.TestCase):
    def test_empty_set_is_all_dash(self):
        for v in totals_for([]).values():
            self.assertEqual(v, "–")

    def test_sums_over_the_process_set_with_dash_for_missing(self):
        procs = [proc(1, cpu=10.5, mem=100, swap=512 * 1024, io_w=100, gpu=80.0),
                 proc(2, cpu=None, mem=200, swap=None, io_r=None, gpu=90.0)]
        t = totals_for(procs)
        self.assertEqual(t["cpu"], fmt.pct(10.5, 1))
        self.assertEqual(t["mem"], fmt.bytes_(300))
        self.assertEqual(t["swap"], fmt.bytes_(512 * 1024))
        self.assertEqual(t["disk"], fmt.rate(100))  # io write only, io read missing
        self.assertEqual(t["gpu"], fmt.pct(100.0, 0))  # capped at 100
        self.assertEqual(t["vram"], "–")  # no contributor: no fabricated zero

    def test_memory_shown_even_at_zero_bytes(self):
        self.assertEqual(totals_for([proc(1, mem=0)])["mem"], "0 B")

    def test_gpu_zero_contributor_is_a_real_zero(self):
        self.assertEqual(totals_for([proc(1, gpu=0.0)])["gpu"], "0 %")

    def test_row_text_renders_missing_values_as_dash(self):
        r = Row((1, 1), "proc")
        r.cpu = r.swap = None
        r.io_r = r.io_w = None
        self.assertEqual(r.text("cpu"), "–")
        self.assertEqual(r.text("swap"), "–")
        self.assertEqual(r.text("disk"), "–")
        self.assertEqual(r.text("name"), "")
        r.name = "[kthreadd]"
        self.assertEqual(r.text("name"), "[kthreadd]")  # no count suffix for non-app rows


class Sections(unittest.TestCase):
    def test_bound_section_label_tracks_the_count(self):
        p = fake_page(web_procs())
        name = p.sections[SECTION_APPS].cells["name"] = Mock()
        name.get_label.return_value = "Apps (0)"

        ProcessesPage._rebuild(p)

        name.set_label.assert_called_with("Apps (1)")
        self.assertEqual(p.sections[SECTION_APPS].text("cpu"), "")

    def test_running_asusius_pid_overrides_a_missing_or_inherited_app_id(self):
        for scope_app in (None, "org.chromium.Chromium"):
            with self.subTest(scope_app=scope_app):
                own = proc(os.getpid(), app=scope_app)
                p = fake_page([own])
                ProcessesPage._rebuild(p)
                group, = p.sections[SECTION_APPS].children
                self.assertEqual(group.key, processes.OWN_APP_ID)
                self.assertEqual(group.name, "Asusius")

    def test_failed_snapshot_clears_previous_rows_and_selection(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        select(p, 20)
        p.hub.procs = []
        ProcessesPage._rebuild(p)
        self.assertEqual(p.sections[SECTION_PROCS].children.get_n_items(), 0)
        self.assertEqual(p.sections[SECTION_APPS].children.get_n_items(), 0)
        self.assertIsNone(p.sel.get_selected_item())
        p.end_btn.set_sensitive.assert_called_with(False)

    def test_two_pinned_sections_with_groups_and_flat_children(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        self.assertEqual([p.root_store.get_item(i).key
                          for i in range(p.root_store.get_n_items())],
                         [SECTION_APPS, SECTION_PROCS])
        self.assertEqual(p.sections[SECTION_APPS].name, "Apps (1)")
        self.assertEqual(p.sections[SECTION_PROCS].name, "Processes (4)")
        kids = p.sections[SECTION_APPS].children
        self.assertEqual([k.key for k in kids], [APP, SYSTEM_KEY])  # pseudo-group last
        self.assertEqual(kids[0].count, 2)
        self.assertEqual([k.pid for k in p.sections[SECTION_PROCS].children],
                         [2, 10, 11, 20])  # every listed process, flat
        self.assertEqual(summary_text(p), "1 apps · 4 of 4 processes · CPU 7.0 %")

    def test_app_children_and_flat_rows_are_distinct_instances(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        for key in ((10, 100), (11, 110)):
            self.assertIn(key, p.rows)
            self.assertIn(key, p.flat)
            self.assertIsNot(p.rows[key], p.flat[key])  # one Row never serves two stores

    def test_section_rows_are_reused_across_rebuilds(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        first = p.root_store.get_item(0)
        ProcessesPage._rebuild(p)
        self.assertIs(p.root_store.get_item(0), first)

    def test_search_keeps_groups_atomic_and_filters_the_flat_section(self):
        p = fake_page(web_procs(), search="svc")
        ProcessesPage._rebuild(p)
        self.assertEqual(p.sections[SECTION_APPS].name, "Apps (0)")  # only the pseudo-group
        self.assertEqual(p.sections[SECTION_APPS].children[0].key, SYSTEM_KEY)
        self.assertEqual(p.sections[SECTION_APPS].children[0].count, 1)
        self.assertEqual([k.pid for k in p.sections[SECTION_PROCS].children], [20])
        p2 = fake_page(web_procs(), search="web")
        ProcessesPage._rebuild(p2)
        self.assertEqual(p2.sections[SECTION_APPS].name, "Apps (1)")
        self.assertEqual(p2.sections[SECTION_APPS].children[0].count, 2)  # whole group
        self.assertEqual([k.pid for k in p2.sections[SECTION_PROCS].children], [10, 11])
        self.assertEqual(summary_text(p2), "1 apps · 2 of 4 processes · CPU 4.0 %")

    def test_search_without_matches_empties_both_sections(self):
        p = fake_page(web_procs(), search="zzz")
        ProcessesPage._rebuild(p)
        self.assertEqual(p.sections[SECTION_APPS].name, "Apps (0)")
        self.assertEqual(p.sections[SECTION_PROCS].children.get_n_items(), 0)
        self.assertEqual(summary_text(p), "0 apps · 0 of 4 processes · CPU 0.0 %")

    def test_headers_carry_the_filtered_set_sums(self):
        procs = web_procs()
        procs.append(proc(30, name="needle", cpu=45.0, mem=7))
        p = fake_page(procs, search="needle")
        ProcessesPage._rebuild(p)
        titles = {k: [c.args[0] for c in p.cols[k].set_title.call_args_list]
                  for k in p.cols}
        self.assertIn(f"CPU\n{fmt.pct(45.0, 1)}", titles["cpu"])  # not the other 3 procs
        self.assertIn(f"Memory\n{fmt.bytes_(7)}", titles["mem"])
        self.assertIn("Swap\n–", titles["swap"])
        self.assertIn("Drive\n–", titles["disk"])

    def test_children_model_of_rows(self):
        app = Row(APP, "app")
        app.children = "store"
        section = Row(SECTION_APPS, "section")
        section.children = "store2"
        prow = Row((1, 2), "proc")
        self.assertIs(ProcessesPage._children(app), "store")
        self.assertIs(ProcessesPage._children(section), "store2")
        self.assertIsNone(ProcessesPage._children(prow))


class Layout(unittest.TestCase):
    """The toolbar construction shape: the buttons live in the top bar and the bottom
    row keeps its baseline three-child shape, so a narrow window never crushes the
    summary's minimum width. Built against the real page (widgets, no display)."""

    def children_of(self, w):
        out, c = [], w.get_first_child()
        while c is not None:
            out.append(c)
            c = c.get_next_sibling()
        return out

    def test_top_bar_holds_collapse_search_details(self):
        page = ProcessesPage(Mock())
        outer = self.children_of(page.get_child())
        bar = self.children_of(outer[0])
        self.assertEqual(bar, [page.collapse_btn, page.search, page.details_btn])
        self.assertEqual(page.search.get_hexpand(), True)  # the filter takes the width

    def test_summary_row_keeps_the_baseline_shape(self):
        page = ProcessesPage(Mock())
        outer = self.children_of(page.get_child())
        sum_row = self.children_of(outer[1])
        self.assertEqual(sum_row, [page.summary, page.end_btn, page.kill_btn])
        self.assertEqual(len(sum_row), 3)  # no fourth button crushing the summary

    def test_details_button_still_tracks_proc_selections(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        p.sel.set_selected(0)  # section row
        p.details_btn.set_sensitive.assert_called_with(False)
        select(p, 10)
        p.details_btn.set_sensitive.assert_called_with(True)


class DialogMeasure(unittest.TestCase):
    """The inspector's content stays shrinkable: value labels ellipsize instead of
    demanding their full text as a minimum, so the dialog measures under its own
    width (QA: "AdwBreakpointBin ... reported min width 635 and natural width 460",
    driven by the unwrapped Cgroup value of the fake firefox)."""

    QA_VALUES = dict(
        status="ok", pid=2101, start=3101, comm="Isolated Web Co", name="firefox",
        argv0="/usr/lib/firefox/firefox", args=["/usr/lib/firefox/firefox",
                                                "-contentproc"], args_count=1,
        uid=1000, user="user", ppid=2101, state="S", threads=87, uptime_s=125952.0,
        nice=0, app="org.mozilla.firefox", unit="app-gnome-firefox-2101.scope",
        cgroup="/user.slice/user-1000.slice/user@1000.service/app.slice/"
               "app-gnome-firefox-2101.scope",
        rss=1 << 29, hwm=None, swap=1 << 24, private=1 << 28, shared=1 << 26)

    def dialog(self):
        page = Mock()
        page.win = Mock()
        row = SimpleNamespace(kind="proc", pid=2101, start=3101, name="firefox",
                              uid=1000)
        dlg = InspectorDialog(page, row)
        dlg.got_detail(SimpleNamespace(**self.QA_VALUES))
        return dlg

    def test_value_labels_ellipsize_and_carry_the_full_text(self):
        dlg = self.dialog()
        for key, lab in dlg.vals.items():
            self.assertNotEqual(lab.get_ellipsize(), Pango.EllipsizeMode.NONE, key)
            self.assertGreater(lab.get_max_width_chars(), 0, key)
        self.assertEqual(dlg.vals["cgroup"].get_ellipsize(), Pango.EllipsizeMode.START)
        self.assertEqual(dlg.vals["cgroup"].get_tooltip_text(), self.QA_VALUES["cgroup"])
        self.assertEqual(dlg.vals["unit"].get_tooltip_text(), self.QA_VALUES["unit"])
        self.assertEqual(dlg.vals["hwm"].get_tooltip_text(), "–")

    def test_content_measures_under_the_dialog_width(self):
        dlg = self.dialog()
        mn, nat, _, _ = dlg.get_child().measure(Gtk.Orientation.HORIZONTAL, -1)
        self.assertLessEqual(mn, 360)  # fits a 360 px bottom sheet — never 635 again
        self.assertGreaterEqual(nat, mn)  # the measure is consistent


class Expansion(unittest.TestCase):
    def test_real_page_opens_apps_and_expands_the_full_list_on_request(self):
        page = ProcessesPage(Mock())
        page.hub = SimpleNamespace(procs=web_procs(), procs_gen=1)
        page._aligned = True
        page._rebuild()
        self.assertTrue(tree_row(page, SECTION_APPS).get_expanded())
        section = tree_row(page, SECTION_PROCS)
        self.assertFalse(section.get_expanded())
        section.set_expanded(True)
        page._rebuild()
        self.assertCountEqual([r.pid for r in tree_items(page) if r.kind == "proc"],
                              [2, 10, 11, 20])
        select(page, 20)
        self.assertTrue(page.details_btn.get_sensitive())

    def test_expanded_sections_are_kept_across_rebuilds(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        keys = [i.key for i in tree_items(p)]
        self.assertEqual(keys[:3], [SECTION_APPS, APP, SYSTEM_KEY])  # both sections open
        ProcessesPage._rebuild(p)
        self.assertEqual([i.key for i in tree_items(p)], keys)

    def test_section_collapse_persists_across_rebuilds(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        tree_row(p, SECTION_APPS).set_expanded(False)
        tree_row(p, SECTION_PROCS).set_expanded(False)
        ProcessesPage._rebuild(p)
        self.assertEqual(p.tree.get_n_items(), 2)  # collapsed: the children stay hidden
        self.assertFalse(tree_row(p, SECTION_APPS).get_expanded())
        self.assertFalse(tree_row(p, SECTION_PROCS).get_expanded())
        tree_row(p, SECTION_APPS).set_expanded(True)
        tree_row(p, SECTION_PROCS).set_expanded(True)
        ProcessesPage._rebuild(p)
        self.assertEqual(p.tree.get_n_items(), 8)
        self.assertTrue(tree_row(p, SECTION_APPS).get_expanded())
        self.assertTrue(tree_row(p, SECTION_PROCS).get_expanded())

    def test_group_expansion_tracked_per_key(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        group = p.tree.get_row(1)  # first child of the open Apps section
        self.assertEqual(group.get_item().key, APP)
        group.set_expanded(True)
        ProcessesPage._rebuild(p)
        self.assertTrue(p.tree.get_row(1).get_expanded())
        self.assertEqual(p.tree.get_n_items(), 10)
        p.tree.get_row(1).set_expanded(False)
        ProcessesPage._rebuild(p)
        self.assertFalse(p.tree.get_row(1).get_expanded())
        self.assertEqual(p.tree.get_n_items(), 8)

    def test_collapse_all_collapses_visible_and_hidden_groups(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        p.tree.get_row(1).set_expanded(True)  # the app group
        p.tree.get_row(0).set_expanded(False)  # hide it again, then collapse everything
        ProcessesPage._collapse_all(p)
        self.assertFalse(any(p._expanded.values()))
        self.assertFalse(p._expanded[APP])  # the hidden group too
        self.assertEqual(p.tree.get_n_items(), 2)
        ProcessesPage._rebuild(p)
        self.assertEqual(p.tree.get_n_items(), 2)  # nothing comes back on its own

    def test_collapse_all_reopens_cleanly(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        ProcessesPage._collapse_all(p)
        tree_row(p, SECTION_APPS).set_expanded(True)
        tree_row(p, SECTION_PROCS).set_expanded(True)
        tree_row(p, APP).set_expanded(True)  # the app group, revealed again
        ProcessesPage._rebuild(p)
        self.assertEqual(tree_row(p, SECTION_APPS).get_item().key, SECTION_APPS)
        self.assertTrue(tree_row(p, APP).get_expanded())
        self.assertEqual(p.tree.get_n_items(), 10)  # sections, group, children, flat — all back


class Selection(unittest.TestCase):
    def test_selection_survives_a_rebuild(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        select(p, 11)
        p.hub.procs[1].cpu = 99.0  # value change only: same identity
        ProcessesPage._rebuild(p)
        self.assertEqual(p.sel.get_selected_item().get_item().key, (11, 110))

    def test_selection_keeps_the_store_instance_when_possible(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        p.tree.get_row(1).set_expanded(True)  # reveal the app's own child rows
        app_child = p.rows[(10, 100)]
        for i, item in enumerate(tree_items(p)):
            if item is app_child:
                p.sel.set_selected(i)
                break
        ProcessesPage._rebuild(p)
        self.assertIs(p.sel.get_selected_item().get_item(), app_child)

    def test_selection_dropped_when_the_process_is_gone(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        select(p, 20)
        p.hub.procs = [q for q in p.hub.procs if q.pid != 20]
        p.hub.procs_gen += 1
        ProcessesPage._rebuild(p)
        self.assertEqual(p.sel.get_selected(), Gtk.INVALID_LIST_POSITION)

    def test_details_button_only_for_proc_rows(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        p.sel.set_selected(0)  # section row
        p.details_btn.set_sensitive.assert_called_with(False)
        p.end_btn.set_sensitive.assert_called_with(False)
        p.end_btn.set_tooltip_text.assert_called_with("Select an app or a process to stop it")
        select(p, 10)  # proc row
        p.details_btn.set_sensitive.assert_called_with(True)
        p.end_btn.set_sensitive.assert_called_with(True)
        for i, item in enumerate(tree_items(p)):
            if item.kind == "app" and item.key == APP:
                p.sel.set_selected(i)
                break
        p.details_btn.set_sensitive.assert_called_with(False)  # group row: no inspector


class LargeList(unittest.TestCase):
    def test_search_over_600_processes_and_totals_from_the_filtered_set(self):
        procs = [proc(1000 + i, name=f"bulk{i:03d}", cpu=1.0) for i in range(590)]
        procs += [proc(2000 + i, name=f"needle{i}", cpu=float(i)) for i in range(10)]
        p = fake_page(procs)
        ProcessesPage._rebuild(p)
        self.assertEqual(p.sections[SECTION_PROCS].name, "Processes (600)")
        self.assertEqual(p.sections[SECTION_PROCS].children.get_n_items(), 600)
        titles = [c.args[0] for c in p.cols["cpu"].set_title.call_args_list]
        self.assertEqual(titles[-1], f"CPU\n{fmt.pct(635.0, 1)}")  # the whole list so far
        p.search.get_text.return_value = "needle"
        ProcessesPage._rebuild(p, force=True)
        self.assertEqual(p.sections[SECTION_APPS].name, "Apps (0)")
        self.assertEqual(p.sections[SECTION_PROCS].name, "Processes (10)")
        flat = p.sections[SECTION_PROCS].children
        self.assertEqual([r.pid for r in flat], list(range(2000, 2010)))
        titles = [c.args[0] for c in p.cols["cpu"].set_title.call_args_list]
        self.assertEqual(titles[-1], f"CPU\n{fmt.pct(45.0, 1)}")  # the 10 needles only
        self.assertIn(f"Memory\n{fmt.bytes_(0)}",
                      [c.args[0] for c in p.cols["mem"].set_title.call_args_list])
        select(p, 2005)
        p.hub.procs[595].cpu = -1.0
        ProcessesPage._rebuild(p)
        self.assertEqual(p.sel.get_selected_item().get_item().pid, 2005)


class InspectorLifecycle(unittest.TestCase):
    def test_rebuild_preserves_the_inspector_when_the_selected_row_moves(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        select(p, 20)
        dlg = p._insp = Mock()
        selected = p.sel.get_selected()
        p.hub.procs.append(proc(15))
        ProcessesPage._rebuild(p)

        self.assertIs(p._insp, dlg)
        self.assertEqual(p.sel.get_selected_item().get_item().pid, 20)
        self.assertNotEqual(p.sel.get_selected(), selected)
        dlg.close.assert_not_called()

    def test_open_inspector_starts_the_loop_for_the_selected_identity(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        select(p, 20)
        with patch.object(processes, "InspectorDialog") as dlg, \
                patch.object(processes, "run_bg") as rb, \
                patch.object(processes.GLib, "timeout_add", return_value=55) as ta:
            ProcessesPage.open_inspector(p)
            dlg.assert_called_once()
            made = dlg.return_value
            made.present.assert_called_once_with(p.win)
            self.assertIs(p._insp, made)
            self.assertEqual(p._insp_target, (20, 200))
            self.assertEqual(rb.call_args.args,
                             (processes.inspect_process, 20, 200, "/"))
            ta.assert_called_once_with(processes.INSPECT_MS, p._insp_tick)

    def test_inspect_reads_through_the_hub_root(self):
        """The snapshot follows the hub's root: the fake tree in fake mode (a read-only
        inspection); the hub.fake guard stays on the dialog's write actions only."""
        p = fake_page(web_procs(), fake_hub=True, root="/tmp/asusius-fake")
        ProcessesPage._rebuild(p)
        select(p, 20)
        with patch.object(processes, "InspectorDialog") as dlg, \
                patch.object(processes, "run_bg") as rb, \
                patch.object(processes.GLib, "timeout_add", return_value=55):
            ProcessesPage.open_inspector(p)
            self.assertEqual(rb.call_args.args,
                             (processes.inspect_process, 20, 200, "/tmp/asusius-fake"))
            dlg.return_value.got_detail(Mock())  # a Mock dialog absorbs rendering

    def test_identity_lost_stays_lost(self):
        """Exited/changed: every control is disabled for good — a snapshot that slips
        through afterwards re-enables nothing and leaves the StatusPage up."""
        p = fake_page()
        d = fake_dialog(p)
        p._insp = d
        p._insp_timer = 55
        p._insp_target = (20, 1020)
        with patch.object(processes.GLib, "source_remove"):
            ProcessesPage._insp_deliver(p, p._insp_token, 20, 1020, ok_detail(status="exited"))
        self.assertTrue(d.gone)
        p._ask_signal = Mock()
        with patch.object(processes, "run_bg") as rb:
            ProcessesPage._insp_deliver(p, p._insp_token, 20, 1020, ok_detail())
        d.stack.set_visible_child.assert_called_with(d.status)  # still the StatusPage
        d.stop_btn.set_sensitive.assert_called_with(False)
        d.force_btn.set_sensitive.assert_called_with(False)
        d.spin.set_sensitive.assert_called_with(False)
        d.copy_btn.set_sensitive.assert_called_with(False)
        InspectorDialog._stop(d, signal.SIGTERM)  # dead identity: never routed
        p._ask_signal.assert_not_called()
        InspectorDialog._apply_priority(d)  # dead identity: never dispatched
        rb.assert_not_called()

    def test_open_inspector_is_a_safe_noop_without_a_proc_selection(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        p.sel.set_selected(0)  # section row
        with patch.object(processes, "InspectorDialog") as dlg:
            ProcessesPage.open_inspector(p)
            dlg.assert_not_called()
            self.assertIsNone(p._insp)

    def test_opening_while_open_replaces_the_dialog(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        select(p, 10)
        with patch.object(processes, "InspectorDialog") as dlg, \
                patch.object(processes, "run_bg"), \
                patch.object(processes.GLib, "timeout_add", return_value=55):
            ProcessesPage.open_inspector(p)
            first = dlg.return_value
            with patch.object(processes.GLib, "source_remove") as sr:
                select(p, 20)  # the selection change closes and discards the dialog...
                ProcessesPage.open_inspector(p)  # ...and a fresh one opens for pid 20
            first.close.assert_called_once()
            sr.assert_called_once_with(55)
            self.assertEqual(p._insp_target, (20, 200))

    def test_delivery_guard_token_identity_open_and_shown(self):
        p = fake_page()
        d = Mock()
        p._insp = d
        p._insp_token = 5
        p._insp_target = (20, 200)
        detail = ok_detail()
        ProcessesPage._insp_deliver(p, 5, 20, 200, detail)
        d.got_detail.assert_called_once_with(detail)
        ProcessesPage._insp_deliver(p, 4, 20, 200, detail)  # stale token
        ProcessesPage._insp_deliver(p, 5, 21, 200, detail)  # another identity
        p._shown = False
        ProcessesPage._insp_deliver(p, 5, 20, 200, detail)  # page hidden
        p._insp = None
        ProcessesPage._insp_deliver(p, 5, 20, 200, detail)  # dialog closed
        d.got_detail.assert_called_once()

    def test_exit_during_inspection_shows_status_stops_timer_and_disables(self):
        p = fake_page()
        d = fake_dialog(p)
        p._insp = d
        p._insp_timer = 55
        p._insp_target = (20, 1020)
        with patch.object(processes.GLib, "source_remove") as sr:
            ProcessesPage._insp_deliver(p, p._insp_token, 20, 1020, ok_detail(status="exited"))
        sr.assert_called_once_with(55)
        self.assertEqual(p._insp_timer, 0)
        d.status.set_title.assert_called_with(processes.EXITED_TEXT)
        d.stack.set_visible_child.assert_called_with(d.status)
        d.stop_btn.set_sensitive.assert_called_with(False)
        d.force_btn.set_sensitive.assert_called_with(False)
        d.spin.set_sensitive.assert_called_with(False)

    def test_identity_reuse_shows_the_changed_text(self):
        p = fake_page()
        d = fake_dialog(p)
        p._insp = d
        p._insp_target = (20, 1020)
        ProcessesPage._insp_deliver(p, p._insp_token, 20, 1020, ok_detail(status="changed"))
        d.status.set_title.assert_called_with(processes.CHANGED_TEXT)

    def test_close_discards_and_reopens_with_a_fresh_token(self):
        p = fake_page()
        d = Mock()
        p._insp = d
        p._insp_timer = 55
        p._insp_target = (20, 200)
        with patch.object(processes.GLib, "source_remove") as sr:
            ProcessesPage._insp_closed(p, d)
        sr.assert_called_once_with(55)
        self.assertIsNone(p._insp)
        self.assertIsNone(p._insp_target)
        stale = p._insp_token
        ProcessesPage._insp_deliver(p, stale, 20, 200, ok_detail())
        d.got_detail.assert_not_called()
        p._insp = d
        p._insp_target = (20, 200)
        ProcessesPage._insp_deliver(p, p._insp_token, 20, 200, ok_detail())
        d.got_detail.assert_called_once()  # token is monotonic: the new round passes

    def test_selection_change_while_open_closes_and_discards(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        select(p, 20)
        with patch.object(processes, "InspectorDialog") as dlg, \
                patch.object(processes, "run_bg"), \
                patch.object(processes.GLib, "timeout_add", return_value=55), \
                patch.object(processes.GLib, "source_remove"):
            ProcessesPage.open_inspector(p)
            token = p._insp_token
            select(p, 10)  # another process selected
        made = dlg.return_value
        made.close.assert_called_once()
        self.assertIsNone(p._insp)
        self.assertGreater(p._insp_token, token)
        ProcessesPage._insp_deliver(p, token, 20, 200, ok_detail())
        made.got_detail.assert_not_called()

    def test_page_hidden_bumps_token_and_stops_the_timer_but_keeps_the_dialog(self):
        p = fake_page()
        d = Mock()
        p._insp = d
        p._insp_timer = 55
        token = p._insp_token
        with patch.object(processes.GLib, "source_remove") as sr:
            ProcessesPage.on_hidden(p)
        sr.assert_called_once_with(55)
        self.assertFalse(p._shown)
        self.assertGreater(p._insp_token, token)
        self.assertIs(p._insp, d)  # frozen, not closed: shown again resumes the loop

    def test_shown_again_resumes_the_loop_until_it_is_armed(self):
        p = fake_page()
        p._insp = Mock()
        p._insp.gone = False
        with patch.object(processes.GLib, "timeout_add", return_value=77) as ta:
            ProcessesPage._insp_resume(p)
        ta.assert_called_once_with(processes.INSPECT_MS, p._insp_tick)
        with patch.object(processes.GLib, "timeout_add", return_value=78) as ta2:
            ProcessesPage._insp_resume(p)
        ta2.assert_not_called()  # already armed: never two loops


class InspectorDialogTests(unittest.TestCase):
    def test_ok_snapshot_fills_every_field_without_rendering_argv(self):
        d = fake_dialog(fake_page())
        InspectorDialog.got_detail(d, ok_detail())
        vals = labels(d)
        self.assertEqual(vals["name"], "svc")
        self.assertEqual(vals["pid"], "20")
        self.assertEqual(vals["ppid"], "1")
        self.assertEqual(vals["user"], "me")
        self.assertEqual(vals["state"], "S")
        self.assertEqual(vals["threads"], "4")
        self.assertEqual(vals["uptime"], fmt.duration(3661.0))
        self.assertEqual(vals["nice"], "0")
        self.assertEqual(vals["unit"], "session.slice")
        self.assertEqual(vals["cgroup"], "/user.slice/user.slice")
        self.assertEqual(vals["rss"], fmt.bytes_(4096))
        self.assertEqual(vals["private"], fmt.bytes_(2048))
        self.assertEqual(vals["shared"], fmt.bytes_(512))
        self.assertEqual(vals["swap"], fmt.bytes_(1024))
        self.assertEqual(vals["args"], "2 arguments")  # count only: argv is never rendered
        d.cmd_row.set_subtitle.assert_called_with("/usr/bin/svc")
        argv_text = " ".join(ok_detail().args)
        for call in d.cmd_row.set_subtitle.call_args_list:
            self.assertNotIn("--one", call.args[0])
        for v in d.vals.values():
            for call in v.set_label.call_args_list:
                self.assertNotIn(argv_text, call.args[0])
        d.copy_btn.set_sensitive.assert_called_with(True)

    def test_missing_sources_render_dash_and_disable_priority(self):
        d = fake_dialog(fake_page())
        InspectorDialog.got_detail(d, ok_detail(hwm=None, user=None, uptime_s=None,
                                                nice=None, unit=None, cgroup=None))
        vals = labels(d)
        self.assertEqual(vals["hwm"], "–")
        self.assertEqual(vals["user"], "–")
        self.assertEqual(vals["uptime"], "–")
        self.assertEqual(vals["nice"], "–")
        self.assertEqual(vals["unit"], "–")
        self.assertEqual(vals["cgroup"], "–")
        d.spin.set_sensitive.assert_called_with(False)
        d.apply_btn.set_sensitive.assert_called_with(False)

    def test_arguments_line_wording(self):
        for n, want in ((0, "No arguments"), (1, "1 argument"), (9, "9 arguments")):
            d = fake_dialog(fake_page())
            InspectorDialog.got_detail(d, ok_detail(args_count=n))
            self.assertEqual(labels(d)["args"], want, n)
        d = fake_dialog(fake_page())
        InspectorDialog.got_detail(d, ok_detail(args_count=None))  # cmdline unreadable
        self.assertEqual(labels(d)["args"], "–")

    def test_copy_puts_the_command_line_on_the_clipboard_and_only_toasts(self):
        p = fake_page()
        d = fake_dialog(p)
        InspectorDialog.got_detail(d, ok_detail())
        InspectorDialog._copy(d)
        d._to_clipboard.assert_called_once_with("svc --one --two")
        p.win.toast.assert_called_with("Command line copied")
        self.assertNotIn("--one", p.win.toast.call_args.args[0])  # the toast echoes nothing

    def test_stop_routes_the_inspected_identity_through_the_page(self):
        p = fake_page()
        p._ask_signal = Mock()
        d = fake_dialog(p)
        InspectorDialog.got_detail(d, ok_detail())
        InspectorDialog._stop(d, signal.SIGKILL)
        p._ask_signal.assert_called_once_with("svc (PID 20)", [(20, 1020)], signal.SIGKILL)

    def test_stop_needs_a_live_snapshot(self):
        p = fake_page()
        p._ask_signal = Mock()
        d = fake_dialog(p)
        InspectorDialog._stop(d, signal.SIGTERM)  # nothing inspected yet
        p._ask_signal.assert_not_called()
        InspectorDialog.got_detail(d, ok_detail(status="exited"))
        InspectorDialog._stop(d, signal.SIGTERM)
        p._ask_signal.assert_not_called()


class Signals(unittest.TestCase):
    def test_signal_sends_the_checked_signal_and_toasts(self):
        p = fake_page()
        with patch.object(processes, "kill") as k, \
                patch.object(processes.GLib, "timeout_add", return_value=9):
            ProcessesPage._signal(p, "svc (PID 20)", [(20, 200)], signal.SIGTERM)
        k.assert_called_once_with(20, 200, signal.SIGTERM)
        p.win.toast.assert_called_with("Asked svc (PID 20) to quit")
        self.assertEqual(p._quit_timer, 9)  # the 3 s still-running check is armed

    def test_signal_toasts_permission_errors(self):
        p = fake_page()
        with patch.object(processes, "kill", side_effect=PermissionError("not permitted")), \
                patch.object(processes.GLib, "timeout_add", return_value=9):
            ProcessesPage._signal(p, "svc (PID 20)", [(20, 200)], signal.SIGKILL)
        p.win.toast.assert_called_with("Signalled 0 of 1 for svc (PID 20). "
                                       "Failed: PID 20 (not permitted)")

    def test_signal_ignores_a_process_that_vanished(self):
        p = fake_page()
        with patch.object(processes, "kill", side_effect=ProcessLookupError), \
                patch.object(processes.GLib, "timeout_add", return_value=9):
            ProcessesPage._signal(p, "svc (PID 20)", [(20, 200)], signal.SIGTERM)
        p.win.toast.assert_called_with("svc (PID 20) already exited")

    def test_fake_hub_never_sends_a_signal(self):
        p = fake_page(fake_hub=True)
        with patch.object(processes, "kill") as k:
            ProcessesPage._signal(p, "svc (PID 20)", [(20, 200)], signal.SIGKILL)
        k.assert_not_called()
        p.win.toast.assert_called_with("Fake hardware: svc (PID 20) not signalled")

    def test_ask_signal_confirms_with_destructive_styling(self):
        p = fake_page()
        p._signal = Mock()
        ProcessesPage._ask_signal(p, "svc (PID 20)", [(20, 200)], signal.SIGTERM)
        heading, body, action, on_yes = p.win.confirm.call_args.args
        self.assertEqual((heading, action), ("Stop svc (PID 20)?", "Stop"))
        self.assertTrue(p.win.confirm.call_args.kwargs["destructive"])
        on_yes()
        p._signal.assert_called_once_with("svc (PID 20)", [(20, 200)], signal.SIGTERM)
        ProcessesPage._ask_signal(p, "svc (PID 20)", [(20, 200)], signal.SIGKILL)
        heading, _b, action, _y = p.win.confirm.call_args.args
        self.assertEqual((heading, action), ("Force stop svc (PID 20)?", "Force Stop"))

    def test_ask_signal_refuses_protected_targets(self):
        p = fake_page()
        ProcessesPage._ask_signal(p, "init", [(1, 0)], signal.SIGKILL)
        p.win.confirm.assert_not_called()
        p2 = fake_page()
        with patch.object(processes.os, "getpid", return_value=4242):
            ProcessesPage._ask_signal(p2, "self", [(4242, 1)], signal.SIGKILL)
        p2.win.confirm.assert_not_called()

    def test_end_task_ignores_section_rows(self):
        p = fake_page(web_procs())
        ProcessesPage._rebuild(p)
        p.sel.set_selected(0)
        ProcessesPage._end_task(p, signal.SIGTERM)
        p.win.confirm.assert_not_called()


class Priority(unittest.TestCase):
    def test_apply_runs_set_priority_off_thread(self):
        p = fake_page()
        d = fake_dialog(p)
        InspectorDialog.got_detail(d, ok_detail(nice=0))
        d.spin.get_value.return_value = -5.0
        with patch.object(processes, "run_bg") as rb:
            InspectorDialog._apply_priority(d)
        self.assertEqual(rb.call_args.args, (processes.set_priority, 20, 1020, -5))

    def test_result_updates_the_nice_row_and_toasts(self):
        p = fake_page()
        d = fake_dialog(p)
        p._insp = d
        p._insp_target = (20, 1020)
        with patch.object(processes, "run_bg") as rb:
            InspectorDialog.got_detail(d, ok_detail(nice=0))
            d.spin.get_value.return_value = 10.0
            InspectorDialog._apply_priority(d)
            rb.call_args.kwargs["done"](10)
        self.assertEqual(labels(d)["nice"], "10")
        p.win.toast.assert_called_with("Priority set to 10")

    def test_eacces_toasts_the_honest_hint(self):
        p = fake_page()
        d = fake_dialog(p)
        p._insp = d
        p._insp_target = (20, 1020)
        exc = PermissionError("renicing the process failed: raising a priority may need "
                              "privileges")
        with patch.object(processes, "run_bg") as rb:
            InspectorDialog.got_detail(d, ok_detail())
            InspectorDialog._apply_priority(d)
            rb.call_args.kwargs["error"](exc)
        p.win.toast.assert_called_with(str(exc))
        d.stack.set_visible_child.assert_called_with(d.main)  # still the fields, no status
        d.status.set_title.assert_not_called()

    def test_exited_between_apply_and_readback_shows_the_status_page(self):
        p = fake_page()
        d = fake_dialog(p)
        p._insp = d
        p._insp_timer = 55
        p._insp_target = (20, 1020)
        with patch.object(processes, "run_bg") as rb, \
                patch.object(processes.GLib, "source_remove") as sr:
            InspectorDialog.got_detail(d, ok_detail())
            InspectorDialog._apply_priority(d)
            rb.call_args.kwargs["error"](ProcessLookupError("gone"))
        sr.assert_called_once_with(55)
        d.status.set_title.assert_called_with(processes.EXITED_TEXT)
        d.stack.set_visible_child.assert_called_with(d.status)

    def test_fake_hub_never_touches_the_os(self):
        p = fake_page(fake_hub=True)
        d = fake_dialog(p)
        with patch.object(processes, "run_bg") as rb:
            InspectorDialog.got_detail(d, ok_detail())
            InspectorDialog._apply_priority(d)
        rb.assert_not_called()
        p.win.toast.assert_called_with("Fake hardware: priority not changed")

    def test_stale_priority_result_is_discarded(self):
        p = fake_page()
        d = Mock()
        p._insp = d
        p._insp_target = (20, 1020)
        ProcessesPage._insp_priority_done(p, p._insp_token + 1, 20, 1020, 10)
        ProcessesPage._insp_priority_done(p, p._insp_token, 21, 1020, 10)
        d.got_priority.assert_not_called()
        ProcessesPage._insp_priority_done(p, p._insp_token, 20, 1020, 10)
        d.got_priority.assert_called_once_with(10)


if __name__ == "__main__":
    unittest.main()
