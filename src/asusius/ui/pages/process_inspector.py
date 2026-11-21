# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Details dialog for inspecting and controlling one process identity."""
from __future__ import annotations

import os
import shlex
import signal
from typing import TYPE_CHECKING
from gi.repository import Adw, Gdk, Gtk, Pango
from ...telemetry import fmt
from ..viewmodels import signal_refusal
from .base import value_row
from .monitor_base import set_text
from .process_rows import (EXITED_TEXT, CHANGED_TEXT, INFO_ROWS, MEMORY_ROWS,
                           VAL_MAX_CHARS, _VAL_START_ELLIPSIZE)

if TYPE_CHECKING:
    from .processes import ProcessesPage
    from .process_rows import Row

def _fit_val(lab: Gtk.Label, key: str) -> Gtk.Label:
    """A dialog value label never demands its full text as a minimum: it ellipsizes
    (paths at the start, so the leaf unit stays visible) and caps its natural width,
    keeping the whole dialog measurable under its own 460 px (and shrinkable onto a
    360 px bottom sheet). The full value stays on hover via the tooltip."""
    lab.set_ellipsize(Pango.EllipsizeMode.START if key in _VAL_START_ELLIPSIZE
                      else Pango.EllipsizeMode.END)
    lab.set_max_width_chars(VAL_MAX_CHARS)
    return lab

_HAS_ADW_DIALOG = hasattr(Adw, "Dialog")
_DIALOG_BASE = Adw.Dialog if _HAS_ADW_DIALOG else Gtk.Dialog

