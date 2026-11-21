# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Memory page: summary, RAM history with a sample cursor, pressure & swap, app memory.

The page owns the inspect-cursor state (view / range / pin / hover); the charts only
draw the cursor the page hands them. History comes from the memory-only ring
(hub.mem_store, ~1 Hz for five minutes) via a getattr seam: before integration (or
without a hub that exposes it) the page renders empty charts and "–" readouts and
never raises.
"""
from __future__ import annotations

import math

from gi.repository import Gdk, Gtk, Pango

from ...telemetry import fmt
from ..charts import Chart, Line, colour, value_at
from ..viewmodels import fmt_temp
from ..widgets import label
from .monitor_base import MonitorPage, Panel, StatGrid, note, set_text

GIB = 1 << 30

DDR_ROOT_TIP = "Speed, type and slot layout need root (DMI)"

MEM_TOTAL_TIP = "MemTotal: RAM the kernel manages"

ZRAM_TIP = ("Compressed RAM block devices and their memory allocation. Swap used and "
            "Swap size include zram devices enabled as swap. zswap is a separate cache.")

COMMIT_TIP = "Committed_AS of CommitLimit — a separate measurement, not a RAM bar"

RANGES = ((60, "1 min"), (120, "2 min"), (300, "5 min"))
DEFAULT_RANGE = 120
RING_CADENCE = 1.0

# colour AND pattern coded (the readout swatches draw the same dash); all six RAM
# series are byte-valued ring keys plotted against a GiB axis, hence scale=1 / GIB
BREAKDOWN_LINES = (
    Line("anon", "Anonymous", "primary", fill=False, scale=1 / GIB),
    Line("file", "File cache", "gold", fill=False, pattern=(6, 3), scale=1 / GIB),
    Line("shmem", "Shared / tmpfs", "sage", fill=False, pattern=(2, 3), scale=1 / GIB),
    Line("slab", "Kernel slab", "slate", fill=False, pattern=(8, 3, 2, 3),
         scale=1 / GIB),
)
USAGE_LINES = (
    Line("in_use", "In use", "primary", scale=1 / GIB),
    Line("available", "Available", "sage", pattern=(5, 3), scale=1 / GIB),
)
BREAKDOWN_NOTE = "File cache covers the file-backed LRU. These counters can overlap."
USAGE_NOTE = "Available is the kernel's estimate of RAM usable without swapping."

CHART_TIP = ("RAM history. Use Left and Right to inspect samples, Enter to pin, "
             "Escape to return to the latest sample. Click or tap also pins.")

PRESSURE_LINES = (
    Line("psi_some", "Some", "primary", fill=False),
    Line("psi_full", "Full", "gold", fill=False, pattern=(3, 3)),
)
SWAP_RATE_LINES = (
    Line("pswpin_bps", "In", "slate", fill=False),
    Line("pswpout_bps", "Out", "gold", fill=False, pattern=(4, 3)),
)


def zram_line(zram_list: list[dict] | None) -> str | None:
    """'On zram' row text: 'zram0: 1.2 GiB stored (compressed to 300 MiB, 320 MiB of RAM)'.

    Devices are ' · '-joined into one row. A field a device did not parse (short or
    malformed mm_stat) is simply omitted; None when nothing parsed at all, so the
    caller never shows a fabricated zero over an absent pool.
    """
    if not zram_list:
        return None
    parts = []
    for d in zram_list:
        name = d.get("device") or "zram"
        if d.get("orig") is None:
            parts.append(f"{name}: –")
            continue
        text = f"{name}: {fmt.bytes_(d['orig'])} stored"
        detail = []
        if d.get("compr") is not None:
            detail.append(f"compressed to {fmt.bytes_(d['compr'])}")
        if d.get("total") is not None:
            detail.append(f"{fmt.bytes_(d['total'])} of RAM")
        if detail:
            text += f" ({', '.join(detail)})"
        parts.append(text)
    return " · ".join(parts)


def ddr_text(modules: list[dict] | None) -> str | None:
    """DDR module summary for the Total tooltip: '2 modules · DDR4 · 3200 MT/s — slots'.

    Unknown fields are left out (never guessed); None when there is nothing to show.
    """
    if not modules:
        return None
    kinds = sorted({m.get("type") for m in modules if m.get("type")})
    speeds = sorted({m.get("speed_mts") for m in modules if m.get("speed_mts")})
    text = f"{len(modules)} module{'s' if len(modules) != 1 else ''}"
    if kinds:
        text += " · " + "/".join(kinds)
    if speeds:
        text += (f" · {speeds[0]} MT/s" if len(speeds) == 1
                 else f" · {speeds[0]}–{speeds[-1]} MT/s")
    slots = [m.get("slot") for m in modules if m.get("slot")]
    if slots:
        text += " — " + ", ".join(slots)
    return text


def _swatch(color_name: str, pattern: tuple[float, ...] | None) -> Gtk.DrawingArea:
    """A small line swatch showing a series' colour and dash (legend/readout rows)."""
    sw = Gtk.DrawingArea(content_width=16, content_height=10, valign=Gtk.Align.CENTER)

    def draw(_a, cr, w, h):
        cr.set_source_rgb(*colour(color_name))
        cr.set_line_width(2)
        cr.move_to(0, round(h / 2) + 0.5)
        cr.line_to(w, round(h / 2) + 0.5)
        if pattern:
            cr.set_dash(list(pattern))
        cr.stroke()

    sw.set_draw_func(draw)
    return sw


