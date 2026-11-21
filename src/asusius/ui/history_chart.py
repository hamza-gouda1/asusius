# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""UPower history chart: three stacked mini-plots on one shared time axis.

Charge percent (fixed 0-100), power (W, 0 to a nice ceiling) and voltage (V, its own
floor/ceiling) have unrelated units, so one mixed-unit axis would be meaningless;
this widget stacks three mini-plots vertically inside one widget — percent on top,
watts in the middle, voltage at the bottom — all sharing the time axis. It is fed
plain snapshots (`set_data`), independent of the live Store ring:

    chart.set_data(t_end, span_s,
                   {"percent": [(t, v | None), ...],
                    "watts":   [(t, v | None), ...],
                    "voltage": [(t, v | None), ...]},
                   [(t0, t1, "Charging", (r, g, b)), ...])
    chart.set_empty_text("No history recorded for this range yet.")

Honesty rules (contract §4): a None/NaN value — or a time hole with no rows at
all (merged history carries gaps as missing timestamps) — breaks the line AND the
fill (no interpolation, nothing is drawn across a gap); the x axis maps
[t_end - span_s, t_end] but data is drawn only over observed t, so a range whose
tail has no samples stays blank instead of being stretched to "now". State bands
are a subtle background wash across all three plots with a concise label at the
top-left. When no series has any usable point, only the empty text is drawn
centred — no axes, no gridlines, no fabricated scales.

