# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Storage: drive stats, mounted file systems and an interactive disk-usage analyzer.

The analyzer scans one folder off the GTK main thread (ui/tasks.run_bg + the scanner's
threading.Event cancel), paints a treemap/rings canvas (ui/disk_map.py) and keeps a
synchronized, sorted folder list, breadcrumbs and "largest files" chips beside it.
Progress callbacks arrive on the worker thread and reach the widgets only through one
throttled GLib idle; the scan, its idle source and the page's responders are cleaned
up on hide, close and park.
"""
from __future__ import annotations

import os
import stat
import threading
import time

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from ...telemetry import fmt, fake
from ...telemetry.disk_analyzer import HOG_MIN_BYTES, ScanResult, scan_path
from ..charts import Line, Meter, legend
from ..disk_map import MODE_RINGS, MODE_TREEMAP, DiskUsageChart
from ..tasks import run_bg
from ..widgets import label
from .base import a11y_name
from .monitor_base import MonitorPage, Panel, StatGrid, note, set_text

SCAN_HEIGHT_WIDE, SCAN_HEIGHT_NARROW = 280, 230
LIST_WIDE_MIN_W = 330          # desktop: folder list beside the canvas at this width
SHARE_METER_W = 72             # Share % bar width inside a folder row
MENU_W = 34                    # row action menu column width (icon button)
LIST_PAGE_SIZE = 12
PROTECTED_ROOTS = frozenset({
    "/usr", "/boot", "/etc", "/bin", "/sbin", "/lib", "/lib64", "/var",
    "/proc", "/sys", "/dev", "/run",
})

# per-drive StatGrid cells; serial/wwn/scheduler are meta cells that update() hides
# via StatGrid.show() when the drive does not expose them.
_DRIVE_CELLS = (
    ("read", "Read"), ("write", "Write"), ("busy", "Busy"),
    ("riops", "Read IOPS"), ("wiops", "Write IOPS"), ("await", "Avg. resp (ms)"),
    ("temp", "Temperature"), ("size", "Capacity"), ("kind", "Type"),
    ("rt", "Read since boot"), ("wt", "Written since boot"),
)
_META_CAPS = {"serial": "Serial", "wwn": "WWN", "scheduler": "Scheduler"}
_HW_PILLS = (("type", "Type"), ("model", "Model"), ("temp", "Temp"))


class StoragePage(MonitorPage):
    key, title = "storage", "Storage"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.name, self.sub, _ = self.header("Storage", "")
        self.disks: dict[str, tuple] = {}       # disk name -> (panel, StatGrid)
        self._hw: dict[str, dict] = {}          # disk name -> pill value labels
        self._meta: dict[str, dict] = {}        # last per-drive metadata (fit_width)
        self.disk_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.body.append(self.disk_box)
        self._fs_key = None
        self._level = 0
        self._destroyed = False
        self._scan_id = 0
        self._pending_folder = None
        self._visible = False
        self._scanning = False
        self._scan_cancel: threading.Event | None = None
        self._prog: tuple[int, int] | None = None
        self._prog_lock = threading.Lock()
        self._prog_id: int | None = None
        self._result: ScanResult | None = None
        self._view_idx = None
        self._mode = MODE_TREEMAP
        self._apparent = False
        self._folder = ""
        self._list_offset = 0
        self._rows: list[dict] = []
        self._head_cells: dict[str, Gtk.Widget] = {}
        self._row_for_idx: dict[int, Gtk.Box] = {}
        self._sel_row: Gtk.Box | None = None
        self._build_analyzer()
        self.fs = self.panel("File systems", suffix=self._scan_folder_button())
        self.fs_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.fs.append(self.fs_rows)
        self.body.append(note("Rates come from /proc/diskstats (512-byte sectors) over the "
                              "sampling interval; busy is the share of time the device had "
                              "I/O in flight. File systems refresh every 10 s."))
        resp = getattr(self.win, "responsive", None)
        if resp is not None:
            resp(self.fit_width)
        self.connect("destroy", self._on_destroy)

    # -- analyzer build ---------------------------------------------------------------
    def _build_analyzer(self) -> None:
        self.scan_btn = Gtk.Button(label="Scan")
        self.scan_btn.set_tooltip_text("Scan the selected folder")
        self.scan_btn.add_css_class("pill-button")
        self.scan_btn.connect("clicked", self._on_scan_btn)
        self.analyzer = self.panel("Disk usage analyzer", suffix=self.scan_btn)

        self.folder_row = Adw.ActionRow(
            title=GLib.markup_escape_text(self._default_folder()),
            subtitle="Pick any folder to map what is using the space")
        self.folder_row.set_title_lines(1)
        self.folder_row.set_tooltip_text(self._default_folder())
        self.folder_row.set_subtitle_lines(0)
        self.folder_row.add_prefix(Gtk.Image.new_from_icon_name("folder-open-symbolic"))
        browse = Gtk.Button(label="Browse…")
        browse.set_tooltip_text("Choose a folder to analyze")
        browse.add_css_class("flat")
        browse.set_valign(Gtk.Align.CENTER)
        browse.connect("clicked", self._on_browse)
        self.folder_row.add_suffix(browse)
        self.analyzer.append(self.folder_row)

        self.crumbs_row = Gtk.Box(spacing=6)
        self.up_btn = Gtk.Button(icon_name="go-up-symbolic")
        a11y_name(self.up_btn, "Up one level")
        self.up_btn.set_tooltip_text("Up one level (Backspace on the map)")
        self.up_btn.set_valign(Gtk.Align.CENTER)
        self.crumbs_row.append(self.up_btn)
        self.up_btn.connect("clicked", self._go_up)
        self.crumbs = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                                  column_spacing=2, row_spacing=2, max_children_per_line=24)
        self.crumbs.add_css_class("disk-crumbs")
        self.crumbs.set_hexpand(True)
        self.crumbs.set_valign(Gtk.Align.CENTER)
        self.crumbs_row.append(self.crumbs)
        self.analyzer.append(self.crumbs_row)

        self.toggles_row = Gtk.Box(spacing=6)
        self.toggles_row.set_visible(False)
        self.units_disk = Gtk.ToggleButton(label="Disk", active=True)
        self.units_app = Gtk.ToggleButton(label="Apparent", group=self.units_disk)
        self.units_disk.set_tooltip_text("On-disk bytes (sparse files count less)")
        self.units_app.set_tooltip_text("Real byte counts of the file contents")
        self.units_disk.connect("toggled", self._on_units)
        self.units_app.connect("toggled", self._on_units)
        units = Gtk.Box(spacing=0)
        units.add_css_class("linked")
        units.append(self.units_disk)
        units.append(self.units_app)
        mode = Gtk.Box(spacing=0)
        mode.add_css_class("linked")
        self.mode_tree = Gtk.ToggleButton(label="Treemap", active=True)
        self.mode_rings = Gtk.ToggleButton(label="Rings", group=self.mode_tree)
        self.mode_tree.set_tooltip_text("Nested rectangles, area = size")
        self.mode_rings.set_tooltip_text("Sunburst wedges around the folder size")
        self.mode_tree.connect("toggled", self._on_mode)
        self.mode_rings.connect("toggled", self._on_mode)
        mode.append(self.mode_tree)
        mode.append(self.mode_rings)
        self.toggles_row.append(units)
        self.toggles_row.append(mode)
        self.analyzer.append(self.toggles_row)

        # progress strip (hidden when idle)
        self.strip = Gtk.Box(spacing=8)
        self.prog_lab = label("Scanning…", ("stat-sub",), wrap=True,
                              wrap_mode=Pango.WrapMode.WORD_CHAR, hexpand=True)
        self.strip.append(self.prog_lab)
        self.cancel_btn = Gtk.Button(icon_name="process-stop-symbolic")
        a11y_name(self.cancel_btn, "Cancel the scan")
        self.cancel_btn.set_tooltip_text("Cancel the scan")
        self.cancel_btn.set_valign(Gtk.Align.CENTER)
        self.cancel_btn.connect("clicked", lambda *_: self._cancel_scan())
        self.strip.append(self.cancel_btn)
        self.strip.set_visible(False)
        self.analyzer.append(self.strip)
        self.scan_note = note("", "monitor-note")
        self.scan_note.set_visible(False)
        self.analyzer.append(self.scan_note)

        # Canvas + paged list. A single scroll area keeps keyboard navigation in order.
        self.mid = Gtk.Box(spacing=12)
        self.dmap = DiskUsageChart(on_drill=self._on_drill, on_select=self._on_select,
                                   height=SCAN_HEIGHT_WIDE, trash_action=self._chart_trash,
                                   trash_allowed=self._trash_allowed)
        self.mid.append(self.dmap)
        self.list_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.list_wrap.set_size_request(LIST_WIDE_MIN_W, -1)
        self.list_wrap.set_valign(Gtk.Align.START)
        self.list_wrap.append(self._build_list())
        self.list_nav = Gtk.Box(spacing=6)
        for caption, step in (("Previous", -1), ("Next", 1)):
            button = Gtk.Button(label=caption)
            button.connect("clicked", lambda _button, step=step: self._page_list(step))
            self.list_nav.append(button)
        self.list_wrap.append(self.list_nav)
        self.mid.append(self.list_wrap)
        self.analyzer.append(self.mid)

        # largest files chips
        self.hogs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.hogs_box.set_visible(False)
        self.analyzer.append(self.hogs_box)
        self._refresh_analyzer()

    def _build_list(self) -> Gtk.Widget:
        head = Gtk.Box(spacing=8)
        self._head_cells = {
            "folder": label("Folder", ("stat-caption",), wrap=True,
                            wrap_mode=Pango.WrapMode.WORD_CHAR, hexpand=True),
            "share": label("Share", ("stat-caption",)),
            "size": label("Size", ("stat-caption",), xalign=1.0),
            "items": label("Items", ("stat-caption",), xalign=1.0),
            "mod": label("Modified", ("stat-caption",), wrap=True,
                         wrap_mode=Pango.WrapMode.WORD_CHAR),
        }
        for w in self._head_cells.values():
            head.append(w)
        head.append(Gtk.Label())  # spacer matching the row menu column
        head.add_css_class("disk-map-head")
        self._head_box = head
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.list_box.append(head)
        self._list_note = note("Scan a folder to list its folders.", "monitor-note")
        self.list_box.append(self._list_note)
        return self.list_box

    # -- scan lifecycle ----------------------------------------------------------------
    def _default_folder(self) -> str:
        if getattr(self.win, "fake", False):
            root = getattr(self.hub, "root", None)
            if root:
                home = os.path.join(str(root), *fake.FAKE_HOME.split("/"))
                if os.path.isdir(home):
                    return home  # the fixture contract: pages scan <root>/FAKE_HOME
                return str(root)
        mounts = (self.hub.info.get("disk") or {}).get("mounts") or []
        for m in mounts:
            if m.get("mount") == "/":
                return "/"
        if mounts:
            return mounts[0]["mount"]
        return os.path.expanduser("~")

    def _scan_folder_button(self) -> Gtk.Button:

        b = Gtk.Button(label="Scan folder…")
        b.set_tooltip_text("Choose a folder and analyze it")
        b.add_css_class("flat")
        b.set_valign(Gtk.Align.CENTER)
        b.connect("clicked", self._on_browse)
        return b

    def _on_browse(self, *_):
        if hasattr(Gtk, "FileDialog"):
            dlg = Gtk.FileDialog(title="Choose a folder to analyze")
            if self._folder:
                dlg.set_initial_folder(Gio.File.new_for_path(self._folder))
            dlg.select_folder(self.win, None, self._folder_chosen)
            return
        dlg = Gtk.FileChooserNative(title="Choose a folder to analyze", transient_for=self.win,
                                    action=Gtk.FileChooserAction.SELECT_FOLDER)
        if self._folder:
            dlg.set_current_folder(Gio.File.new_for_path(self._folder))
        dlg.connect("response", self._native_folder_chosen)
        dlg.show()

    def _native_folder_chosen(self, dlg, response) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._scan_chosen_folder(dlg.get_file())
        dlg.destroy()

    def _folder_chosen(self, dlg, res, _ud=None) -> None:
        try:
            folder = dlg.select_folder_finish(res)
        except GLib.Error:
            return  # dialog cancelled
        self._scan_chosen_folder(folder)

    def _scan_chosen_folder(self, folder) -> None:
        if self._visible and not self._destroyed and folder is not None:
            self._start_scan(folder.get_path())

    def _on_scan_btn(self, *_):
        if self._scanning:
            self._cancel_scan()
        else:
            self._start_scan(self._folder or self._default_folder())

    def _start_scan(self, folder: str | None) -> None:
        if not folder or self._destroyed:
            return
        if self._scanning:
            self._pending_folder = folder
            self._scan_cancel.set()
            return
        self._folder = folder
        self._update_folder_row()
        self._scan_id += 1
        sid = self._scan_id
        ev = threading.Event()
        self._scan_cancel = ev
        self.scan_note.set_visible(False)
        self._scan_ui(True)
        run_bg(scan_path, folder, ev, progress_cb=self._on_progress,
               done=lambda res: self._scan_done(sid, res),
               error=lambda exc: self._scan_done(sid, None, exc))

    def _cancel_scan(self) -> None:
        self._pending_folder = None
        if self._scan_cancel is not None:
            self._scan_cancel.set()

    def _scan_done(self, sid: int, res: ScanResult | None,
                   exc: BaseException | None = None) -> None:
        if self._destroyed or sid != self._scan_id:
            return
        cancelled = self._scan_cancel.is_set()
        self._scan_cancel = None
        self._scan_ui(False)
        pending, self._pending_folder = self._pending_folder, None
        if not self._visible:
            return
        if pending:
            self._start_scan(pending)
            return
        if cancelled or (res is not None and res.stats.get("cancelled")):
            self.win.toast("Scan cancelled")
            return
        if exc is not None:
            self.win.toast_error(exc)
            return
        if res is None:
            return
        self._result = res
        self.dmap.set_model(res, mode=self._mode, use_apparent=self._apparent)
        self._view_idx = None if res.error else res.root
        self._list_offset = 0
        self._refresh_analyzer()
        if res.error:
            self.win.toast("Scan failed. See the analyzer for details.")
            return
        skipped = res.stats["skipped"]
        mounts = res.stats["mounts_skipped"]
        if skipped or mounts:
            set_text(self.scan_note, f"Skipped {fmt.count(skipped)} unreadable entries and "
                     f"{fmt.count(mounts)} mount points.")
            self.scan_note.set_visible(True)
        self.win.toast(f"Scan finished · {fmt.bytes_(self._metric(res.node(res.root)))}")

    def _scan_ui(self, running: bool) -> None:
        self._scanning = running
        self.scan_btn.set_label("Cancel" if running else "Scan")
        self.scan_btn.set_tooltip_text("Cancel the running scan" if running
                                       else "Scan the selected folder")
        self.strip.set_visible(running)
        if not running:
            with self._prog_lock:
                self._prog = None
            set_text(self.prog_lab, "Scanning…")

    # -- worker-thread progress --------------------------------------------------------
    def _on_progress(self, dirs_done: int, bytes_done: float) -> None:
        """Runs on the WORKER thread: plain state only, one throttled GLib idle."""
        with self._prog_lock:
            self._prog = (int(dirs_done), int(bytes_done))
            if self._prog_id is None and not self._destroyed:
                self._prog_id = GLib.idle_add(self._prog_tick)

    def _prog_tick(self) -> bool:
        with self._prog_lock:
            self._prog_id = None
            progress = self._prog
        if self._destroyed or not self._visible or not self._scanning:
            return GLib.SOURCE_REMOVE
        dirs, byts = progress or (0, 0)
        set_text(self.prog_lab, f"Scanning… {fmt.count(dirs)} dirs · {fmt.bytes_(byts)}")
        return GLib.SOURCE_REMOVE

    # -- analyzer refresh --------------------------------------------------------------
    def _refresh_analyzer(self) -> None:
        res = self._result
        has = res is not None and not res.error
        self.toggles_row.set_visible(has)
        self.crumbs_row.set_visible(has)
        self.hogs_box.set_visible(has)
        self._fill_crumbs()
        self._fill_list()
        self._fill_hogs()

    def _fill_crumbs(self) -> None:
        _clear(self.crumbs)
        res = self._result
        if res is None or res.error:
            return
        chain: list[tuple[int, str]] = []
        i = self._view_idx
        while i is not None and i != res.root:
            n = res.node(i)
            if n is None:
                break
            chain.append((i, n.name))
            i = n.parent_idx
        chain.append((res.root, _root_label(res.root_path)))
        chain.reverse()
        for idx, name in chain:
            if idx == self._view_idx:
                self.crumbs.append(_crumb_cur(name))
                continue
            crumb = label(name)
            crumb.set_ellipsize(Pango.EllipsizeMode.END)
            crumb.set_max_width_chars(18)
            btn = Gtk.Button(child=crumb)
            btn.add_css_class("flat")
            btn.add_css_class("disk-crumb")
            btn.set_tooltip_text(res.path(idx))
            btn.connect("clicked", lambda _b, i=idx: self.dmap.drill(i))
            self.crumbs.append(btn)

    def _page_list(self, step: int) -> None:
        self._list_offset += step * LIST_PAGE_SIZE
        self._fill_list()

    def _fill_list(self) -> None:
        self.list_nav.set_visible(False)
        child = self.list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            if child is not self._head_box and child is not self._list_note:
                self.list_box.remove(child)
            child = nxt
        self._rows.clear()
        self._row_for_idx.clear()
        self._sel_row = None
        res = self._result
        if res is None or res.error or self._view_idx is None:
            set_text(self._list_note, "Scan a folder to list its folders.")
            self._list_note.set_visible(True)
            return
        self._list_note.set_visible(False)
        view = res.node(self._view_idx)
        if view is None:
            self._list_note.set_visible(True)
            return
        total = max(1, self._metric(view))
        kids = list(res.children(self._view_idx))
        kids.sort(key=lambda c: (-self._metric(res.node(c)), res.node(c).name))
        start = self._list_offset
        end = min(start + LIST_PAGE_SIZE, len(kids))
        if len(kids) > LIST_PAGE_SIZE:
            self.list_nav.set_visible(True)
            self.list_nav.get_first_child().set_sensitive(start > 0)
            self.list_nav.get_last_child().set_sensitive(end < len(kids))
            set_text(self._list_note, f"Folders {start + 1}–{end} of {len(kids)}")
            self._list_note.set_visible(True)
        for c in kids[start:end]:
            n = res.node(c)
            path = res.path(c)
            row = Gtk.Box(spacing=8, margin_top=2, margin_bottom=2)
            row.add_css_class("disk-map-row")
            name_lab = label(n.name, (), wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                             hexpand=True, xalign=0.0)
            meter = Meter(height=8)
            meter.set_content_width(SHARE_METER_W)
            meter.set_value(self._metric(n) / total if total else 0.0, "primary")
            size_lab = label(fmt.bytes_(self._metric(n)), ("numeric", "stat-sub"),
                             xalign=1.0)
            items_lab = label(fmt.count(n.file_count), ("numeric", "stat-sub"), xalign=1.0)
            mod_lab = label(_date(n.mtime), ("stat-sub",), xalign=1.0)
            menu = self._row_menu(c, n, path)
            row.append(name_lab)
            row.append(meter)
            row.append(size_lab)
            row.append(items_lab)
            row.append(mod_lab)
            row.append(menu)
            self.list_box.append(row)
            self._row_for_idx[c] = row
            self._rows.append({"idx": c, "row": row, "meter": meter,
                               "size": size_lab, "items": items_lab,
                               "mod": mod_lab})
        self._apply_row_levels()

    def _row_menu(self, c: int, n, path: str) -> Gtk.MenuButton:
        name = n.name
        menu = Gtk.MenuButton(icon_name="view-more-symbolic", focusable=True)
        a11y_name(menu, f"Actions for {name}")
        menu.set_tooltip_text(f"Actions for {name}")
        menu.set_valign(Gtk.Align.CENTER)
        pop = Gtk.Popover(autohide=True, has_arrow=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=4, margin_bottom=4)
        for text, cb, sensitive, tip in self._row_actions(c, path):
            mb = Gtk.Button(label=text)  # Gtk.ModelButton is gone in GTK 4.14
            mb.add_css_class("flat")
            mb.set_sensitive(sensitive)
            if tip:
                mb.set_tooltip_text(tip)
            mb.connect("clicked", lambda _b, cb=cb: cb())
            box.append(mb)
        pop.set_child(box)
        menu.set_popover(pop)
        menu.set_size_request(MENU_W, -1)
        return menu

    def _row_actions(self, c: int, path: str) -> list[tuple]:
        model = self._result
        allowed = self._trash_allowed(model, c)
        return [
            ("Analyze", lambda: self.dmap.drill(c), True, None),
            ("Open in Files", lambda: self._open_files(path), True, None),
            ("Copy Path", lambda: self._copy_path(path), True, None),
            ("Move to Trash", lambda: self._chart_trash(model, c),
             allowed, None if allowed else "Trash is unavailable for this folder."),
        ]

    def _fill_hogs(self) -> None:
        _clear(self.hogs_box)
        res = self._result
        if res is None or res.error:
            self.hogs_box.set_visible(False)
            return
        hogs = res.top_hogs(12, use_apparent=self._apparent)
        big = [h for h in hogs if h[1] >= HOG_MIN_BYTES] or hogs[:6]
        if not big:
            self.hogs_box.set_visible(False)
            return
        cap = label("Largest files", ("stat-caption",))
        cap.set_wrap(True)
        cap.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.hogs_box.append(cap)
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                           column_spacing=6, row_spacing=6, max_children_per_line=6)
        flow.add_css_class("disk-hogs")
        flow.set_valign(Gtk.Align.CENTER)
        for path, size, btag in big[:6]:
            chip = self._hog_chip(path, size, btag)
            flow.append(chip)
            chip.get_parent().set_focusable(False)
        self.hogs_box.append(flow)
        self.hogs_box.set_visible(True)

    def _hog_chip(self, path: str, size: int, btag: str) -> Gtk.Button:
        from ...telemetry.disk_analyzer import reclaimable_tag
        tag = reclaimable_tag(path) or btag
        name = os.path.basename(path) or path
        chip = Gtk.Button()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_start=4, margin_end=4, margin_top=2, margin_bottom=2)
        tag_lab = label(tag, ("disk-hog-tag",), xalign=0.5)
        name_lab = label(name, (), wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        name_lab.set_max_width_chars(16)
        name_lab.set_ellipsize(Pango.EllipsizeMode.END)
        size_lab = label(fmt.bytes_(size), ("numeric", "stat-sub"), xalign=1.0)
        box.append(tag_lab)
        box.append(name_lab)
        box.append(size_lab)
        chip.set_child(box)
        chip.set_tooltip_text(path)
        a11y_name(chip, f"{name} · {fmt.bytes_(size)}")
        chip.connect("clicked", lambda _b: self._open_files(os.path.dirname(path)))
        return chip

    def _trash_allowed(self, model: ScanResult | None, idx: int) -> bool:
        return (model is not None and idx != model.root and not self._fake()
                and not self._protected(model.path(idx)))

    def _chart_trash(self, model: ScanResult, idx: int) -> None:
        if self._destroyed or model is not self._result or not self._trash_allowed(model, idx):
            return
        path = model.path(idx)
        identity = model.node(idx).identity
        self.win.confirm("Move to Trash?", f"{path} and its contents will be moved to the Trash.",
                         "Move to Trash", on_yes=lambda: self._trash(model, path, identity),
                         destructive=True)

    def _trash(self, model: ScanResult, path: str, identity: tuple[int, int] | None) -> None:
        if self._destroyed or not self._visible or model is not self._result or self._fake():
            return

        def do() -> bool:
            if self._protected(path) or path != os.path.realpath(path) or os.path.ismount(path):
                raise ValueError("This location cannot be moved to the Trash.")
            st = os.stat(path, follow_symlinks=False)
            if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
                    or (st.st_dev, st.st_ino) != identity):
                raise ValueError("The selected folder changed or is owned by another user.")
            return Gio.File.new_for_path(path).trash(None)

        def done(moved: bool) -> None:
            if self._destroyed or not self._visible:
                return
            if moved:
                self.win.toast("Moved to Trash")
                if model is self._result and not self._scanning:
                    self._start_scan(model.root_path)

        def failed(exc: BaseException) -> None:
            if not self._destroyed and self._visible:
                self.win.toast_error(exc)

        run_bg(do, done=done, error=failed)

    def _open_files(self, path: str) -> None:
        try:
            uri = Gio.File.new_for_path(path).get_uri()
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception:
            pass  # no window handle here; nothing else to do

    def _copy_path(self, path: str) -> None:
        disp = Gdk.Display.get_default()
        if disp is None:
            return
        disp.get_clipboard().set_content(
            Gdk.ContentProvider.new_for_value(str(path)))

    # -- metric/mode toggles -------------------------------------------------------------
    def _on_units(self, btn: Gtk.ToggleButton) -> None:
        if not btn.get_active():
            return
        want = btn is self.units_app
        if want != self._apparent:
            self._apparent = want
            if self._result is not None:
                self.dmap.set_model(self._result, mode=self._mode,
                                    use_apparent=want)
                self._view_idx = self._result.root
                self._list_offset = 0
                self._refresh_analyzer()

    def _on_mode(self, btn: Gtk.ToggleButton) -> None:
        if not btn.get_active():
            return
        want = MODE_RINGS if btn is self.mode_rings else MODE_TREEMAP
        if want != self._mode:
            self._set_mode(want)

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        if self._result is None:
            return
        self.dmap.set_model(self._result, mode=mode, use_apparent=self._apparent)
        self._view_idx = self._result.root
        self._list_offset = 0
        self._refresh_analyzer()

    def _metric(self, node) -> int:
        return node.apparent_size if self._apparent else node.size

    def _on_drill(self, res, idx) -> None:
        self._view_idx = idx
        self._list_offset = 0
        self._fill_crumbs()
        self._fill_list()
        self._fill_hogs()

    def _go_up(self, *_):
        """The Up button: drill to the current view's parent (Backspace on the map)."""
        self.dmap.up()

    def _on_select(self, res, idx) -> None:
        if self._sel_row is not None:
            self._sel_row.remove_css_class("disk-map-row-sel")
            self._sel_row = None
        if idx is None or res is None:
            return
        row = self._row_for_idx.get(idx)
        if row is not None:
            row.add_css_class("disk-map-row-sel")
            self._sel_row = row

    def _update_folder_row(self) -> None:
        self.folder_row.set_title(GLib.markup_escape_text(self._folder))
        self.folder_row.set_tooltip_text(self._folder)

    @staticmethod
    def _protected(path: str) -> bool:
        norm = os.path.normpath(path)
        return norm == "/" or any(norm == root or norm.startswith(root + "/")
                                  for root in PROTECTED_ROOTS)

    def _fake(self) -> bool:
        return bool(getattr(self.win, "fake", False)
                    or os.environ.get("ASUSIUS_FAKE")
                    or os.environ.get("ASUSIUS_FAKE_ROOT"))

    # -- lifecycle ------------------------------------------------------------------------
    def on_shown(self) -> None:
        self._visible = True
        self.hub.sampler.disk.want_mounts = True
        super().on_shown()

    def on_hidden(self) -> None:
        self._visible = False
        self.hub.sampler.disk.want_mounts = False
        self._cancel_scan()

    def _on_destroy(self, _w) -> None:
        self._cancel_scan()
        with self._prog_lock:
            self._destroyed = True
            if self._prog_id is not None:
                GLib.source_remove(self._prog_id)
                self._prog_id = None

    # -- responsive ----------------------------------------------------------------------
    def _apply_row_levels(self) -> None:
        """Column visibility per width level for the head + every current row.

        _fill_list rebuilds rows on every scan/drill, so it calls this too: rows
        built at 360x640 must not show the wide-only Items/Modified columns.
        """
        show_mod = self._level < 2
        show_items = self._level < 3
        self._head_cells["mod"].set_visible(show_mod)
        self._head_cells["items"].set_visible(show_items)
        for r in self._rows:
            r["mod"].set_visible(show_mod)
            r["items"].set_visible(show_items)

    def fit_width(self, level: int) -> None:
        """Width class from win.responsive: 0 wide, 1 <=900, 2 <=640, 3 <=460 sp."""
        self._level = level
        self._apply_level()

    def _apply_level(self) -> None:
        narrow = self._level >= 2
        self.toggles_row.set_orientation(Gtk.Orientation.VERTICAL if narrow
                                         else Gtk.Orientation.HORIZONTAL)
        self.mid.set_orientation(Gtk.Orientation.VERTICAL if narrow
                                 else Gtk.Orientation.HORIZONTAL)
        self.dmap.set_content_height(SCAN_HEIGHT_NARROW if narrow else SCAN_HEIGHT_WIDE)
        self.list_wrap.set_size_request(LIST_WIDE_MIN_W if not narrow else -1, -1)
        for _n, (_p, g) in self.disks.items():
            g.set_min_children_per_line(2)
            g.set_max_children_per_line(2 if narrow else 4)
        for pills in self._hw.values():
            flow = pills.get("flow")
            if flow is not None:
                flow.set_max_children_per_line(3 if narrow else 6)
        self._apply_row_levels()
        # meta cells: re-show serial/wwn/scheduler per last metadata + level
        for name, meta in self._meta.items():
            g = self.disks.get(name)
            if g is None:
                continue
            _p, g = g
            g.show("wwn", meta.get("wwn") is not None and self._level < 2)
            for k in ("serial", "scheduler"):
                g.show(k, meta.get(k) is not None)

    # -- drive panels ---------------------------------------------------------------------
    def _disk_panel(self, name: str, meta: dict) -> tuple:
        title = f"{name} · {meta.get('model') or meta.get('kind')}"
        p = Panel(title, legend([("read", "primary"), ("write", "gold")]))
        p.append(self.chart([Line(f"disk.{name}.read", "read"),
                             Line(f"disk.{name}.write", "write", "gold", fill=False)],
                            floor=0, min_span=1e7, fmt=fmt.axis_rate, height=150))
        p.append(legend([("busy %", "slate")]))
        p.append(self.chart([Line(f"disk.{name}.busy", "busy", "slate")], y_fixed=(0, 100),
                            fmt=lambda v: f"{v:.0f}%", height=80))
        g = StatGrid(2, 4)
        for key, cap in _DRIVE_CELLS:
            g.add(key, cap)
        for key in _META_CAPS:
            g.add(key, _META_CAPS[key])
            g.show(key, False)
        p.append(g)
        pills = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                            column_spacing=6, row_spacing=4, max_children_per_line=6)
        pills.add_css_class("disk-pills")
        hw = {"flow": pills}
        for key, cap in _HW_PILLS:
            pill = Gtk.Box(spacing=4)
            pill.add_css_class("disk-pill")
            cap_lab = label(cap, ("stat-caption",))
            val_lab = label("–", ("stat-sub",), wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
            pill.append(cap_lab)
            pill.append(val_lab)
            pills.append(pill)
            pill.get_parent().set_focusable(False)
            hw[key] = val_lab
        p.append(pills)
        self._hw[name] = hw
        self.disk_box.append(p)
        return p, g

    # -- telemetry ------------------------------------------------------------------------
    def update(self) -> None:
        st = self.hub.store
        d = self.hub.info.get("disk") or {}
        disks = d.get("disks") or {}
        sens = self.hub.info.get("sensors") or {}
        for name, meta in disks.items():
            if name not in self.disks:
                self.disks[name] = self._disk_panel(name, meta)
            _p, g = self.disks[name]
            g.set("read", fmt.rate(st.last(f"disk.{name}.read")))
            g.set("write", fmt.rate(st.last(f"disk.{name}.write")))
            g.set("busy", fmt.pct(st.last(f"disk.{name}.busy"), 1))
            labels = (sens.get("nvme") or {}).get(name) or []
            temps = [st.last(f"temp.disk.{name}.{i}") for i in range(len(labels))]
            g.set("temp", " · ".join(fmt.temp(t) for t in temps if t is not None) or
                  "no sensor", tooltip=" · ".join(labels) or None)
            g.set("size", fmt.bytes_(meta.get("size")))
            g.set("kind", meta.get("kind") or "–")
            tot = (d.get("totals") or {}).get(name)
            g.set("rt", fmt.bytes_(tot[0]) if tot else "–")
            g.set("wt", fmt.bytes_(tot[1]) if tot else "–")
            g.set("riops", fmt.count(st.last(f"disk.{name}.riops")))
            g.set("wiops", fmt.count(st.last(f"disk.{name}.wiops")))
            g.set("await", fmt.num(st.last(f"disk.{name}.await_ms"), 2))
            g.show("serial", meta.get("serial") is not None)
            g.show("wwn", meta.get("wwn") is not None and self._level < 2)
            g.show("scheduler", meta.get("scheduler") is not None)
            g.set("serial", meta.get("serial") or "–")
            g.set("wwn", meta.get("wwn") or "–")
            g.set("scheduler", meta.get("scheduler") or "–")
            hw = self._hw.get(name)
            if hw is not None:
                set_text(hw["type"], meta.get("kind") or "–")
                set_text(hw["model"], meta.get("model") or "–")
                set_text(hw["temp"], " · ".join(fmt.temp(t) for t in temps
                                                if t is not None) or "–")
            self._meta[name] = dict(meta)
        set_text(self.sub, f"{len(disks)} physical disk{'s' if len(disks) != 1 else ''} · "
                 f"read {fmt.rate(st.last('disk.read'))} · write "
                 f"{fmt.rate(st.last('disk.write'))}")
        mounts = d.get("mounts")
        if mounts is not None:
            key = tuple((m["mount"], m["used"] >> 20) for m in mounts)
            if key != self._fs_key:
                self._fs_key = key
                self._fill_fs(mounts)
        self.redraw()

    def _fill_fs(self, mounts: list[dict]) -> None:
        child = self.fs_rows.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.fs_rows.remove(child)
            child = nxt
        if not mounts:
            self.fs_rows.append(note("No block-device file systems mounted."))
            return
        for m in mounts:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            wrap = {"wrap": True, "wrap_mode": Pango.WrapMode.WORD_CHAR}
            top = Gtk.Box(spacing=10)
            top.append(label(m["mount"], ("fs-mount",), hexpand=True, **wrap))
            frac = m["used"] / m["size"] if m["size"] else 0
            top.append(label(f"{fmt.bytes_(m['used'])} of {fmt.bytes_(m['size'])} · "
                             f"{fmt.bytes_(m['avail'])} free", ("numeric", "stat-sub"),
                             xalign=1.0, **wrap))
            analyze = Gtk.Button(icon_name="system-search-symbolic")
            a11y_name(analyze, f"Analyze space on {m['mount']}")
            analyze.set_tooltip_text(f"Analyze space on {m['mount']}")
            analyze.set_valign(Gtk.Align.CENTER)
            analyze.connect("clicked", lambda _b, path=m["mount"]: self._start_scan(path))
            top.append(analyze)
            row.append(top)
            row.append(label(f"{m['fstype']} · {m['source']}", ("dim-label", "stat-sub"),
                             **wrap))
            meter = Meter(height=8)
            meter.set_hexpand(True)
            meter.set_value(frac, "rose" if frac > 0.9 else "primary")
            row.append(meter)
            self.fs_rows.append(row)


def _clear(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def _date(mtime) -> str:
    if not mtime:
        return "–"
    try:
        return time.strftime("%Y-%m-%d", time.localtime(mtime))
    except (TypeError, ValueError, OverflowError, OSError):
        return "–"


def _root_label(root_path: str) -> str:
    base = os.path.basename(root_path.rstrip("/"))
    return base or "/"


def _crumb_cur(name: str) -> Gtk.Label:
    lab = label(name, ("disk-crumb-cur",))
    lab.set_ellipsize(Pango.EllipsizeMode.END)
    lab.set_max_width_chars(18)
    lab.set_tooltip_text(name)
    return lab