def _seg_flow(max_children: int) -> Gtk.FlowBox:
    """A wrapping button row: at narrow widths the buttons flow onto extra rows
    instead of demanding one fixed-width line."""
    flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                       max_children_per_line=max_children, column_spacing=4,
                       row_spacing=4, valign=Gtk.Align.CENTER)
    return flow


def _readout_row(color_name: str, pattern: tuple[float, ...] | None,
                 name: str) -> tuple[Gtk.Box, Gtk.Label]:
    """One legend/readout line: swatch + name (wraps) + tabular value on the right.

    The value never wraps and is never ellipsized (a numeric reading is essential and
    its single-line height keeps the row stable when the text changes per sample)."""
    row = Gtk.Box(spacing=8)
    row.append(_swatch(color_name, pattern))
    row.append(label(name, hexpand=True, wrap=True,
                     wrap_mode=Pango.WrapMode.WORD_CHAR))
    val = label("–", ("numeric", "stat-cell-value"), xalign=1.0)
    row.append(val)
    return row, val


def _summary_card(caption: str, tip: str | None = None) -> tuple[Gtk.Box, Gtk.Label,
                                                                 Gtk.Label]:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    box.add_css_class("stat-cell")
    wrap = {"wrap": True, "wrap_mode": Pango.WrapMode.WORD_CHAR}
    box.append(label(caption, ("stat-caption",), **wrap))
    val = label("–", ("stat-cell-value",), **wrap)
    box.append(val)
    sub = label("", ("stat-sub",), **wrap)
    box.append(sub)
    if tip:
        box.set_tooltip_text(tip)
    return box, val, sub