The widget redraws only on set_data, a size change or a dark/light switch (GTK
itself skips the draw func while unmapped, the same discipline as ui/charts.py);
pixel point buffers are reused between frames.
"""
from __future__ import annotations

import math
import time as _time

import cairo
from gi.repository import Adw, Gdk, Gtk, PangoCairo

from ..telemetry.util import nice_ceiling
from . import theme
from .chartmath import nice_range
from .charts import _text, colour, make_layout

HEIGHT = 176
TOP_PAD = 4.0
STATE_H = 16.0        # state labels above the first plot
BAND_GAP = 6.0        # between the three mini-plots
TIME_H = 16.0         # bottom time-axis label strip
GUTTER = 38.0         # right gutter for the value labels (compact-chart style)
LEFT_PAD = 6.0

# series key -> (colour name, fixed top label, fixed bottom label, unit suffix).
# A None label means "derive from the axis range"; percent is always fixed 0-100.
BANDS: tuple[tuple[str, str, str | None, str | None, str], ...] = (
    ("percent", "primary", "100%", "0%", "%"),
    ("watts", "gold", None, None, " W"),
    ("voltage", "sage", None, None, " V"),
)


def _fmt_axis(v: float, unit: str) -> str:
    """Compact axis text: two decimals trimmed, unit appended."""
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{s}{unit}"


def _fmt_clock(t: float, span_s: float) -> str:
    """Local clock label; the date appears once the range spans days."""
    lt = _time.localtime(t)
    if span_s >= 48 * 3600:
        return _time.strftime("%d %b %H:%M", lt)
    return _time.strftime("%H:%M", lt)


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _gap_step(pts: list) -> float:
    """Time-jump threshold that counts as a hole: ~2.6x the series' own median
    step (charts.py's GAP_FACTOR discipline). Merged rows carry holes as missing
    timestamps, not as None values, so the line must break on those too."""
    ts = sorted(t for t, _v in pts)
    steps = sorted(b - a for a, b in zip(ts, ts[1:]) if b > a)
    return steps[len(steps) // 2] * 2.6 if steps else math.inf


class HistoryChart(Gtk.DrawingArea):
    """Snapshot-fed history chart; see the module docstring for the contract."""

    def __init__(self) -> None:
        super().__init__()
        self.set_content_height(HEIGHT)
        self.set_hexpand(True)
        self.set_focusable(False)
        self.set_can_focus(False)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Battery history chart"])
        self.add_css_class("chart")
        self.set_draw_func(self._draw)
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._query_tooltip)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self._kb_t: float | None = None
        self._plot_right = LEFT_PAD + 1
        self._series: dict[str, list] = {}
        self._states: list = []
        self._t_end = 0.0
        self._span_s = 1.0
        self._empty_text = ""
        self._empty = True
        # reusable pixel buffers, refilled every frame (charts.py discipline)
        keys = [k for k, _c, _t, _b, _u in BANDS]
        self._xs: dict[str, list[float]] = {k: [] for k in keys}
        self._ys: dict[str, list[float]] = {k: [] for k in keys}
        self._gap_step: dict[str, float] = {}
        self._dark_sid = Adw.StyleManager.get_default().connect(
            "notify::dark", lambda *_: self.queue_draw())
        self.connect("destroy", self._on_destroy)

    def _on_destroy(self, *_a) -> None:
        if self._dark_sid:
            Adw.StyleManager.get_default().disconnect(self._dark_sid)
            self._dark_sid = 0

    # -- snapshots ------------------------------------------------------------------------

    def set_data(self, t_end: float, span_s: float,
                 series: dict[str, list], states: list) -> None:
        """One snapshot of history (shapes in the module docstring). A snapshot with
        no usable point switches to the empty state on its own."""
        self._t_end = float(t_end or 0.0)
        self._span_s = max(1.0, float(span_s or 1.0))
        self._series = {k: list(v or []) for k, v in (series or {}).items()}
        self._states = list(states or [])
        self._empty = not self._has_points()
        self.set_focusable(not self._empty)
        self.set_can_focus(not self._empty)
        if self._empty or self._kb_t is None or not (
                self._t_end - self._span_s <= self._kb_t <= self._t_end):
            self._kb_t = None
        limit = max(120.0, min(7200.0, self._span_s / 10))
        self._gap_step = {k: min(_gap_step(v), limit) for k, v in self._series.items()}
        self.queue_draw()

    def set_empty_text(self, text: str) -> None:
        """Show only this text, centred (the empty state)."""
        self._empty_text = text or ""
        self._empty = True
        self._kb_t = None
        self.set_focusable(False)
        self.set_can_focus(False)
        self.queue_draw()

    def _sample_times(self) -> list[float]:
        t0 = self._t_end - self._span_s
        return sorted({
            float(t)
            for key, _c, _top, _bot, _u in BANDS
            for t, v in self._series.get(key, ())
            if t0 <= t <= self._t_end and _finite(v)
        })

    def _on_key(self, _c, keyval, _code, _state) -> bool:
        if self._empty:
            return False
        if keyval == Gdk.KEY_Escape:
            if self._kb_t is None:
                return False
            self._kb_t = None
            self.queue_draw()
            return True
        times = self._sample_times()
        if not times:
            return False
        if keyval == Gdk.KEY_Home:
            self._kb_t = times[0]
        elif keyval == Gdk.KEY_End:
            self._kb_t = times[-1]
        elif keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            step = -1 if keyval == Gdk.KEY_Left else 1
            if self._kb_t is None:
                self._kb_t = times[-1] if step < 0 else times[0]
            else:
                i = min(range(len(times)), key=lambda j: abs(times[j] - self._kb_t))
                self._kb_t = times[min(len(times) - 1, max(0, i + step))]
        else:
            return False
        self.queue_draw()
        if hasattr(self, "trigger_tooltip_query"):
            self.trigger_tooltip_query()
        return True

    def _has_points(self) -> bool:
        for key, _c, _top, _bot, _u in BANDS:
            if any(_finite(v) for _t, v in self._series.get(key, ())):
                return True
        return False

    # -- drawing --------------------------------------------------------------------------

    def _axis(self, key: str) -> tuple[float, float] | None:
        """(lo, hi) value range for one mini-plot; None when the series has no data."""
        vals = [v for _t, v in self._series.get(key, ()) if _finite(v)]
        if not vals:
            return None
        if key == "percent":
            return 0.0, 100.0
        if key == "watts":
            lo, hi = min(vals), max(vals)
            return (-nice_ceiling(-lo) if lo < 0 else 0,
                    nice_ceiling(hi) if hi > 0 or lo >= 0 else 0)
        lo, hi = min(vals), max(vals)
        pad = max(0.05, (hi - lo) * 0.15)
        a, b, _step = nice_range(lo - pad, hi + pad, floor=None, min_span=0.5, target=2)
        return a, b

    def _query_tooltip(self, _widget, x, _y, keyboard, tooltip) -> bool:
        if self._empty:
            return False
        if not keyboard and not LEFT_PAD <= x <= self._plot_right:
            return False
        t0 = self._t_end - self._span_s
        width = max(1.0, self._plot_right - LEFT_PAD)
        samples = {}
        for key, rows in self._series.items():
            for t, value in rows:
                if t0 <= t <= self._t_end and _finite(value):
                    samples.setdefault(t, {})[key] = value
        if not samples:
            return False
        if keyboard:
            target = self._kb_t if self._kb_t is not None else max(samples)
            t = min(samples, key=lambda stamp: abs(stamp - target))
        else:
            target = t0 + (x - LEFT_PAD) * self._span_s / width
            t = min(samples, key=lambda stamp: abs(stamp - target))
            if abs(t - target) > self._span_s * 8 / width:
                return False
        lines = [_time.strftime("%d %b %H:%M:%S", _time.localtime(t))]
        values = samples[t]
        for key, name, unit in (("percent", "Charge", "%"), ("watts", "Power", " W"),
                                ("voltage", "Voltage", " V")):
            if key in values:
                lines.append(f"{name} · {_fmt_axis(values[key], unit)}")
        tooltip.set_text("\n".join(lines))
        return True

    def _draw(self, _area, cr, w, h) -> None:
        pal = theme.colours()
        dark = theme.is_dark()
        layout = make_layout(cr)
        layout.set_font_description(theme.mono_font(9.0))
        if self._empty:
            self._draw_empty(cr, layout, w, h, pal)
            return
        n_bands = len(BANDS)
        axes = {key: self._axis(key) for key, *_rest in BANDS}
        gutter = GUTTER
        for key, _colour, top_label, bottom_label, unit in BANDS:
            axis = axes[key]
            if axis is not None:
                for val, default in zip(axis, (bottom_label, top_label)):
                    layout.set_text(default or _fmt_axis(val, unit), -1)
                    gutter = max(gutter, layout.get_pixel_size()[0] + 10)
        plot_r = max(LEFT_PAD + 1, w - gutter)
        self._plot_right = plot_r
        plot_top = TOP_PAD + STATE_H
        band_h = max(8.0, (h - plot_top - TIME_H - BAND_GAP * (n_bands - 1)) / n_bands)
        t0, t1 = self._t_end - self._span_s, self._t_end
        span = max(1.0, t1 - t0)
        top_y = TOP_PAD
        bot_y = plot_top + band_h * n_bands + BAND_GAP * (n_bands - 1)
        cr.set_line_width(1)

        # state bands: one wash behind all three plots, label at the top-left
        wash_a = 0.10 if dark else 0.07
        for b_t0, b_t1, label, tint in self._states:
            if b_t1 <= t0 or b_t0 >= t1 or len(tint) < 3:
                continue
            x0 = LEFT_PAD + (max(b_t0, t0) - t0) / span * (plot_r - LEFT_PAD)
            x1 = LEFT_PAD + (min(b_t1, t1) - t0) / span * (plot_r - LEFT_PAD)
            cr.rectangle(x0, top_y, max(1.0, x1 - x0), bot_y - top_y)
            cr.set_source_rgba(tint[0], tint[1], tint[2], wash_a)
            cr.fill()
            layout.set_text(label, -1)
            lw, _lh = layout.get_pixel_size()
            if x1 - x0 >= lw + 8:  # narrow bands keep the wash but drop the label
                cr.set_source_rgb(*pal["dim"][:3])
                cr.move_to(min(x0 + 3, plot_r - lw - 2), top_y + 2)
                PangoCairo.show_layout(cr, layout)

        # the three mini-plots
        band_y = plot_top
        for key, cname, top_lbl, bot_lbl, unit in BANDS:
            self._plot_band(cr, layout, key, cname, axes[key], top_lbl, bot_lbl,
                            unit, band_y, band_h, t0, span, plot_r, w - 3, pal, dark)
            band_y += band_h + BAND_GAP

        # shared time axis over the requested window; honest left/right clock labels
        ty = bot_y + 4
        dim = pal["dim"]
        labels = [_fmt_clock(t0, self._span_s),
                  _fmt_clock(t0 + span / 2, self._span_s),
                  _fmt_clock(t1, self._span_s)]
        sizes = []
        for text in labels:
            layout.set_text(text, -1)
            sizes.append(layout.get_pixel_size()[0])
        if LEFT_PAD + sizes[0] + 16 + sizes[1] + 16 + sizes[2] <= plot_r:
            show = labels  # room for the midpoint label too
            xs = [LEFT_PAD, (LEFT_PAD + plot_r) / 2 - sizes[1] / 2, plot_r - sizes[2]]
        else:
            show = (labels[0], labels[2])
            xs = [LEFT_PAD, plot_r - sizes[2]]
        for text, x in zip(show, xs):
            layout.set_text(text, -1)
            cr.set_source_rgb(*dim[:3])
            cr.move_to(x, ty)
            PangoCairo.show_layout(cr, layout)

        if self._kb_t is not None and t0 <= self._kb_t <= t1:
            kx = round(LEFT_PAD + (self._kb_t - t0) / span * (plot_r - LEFT_PAD)) + 0.5
            cr.set_source_rgba(*dim[:3], 0.55)
            cr.set_line_width(1)
            cr.move_to(kx, plot_top)
            cr.line_to(kx, bot_y)
            cr.stroke()

    def _plot_band(self, cr, layout, key: str, cname: str, rng, top_lbl, bot_lbl,
                   unit: str, y: float, bh: float, t0: float, span: float,
                   plot_r: float, label_right: float, pal, dark: bool) -> None:
        pw = max(1.0, plot_r - LEFT_PAD)
        rgb = colour(cname)
        # frame: top and bottom gridlines
        cr.set_source_rgba(*pal["grid"])
        for gy in (round(y) + 0.5, round(y + bh) + 0.5):
            cr.move_to(LEFT_PAD, gy)
            cr.line_to(plot_r, gy)
            cr.stroke()
        # value labels in the right gutter ("–" when the series has no data)
        dim = pal["dim"]
        if rng is None:
            pairs = (("–", y + 1.0, "top"), ("–", y + bh - 1.0, "bottom"))
        else:
            lo, hi = rng
            top = top_lbl if top_lbl is not None else _fmt_axis(hi, unit)
            bot = bot_lbl if bot_lbl is not None else _fmt_axis(lo, unit)
            pairs = ((top, y + 1.0, "top"), (bot, y + bh - 1.0, "bottom"))
        for lbl, ly, valign in pairs:
            _text(cr, layout, lbl, label_right, ly, (*dim[:3], 1.0),
                  "right", valign)

        # points -> pixels (None/NaN values and time holes become gap markers),
        # reusable buffers
        xs, ys = self._xs[key], self._ys[key]
        xs.clear()
        ys.clear()
        lo, hi = rng if rng is not None else (0.0, 1.0)
        vspan = (hi - lo) or 1.0
        threshold = self._gap_step.get(key, math.inf)
        prev_t = None
        for t, v in self._series.get(key, ()):
            if prev_t is not None and t - prev_t > threshold:
                xs.append(math.nan)  # a hole: the line and the fill break here
                ys.append(math.nan)
            prev_t = t
            if not _finite(v):
                xs.append(math.nan)
                ys.append(math.nan)
                continue
            x = LEFT_PAD + (t - t0) / span * pw
            fy = min(1.0, max(0.0, 1.0 - (v - lo) / vspan))
            xs.append(x)
            ys.append(y + fy * bh)
        cr.save()
        cr.rectangle(LEFT_PAD, y - 1, pw + 1, bh + 2)
        cr.clip()
        run_start = None
        for i in range(len(xs) + 1):
            gap = i == len(xs) or math.isnan(xs[i])
            if not gap and run_start is None:
                run_start = i
            elif gap and run_start is not None:
                self._draw_run(cr, xs, ys, run_start, i, y, bh, rgb, dark)
                run_start = None
        cr.restore()

    def _draw_run(self, cr, xs, ys, a: int, b: int, y: float, bh: float,
                  rgb, dark: bool) -> None:
        """One contiguous run between gaps: area fill + line, no interpolation."""
        if a >= b:
            return
        base_y = y + bh
        cr.move_to(xs[a], ys[a])
        for i in range(a + 1, b):
            cr.line_to(xs[i], ys[i])
        cr.line_to(xs[b - 1], base_y)
        cr.line_to(xs[a], base_y)
        cr.close_path()
        grad = cairo.LinearGradient(0, y, 0, y + bh)
        top = 0.30 if dark else 0.18
        grad.add_color_stop_rgba(0, *rgb, top)
        grad.add_color_stop_rgba(1, *rgb, top * 0.2)
        cr.set_source(grad)
        cr.fill()
        cr.move_to(xs[a], ys[a])
        for i in range(a + 1, b):
            cr.line_to(xs[i], ys[i])
        cr.set_source_rgb(*rgb)
        cr.set_line_width(1.4)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.stroke()
        if b - a == 1:  # an isolated sample between gaps: a dot, not an invisible line
            cr.arc(xs[a], ys[a], 1.8, 0, 2 * math.pi)
            cr.fill()

    def _draw_empty(self, cr, layout, w, h, pal) -> None:
        """Empty state: only the text, centred — no axes, no gridlines, no scales."""
        layout.set_text(self._empty_text or "No data", -1)
        tw, th = layout.get_pixel_size()
        dim = pal["dim"]
        cr.set_source_rgba(*dim[:3], dim[3] * 0.9)
        cr.move_to((w - tw) / 2, (h - th) / 2)
        PangoCairo.show_layout(cr, layout)