class InspectorDialog(_DIALOG_BASE):
    """Details for one (pid, start) identity, re-inspected every 2 s while open.

    The page owns the refresh loop and the delivery guard; this dialog only
    renders snapshots, routes Stop / Force Stop back through the page's confirm
    path for the inspected identity, and never renders the argument vector.
    """

    def __init__(self, page: ProcessesPage, row: Row) -> None:
        if _HAS_ADW_DIALOG:
            super().__init__(content_width=460, content_height=640)
        else:  # libadwaita older than 1.4: a plain dialog, transient for the window
            super().__init__(modal=True)
            self.set_transient_for(page.win)
            self.set_default_size(460, 640)
        self.page = page
        self.pid, self.start = row.pid, row.start
        self.detail = None
        self.gone = False  # identity lost: the StatusPage stays, controls stay disabled
        self.set_title(row.name or "Process details")

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        self.stack = Gtk.Stack(vexpand=True,
                               transition_type=Gtk.StackTransitionType.CROSSFADE)
        tv.set_content(self.stack)

        prefs = Adw.PreferencesPage()
        self.main = prefs
        self.stack.add_named(prefs, "main")
        self.status = Adw.StatusPage()
        self.stack.add_named(self.status, "status")

        info = Adw.PreferencesGroup(title="Process")
        self.vals: dict[str, Gtk.Label] = {}
        for key, caption in INFO_ROWS:
            grow, val = value_row(caption)
            info.add(grow)
            self.vals[key] = _fit_val(val, key)
        prefs.add(info)

        mem = Adw.PreferencesGroup(title="Memory")
        for key, caption in MEMORY_ROWS:
            grow, val = value_row(caption)
            mem.add(grow)
            self.vals[key] = _fit_val(val, key)
        prefs.add(mem)

        cmd = Adw.PreferencesGroup(title="Command")
        self.cmd_row = Adw.ActionRow(title="Executable", use_markup=False)
        self.copy_btn = Gtk.Button(label="Copy", sensitive=False, valign=Gtk.Align.CENTER,
                                   tooltip_text="Put the full command line on the clipboard")
        self.copy_btn.connect("clicked", self._copy)
        self.cmd_row.add_suffix(self.copy_btn)
        cmd.add(self.cmd_row)
        self.args_row, args_val = value_row("Arguments", numeric=False)
        self.vals["args"] = _fit_val(args_val, "args")
        cmd.add(self.args_row)
        prefs.add(cmd)

        prio = Adw.PreferencesGroup(
            title="Priority",
            description="Nice value: lower runs sooner. Raising the priority "
                        "(a lower value) may need privileges.")
        self.spin = Adw.SpinRow.new_with_range(-20, 19, 1)
        self.spin.set_title("Nice")
        self.apply_btn = Gtk.Button(label="Apply", sensitive=False, valign=Gtk.Align.CENTER)
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", self._apply_priority)
        self.spin.add_suffix(self.apply_btn)
        prio.add(self.spin)
        prefs.add(prio)

        bottom = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=6, margin_bottom=6,
                         margin_start=10, margin_end=10)
        bottom.add_css_class("toolbar")
        self.stop_btn = Gtk.Button(label="Stop",
                                   tooltip_text="Ask this process to quit (SIGTERM)")
        self.stop_btn.connect("clicked", lambda *_: self._stop(signal.SIGTERM))
        self.force_btn = Gtk.Button(label="Force Stop",
                                    tooltip_text="Kill this process (SIGKILL)")
        self.force_btn.add_css_class("destructive-action")
        self.force_btn.connect("clicked", lambda *_: self._stop(signal.SIGKILL))
        bottom.append(self.stop_btn)
        bottom.append(self.force_btn)
        tv.add_bottom_bar(bottom)

        if _HAS_ADW_DIALOG:
            self.set_child(tv)
            self.connect("closed", lambda *_: page._insp_closed(self))
        else:
            self.get_content_area().append(tv)
            self.connect("close-request", self._fallback_close)

        why = signal_refusal(self.pid, {os.getpid()})
        self._set_stop_actions(False)
        self.stop_btn.set_tooltip_text(why or "Ask this process to quit (SIGTERM)")
        self.force_btn.set_tooltip_text(why or "Kill this process (SIGKILL)")

    # -- snapshot -------------------------------------------------------------------------
    def _set_val(self, key: str, text: str) -> None:
        lab = self.vals[key]
        set_text(lab, text)
        lab.set_tooltip_text(text)  # values ellipsize: the full text stays on hover

    def _mark_gone(self, status: str) -> None:
        """Exited or replaced: replace the content with a StatusPage and freeze.

        Once the identity is lost it stays lost for this dialog: every control
        is disabled and nothing re-enables them, so a snapshot that slips through
        after the fact can never make the dialog act on a dead identity.
        """
        self.gone = True
        self.page._insp_stop_timer()
        self.status.set_title(EXITED_TEXT if status == "exited" else CHANGED_TEXT)
        self.stack.set_visible_child(self.status)
        self._set_stop_actions(False)
        self.apply_btn.set_sensitive(False)
        self.spin.set_sensitive(False)
        self.copy_btn.set_sensitive(False)

    def got_detail(self, d) -> None:
        """Apply one inspect_process snapshot (page-side guards already passed)."""
        if self.gone:
            return  # identity lost: keep the StatusPage and the controls disabled
        previous = self.detail
        self.detail = d
        if d.status != "ok":
            self._mark_gone(d.status)
            return
        self.stack.set_visible_child(self.main)
        for key, text in {
            "name": d.name or d.comm or "–",
            "pid": str(d.pid),
            "ppid": "–" if d.ppid is None else str(d.ppid),
            "user": d.user or "–",
            "state": d.state or "–",
            "threads": "–" if d.threads is None else str(d.threads),
            "uptime": fmt.duration(d.uptime_s),
            "nice": fmt.num(d.nice),
            "unit": d.unit or "–",
            "cgroup": d.cgroup or "–",
            "rss": fmt.bytes_(d.rss),
            "hwm": fmt.bytes_(d.hwm),
            "private": fmt.bytes_(d.private),
            "shared": fmt.bytes_(d.shared),
            "swap": fmt.bytes_(d.swap),
        }.items():
            self._set_val(key, text)
        n = d.args_count
        self._set_val("args", "–" if n is None else "No arguments" if n == 0 else
                      "1 argument" if n == 1 else f"{n} arguments")
        self.cmd_row.set_subtitle(d.argv0 or "–")
        self.copy_btn.set_sensitive(bool(d.args or d.argv0))
        controllable = d.uid == os.getuid() and signal_refusal(self.pid, {os.getpid()}) is None
        if d.nice is None or not controllable:
            self.spin.set_sensitive(False)
            self.apply_btn.set_sensitive(False)
        else:
            self.spin.set_sensitive(True)
            if (previous is None or previous.nice is None
                    or self.spin.get_value() == previous.nice):
                if self.spin.get_value() != d.nice:
                    self.spin.set_value(float(d.nice))
            self.apply_btn.set_sensitive(True)
        self._set_stop_actions(controllable)

    # -- command line ---------------------------------------------------------------------
    @staticmethod
    def _to_clipboard(text: str) -> None:
        disp = Gdk.Display.get_default()
        if disp is not None:
            disp.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))

    def _copy(self, *_args) -> None:
        d = self.detail
        if self.gone or d is None or d.status != "ok":
            return
        line = shlex.join(d.args) if d.args else (d.argv0 or "")
        if not line:
            return
        self._to_clipboard(line)
        self.page.win.toast("Command line copied")

    # -- priority -------------------------------------------------------------------------
    def _apply_priority(self, *_args) -> None:
        d = self.detail
        if (self.gone or d is None or d.status != "ok" or d.nice is None or d.uid != os.getuid()
                or signal_refusal(self.pid, {os.getpid()})):
            return
        page = self.page
        if page.hub.fake:  # fake PIDs could belong to real processes: never touch the OS
            page.win.toast("Fake hardware: priority not changed")
            return
        nice = int(self.spin.get_value())
        token = page._insp_token
        from . import processes  # defer the compatibility facade import until invoked
        processes.run_bg(processes.set_priority, self.pid, self.start, nice,
               done=lambda eff: page._insp_priority_done(token, self.pid, self.start, eff),
               error=lambda exc: page._insp_priority_error(token, self.pid, self.start, exc))

    def got_priority(self, eff: int) -> None:
        self._set_val("nice", fmt.num(eff))
        if self.detail is not None and self.detail.status == "ok":
            self.detail.nice = eff
        self.spin.set_sensitive(True)
        if self.spin.get_value() != eff:
            self.spin.set_value(float(eff))
        self.apply_btn.set_sensitive(True)
        self.page.win.toast(f"Priority set to {eff}")

    # -- signals --------------------------------------------------------------------------
    def _set_stop_actions(self, sensitive: bool) -> None:
        self.stop_btn.set_sensitive(sensitive)
        self.force_btn.set_sensitive(sensitive)

    def _stop(self, sig: int) -> None:
        d = self.detail
        if self.gone or d is None or d.status != "ok" or d.uid != os.getuid():
            return
        what = f"{d.name or d.comm or 'the process'} (PID {self.pid})"
        self.page._ask_signal(what, [(self.pid, self.start)], sig)

    def _fallback_close(self, *_args) -> bool:
        self.page._insp_closed(self)
        self.destroy()
        return True
