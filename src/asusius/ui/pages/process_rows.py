# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Process table columns, aggregation helpers, desktop-app lookup and row model."""
from __future__ import annotations

import os
from gi.repository import Gio, GObject, Gtk
from ...telemetry import fmt
from ..viewmodels import OWN_APP_ID, OWN_APP_KEYS, desktop_index, norm_app_key

COLUMNS = [  # key, title, width chars, tooltip
    ("name", "Name", 0, None),
    ("pid", "PID", 6, None),
    ("cpu", "CPU", 6, "Share of the whole processor (all logical CPUs = 100 %)"),
    ("mem", "Memory", 8, "Private memory: resident minus shared pages (statm)"),
    ("swap", "Swap", 8, "Swapped-out memory of the process (VmSwap in /proc/PID/status)"),
    ("disk", "Drive", 9, "Read + write at the storage layer, per second (your processes only)"),
    ("gpu", "GPU", 5, "Busiest engine on the iGPU (DRM fdinfo) or dGPU"),
    ("vram", "GPU memory", 9, "dGPU memory"),
]
SORT_KEYS = {
    "name": lambda r: r.name.lower(),
    "pid": lambda r: r.pid or 0,
    "cpu": lambda r: r.cpu or 0.0,
    "mem": lambda r: r.mem or 0,
    "swap": lambda r: r.swap or 0,
    "disk": lambda r: (r.io_r or 0) + (r.io_w or 0),
    "gpu": lambda r: r.gpu or 0.0,
    "vram": lambda r: r.vram or 0,
}

INSPECT_MS = 2000  # one inspect_process per 2 s while the Details dialog is open
SECTION_APPS = "__section_apps__"
SECTION_PROCS = "__section_procs__"
SYSTEM_KEY = "__system__"
EXITED_TEXT = "The process has exited"
CHANGED_TEXT = "The PID now belongs to a different process"

# the inspector's (key, caption) rows; keys double as the vals dict used by tests
INFO_ROWS = (("name", "Name"), ("pid", "PID"), ("ppid", "Parent PID"), ("user", "User"),
             ("state", "State"), ("threads", "Threads"), ("uptime", "Uptime"),
             ("nice", "Nice"), ("unit", "Unit"), ("cgroup", "Cgroup"))
MEMORY_ROWS = (("rss", "Resident"), ("hwm", "Peak"), ("private", "Private"),
               ("shared", "Shared"), ("swap", "Swap"))
VAL_MAX_CHARS = 26  # value labels cap their natural width so the dialog stays shrinkable
_VAL_START_ELLIPSIZE = {"cgroup"}  # paths read tail-first: keep the leaf unit visible


def _attach_header_tooltip(header) -> None:
    """Attach the explanatory column text to its visible title label."""
    title = header.get_text().split("\n")[0]
    for _key, column_title, _chars, tip in COLUMNS:
        if column_title == title:
            if tip:
                header.set_tooltip_text(tip)
            return

def totals_for(procs) -> dict[str, str]:
    """Header second lines: sums over the displayed process set.

    One contribution per process — a process shown under an app and flat still
    counts once, because the sums come from the process list, not the rows.
    A column with no readable contributor shows a dash (never a fabricated
    zero); memory is shown even at 0 B, and dash only for an empty set.
    """
    if not procs:
        return {"cpu": fmt.DASH, "mem": fmt.DASH, "swap": fmt.DASH, "disk": fmt.DASH,
                "gpu": fmt.DASH, "vram": fmt.DASH}
    cpus = [p.cpu for p in procs if p.cpu is not None]
    swaps = [p.swap for p in procs if p.swap is not None]
    disks = [p for p in procs if p.io_read is not None or p.io_write is not None]
    gpus = [p.gpu for p in procs if p.gpu is not None]
    vrams = [p.vram for p in procs if p.vram is not None]
    return {
        "cpu": fmt.pct(sum(cpus), 1) if cpus else fmt.DASH,
        "mem": fmt.bytes_(sum(p.mem or 0 for p in procs)),
        "swap": fmt.bytes_(sum(swaps)) if swaps else fmt.DASH,
        "disk": fmt.rate(sum((p.io_read or 0) + (p.io_write or 0) for p in disks))
                if disks else fmt.DASH,
        "gpu": fmt.pct(min(100.0, sum(gpus)), 0) if gpus else fmt.DASH,
        "vram": fmt.bytes_(sum(vrams)) if vrams else fmt.DASH,
    }
