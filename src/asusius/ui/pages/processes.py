# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Processes and apps page: grouped and flat views over a GTK column view."""
from __future__ import annotations

import os
import signal

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ...telemetry import fmt
from ...telemetry.app_memory import group_apps
from ...telemetry.procs import alive, inspect_process, kill, set_priority
from ..tasks import run_bg
from ..viewmodels import OWN_APP_ID, signal_refusal
from ..widgets import label
from .base import a11y_name
from .monitor_base import MonitorPage, set_text
from .process_inspector import InspectorDialog, _fit_val, _HAS_ADW_DIALOG, _DIALOG_BASE
from .process_rows import (
    CHANGED_TEXT,
    COLUMNS,
    EXITED_TEXT,
    INFO_ROWS,
    INSPECT_MS,
    MEMORY_ROWS,
    SECTION_APPS,
    SECTION_PROCS,
    SORT_KEYS,
    SYSTEM_KEY,
    VAL_MAX_CHARS,
    Row,
    _attach_header_tooltip,
    _sum_opt,
    app_display_info,
    installed_desktop_index,
    totals_for,
)


def _ancestors(w):
    p = w.get_parent()
    while p is not None:
        yield p
        p = p.get_parent()


class ProcessesPage(MonitorPage):
    key, title = "processes", "Processes"
    rate_s = 1.0

    def __init__(self, win) -> None:
        super().__init__(win)
        # this page manages its own layout: no clamp, the table fills the page
        self.set_child(None)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=16,
                        margin_start=16, margin_end=16, margin_bottom=12)
        bar = Gtk.Box(spacing=8)
        self.collapse_btn = Gtk.Button(
            label="Collapse All",
            tooltip_text="Collapse both sections and every app group")
        self.collapse_btn.connect("clicked", self._collapse_all)
        bar.append(self.collapse_btn)
        self.search = Gtk.SearchEntry(placeholder_text="Filter by name, command or PID",
                                      hexpand=True,
                                      tooltip_text="Filter by name, command or PID")
        self.search.connect("search-changed", lambda *_: self._rebuild(force=True))
        bar.append(self.search)
        self.details_btn = Gtk.Button(label="Details", sensitive=False,
                                      tooltip_text="Inspect the selected process")
        a11y_name(self.details_btn, "Process details")
        self.details_btn.connect("clicked", lambda *_: self.open_inspector())
        bar.append(self.details_btn)
        outer.append(bar)
        self.end_btn = Gtk.Button(label="Stop", sensitive=False, valign=Gtk.Align.CENTER,
                                  tooltip_text="Ask the selected app or process to quit (SIGTERM)")
        self.end_btn.connect("clicked", lambda *_: self._end_task(signal.SIGTERM))
        self.kill_btn = Gtk.Button(label="Force Stop", sensitive=False, valign=Gtk.Align.CENTER,
                                   tooltip_text="Kill the selected app or process (SIGKILL)")
        self.kill_btn.add_css_class("destructive-action")
        self.kill_btn.connect("clicked", lambda *_: self._end_task(signal.SIGKILL))
        sum_row = Gtk.Box(spacing=8)
        self.summary = label("", ("section-caption", "numeric"), hexpand=True, wrap=True,
                             wrap_mode=Pango.WrapMode.WORD_CHAR)
        sum_row.append(self.summary)
        sum_row.append(self.end_btn)  # a second row: the filter keeps its width when narrow
        sum_row.append(self.kill_btn)
        outer.append(sum_row)

        self.sections = {}
        for key, title, tip in (
                (SECTION_APPS, "Apps (0)", "Applications with the processes they started"),
                (SECTION_PROCS, "Processes (0)", "Every running process, one per line")):
            r = Row(key, "section")
            r.name, r.cmd = title, tip
            r.children = Gio.ListStore(item_type=Row)
            self.sections[key] = r
        self.root_store = Gio.ListStore(item_type=Row)
        self.tree = Gtk.TreeListModel.new(self.root_store, False, False, self._children)
        self.sel = Gtk.SingleSelection(model=self.tree, autoselect=False, can_unselect=True)
        self.sel.connect("notify::selected", lambda *_: self._selection_changed())
        cv = Gtk.ColumnView(model=self.sel, show_row_separators=False, reorderable=False)
        cv.add_css_class("proc-table")
        self.cv = cv
        self.cols = {}
        for key, title, chars, tip in COLUMNS:
            fac = Gtk.SignalListItemFactory()
            fac.connect("setup", self._setup, key, chars)
            fac.connect("bind", self._bind, key)
            fac.connect("unbind", self._unbind, key)
            col = Gtk.ColumnViewColumn(title=title, factory=fac, resizable=True)
            col.set_sorter(Gtk.CustomSorter.new(lambda *_: 0, None))
            if key == "name":
                col.set_expand(True)
            self.cols[key] = col
            cv.append_column(col)
        cv.sort_by_column(self.cols["cpu"], Gtk.SortType.DESCENDING)
        sw = Gtk.ScrolledWindow(child=cv, vexpand=True)
        sw.add_css_class("proc-frame")
        outer.append(sw)
        self.note = label("CPU is a share of all logical CPUs. Disk I/O is only readable for "
                          "your own processes. Network use per process needs root (eBPF) "
                          "and is not shown.", ("monitor-note",), wrap=True)
        outer.append(self.note)
        self.set_child(outer)

        self.rows: dict = {}  # apps section: group rows by app id, their children by (pid, start)
        self.flat: dict = {}  # processes section: separate Row instances, reused per pass
        self.apps: dict[str, tuple[str, object] | None] = {}  # app id -> (name, gicon)
        self._dindex: dict | None = None
        self._quit_timer = 0
        self._aligned = False
        self._gen = -1
        self._order: list = []
        self._sec_kids = {SECTION_APPS: [], SECTION_PROCS: []}
        # Apps is the landing view. Expanding the full list realizes its GTK rows.
        self._expanded = {SECTION_APPS: True, SECTION_PROCS: False}
        self._shown = False
        self._rebuilding = False
        self._insp = None
        self._insp_token = 0
        self._insp_timer = 0
        self._insp_pending: int | None = None
        self._insp_target: tuple[int, int] | None = None
        cv.get_sorter().connect("changed", lambda *_: self._rebuild(force=True))

    def fit_columns(self, level: int) -> None:
        """Fewer columns as the window narrows (Drive, Swap and GPU memory up to
        900 sp; PID and GPU too up to 460 sp) so the table never needs a horizontal
        scrollbar."""
        narrow = level >= 3
        self.collapse_btn.set_label("Collapse" if narrow else "Collapse All")
        self.search.set_placeholder_text("Filter processes" if narrow
                                         else "Filter by name, command or PID")
        if narrow:
            self.details_btn.set_icon_name("dialog-information-symbolic")
        else:
            self.details_btn.set_label("Details")
        hide = ({"disk", "vram", "swap"} if level >= 1 else set()) | (
            {"pid", "gpu"} if narrow else set())
        for key, col in self.cols.items():
            col.set_visible(key not in hide)

    def sort_by(self, key: str) -> None:
        """Sort the table by one column, descending ("Open Processes" on the Memory
        page sorts by Memory). The sorter's changed signal rebuilds too; the explicit
        rebuild covers a no-op sort_by_column."""
        self.cv.sort_by_column(self.cols[key], Gtk.SortType.DESCENDING)
        self._rebuild(force=True)

    # -- visibility --------------------------------------------------------------------
    def on_shown(self) -> None:
        self._shown = True
        self.hub.want_processes(True)
        self._insp_resume()
        super().on_shown()

    def _insp_resume(self) -> None:
        """Re-arm the inspector loop after a page-hide (the dialog itself stays up)."""
        if self._insp is not None and not self._insp.gone and not self._insp_timer:
            self._insp_timer = GLib.timeout_add(INSPECT_MS, self._insp_tick)

    def on_hidden(self) -> None:
        """Release everything: the walk stops and the rows are dropped."""
        if self._quit_timer:
            GLib.source_remove(self._quit_timer)
            self._quit_timer = 0
        if self._insp is not None:  # freeze the loop; the dialog itself stays up
            self._insp_token += 1
            self._insp_stop_timer()
        self._shown = False
        self.hub.want_processes(False)
        self.root_store.remove_all()
        for sec in self.sections.values():
            sec.children.remove_all()
        self._sec_kids = {SECTION_APPS: [], SECTION_PROCS: []}
        self.rows.clear()
        self.flat.clear()
        self._order = []
        self._gen = -1

    # -- factories -----------------------------------------------------------------------
    def _setup(self, _f, item, key, chars):
        if key == "name":
            exp = Gtk.TreeExpander()
            box = Gtk.Box(spacing=8)
            img = Gtk.Image(pixel_size=16)
            lab = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            box.append(img)
            box.append(lab)
            exp.set_child(box)
            exp.img, exp.lab = img, lab
            item.set_child(exp)
        else:
            lab = Gtk.Label(xalign=1.0, width_chars=chars)
            lab.add_css_class("numeric")
            item.set_child(lab)

    def _bind(self, _f, item, key):
        tree_row = item.get_item()
        row = tree_row.get_item()
        w = item.get_child()
        if key == "name":
            w.set_list_row(tree_row)
            if row.icon is not None:
                w.img.set_from_gicon(row.icon)
                w.img.set_visible(True)
            else:
                w.img.set_visible(row.kind == "app")
                w.img.set_from_icon_name("application-x-executable-symbolic")
            w.lab.set_label(row.text("name"))
            w.set_tooltip_text(row.cmd or row.text("name"))
            row.cells["name"] = w.lab
        else:
            w.set_label(row.text(key))
            row.cells[key] = w

    def _unbind(self, _f, item, key):
        tree_row = item.get_item()
        row = tree_row.get_item() if tree_row is not None else None
        if row is not None:
            row.cells.pop(key, None)

    @staticmethod
    def _children(item):
        return item.children if item.kind in ("app", "section") else None

    # -- data ---------------------------------------------------------------------------
    def _app_info(self, app_id: str, launched: bool = True):
        """(name, icon) from the .desktop file (app_display_info with this page's
        memo and desktop index)."""
        return app_display_info(app_id, launched, self.apps, self._desktop_index())

    def _desktop_index(self) -> dict:
        if self._dindex is None:
            self._dindex = installed_desktop_index()
        return self._dindex

    def update(self) -> None:
        if self.hub.procs_gen != self._gen:
            self._rebuild()

    def _sort(self):
        sorter = self.cv.get_sorter()
        col = sorter.get_primary_sort_column()
        key = next((k for k, c in self.cols.items() if c == col), "cpu")
        desc = sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING
        return SORT_KEYS[key], desc

    def _proc_row(self, p, cache: dict) -> Row:
        """One proc row out of the given store's reuse dict: the apps section keeps its
        own instances in self.rows, the flat section separate ones in self.flat (a Row
        caches its bound widgets, so one instance can never serve two stores)."""
        key = (p.pid, p.start)
        r = cache.get(key)
        if r is None:
            r = cache[key] = Row(key, "proc")
            r.pid, r.start, r.uid = p.pid, p.start, p.uid
            r.name = f"[{p.name}]" if p.kthread else p.name
            r.cmd = p.cmd
        r.cpu, r.mem, r.io_r, r.io_w = p.cpu, p.mem, p.io_read, p.io_write
        r.swap = p.swap
        r.gpu, r.vram = p.gpu, p.vram
        r.procs = [p]
        return r

    def _rebuild(self, force: bool = False) -> None:
        self._gen = self.hub.procs_gen
        procs = self.hub.procs
        q = self.search.get_text().strip().lower()
        keyf, desc = self._sort()
        shown = [p for p in procs if not q or q in p.name.lower() or q in p.cmd.lower()
                 or q == str(p.pid)]
        # -- apps section: one group per application, the System pseudo-group last
        groups = group_apps(procs, os.getpid(), OWN_APP_ID)
        app_rows: list[Row] = []
        assigned: set[int] = set()
        live: set = set()
        for app, members in groups.items():
            info = self._app_info(app, any((m.unit or "").startswith("app-")
                                           for m in members))
            if info is None:
                continue
            name = info[0]
            if q and q not in name.lower() and q not in app.lower() and not any(
                    q in m.name.lower() or q == str(m.pid) for m in members):
                continue
            r = self.rows.get(app)
            if r is None:
                r = self.rows[app] = Row(app, "app")
                r.children = Gio.ListStore(item_type=Row)
                r.cmd = app
            r.name, r.icon = name, info[1]
            live.add(app)
            self._fill_group(r, members, keyf, desc, live)
            assigned.update(m.pid for m in members)
            app_rows.append(r)
        app_rows.sort(key=keyf, reverse=desc)
        others = [p for p in procs if p.pid not in assigned and not p.kthread and (
            not q or q in p.name.lower() or q in p.cmd.lower() or q == str(p.pid))]
        if others:  # everything that is not an app, in one group at the bottom
            r = self.rows.get(SYSTEM_KEY)
            if r is None:
                r = self.rows[SYSTEM_KEY] = Row(SYSTEM_KEY, "app")
                r.name, r.cmd = "System processes", "Services and background processes"
                r.children = Gio.ListStore(item_type=Row)
            live.add(SYSTEM_KEY)
            self._fill_group(r, others, keyf, desc, live)
            r.count = len(others)
            app_rows.append(r)
        # -- processes section: every listed process, flat
        flat_rows = [self._proc_row(p, self.flat) for p in shown]
        live_flat = {r.key for r in flat_rows}
        flat_rows.sort(key=keyf, reverse=desc)
        for k in [k for k in self.rows if k not in live]:
            del self.rows[k]
        for k in [k for k in self.flat if k not in live_flat]:
            del self.flat[k]
        n_apps = len(app_rows) - (1 if others else 0)
        self.sections[SECTION_APPS].name = f"Apps ({n_apps})"
        self.sections[SECTION_PROCS].name = f"Processes ({len(shown)})"
        cpu = sum(p.cpu or 0 for p in shown)
        set_text(self.summary, (f"{n_apps} apps · {len(shown)} of {len(procs)} processes · "
                               f"CPU {fmt.pct(cpu, 1)}") if procs else "Reading processes…")
        self._rebuilding = True
        try:
            with self.sel.freeze_notify():
                self._splice([self.sections[SECTION_APPS], self.sections[SECTION_PROCS]],
                             {SECTION_APPS: app_rows, SECTION_PROCS: flat_rows}, force)
        finally:
            self._rebuilding = False
        for cache in (self.sections, self.rows, self.flat):
            for r in cache.values():
                for col, w in r.cells.items():
                    text = r.text(col)
                    if w.get_label() != text:
                        w.set_label(text)
        self._update_titles(shown)

    def _update_titles(self, procs) -> None:
        """Column headers carry the displayed-set sums under the name (totals_for), like
        Mission Center ("CPU / 4%"): CPU, memory, swap, drive, GPU busy, VRAM used."""
        tots = totals_for(procs)
        for key, title, _c, _t in COLUMNS:
            want = f"{title}\n{tots[key]}" if tots.get(key) else title
            col = self.cols[key]
            if col.get_title() != want:
                col.set_title(want)
        if not self._aligned:
            self._aligned = True
            GLib.idle_add(self._align_headers)

    def _align_headers(self) -> bool:
        """Numeric columns right-align their cells; their header text follows."""
        stack = [self.cv]
        while stack:
            w = stack.pop()
            if isinstance(w, Gtk.Label) and any(
                    a.__gtype__.name == "GtkColumnViewTitle" for a in _ancestors(w)):
                title = w.get_text().split("\n")[0]
                _attach_header_tooltip(w)
                if title != "Name":
                    w.set_xalign(1.0)
                    w.set_justify(Gtk.Justification.RIGHT)
                    w.set_hexpand(True)
            c = w.get_first_child()
            while c is not None:
                stack.append(c)
                c = c.get_next_sibling()
        return GLib.SOURCE_REMOVE

    def _fill_group(self, r: Row, members, keyf, desc, live) -> int:
        """Sum a group's member processes into its row; returns how many there are."""
        kids = [self._proc_row(m, self.rows) for m in members]
        for k in kids:
            live.add(k.key)
        kids.sort(key=keyf, reverse=desc)
        r.count = len(kids)
        r.cpu = sum(k.cpu or 0 for k in kids)
        r.mem = sum(k.mem for k in kids)
        r.swap = _sum_opt(k.swap for k in kids)
        ios = [k for k in kids if k.io_r is not None]
        r.io_r = sum(k.io_r for k in ios) if ios else None
        r.io_w = sum(k.io_w or 0 for k in ios) if ios else None
        r.gpu = min(100.0, sum(k.gpu or 0 for k in kids)) or None
        r.vram = sum(k.vram or 0 for k in kids)
        r.uid = kids[0].uid if all(k.uid == kids[0].uid for k in kids) else -1
        r.procs = [k.procs[0] for k in kids]
        if [id(k) for k in kids] != [id(k) for k in r.child_rows]:
            r.child_rows = kids
            r.children.splice(0, r.children.get_n_items(), kids)
        return len(kids)

    # -- tree splicing --------------------------------------------------------------------
    def _splice(self, rows: list[Row], kids_map: dict[str, list[Row]],
                force: bool = False) -> None:
        """Refresh the tree: two pinned section rows, their children sorted, expansion
        and the selection kept across rebuilds and searches. Expansion is tracked per
        key (sections and app groups), applied after every splice."""
        selected = self._selected_row()
        for i in range(self.tree.get_n_items()):
            tr = self.tree.get_row(i)
            if tr is None:
                continue
            item = tr.get_item()
            if item is not None and item.kind in ("app", "section"):
                self._expanded[item.key] = tr.get_expanded()
        if [id(r) for r in rows] != self._order:  # sections are pinned: rarely changes
            self.root_store.splice(0, self.root_store.get_n_items(), rows)
            self._order = [id(r) for r in rows]
        for key, kids in kids_map.items():
            if force or [id(k) for k in kids] != [id(k) for k in self._sec_kids[key]]:
                self._sec_kids[key] = kids
                store = self.sections[key].children
                store.splice(0, store.get_n_items(), kids)
        i = 0
        while i < self.tree.get_n_items():
            tr = self.tree.get_row(i)
            i += 1
            if tr is None:
                continue
            item = tr.get_item()
            if item is None or item.kind not in ("app", "section"):
                continue
            want = self._expanded.get(item.key)
            if want is not None and tr.get_expanded() != want:
                tr.set_expanded(want)
        if selected is not None:
            hit = None
            for i in range(self.tree.get_n_items()):
                tr = self.tree.get_row(i)
                if tr is None:
                    continue
                item = tr.get_item()
                if item is selected:
                    hit = i
                    break
                if hit is None and item.key == selected.key:
                    hit = i
            self.sel.set_selected(hit if hit is not None else Gtk.INVALID_LIST_POSITION)

    def _collapse_all(self, *_args) -> None:
        """Collapse both sections and every app group — also the ones currently hidden
        inside a collapsed section (self.rows knows them all) — so nothing comes back
        expanded on a rebuild."""
        self._expanded[SECTION_APPS] = False
        self._expanded[SECTION_PROCS] = False
        for key, r in self.rows.items():
            if r.kind == "app":
                self._expanded[key] = False
        i = 0
        while i < self.tree.get_n_items():
            tr = self.tree.get_row(i)
            i += 1
            if tr is None:
                continue
            item = tr.get_item()
            if item is not None and item.kind in ("app", "section") and tr.get_expanded():
                tr.set_expanded(False)

    # -- end task -----------------------------------------------------------------------
    def _selected_row(self) -> Row | None:
        tr = self.sel.get_selected_item()
        return tr.get_item() if tr is not None else None

    def _selection_changed(self) -> None:
        if self._insp is not None and not self._rebuilding:
            dlg = self._insp
            self._insp_closed(dlg)
            dlg.close()
        r = self._selected_row()
        is_proc = r is not None and r.kind == "proc" and r.pid is not None
        self.details_btn.set_sensitive(bool(is_proc))
        mine = r is not None and r.kind in ("app", "proc") and r.uid == os.getuid() \
            and self._refusal(r) is None
        for b in (self.end_btn, self.kill_btn):
            b.set_sensitive(bool(mine))
        why = None
        if r is None or r.kind == "section":
            why = "Select an app or a process to stop it"
        elif not mine:
            why = self._refusal(r) or "Owned by another user: stopping it needs root"
        self.end_btn.set_tooltip_text(why or "Ask the selected app or process to quit (SIGTERM)")
        self.kill_btn.set_tooltip_text(why or "Kill the selected app or process (SIGKILL)")

    def _refusal(self, r: Row) -> str | None:
        own = {os.getpid()}
        for p in r.procs:
            why = signal_refusal(p.pid, own)
            if why:
                return why
        return signal_refusal(r.pid, own)

    def _targets(self, r: Row) -> list[tuple[int, int]]:
        return [(p.pid, p.start) for p in r.procs if p.uid == os.getuid()]

    def _ask_signal(self, what: str, targets, sig) -> None:
        own = {os.getpid()}
        if any(signal_refusal(pid, own) for pid, _start in targets):
            return
        n = len(targets)
        who = f"its {n} processes" if n > 1 else "it"
        if sig == signal.SIGKILL:
            self.win.confirm(f"Force stop {what}?",
                             f"Kills {who} at once (SIGKILL). It gets no chance to save "
                             "anything.", "Force Stop",
                             lambda: self._signal(what, targets, signal.SIGKILL),
                             destructive=True)
            return
        self.win.confirm(f"Stop {what}?", f"Asks {who} to quit (SIGTERM). Unsaved work may "
                         "be lost.", "Stop", lambda: self._signal(what, targets, signal.SIGTERM),
                         destructive=True)

    def _end_task(self, sig=signal.SIGTERM) -> None:
        r = self._selected_row()
        if r is None or r.kind == "section" or self._refusal(r):
            return
        targets = self._targets(r)
        if not targets:
            return
        what = r.name if r.kind == "app" else f"{r.name} (PID {r.pid})"
        self._ask_signal(what, targets, sig)

    def _signal(self, what: str, targets, sig) -> None:
        if self.hub.fake:  # fake PIDs could belong to real processes: never signal them
            self.win.toast(f"Fake hardware: {what} not signalled")
            return
        errors = []
        sent = []
        for pid, start in targets:
            try:
                kill(pid, start, sig)
            except ProcessLookupError:
                pass
            except OSError as exc:
                errors.append(f"PID {pid} ({exc})")
            else:
                sent.append((pid, start))
        if errors:
            self.win.toast(f"Signalled {len(sent)} of {len(targets)} for {what}. "
                           f"Failed: {errors[0]}")
        elif not sent:
            self.win.toast(f"{what} already exited")
        elif sig == signal.SIGKILL:
            self.win.toast(f"{what} killed")
        else:
            self.win.toast(f"Asked {what} to quit")
        if sig == signal.SIGKILL or not sent:
            return

        def check():
            self._quit_timer = 0
            left = [(p, s) for p, s in sent if alive(p, s)]
            if left:
                self.win.confirm(f"{what} is still running",
                                 "It did not quit within 3 seconds. Force it to stop "
                                 "(SIGKILL)? It gets no chance to save anything.",
                                 "Force quit", lambda: self._signal(what, left, signal.SIGKILL),
                                 destructive=True)
            return GLib.SOURCE_REMOVE

        if self._quit_timer:
            GLib.source_remove(self._quit_timer)
        self._quit_timer = GLib.timeout_add(3000, check)

    # -- details inspector ----------------------------------------------------------------
    def open_inspector(self) -> None:
        """Open the Details dialog for the selected process row (QA entry point);
        a safe no-op when the selection is a section, a group or nothing."""
        r = self._selected_row()
        if r is not None and r.kind == "proc" and r.pid is not None:
            self._open_inspector(r)

    def _open_inspector(self, row: Row) -> None:
        if self._insp is not None:
            dlg = self._insp
            self._insp_closed(dlg)
            dlg.close()
        self._insp_token += 1
        self._insp_target = (row.pid, row.start)
        dlg = InspectorDialog(self, row)
        self._insp = dlg
        if _HAS_ADW_DIALOG:
            dlg.present(self.win)
        else:
            dlg.show()
        self._insp_arm()

    def _insp_arm(self) -> None:
        """First snapshot now, then one inspect_process per 2 s while open and shown."""
        self._insp_stop_timer()
        self._insp_fetch()
        if self._insp is not None and self._shown and not self._insp_timer:
            self._insp_timer = GLib.timeout_add(INSPECT_MS, self._insp_tick)

    def _insp_fetch(self) -> None:
        """One inspect_process snapshot for the inspected identity, off-thread.

        The read follows the hub's root — the fake tree in fake mode, where the
        read-only inspection is intended; the hub.fake guard stays on the writes
        (dialog Stop / Force Stop and the priority Apply) and on nothing else.
        """
        if self._insp is None or self._insp_target is None or not self._shown:
            return
        pid, start = self._insp_target
        token = self._insp_token
        if self._insp_pending == token:
            return
        self._insp_pending = token
        run_bg(inspect_process, pid, start, self.hub.root,
               done=lambda d: self._insp_deliver(token, pid, start, d),
               error=lambda _exc: self._insp_deliver(token, pid, start, None))

    def _insp_tick(self) -> bool:
        if self._insp is None or not self._shown:
            self._insp_timer = 0
            return GLib.SOURCE_REMOVE
        self._insp_fetch()
        return GLib.SOURCE_CONTINUE

    def _insp_stop_timer(self) -> None:
        if self._insp_timer:
            GLib.source_remove(self._insp_timer)
            self._insp_timer = 0

    def _insp_deliver(self, token: int, pid: int, start: int, detail) -> None:
        """Delivery guard: token, dialog still open, identity and page still shown."""
        if self._insp_pending == token:
            self._insp_pending = None
        if detail is None or token != self._insp_token or self._insp is None:
            return
        if (pid, start) != self._insp_target or not self._shown:
            return
        self._insp.got_detail(detail)

    def _insp_closed(self, dlg) -> None:
        if self._insp is not dlg:
            return
        self._insp_token += 1
        self._insp_stop_timer()
        self._insp = None
        self._insp_target = None

    def _insp_priority_done(self, token: int, pid: int, start: int, eff: int) -> None:
        if token != self._insp_token or self._insp is None \
                or self._insp_target != (pid, start):
            return
        self._insp.got_priority(eff)

    def _insp_priority_error(self, token: int, pid: int, start: int, exc) -> None:
        if token != self._insp_token or self._insp is None \
                or self._insp_target != (pid, start):
            return
        if isinstance(exc, ProcessLookupError):  # gone between Apply and the readback
            self._insp._mark_gone("exited")
        else:  # EACCES or a nice that did not take effect: one honest toast
            self.win.toast(str(exc) or type(exc).__name__)