class MemoryPage(MonitorPage):
    key, title = "memory", "Memory"

    def __init__(self, win) -> None:
        super().__init__(win)
        # page-owned cursor state (the charts only draw what is handed to them)
        self.view = "breakdown"
        self.range_s = DEFAULT_RANGE
        self.pin_ts: float | None = None
        self.hover_ts: float | None = None
        self._sel = {line.key for line in BREAKDOWN_LINES}
        self._seg_guard = False
        self._app_open = False
        self._fmt_in_use = fmt.sticky_bytes()
        self._fmt_avail = fmt.sticky_bytes()
        self._fmt_free = fmt.sticky_bytes()
        self._fmt_swap_used = fmt.sticky_bytes()
        self._fmt_swap_in = fmt.sticky_rate()
        self._fmt_swap_out = fmt.sticky_rate()
        self._charts_store = self._mem_store()
        self.name, self.sub, _ = self.header("Memory", "")

        # 1 summary cards (4-across wide, 2x2 narrow)
        self.summary = Gtk.Grid(column_homogeneous=True, column_spacing=10, row_spacing=10)
        self._summary_columns = 4
        self._summary_cells = []
        self.cards: dict[str, tuple[Gtk.Label, Gtk.Label]] = {}
        for key, cap, tip in (("in_use", "In use", "Total minus available memory"),
                              ("available", "Available",
                               "Usable without swapping (includes reclaimable file cache; "
                               "free is completely unused RAM)"),
                              ("pressure", "Memory pressure",
                               "Share of time some tasks stalled for memory (PSI)"),
                              ("swap", "Swap used", None)):
            card, val, sub = _summary_card(cap, tip)
            self.summary.attach(card, len(self._summary_cells), 0, 1, 1)
            self._summary_cells.append(card)
            self.cards[key] = (val, sub)
        self.body.append(self.summary)

        # 2 + 3 side by side when wide, stacked from level 2 (see fit_header)
        self.duo = Gtk.Box(spacing=14)
        self.body.append(self.duo)
        self._build_ram_card()
        self._build_pressure_card()
        self._mem_charts = [self.ram_chart, self.psi_chart, self.swap_chart]
        self._build_app_panel()
        self._build_details()
        self._rebuild_legend()
        self._refresh_readout()

    # -- builders ---------------------------------------------------------------------

    def _chart(self, lines: list[Line], **kw) -> Chart:
        kw.setdefault("store", self._charts_store)
        kw.setdefault("sample_interval", RING_CADENCE)
        kw.setdefault("tooltip", False)  # the in-card readout strip replaces canvas tips
        kw.setdefault("monitor", True)
        return self.chart(lines, **kw)

    def _build_ram_card(self) -> None:
        p = Panel("RAM history")
        self.view_btns: dict[str, Gtk.ToggleButton] = {}
        self.ram_controls = _seg_flow(5)
        for key, name in (("breakdown", "Breakdown"), ("usage", "Usage")):
            b = Gtk.ToggleButton(label=name)
            b.add_css_class("pill-button")
            b.connect("toggled", self._on_view_toggle, key)
            self.ram_controls.append(b)
            self.view_btns[key] = b
        self.range_btns: dict[int, Gtk.ToggleButton] = {}
        for secs, name in RANGES:
            b = Gtk.ToggleButton(label=name)
            b.add_css_class("pill-button")
            b.connect("toggled", self._on_range_toggle, secs)
            self.ram_controls.append(b)
            self.range_btns[secs] = b
        # the controls get their own full-width wrapping row below the title: sharing
        # the title row with an expanding sibling starves the wrapping controls down
        # to their minimum and makes the head's height depend on the card width
        p.append(self.ram_controls)
        self._seg_guard = True
        self.view_btns["breakdown"].set_active(True)
        self.range_btns[DEFAULT_RANGE].set_active(True)
        self._seg_guard = False
        gib = lambda v: f"{v:g} GiB"  # noqa: E731
        self.ram_chart = self._chart(list(BREAKDOWN_LINES), floor=0, min_span=2,
                                     window_s=self.range_s, fmt=gib, height=185,
                                     dense_grid=True)
        self.ram_chart.set_focusable(True)
        self.ram_chart.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
            ["RAM history", CHART_TIP])
        p.append(self.ram_chart)
        mo = Gtk.EventControllerMotion()
        mo.connect("motion", self._ram_motion)
        mo.connect("leave", self._ram_leave)
        self.ram_chart.add_controller(mo)
        click = Gtk.GestureClick()
        click.connect("released", self._ram_click)
        self.ram_chart.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._ram_key)
        self.ram_chart.add_controller(keys)
        head = Gtk.Box(spacing=8)
        self.sample_label = label("Latest sample", ("heading",), hexpand=True, wrap=True,
                                  wrap_mode=Pango.WrapMode.WORD_CHAR)
        head.append(self.sample_label)
        self.latest_btn = Gtk.Button(label="Latest", valign=Gtk.Align.CENTER)
        self.latest_btn.add_css_class("flat")
        self.latest_btn.connect("clicked", self._go_latest)
        head.append(self.latest_btn)
        p.append(head)
        self.legend_box = Gtk.Grid(column_homogeneous=True, column_spacing=8, row_spacing=6)
        self._legend_cols = 2
        p.append(self.legend_box)
        self.chart_note = note("")
        # a bounded natural width: the note can never stretch the card nor reflow its
        # line count on a width change the panel's height budget did not account for
        self.chart_note.set_max_width_chars(52)
        p.append(self.chart_note)
        set_text(self.chart_note, BREAKDOWN_NOTE)
        self.duo.append(p)
        self.ram_panel = p

    def _build_pressure_card(self) -> None:
        p = Panel("Pressure & swap")
        self.sample_badge = label("Selected sample", ("pill", "dim"),
                                  valign=Gtk.Align.CENTER)
        p.suffix_box.append(self.sample_badge)
        self.pressure_vals: dict[str, Gtk.Label] = {}

        def block(caption: str, tail: str) -> None:
            head = Gtk.Box(spacing=6)
            head.append(label(caption, ("heading",), hexpand=True, wrap=True,
                              wrap_mode=Pango.WrapMode.WORD_CHAR))
            head.append(label(tail, ("caption", "dim-label"), valign=Gtk.Align.CENTER))
            p.append(head)

        block("Memory stalls", "10 s average")
        pct = lambda v: f"{v:g}%"  # noqa: E731
        self.psi_chart = self._chart(list(PRESSURE_LINES), floor=0, min_span=0.5,
                                     fmt=pct, height=54)
        p.append(self.psi_chart)
        for line, key in ((PRESSURE_LINES[0], "some"), (PRESSURE_LINES[1], "full")):
            row, val = _readout_row(line.color, line.pattern,
                                    "Some tasks" if key == "some" else "All non-idle tasks")
            p.append(row)
            self.pressure_vals[key] = val
        self.psi_note = note("")
        self.psi_note.set_visible(False)
        p.append(self.psi_note)
        block("Swap activity", "Amount per second")
        self.swap_chart = self._chart(list(SWAP_RATE_LINES), floor=0, min_span=1024,
                                      fmt=fmt.axis_rate, height=54)
        p.append(self.swap_chart)
        for line, key in ((SWAP_RATE_LINES[0], "in"), (SWAP_RATE_LINES[1], "out")):
            row, val = _readout_row(line.color, line.pattern,
                                    "Swap in" if key == "in" else "Swap out")
            p.append(row)
            self.pressure_vals[key] = val
        swap_row = Gtk.Box(spacing=8)
        swap_row.append(label("Swap used", hexpand=True))
        self.swap_used_val = label("–", ("numeric", "stat-cell-value"), xalign=1.0)
        swap_row.append(self.swap_used_val)
        p.append(swap_row)
        self.swap_used_val.set_tooltip_text(
            "Measured at the selected sample; the summary card shows the latest reading.")
        # compressed memory disclosure: zram devices + zswap, all from info["mem"]
        comp_head = Gtk.Box(spacing=8)
        comp_head.append(label("Compressed memory", ("heading",), hexpand=True, wrap=True,
                               wrap_mode=Pango.WrapMode.WORD_CHAR))
        self.comp_toggle = Gtk.ToggleButton(label="Show", valign=Gtk.Align.CENTER)
        self.comp_toggle.update_property([Gtk.AccessibleProperty.LABEL],
                                         ["Compressed memory details"])
        comp_head.append(self.comp_toggle)
        p.append(comp_head)
        comp_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.zram_val = self._detail_row(comp_body, "On zram", ZRAM_TIP)
        self.zswap_val = self._detail_row(
            comp_body, "zswap", "Compressed cache of swapped pages in RAM")
        self.comp_reveal = Gtk.Revealer(child=comp_body, reveal_child=False,
                                        transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        p.append(self.comp_reveal)
        self.comp_toggle.connect("toggled", self._on_disclose,
                                 self.comp_reveal, self.comp_toggle)
        self.duo.append(p)
        self.pressure_panel = p

    @staticmethod
    def _detail_row(box: Gtk.Box, caption: str,
                    tip: str | None = None) -> Gtk.Label:
        row = Gtk.Box(spacing=8)
        row.append(label(caption, hexpand=True, wrap=True,
                         wrap_mode=Pango.WrapMode.WORD_CHAR))
        val = label("–", ("numeric", "stat-cell-value"), xalign=1.0, wrap=True,
                    wrap_mode=Pango.WrapMode.WORD_CHAR)
        row.append(val)
        box.append(row)
        if tip:
            row.set_tooltip_text(tip)
        return val

    def _build_app_panel(self) -> None:
        """The App memory shell. C's widget is built lazily on first open (a collapsed
        disclosure then adds no minimum width at 360 px)."""
        p = Panel("App memory")
        self.app_toggle = Gtk.ToggleButton(label="Show", valign=Gtk.Align.CENTER)
        self.app_toggle.update_property([Gtk.AccessibleProperty.LABEL],
                                        ["App memory panel"])
        self.app_toggle.connect("toggled", self._on_app_toggle)
        p.suffix_box.append(self.app_toggle)
        self.app_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.app_body.append(note("Private RAM estimates for recognized apps. "
                                  "Growth is measured from when this panel opened."))
        self._app_placeholder = True
        self.app_widget = label("–", ("stat-sub",), wrap=True,
                                wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.app_body.append(self.app_widget)
        self.app_reveal = Gtk.Revealer(child=self.app_body, reveal_child=False,
                                       transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        p.append(self.app_reveal)
        self.body.append(p)
        self.app_panel = p

    def _make_app_memory_panel(self):
        """Seam: defer-import C's AppMemoryPanel; None -> the caller keeps the placeholder.

        The module (ui/app_memory.py) may not exist in a parallel phase; this never
        raises, so the page stays usable without app attribution.
        """
        try:
            from .. import app_memory
        except Exception:
            return None
        make = getattr(app_memory, "AppMemoryPanel", None)
        if make is None:
            return None
        try:
            return make(self.hub)
        except TypeError:
            try:
                return make(self)
            except Exception:
                return None
        except Exception:
            return None

    def _build_details(self) -> None:
        """System memory details: today's technical readings plus the new kernel rows."""
        p = Panel("System memory details")
        self.det_toggle = Gtk.ToggleButton(label="Show", valign=Gtk.Align.CENTER)
        self.det_toggle.update_property([Gtk.AccessibleProperty.LABEL],
                                        ["System memory details"])
        self.details = StatGrid(2, 4)
        for key, cap, tip in (
                ("total", "Total", MEM_TOTAL_TIP),
                ("in_use", "In use", None), ("available", "Available", None),
                ("free", "Free", None),
                ("buff_cache", "Buffers + cache", "Buffers + Cached + SReclaimable, "
                 "as free -b"),
                ("shmem", "Shared (tmpfs, shm)", None),
                ("slab_r", "Slab reclaimable", None),
                ("slab_u", "Slab unreclaimable", None),
                ("dirty", "Dirty · writeback", None),
                ("committed", "Committed VM", COMMIT_TIP),
                ("page_tables", "Page tables", None),
                ("kernel_stack", "Kernel stacks", None),
                ("swap_cached", "Swap cached", None),
                ("dimm", "Module temperature", None),
                ("psi_some60", "Some stalled (60 s)", None),
                ("psi_some300", "Some stalled (300 s)", None),
                ("psi_full60", "All stalled (60 s)", None),
                ("psi_full300", "All stalled (300 s)", None)):
            self.details.add(key, cap, tip)
        self.det_reveal = Gtk.Revealer(child=self.details, reveal_child=False,
                                       transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        p.append(self.det_reveal)
        self.det_toggle.connect("toggled", self._on_disclose,
                                self.det_reveal, self.det_toggle)
        p.suffix_box.append(self.det_toggle)
        self.body.append(p)
        self.det_panel = p

    # -- toggles and legend -------------------------------------------------------------

    def _on_view_toggle(self, btn, key: str) -> None:
        if self._seg_guard:
            return
        if not btn.get_active():  # the active segment was clicked: it stays on
            self._seg_guard = True
            btn.set_active(True)
            self._seg_guard = False
            return
        self._seg_guard = True
        for k, b in self.view_btns.items():
            b.set_active(k == key)
        self._seg_guard = False
        self._set_view(key)

    def _on_range_toggle(self, btn, secs: int) -> None:
        if self._seg_guard:
            return
        if not btn.get_active():
            self._seg_guard = True
            btn.set_active(True)
            self._seg_guard = False
            return
        self._seg_guard = True
        for k, b in self.range_btns.items():
            b.set_active(k == secs)
        self._seg_guard = False
        self._set_range(secs)

    def _set_view(self, view: str) -> None:
        """Breakdown <-> Usage. A pin survives the switch (same timestamp, new lines)."""
        if view == self.view:
            return
        self.view = view
        usage = view == "usage"
        lines = list(USAGE_LINES) if usage else [line for line in BREAKDOWN_LINES
                                                 if line.key in self._sel]
        self.ram_chart.set_lines(lines)
        self.ram_chart.stacked = usage
        # Use the same mean buckets for both parts so they sum to capacity.
        self.ram_chart.use_max = not usage
        total = (self.hub.info.get("mem") or {}).get("total")
        self.ram_chart.y_fixed = (0, total / GIB) if usage and total else None
        set_text(self.chart_note, USAGE_NOTE if usage else BREAKDOWN_NOTE)
        self._rebuild_legend()
        self._refresh_readout()

    def _set_range(self, secs: int) -> None:
        """A range change clears the pin (and hover): back to the live sample."""
        if secs == self.range_s:
            return
        self.range_s = secs
        for c in self._mem_charts:
            c.set_window(secs)
        self.pin_ts = None
        self.hover_ts = None
        self._refresh_readout()

    def _on_legend_toggle(self, btn, key: str) -> None:
        if self._seg_guard:
            return
        on = btn.get_active()
        if not on and key in self._sel and len(self._sel) == 1:
            self._seg_guard = True  # at least one line always stays on
            btn.set_active(True)
            self._seg_guard = False
            return
        if on:
            self._sel.add(key)
        else:
            self._sel.discard(key)
        self.ram_chart.set_lines([line for line in BREAKDOWN_LINES
                                  if line.key in self._sel])

    def _rebuild_legend(self) -> None:
        """Breakdown: one toggle per line (swatch + name + value). Usage: plain rows."""
        child = self.legend_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.legend_box.remove(child)
            child = nxt
        self.readout_vals: dict[str, Gtk.Label] = {}
        lines = BREAKDOWN_LINES if self.view == "breakdown" else USAGE_LINES
        for i, line in enumerate(lines):
            row, val = _readout_row(line.color, line.pattern, line.label or line.key)
            self.readout_vals[line.key] = val
            if self.view == "breakdown":
                self._seg_guard = True
                btn = Gtk.ToggleButton()
                btn.set_active(line.key in self._sel)
                btn.set_child(row)
                self._seg_guard = False
                btn.connect("toggled", self._on_legend_toggle, line.key)
                item = btn
            else:
                item = row
            self.legend_box.attach(item, i % self._legend_cols, i // self._legend_cols, 1, 1)

    @staticmethod
    def _on_disclose(btn, reveal: Gtk.Revealer, toggle) -> None:
        reveal.set_reveal_child(btn.get_active())
        toggle.set_label("Hide" if btn.get_active() else "Show")

    # -- cursor model (selected = pin ?? hover ?? latest) ---------------------------------

    def _mem_store(self):
        """The memory-only ring via the hub seam; None before integration (never raises)."""
        return getattr(self.hub, "mem_store", None)

    def _selected_ts(self, st) -> float | None:
        if st is None or not st.count:
            self.pin_ts = None  # no ring window at all: nothing to pin to
            self.hover_ts = None
            return None
        last = st.last_t
        earliest = max(st.times[st.slot(0)], last - self.range_s)
        if self.pin_ts is not None and earliest <= self.pin_ts <= last:
            return self.pin_ts
        self.pin_ts = None
        if self.hover_ts is not None and earliest <= self.hover_ts <= last:
            return self.hover_ts
        self.hover_ts = None
        return last

    @staticmethod
    def _nearest_ts(st, ts: float | None) -> float | None:
        """Nearest ring sample within one ring tick of `ts`; None otherwise.

        Hover over stale geometry (outside the ring window or across a gap) selects
        nothing, and a click cannot pin a sample the expiry rule would immediately
        clear.
        """
        if st is None or not st.count:
            return None
        if ts is None:
            return st.last_t
        best, best_d = None, math.inf
        for k in st.index_range(ts - RING_CADENCE):
            t = st.times[st.slot(k)]
            if t > ts + RING_CADENCE:
                break
            d = abs(t - ts)
            if d < best_d:
                best, best_d = t, d
        return best

    @staticmethod
    def _step_ts(st, ts: float, delta: int, earliest: float) -> float | None:
        """One ring sample to the left/right of `ts` (None at the ends)."""
        times = [st.times[st.slot(k)] for k in st.index_range(earliest)]
        if not times:
            return None
        i = min(range(len(times)), key=lambda j: abs(times[j] - ts))
        j = min(len(times) - 1, max(0, i + delta))
        return times[j]

    def _ram_motion(self, c, x, _y) -> None:
        if self.pin_ts is not None:
            return  # hover is ignored while pinned
        ev = c.get_current_event() if c is not None else None
        if ev is not None:
            dev = ev.get_device()
            if dev is not None and dev.get_source() == Gdk.InputSource.TOUCHSCREEN:
                return  # touch drags scroll and tap pins (preview parity: no hover)
        st = self._mem_store()
        ts = self.ram_chart.ts_at_x(x)
        snapped = self._nearest_ts(st, ts) if ts is not None else None
        if snapped != self.hover_ts:
            self.hover_ts = snapped
            self._refresh_readout()

    def _ram_leave(self, *_args) -> None:
        if self.hover_ts is not None:
            self.hover_ts = None
            self._refresh_readout()

    def _ram_click(self, _g, _n, x, _y) -> None:
        self.ram_chart.grab_focus()
        st = self._mem_store()
        px_ts = self.ram_chart.ts_at_x(x)
        target = self._nearest_ts(st, px_ts) if px_ts is not None else None
        if target is None:
            return
        self.hover_ts = None
        self.pin_ts = target
        self._refresh_readout()

    def _ram_key(self, _c, keyval, _code, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            if self.pin_ts is None and self.hover_ts is None:
                return False
            self.pin_ts = None
            self.hover_ts = None
            self._refresh_readout()
            return True
        st = self._mem_store()
        cur = self._selected_ts(st)
        if cur is None:
            return keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter,
                              Gdk.KEY_Left, Gdk.KEY_Right)
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.pin_ts = cur
            self.hover_ts = None
            self._refresh_readout()
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            nxt = self._step_ts(st, cur, -1 if keyval == Gdk.KEY_Left else 1,
                                st.last_t - self.range_s)
            if nxt is not None:
                if self.pin_ts is not None:
                    self.pin_ts = nxt
                else:
                    self.hover_ts = nxt
                self._refresh_readout()
            return True
        return False

    def _go_latest(self, *_args) -> None:
        self.pin_ts = None
        self.hover_ts = None
        self._refresh_readout()

    def qa_pin(self, ts: float | None) -> None:
        """QA hook: pin the cursor to a ring timestamp (None returns to latest)."""
        self.pin_ts = None if ts is None else float(ts)
        self._refresh_readout()

    def qa_set_app_panel(self, open: bool) -> None:  # noqa: A002
        """QA hook: open or close the App memory panel (drives the hub seams)."""
        self.app_toggle.set_active(bool(open))

    # -- app memory shell -------------------------------------------------------------------

    def _on_app_toggle(self, btn) -> None:
        if self._seg_guard:
            return
        self._set_app_panel(btn.get_active())

    def _set_app_panel(self, open_: bool) -> None:
        """Open/close the panel; process walks run only while it is open."""
        self._app_open = open_
        self.app_reveal.set_reveal_child(open_)
        self.app_toggle.set_label("Hide" if open_ else "Show")
        if open_ and self._app_placeholder:
            self._build_app_widget()
        self._drive_app(open_)

    def _build_app_widget(self) -> None:
        made = self._make_app_memory_panel()
        if made is not None:
            self.app_body.remove(self.app_widget)
            self.app_widget = made
            self.app_body.append(made)
            self._app_placeholder = False

    def _app_panel_widget(self):
        return None if self._app_placeholder else self.app_widget

    def _drive_app(self, on: bool) -> None:
        """Forward the disclosure state: C's panel drives the tracker and the owner-
        tagged walk when present; without it this shell keeps the hub seams itself so
        pre-integration builds behave identically."""
        panel = self._app_panel_widget()
        if panel is not None and hasattr(panel, "set_open"):
            panel.set_open(on)
            return
        self._request_hub_walks(on)

    def _request_hub_walks(self, on: bool) -> None:
        """Owner-tagged walk request via the hub seam (app_memory reset on activation)."""
        hub = self.hub
        req = getattr(hub, "request_processes", None)
        if req is None:
            return
        if on:
            tracker = getattr(hub, "app_memory", None)
            if tracker is not None and hasattr(tracker, "reset"):
                tracker.reset()  # every off->on transition re-baselines
            req(True, "memory-apps")
        else:
            req(False, "memory-apps")

    # -- readout strip ---------------------------------------------------------------------

    def _refresh_readout(self) -> None:
        """Values at the selected sample (pin ?? hover ?? latest), straight from the ring.

        A missing value is "–" (never a fabricated zero, never interpolated across a
        gap); a measured zero is "0 B". Runs on every sample and on every cursor move.
        """
        st = self._mem_store()
        sel = self._selected_ts(st)
        last = st.last_t if st is not None and st.count else None
        pinned = self.pin_ts is not None
        at_latest = sel is None or last is None or last - sel <= 0.25
        if pinned and at_latest:
            set_text(self.sample_label, "Pinned · latest")
        elif pinned:
            set_text(self.sample_label,
                     f"Pinned · {fmt.duration(max(0.0, last - sel))} ago")
        elif at_latest:
            set_text(self.sample_label, "Latest sample")
        else:
            set_text(self.sample_label, f"{fmt.duration(max(0.0, last - sel))} ago")
        self.latest_btn.set_visible(pinned)
        for line in (USAGE_LINES if self.view == "usage" else BREAKDOWN_LINES):
            lab = self.readout_vals.get(line.key)
            if lab is not None:
                v = value_at(st, line.key, sel, RING_CADENCE)
                set_text(lab, fmt.bytes_(v))
        # pressure & swap card readouts: the same selected timestamp
        some = value_at(st, "psi_some", sel, RING_CADENCE)
        full = value_at(st, "psi_full", sel, RING_CADENCE)
        pin_bps = value_at(st, "pswpin_bps", sel, RING_CADENCE)
        pout_bps = value_at(st, "pswpout_bps", sel, RING_CADENCE)
        swap_used = value_at(st, "swap_used", sel, RING_CADENCE)
        set_text(self.pressure_vals["some"], fmt.pct(some, 2))
        set_text(self.pressure_vals["full"], fmt.pct(full, 2))
        live = at_latest and not pinned
        set_text(self.pressure_vals["in"],
                 self._fmt_swap_in(pin_bps) if live else fmt.rate(pin_bps))
        set_text(self.pressure_vals["out"],
                 self._fmt_swap_out(pout_bps) if live else fmt.rate(pout_bps))
        # consistent with the summary card: feature-off is not a zero reading
        swap_total = value_at(st, "swap_total", sel, RING_CADENCE)
        set_text(self.swap_used_val, "no swap configured" if swap_total == 0
                 else fmt.bytes_(swap_used))
        self.sample_badge.set_visible(sel is not None and not at_latest)
        for c in self._mem_charts:
            c.set_cursor_ts(sel)

    # -- live update -----------------------------------------------------------------------

    def refresh_temperature_unit(self) -> None:
        self.update()

    def update(self) -> None:
        """Per-sample refresh: summary cards, axes, details and the cursor readout.

        Runs at the sampler cadence — everything here is label sets and small ring
        reads; no allocations per drawn point (the charts keep their own buffers).
        """
        hub = self.hub
        info = hub.info
        m = info.get("mem") or {}
        total = m.get("total")
        in_use = m.get("in_use")
        psi = m.get("psi")
        # re-point the charts when the hub's memory ring appears (integration order)
        st = self._mem_store()
        if st is not self._charts_store:
            self._charts_store = st
            for c in self._mem_charts:
                c.store = st
        # header
        err = m.get("error")
        pct_used = fmt.pct(100 * in_use / total) if total and in_use is not None else "–"
        if total:
            set_text(self.sub, f"{fmt.bytes_(total)} usable · {fmt.bytes_(in_use)} in use "
                               f"({pct_used})")
        else:
            set_text(self.sub, (err or "No data yet") if not m else "Total unavailable")
        # 1 summary cards: measured zero / absent / feature-off stay distinct
        card = self.cards
        set_text(card["in_use"][0], self._fmt_in_use(in_use))
        set_text(card["in_use"][1],
                 f"{pct_used} of usable RAM"
                 if total and in_use is not None else "–")
        set_text(card["available"][0], self._fmt_avail(m.get("available")))
        free = m.get("free")
        set_text(card["available"][1],
                 f"{self._fmt_free(free)} free" if free is not None else "–")
        some10 = (psi or {}).get("some", {}).get("avg10") if psi else None
        set_text(card["pressure"][0], fmt.pct(some10, 2))
        sub = "Some tasks · 10 s avg" if some10 is not None else "PSI unavailable"
        set_text(card["pressure"][1], sub)
        swap_total = m.get("swap_capacity_bytes", m.get("swap_total"))
        if swap_total:
            swap_used = m.get("swap_used_bytes", m.get("swap_used"))
            set_text(card["swap"][0], self._fmt_swap_used(swap_used))
            set_text(card["swap"][1], f"of {fmt.bytes_(swap_total)} configured")
        else:
            set_text(card["swap"][0], "no swap configured" if swap_total == 0 else "–")
            set_text(card["swap"][1], "")
        # 2 RAM chart axes: the y-axis total comes from info["mem"], never the ring
        if self.view == "usage":
            self.ram_chart.y_fixed = (0, total / GIB) if total else None
        # 3 pressure & swap card
        self.psi_note.set_visible(psi is None)
        if psi is None:
            set_text(self.psi_note, "Memory stalls are not measured: "
                                    + (m.get("psi_reason") or "not reported."))
        self._compressed(m)
        # 5 details
        self._details(m, psi)
        # readout + cursor (also drives the charts' redraw on sample delivery)
        self._refresh_readout()
        self.redraw()

    def _compressed(self, m: dict) -> None:
        set_text(self.zram_val, zram_line(m.get("zram")) or "–")
        zs = None
        reader = getattr(getattr(self.hub, "sampler", None), "mem", None)
        if reader is not None and hasattr(reader, "zswap_enabled"):
            zs = reader.zswap_enabled()
        if m.get("zswap_pool") is None:
            set_text(self.zswap_val, "not reported")
        elif not zs and not m.get("zswap_stored"):
            set_text(self.zswap_val, "off")
        else:
            set_text(self.zswap_val, f"{fmt.bytes_(m.get('zswap_stored'))} in "
                                     f"{fmt.bytes_(m.get('zswap_pool'))}")

    def _details(self, m: dict, psi: dict | None) -> None:
        g = self.details
        ddr = m.get("ddr")
        if ddr:
            tip = ddr_text(ddr.get("modules")) or MEM_TOTAL_TIP
        else:
            tip = f"{MEM_TOTAL_TIP}. {DDR_ROOT_TIP}"
        g.set("total", fmt.bytes_(m.get("total")), tip)
        for key, src in (("in_use", "in_use"), ("available", "available"),
                         ("free", "free"), ("buff_cache", "buff_cache"),
                         ("shmem", "shmem"), ("slab_r", "slab_reclaimable"),
                         ("slab_u", "slab_unreclaimable"),
                         ("page_tables", "page_tables"),
                         ("kernel_stack", "kernel_stack"),
                         ("swap_cached", "swap_cached")):
            g.set(key, fmt.bytes_(m.get(src)))
        g.set("dirty", f"{fmt.bytes_(m.get('dirty'))} · {fmt.bytes_(m.get('writeback'))}")
        g.set("committed", f"{fmt.bytes_(m.get('committed'))} of "
                           f"{fmt.bytes_(m.get('commit_limit'))}", COMMIT_TIP)
        d = self.hub.store.last("temp.dimm0")
        g.set("dimm", fmt_temp(d) if d is not None else "no sensor")
        some = (psi or {}).get("some") or {}
        full = (psi or {}).get("full") or {}
        g.set("psi_some60", fmt.pct(some.get("avg60"), 2))
        g.set("psi_some300", fmt.pct(some.get("avg300"), 2))
        g.set("psi_full60", fmt.pct(full.get("avg60"), 2))
        g.set("psi_full300", fmt.pct(full.get("avg300"), 2))

    # -- layout and lifecycle ----------------------------------------------------------------

    def fit_header(self, level: int) -> None:
        """Level 0: RAM + pressure cards side by side; level >= 2: stacked."""
        super().fit_header(level)
        self.duo.set_orientation(Gtk.Orientation.VERTICAL if level >= 2
                                 else Gtk.Orientation.HORIZONTAL)
        columns = 2 if level >= 1 else 4
        if columns != self._summary_columns:
            for card in self._summary_cells:
                self.summary.remove(card)
            for i, card in enumerate(self._summary_cells):
                self.summary.attach(card, i % columns, i // columns, 1, 1)
            self._summary_columns = columns
        legend_cols = 1 if level >= 3 else 2
        if legend_cols != self._legend_cols:
            self._legend_cols = legend_cols
            self._rebuild_legend()
            self._refresh_readout()

    def save_state(self) -> dict:
        panel = self._app_panel_widget()
        return {
            "view": self.view,
            "range_s": self.range_s,
            "sel": set(self._sel),
            "comp_open": self.comp_toggle.get_active(),
            "det_open": self.det_toggle.get_active(),
            "app_open": self.app_toggle.get_active(),
            "app_mode": getattr(panel, "_mode", None) if panel is not None else None,
        }

    def restore_state(self, state: dict) -> None:
        if not state:
            return
        sel = state.get("sel")
        valid_sel = {line.key for line in BREAKDOWN_LINES}
        if isinstance(sel, (set, list, tuple)):
            kept = set(sel) & valid_sel
            if kept:
                self._sel = kept
        range_s = state.get("range_s")
        if range_s in self.range_btns:
            self.range_btns[range_s].set_active(True)
        view = state.get("view")
        if view in self.view_btns:
            self.view_btns[view].set_active(True)
        if self.view == "breakdown":
            self.ram_chart.set_lines([line for line in BREAKDOWN_LINES if line.key in self._sel])
            self._rebuild_legend()
            self._refresh_readout()
        if "comp_open" in state:
            self.comp_toggle.set_active(bool(state["comp_open"]))
        if "det_open" in state:
            self.det_toggle.set_active(bool(state["det_open"]))
        if state.get("app_open"):
            self.app_toggle.set_active(True)
            panel = self._app_panel_widget()
            mode = state.get("app_mode")
            if panel is not None and mode in ("size", "growth"):
                btn = panel.growth_btn if mode == "growth" else panel.size_btn
                btn.set_active(True)

    def on_shown(self) -> None:
        super().on_shown()
        shown = getattr(self.hub, "mem_page_shown", None)
        if shown is not None:
            shown(self)
        if self._app_open:  # a park survived the toggle: re-baseline the walks
            panel = self._app_panel_widget()
            if panel is not None and hasattr(panel, "on_page_shown"):
                panel.on_page_shown()
            else:
                self._request_hub_walks(True)

    def on_hidden(self) -> None:
        hidden = getattr(self.hub, "mem_page_hidden", None)
        if hidden is not None:
            hidden(self)
        if self._app_open:
            panel = self._app_panel_widget()
            if panel is not None and hasattr(panel, "on_page_hidden"):
                panel.on_page_hidden()
            else:
                self._request_hub_walks(False)