def _sum_opt(values) -> int | None:
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


def installed_desktop_index() -> dict:
    """Index installed app entries on the GTK thread when a consumer opens."""
    return desktop_index(
        (info, info.get_id() or "", info.get_startup_wm_class(),
         info.get_string("Exec") or "")
        for info in Gio.AppInfo.get_all()
        if isinstance(info, Gio.DesktopAppInfo))


def app_display_info(app_id: str, launched: bool = True, cache: dict | None = None,
                     dindex: dict | None = None):
    """(name, icon) for an app id — the core of ProcessesPage._app_info, shared with
    the Memory page's app panel so the panel never runs a second desktop scan.

    cache is the caller's memo dict; dindex the warmed desktop index ({} when none
    is available: direct .desktop id lookups still work). Without a .desktop file,
    only a unit that was itself started as an app (app-*.scope/.service) counts, so
    session slices stay out."""
    cache = {} if cache is None else cache
    if app_id in cache:
        found = cache[app_id]
        return found if found is None or found[1] is not None or launched else None
    found = None
    nk = norm_app_key(app_id)
    if nk in OWN_APP_KEYS:  # this app, whatever launcher or scope name started it
        try:
            info = Gio.DesktopAppInfo.new(OWN_APP_ID + ".desktop")
        except TypeError:
            info = None
        found = (info.get_name() if info else "Asusius",
                 info.get_icon() if info else Gio.ThemedIcon.new(OWN_APP_ID))
        cache[app_id] = found
        return found
    for cand in (app_id, app_id.split(".")[-1] if "." in app_id else None,
                 app_id.lower()):
        if not cand:
            continue
        try:
            info = Gio.DesktopAppInfo.new(cand + ".desktop")
        except TypeError:
            info = None
        if info is not None:
            found = (info.get_name(), info.get_icon())
            break
    if found is None:  # by StartupWMClass, executable name or a differently spelled id
        info = (dindex or {}).get(nk)
        if info is not None:
            found = (info.get_name(), info.get_icon())
    for d in ("/etc/xdg/autostart", os.path.expanduser("~/.config/autostart")):
        if found is None and os.path.exists(f"{d}/{app_id}.desktop"):
            info = Gio.DesktopAppInfo.new_from_filename(f"{d}/{app_id}.desktop")
            if info is not None:
                found = (info.get_name(), info.get_icon())
    if found is None and not launched:
        return None
    if found is None:  # launched as an app (systemd scope) but no .desktop file found
        base = app_id.split(".")[-1] if "." in app_id else app_id
        found = (base.replace("-", " ").replace("_", " ").strip().title() or app_id, None)
    cache[app_id] = found
    return found
class Row(GObject.Object):
    __gtype_name__ = "AsusiusProcRow"

    def __init__(self, key, kind: str) -> None:
        super().__init__()
        self.key, self.kind = key, kind
        self.name = ""
        self.icon = None
        self.pid = None
        self.start = None
        self.uid = -1
        self.cmd = ""
        self.cpu = self.gpu = None
        self.mem = self.vram = 0
        self.swap = None
        self.io_r = self.io_w = None
        self.count = 1
        self.children: Gio.ListStore | None = None
        self.child_rows: list[Row] = []
        self.procs: list = []
        self.cells: dict[str, Gtk.Widget] = {}

    def text(self, col: str) -> str:
        if col == "name":
            return self.name + (f"  ({self.count})" if self.kind == "app" and self.count > 1
                                else "")
        if self.kind == "section":
            return ""
        if col == "pid":
            return str(self.pid) if self.pid else ""
        if col == "cpu":
            return fmt.pct(self.cpu, 1) if self.cpu is not None else "–"
        if col == "mem":
            return fmt.bytes_(self.mem) if self.mem else "–"
        if col == "swap":
            return fmt.bytes_(self.swap) if self.swap is not None else "–"
        if col == "disk":
            if self.io_r is None and self.io_w is None:
                return "–"
            return fmt.rate((self.io_r or 0) + (self.io_w or 0))
        if col == "gpu":
            return fmt.pct(self.gpu, 0) if self.gpu else ""
        if col == "vram":
            return fmt.bytes_(self.vram) if self.vram else ""
        return ""
