# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""App memory panel: the body of the Memory page's "App memory" disclosure.

Built by the Memory page's _make_app_memory_panel() seam (defer-imported, so this
module can land in parallel with the page). AppMemoryPanel(win) takes a window-
like or page-like context (hub = context.hub; navigation needs the window); the
hub-only form is refused with TypeError so the host's own fallback passes the page.

Two hosts drive the same widget. The page may drive the hub seams itself (owner
"memory-apps", tracker reset on every off->on activation) — the panel then only
renders: it follows hub deliveries while mapped and unsubscribes when hidden, so
a parked page leaves nothing behind. Or the host may drive the panel directly
through set_open / on_page_shown / on_page_hidden: opening resets the tracker,
takes the walk owner and follows deliveries; closing or leaving the page releases
them. Stale deliveries are ignored either way (unsubscribed after close).
"""
from __future__ import annotations

from gi.repository import Gtk, Pango

from ..telemetry import fmt
from ..telemetry.app_memory import APP_MEMORY_OWNER
from .pages.processes import app_display_info, installed_desktop_index
from .widgets import label

ROW_LIMIT = 5          # up to five app groups, by size or growth
OWNER = APP_MEMORY_OWNER
DASH = fmt.DASH


class AppMemoryPanel(Gtk.Box):
    """Up to five recognized app groups ranked by size or growth.

    Rows show the name (with a member count), the summed approximate-private
    memory, and the growth since the panel's first valid sample — "New" for a
    group with no opening-anchored history, a dash without a comparable baseline,
    a partial marker when any member fell back to RSS. Long names ellipsize; the
    caption line carries the baseline age and wraps at narrow widths."""

    def __init__(self, win) -> None:
        """win is a window-like context (win.hub), the host page (win.win) or — via
        the host's own fallback — the hub itself is refused: the panel needs the
        window for "Open Processes" and the hub for deliveries, and a page or the
        window carries both."""
        hub = getattr(win, "hub", None)
        if hub is None:
            raise TypeError("AppMemoryPanel needs a window or page context (win.hub)")
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.hub = hub
        self.win = win if hasattr(win, "show_page") else getattr(win, "win", None)
        self._open = False
        self._page_visible = False
        self._mode = "size"
        self._name_cache: dict = {}
        self._dindex: dict | None = None
        self._last_gen = -1
        self.size_btn = Gtk.ToggleButton(label="Size", active=True)
        self.size_btn.connect("toggled", self._mode_toggled)
        self.growth_btn = Gtk.ToggleButton(label="Growth")
        self.growth_btn.set_group(self.size_btn)
        self.growth_btn.connect("toggled", self._mode_toggled)
        open_btn = Gtk.Button(label="Open Processes",
                              tooltip_text="Open the Processes page sorted by Memory",
                              halign=Gtk.Align.END)
        open_btn.connect("clicked", self._open_processes)
        # the head wraps (FlowBox): one line when wide, the Open Processes button
        # drops below the sort toggle at narrow widths — a fixed one-line row of
        # three buttons cannot fit a 360 px window under theme padding
        head = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                           activate_on_single_click=False, homogeneous=False,
                           max_children_per_line=3, column_spacing=8, row_spacing=6,
                           hexpand=True)
        for name, btn, end in (("sort-size", self.size_btn, False),
                               ("sort-growth", self.growth_btn, False),
                               ("open-processes", open_btn, True)):
            cell = Gtk.FlowBoxChild(name=f"appmem-{name}")
            if end:
                cell.set_hexpand(True)
            cell.set_child(btn)
            head.append(cell)
        self.append(head)
        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.append(self.rows_box)
        self.note = label("", ("section-caption", "numeric"), wrap=True,
                          wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.note.set_visible(False)
        self.append(self.note)
        # deliveries refresh the rows while the panel is on screen; an unmapped
        # panel (page hidden, parked) unsubscribes, so nothing leaks or lags
        self.connect("notify::mapped", self._on_mapped)
        self._on_mapped()

    # -- lifecycle (driven by the host page) ----------------------------------------------
    def set_open(self, open_: bool) -> None:
        """The host disclosure toggled: opening arms the tracker and the walk."""
        open_ = bool(open_)
        if open_ == self._open:
            return
        self._open = open_
        if open_:
            self._activate()
        else:
            self._release()
        self.refresh()

    def on_page_shown(self) -> None:
        """The Memory page is visible again: re-arm an open panel (a park survives
        the page object; the hub's epoch guard makes the re-request idempotent)."""
        self._page_visible = True
        if self._open:
            self._activate()

    def on_page_hidden(self) -> None:
        """The Memory page was left: release the walk, keep the panel's state."""
        self._page_visible = False
        if self._open:
            self._release()

    def _activate(self) -> None:
        hub = self.hub
        tracker = getattr(hub, "app_memory", None)
        if tracker is not None:
            tracker.reset()  # the next valid delivery sets the new baseline
        request = getattr(hub, "request_processes", None)
        if request is not None:
            request(True, OWNER)
        self._subscribe()
        self._last_gen = -1

    def _release(self) -> None:
        self._unsubscribe()
        request = getattr(self.hub, "request_processes", None)
        if request is not None:
            request(False, OWNER)
        tracker = getattr(self.hub, "app_memory", None)
        if tracker is not None and hasattr(tracker, "reset"):
            tracker.reset()  # close / page leave clears the baseline (contract §6.2)
        self._dindex = None
        self._name_cache.clear()

    def _subscribe(self) -> None:
        if hasattr(self.hub, "listeners") and self._on_hub not in self.hub.listeners:
            self.hub.listeners.append(self._on_hub)

    def _unsubscribe(self) -> None:
        if hasattr(self.hub, "listeners") and self._on_hub in self.hub.listeners:
            self.hub.listeners.remove(self._on_hub)

    def _on_mapped(self, *_args) -> None:
        """Visible: follow deliveries; hidden: unsubscribe (a parked page's panel
        never holds a listener, so destroying the page cannot leak one)."""
        if self.get_mapped():
            self._subscribe()
            self._last_gen = -1  # remap: one refresh even without a fresh delivery
            self.refresh()
        else:
            self._unsubscribe()

    def _on_hub(self, hub) -> None:
        if not self._open:  # a delivery landing after close never reaches the panel
            return
        gen = getattr(hub, "procs_gen", -1)
        if gen != self._last_gen:
            self._last_gen = gen
            self.refresh()

    # -- actions -----------------------------------------------------------------------------
    def _mode_toggled(self, btn) -> None:
        if not btn.get_active():
            return
        self._mode = "growth" if btn is self.growth_btn else "size"
        self.refresh()

    def _open_processes(self, *_args) -> None:
        """Navigate to the Processes page, sorted by Memory (Pages builds on access)."""
        show = getattr(self.win, "show_page", None)
        if show is None:
            return
        show("processes")
        self.win.pages["processes"].sort_by("mem")

    # -- rendering ---------------------------------------------------------------------------
    def _resolve(self, app_id: str, launched: bool = True):
        """(name, icon) via the Processes page's shared helper; the page's warmed
        desktop index is reused when that page exists, never scanned again here.
        launched mirrors the page's rule: without a .desktop file only a unit that
        was itself started as an app counts, so unrecognized groups resolve to
        None (and are skipped), never to a synthesized name."""
        page = None
        pages = getattr(self.win, "pages", None)
        if pages is not None and hasattr(pages, "get_built"):
            page = pages.get_built("processes")
        if self._dindex is None:
            self._dindex = (page._desktop_index() if page is not None
                            else installed_desktop_index())
        dindex = self._dindex
        return app_display_info(app_id, launched, self._name_cache, dindex)

    def refresh(self) -> None:
        """Rebuild the rows from the tracker's latest ranking.

        Rows are taken in ranking order until ROW_LIMIT RECOGNIZED groups fill the
        panel: a group that resolves to no display info (no .desktop file and no
        member launched as an app) is skipped, like the page's Apps section."""
        tracker = getattr(self.hub, "app_memory", None)
        rows = []
        if tracker is not None:
            for row in tracker.ranking(self._mode):
                info = self._resolve(row.app_id, row.launched)
                if info is None:
                    continue  # unrecognized group: the Processes page hides it too
                rows.append((row, info))
                if len(rows) >= ROW_LIMIT:
                    break
        child = self.rows_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.rows_box.remove(child)
            child = nxt
        if not rows:
            self.rows_box.append(label(DASH if not self._open else "Reading processes…",
                                       ("section-caption",)))
        for row, info in rows:
            self.rows_box.append(self._row(tracker, row, info))
        if tracker is not None and tracker.coverage == "partial":
            self.note.set_text("Growth tracking limit reached. Some comparisons are unavailable.")
            self.note.set_visible(True)
        elif self._open and rows and self._mode == "growth":
            self.note.set_text("Growth is the change since the first valid sample "
                               "after opening")
            self.note.set_visible(True)
        else:
            self.note.set_visible(False)

    @staticmethod
    def _growth_text(row) -> str:
        if row.growth_bytes is not None:
            text = fmt.bytes_(row.growth_bytes)
            return f"+{text}" if row.growth_bytes >= 0 else text
        return "New" if row.is_new else DASH

    def _row(self, tracker, row, info) -> Gtk.Box:
        name, icon = info
        n = tracker.member_count(row.app_id) if tracker is not None else 0
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        line = Gtk.Box(spacing=8)
        img = Gtk.Image(pixel_size=16)
        if icon is not None:
            img.set_from_gicon(icon)
        else:
            img.set_visible(False)
        line.append(img)
        name_lab = label(f"{name}  ({n})" if n > 1 else str(name), hexpand=True,
                         ellipsize=Pango.EllipsizeMode.END,
                         tooltip_text=f"{name}  ({n})" if n > 1 else str(name))
        line.append(name_lab)
        size_lab = label(fmt.bytes_(row.size_bytes) + ("  ≈" if row.partial else ""),
                         ("numeric",), xalign=1.0)
        size_lab.set_tooltip_text("Approximate private memory; ≈: some members could "
                                  "only be measured as RSS (statm unreadable)"
                                  if row.partial else
                                  "Approximate private memory (statm resident-shared)")
        line.append(size_lab)
        growth_lab = label(self._growth_text(row), ("numeric",), xalign=1.0,
                           width_chars=9)
        growth_lab.set_tooltip_text(self._growth_tip(row))
        line.append(growth_lab)
        box.append(line)
        caption = self._caption(row)
        if caption:
            cap_lab = label(caption, ("section-caption",), wrap=True,
                            wrap_mode=Pango.WrapMode.WORD_CHAR)
            box.append(cap_lab)
        return box

    def _caption(self, row) -> str:
        parts = []
        if self._mode == "growth" and row.baseline_age_s is not None:
            parts.append(f"baseline {fmt.duration(row.baseline_age_s)} ago")
        if row.partial:
            parts.append("partial read (RSS fallback)")
        return " · ".join(parts)

    @staticmethod
    def _growth_tip(row) -> str:
        if row.growth_bytes is not None:
            return "Change since the first valid sample after opening"
        if row.is_new:
            return "New since the panel opened: no opening-anchored baseline yet"
        return "No comparable baseline: the members all changed since the last one"
