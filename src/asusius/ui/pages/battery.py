# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Battery page: charge limit control, live status and the UPower history section.

The charge-limit block, Charge now, the health rows and the "Right now" row are the
original controls. On top of them this page shows a live status card (percent, state,
W and V), lists peripherals and UPS devices reported by UPower, and draws the recorded
history for a selected device and range. LiveHub polls the inventory while the
page is shown and supplies one history series at a time. The page combines those
series for display and computes the "All batteries" aggregate.
"""
from __future__ import annotations

import math
import time
from time import time as wall_time

from gi.repository import Adw, GLib, Gtk, Pango

from ...core import Capability
from ...telemetry.upower import KIND_BY_ID
from ..tasks import run_bg
from ..viewmodels import (
    CHARGE_PRESETS,
    CHARGE_RANGE,
    DASH,
    charge_limit_hint,
    fmt_num,
    health_label,
    validate_charge_limit,
)
from ..widgets import label
from .base import Page, a11y_name, button, info_row, pill, prefs_page, value_row

EMPTY_TEXT = "No history recorded for this range yet."
AGGREGATE_KEY = "aggregate:all"  # page-level key; the hub serves history per device only

# History: three series per device, normalized before merging.
HISTORY_SERIES = ("rate", "charge", "voltage")
SERIES_FIELD = {"rate": "watts", "charge": "percent", "voltage": "voltage"}
RANGES = (("1h", 3600), ("6h", 6 * 3600), ("24h", 24 * 3600), ("7d", 7 * 24 * 3600))
DEFAULT_RANGE_S = 24 * 3600

# Raw state strings (sysfs "Discharging" and UPower "discharging" both land here) to
# the concise wording shown next to the big percent and in the device rows.
STATE_LABELS = {
    "charging": "Charging",
    "discharging": "Discharging",
    "full": "Plugged in",
    "fully-charged": "Plugged in",
    "not charging": "Plugged in",
    "empty": "Empty",
    "pending-charge": "Pending charge",
    "pending-discharge": "Pending discharge",
}

# History band labels keep the UPower wording; tints are (r, g, b) 0-1 tuples the chart
# renders as a subtle background wash (alpha is the chart's business, not ours).
BAND_LABELS = {
    "charging": "Charging",
    "discharging": "Discharging",
    "fully-charged": "Fully charged",
    "empty": "Empty",
    "pending-charge": "Pending charge",
    "pending-discharge": "Pending discharge",
}
STATE_TINTS = {
    "charging": (0.34, 0.66, 0.44),
    "discharging": (0.86, 0.64, 0.28),
    "fully-charged": (0.44, 0.60, 0.84),
    "empty": (0.85, 0.38, 0.38),
    "pending-charge": (0.72, 0.62, 0.40),
    "pending-discharge": (0.72, 0.62, 0.40),
}

# UPower device state enum, kept as a fallback when a raw int arrives instead of text.
_UPOWER_STATES = {1: "charging", 2: "discharging", 3: "empty", 4: "fully-charged",
                  5: "pending-charge", 6: "pending-discharge"}

# -- device helpers ---------------------------------------------------------------------------


def _state_str(state) -> str | None:
    if state is None:
        return None
    if isinstance(state, int) and not isinstance(state, bool):
        return _UPOWER_STATES.get(state)
    s = str(state).strip().lower().replace("_", "-")
    return None if s in ("", "unknown") else s


def state_text(status: str | None) -> str:
    s = _state_str(status)
    if s is None:
        return DASH
    return STATE_LABELS.get(s, BAND_LABELS.get(s, s.capitalize()))


def device_kind(device) -> str:
    """The normalized UPower kind for a device that should appear on the page."""
    kind = getattr(device, "kind", None)
    if isinstance(kind, int) and not isinstance(kind, bool):
        kind = KIND_BY_ID.get(kind, "unknown")
    if kind in ("laptop", "ups", "peripheral"):
        return kind
    return "other"  # line power and display composites are never battery rows


def _path_base(device) -> str:
    native = getattr(device, "native_path", "") or getattr(device, "object_path", "")
    return native.rstrip("/").rsplit("/", 1)[-1].replace("_", " ") if native else ""


def device_name(device) -> str:
    """A row/selector label: the model, else the UPower type, else the sysfs name.
    The serial is never shown."""
    model = getattr(device, "model", None)
    if model:
        return str(model)
    type_text = getattr(device, "type_text", None)
    if type_text:
        return str(type_text)
    base = _path_base(device)
    return base or "Battery"


def device_subtitle(device) -> str:
    parts = [state_text(getattr(device, "state", None))]
    w = getattr(device, "watts", None)
    v = getattr(device, "voltage", None)
    if w is not None:
        parts.append(f"{fmt_num(w, 1)} W")
    if v is not None:
        parts.append(f"{fmt_num(v, 2)} V")
    return " · ".join(parts)


def level_text(device) -> str:
    coarse = getattr(device, "coarse_level", None)
    if coarse:
        return coarse
    pct = getattr(device, "percent", None)
    return "Level unknown" if pct is None else f"{fmt_num(pct)}%"


def battery_weight(device) -> float | None:
    """Energy weight for the aggregate (Wh); None when the device reports no energy."""
    w = getattr(device, "energy_full", None)
    if not w:
        w = getattr(device, "energy", None)
    return w if w and w > 0 else None


def history_key(dev_key: str, series: str) -> str:
    """Seam with LiveHub: the series rides in the request key so the hub's per
    (dev_key, range_s) token guard keeps the latest request per series (§5)."""
    return f"{dev_key}|{series}"


# -- history math (display-free, unit-tested directly) -----------------------------------------


def merge_series(parts: dict[str, list]) -> list[dict]:
    """Merge normalized per-series point triples (t, value|None, state) into per-t
    rows: percent from charge, watts from rate, voltage from voltage; the row state
    comes from the charge series. Gaps stay gaps — nothing is interpolated."""
    rows: dict[float, dict] = {}
    for series, pts in parts.items():
        field = SERIES_FIELD.get(series)
        if field is None:
            continue
        for t, v, state in pts:
            row = rows.setdefault(t, {})
            row[field] = v
            if series == "charge" and state:
                row["state"] = state
    return [{"t": t, **rows[t]} for t in sorted(rows)]


def build_bands(rows: list[dict], gap_s: float = math.inf) -> list[tuple]:
    """Consecutive same-state stretches of the charge series as (t0, t1, label, tint)."""
    bands: list[tuple] = []
    start = prev_t = None
    cur = None
    for r in rows:
        st = r.get("state")
        if st != cur or (prev_t is not None and r["t"] - prev_t > gap_s):
            if cur in BAND_LABELS and start is not None:
                bands.append((start, prev_t, BAND_LABELS[cur], STATE_TINTS[cur]))
            cur, start = st, r["t"]
        prev_t = r["t"]
    if cur in BAND_LABELS and start is not None:
        bands.append((start, prev_t, BAND_LABELS[cur], STATE_TINTS[cur]))
    return bands


def summary_lines(rows: list[dict]) -> dict[str, tuple]:
    """(latest, min, max) per series over the observed points; absent when no data."""
    out: dict[str, tuple] = {}
    for field in ("percent", "watts", "voltage"):
        vals = [(r["t"], r[field]) for r in rows if r.get(field) is not None]
        if not vals:
            continue
        vs = [v for _t, v in vals]
        out[field] = (max(vals, key=lambda p: p[0])[1], min(vs), max(vs))
    return out


def _weighted(vals: list[tuple[float | None, float | None]]) -> float | None:
    """Weighted mean of the present values (weights from the battery energies); a plain
    mean when no energies are known. Gaps (None) are skipped, never fabricated."""
    present = [(v, w) for v, w in vals if v is not None]
    if not present:
        return None
    weights = [(v, w) for v, w in present if w and w > 0]
    if weights and len(weights) == len(present):
        wsum = sum(w for _v, w in weights)
        return sum(v * w for v, w in weights) / wsum
    return sum(v for v, _w in present) / len(present)


def aggregate_history(per_dev: dict[str, dict[str, list]],
                      weights: dict[str, float | None]) -> dict[str, list]:
    """Combine timestamps shared by all batteries, preserving gaps.

    Sum the power and weight charge and voltage by capacity when known. Draw a
    charge-state band only where all batteries report the same state.
    """
    maps = {k: {s: {p[0]: p for p in pts} for s, pts in parts.items()}
            for k, parts in per_dev.items()}
    keys = sorted(maps)
    out: dict[str, list] = {}
    for series in HISTORY_SERIES:
        stamps = sorted({t for k in keys for t in maps[k].get(series, {})})
        pts: list[tuple] = []
        for t in stamps:
            got = [(k, maps[k][series][t]) for k in keys if t in maps[k].get(series, {})]
            if len(got) != len(keys) or any(p[1] is None for _k, p in got):
                pts.append((t, None, None))
                continue
            if series == "rate":
                pts.append((t, sum(p[1] for _k, p in got), None))
                continue
            state = None
            if series == "charge":
                states = {p[2] for _k, p in got}
                state = next(iter(states)) if len(states) == 1 else None
            vals = [(p[1], weights.get(k)) for k, p in got]
            pts.append((t, _weighted(vals), state))
        out[series] = pts
    return out


def _fmt_series(field: str, v: float | None) -> str:
    if v is None:
        return DASH
    if field == "percent":
        return f"{fmt_num(v)}%"
    if field == "watts":
        return f"{fmt_num(v, 1)} W"
    return f"{fmt_num(v, 2)} V"


# -- the page -----------------------------------------------------------------------------------


class BatteryPage(Page):
    key, title = "battery", "Battery"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.current: int | None = None
        self._quiet = False
        self._pending = None

        # history state (before any widget signal can fire)
        self._hist_entries: list[dict] = []
        self._hist_results: dict = {}
        self._hist_errors: set = set()
        self._hist_expected: list = []
        self._hist_gen = 0
        self._hist_request = None
        self._hist_refresh_at = 0.0
        self._hist_rebuilding = False
        self._range_s = DEFAULT_RANGE_S
        self._laptops: list = []
        self._laptop_map: dict = {}
        self._peripherals: list = []
        self._ups: list = []
        self._inventory_status = ""
        self._power_seam = hasattr(self.win.hub, "power_page_shown")

        # -- live status card (new; telemetry-driven) ----------------------------------------
        self.status_group = Adw.PreferencesGroup(title="Battery")
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.add_css_class("tile")
        top = Gtk.Box(spacing=12)
        self.card_big = label(DASH, ("big-scale-value",))
        top.append(self.card_big)
        self.card_state = label(DASH, ("stat-sub",), wrap=True,
                                wrap_mode=Pango.WrapMode.WORD_CHAR, hexpand=True,
                                valign=Gtk.Align.CENTER)
        top.append(self.card_state)
        card.append(top)
        stats = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                            min_children_per_line=1, max_children_per_line=3,
                            column_spacing=8, row_spacing=8)
        self.card_w = self._stat_cell(stats, "Battery power")
        self.card_v = self._stat_cell(stats, "Voltage")
        self.card_cpu = self._stat_cell(stats, "CPU package")
        card.append(stats)
        self.status_group.add(card)

        # -- charge limit (preserved verbatim) ------------------------------------------------
        self.limit = Adw.PreferencesGroup(
            title="Charge limit",
            description="Stopping below 100% slows battery wear when you are mostly plugged in.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.add_css_class("tile")
        top = Gtk.Box(spacing=12)
        self.top = top
        self.big = label(DASH, ("big-scale-value",))
        top.append(self.big)
        self.hint = label("", ("stat-sub",), wrap=True, hexpand=True, valign=Gtk.Align.CENTER)
        top.append(self.hint)
        self.presets = Gtk.Box(valign=Gtk.Align.CENTER)
        self.presets.add_css_class("linked")
        for p in CHARGE_PRESETS:
            b = Gtk.Button(label=f"{p}%")
            b.add_css_class("numeric")
            b.connect("clicked", lambda _b, p=p: self.adj.set_value(p))
            self.presets.append(b)
        top.append(self.presets)
        box.append(top)
        lo, hi = CHARGE_RANGE
        self.adj = Gtk.Adjustment(value=80, lower=lo, upper=hi, step_increment=1,
                                  page_increment=5)
        self.scale = Gtk.Scale(adjustment=self.adj, draw_value=False, hexpand=True)
        a11y_name(self.scale, "Charge limit")
        for p in CHARGE_PRESETS:
            self.scale.add_mark(p, Gtk.PositionType.BOTTOM, None)
        self.adj.connect("value-changed", self._changed)
        box.append(self.scale)
        self.limit.add(box)
        self.oneshot = Adw.ActionRow(title="Charge to 100% once",
                                     subtitle="For a long trip. The limit returns afterwards.")
        self.oneshot.add_suffix(button("Charge now", on_click=self._oneshot))
        self.limit_err = info_row("Charge limit unavailable", "")
        self.limit_err.set_visible(False)
        g_extra = Adw.PreferencesGroup()
        g_extra.add(self.oneshot)
        g_extra.add(self.limit_err)

        # -- health (preserved verbatim) --------------------------------------------------------
        self.health = Adw.PreferencesGroup(title="Health")
        self.h_row, self.h_val = value_row("Health", "Full charge vs. design capacity")
        self.level = Gtk.LevelBar(min_value=0, max_value=100, valign=Gtk.Align.CENTER,
                                  width_request=120)
        self.h_row.add_suffix(self.level)
        self.cap_row, self.cap_val = value_row("Capacity")
        self.cyc_row, self.cyc_val = value_row("Charge cycles")
        self.now_row, self.now_val = value_row("Right now")
        for r in (self.h_row, self.cap_row, self.cyc_row, self.now_row):
            self.health.add(r)

        # -- UPower device groups (new) ----------------------------------------------------------
        self.periph_group = Adw.PreferencesGroup(
            title="Peripherals",
            description="Batteries of devices that report their level through UPower.")
        self.periph_group.set_visible(False)
        self.periph_rows: list = []
        self.ups_group = Adw.PreferencesGroup(
            title="UPS", description="Uninterruptible power supplies reported by UPower.")
        self.ups_group.set_visible(False)
        self.ups_rows: list = []
        self.batteries_group = Adw.PreferencesGroup(title="Installed batteries", visible=False)
        self.battery_rows: list = []

        # -- history (new) -------------------------------------------------------------------------
        self.hist_group = Adw.PreferencesGroup(
            title="History",
            description="Positive power is discharge. Negative power is charging.")
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        tile.add_css_class("tile")
        self.hist_head = Gtk.Box(spacing=10)
        self.hist_selector = Gtk.DropDown(model=Gtk.StringList.new([]))
        self.hist_selector.set_hexpand(True)
        self.hist_selector.set_focusable(True)
        # an empty DropDown has nothing to select: keep it insensitive until the
        # inventory answers
        self.hist_selector.set_sensitive(False)
        a11y_name(self.hist_selector, "Battery history device")
        self.hist_selector.connect("notify::selected", self._on_hist_device)
        self.hist_head.append(self.hist_selector)
        self.range_chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                       min_children_per_line=1, max_children_per_line=4,
                                       column_spacing=6, row_spacing=6, hexpand=True)
        self._chip_guard = False
        self.chip_buttons: list[Gtk.ToggleButton] = []
        for text, secs in RANGES:
            b = Gtk.ToggleButton(label=text, tooltip_text=f"Show the last {text} of history")
            b.add_css_class("preset-chip")
            b.add_css_class("numeric")
            b.connect("toggled", self._on_range_toggled, secs)
            self.range_chips.append(b)
            self.chip_buttons.append(b)
            if b.get_parent() is not None:
                b.get_parent().set_focusable(False)
        self.chip_buttons[2].set_active(True)  # 24h default; the handler no-ops yet
        self.hist_head.append(self.range_chips)
        tile.append(self.hist_head)
        self.hist_chart_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._chart = self._make_chart()
        if isinstance(self._chart, Gtk.Widget):  # not the test stub, not None
            self.hist_chart_holder.append(self._chart)
        tile.append(self.hist_chart_holder)
        self.hist_legend = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                                       min_children_per_line=1, max_children_per_line=3,
                                       column_spacing=6, row_spacing=4)
        for text in ("Charge %", "Power W", "Voltage V"):
            self.hist_legend.append(pill(text))
            child = self.hist_legend.get_last_child()
            if child is not None:
                child.set_focusable(False)
        tile.append(self.hist_legend)
        self.hist_summary = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._summary_rows: dict[str, tuple[Gtk.Box, Gtk.Label]] = {}
        for field, caption in (("percent", "Charge level"), ("watts", "Power"),
                               ("voltage", "Voltage")):
            row = Gtk.Box(spacing=8, hexpand=True)
            row.append(label(caption, ("stat-caption",), hexpand=True))
            val = label(DASH, ("numeric", "section-caption"), xalign=1.0,
                        wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
            row.append(val)
            self.hist_summary.append(row)
            self._summary_rows[field] = (row, val)
        tile.append(self.hist_summary)
        self.hist_empty = label(EMPTY_TEXT, ("monitor-note",), wrap=True,
                                wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.hist_empty.set_visible(False)
        tile.append(self.hist_empty)
        self.hist_note = label("History needs UPower.", ("monitor-note",), wrap=True,
                               wrap_mode=Pango.WrapMode.WORD_CHAR)
        tile.append(self.hist_note)
        self.hist_group.add(tile)
        self._set_history_available(False)
        if self._power_seam:
            self.hist_group.set_visible(False)  # pops in with the first inventory

        self.set_child(prefs_page(self.status_group, self.limit, g_extra, self.health,
                                  self.batteries_group, self.periph_group, self.ups_group,
                                  self.hist_group))

    # -- layout ---------------------------------------------------------------------------

    def _stat_cell(self, parent: Gtk.FlowBox, caption: str) -> Gtk.Label:
        cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        cell.add_css_class("stat-cell")
        cell.append(label(caption, ("stat-caption",), wrap=True,
                          wrap_mode=Pango.WrapMode.WORD_CHAR))
        val = label(DASH, ("stat-cell-value", "numeric"), xalign=0.0)
        cell.append(val)
        parent.append(cell)
        cell.get_parent().set_focusable(False)
        return val

    def _make_chart(self):
        """Create the history chart when the Battery page opens."""
        try:
            from ..history_chart import HistoryChart
        except ImportError:
            return None
        chart = HistoryChart()
        chart.set_hexpand(True)
        return chart

    def fit_width(self, level: int) -> None:
        """Phone width: the presets drop below the value instead of squeezing the row,
        the range chips wrap into two columns and the device picker gets its own row."""
        narrow = level == 3
        self.top.set_orientation(Gtk.Orientation.VERTICAL if narrow
                                 else Gtk.Orientation.HORIZONTAL)
        self.presets.set_halign(Gtk.Align.START if narrow else Gtk.Align.FILL)
        self.hist_head.set_orientation(Gtk.Orientation.VERTICAL if narrow
                                       else Gtk.Orientation.HORIZONTAL)
        self.range_chips.set_max_children_per_line(2 if narrow else 4)
        for row, value in self._summary_rows.values():
            row.set_orientation(Gtk.Orientation.VERTICAL if narrow
                                else Gtk.Orientation.HORIZONTAL)
            row.set_spacing(2 if narrow else 8)
            value.set_xalign(0.0 if narrow else 1.0)

    # -- lifecycle --------------------------------------------------------------------------

    def refresh(self) -> None:
        run_bg(lambda: self.ctl.get_battery_threshold()[0], done=self._got_limit,
               error=self._limit_failed)
        run_bg(lambda: self.ctl.read(Capability.BATTERY_HEALTH)[0], done=self._got_health,
               error=self._health_failed)

    def on_shown(self) -> None:
        super().on_shown()
        shown = getattr(self.win.hub, "power_page_shown", None)
        if shown is not None:
            shown(self)

    def on_hidden(self) -> None:
        if self._pending:
            GLib.source_remove(self._pending)
            self._pending = None
        self._hist_gen += 1  # in-flight responses are dropped
        self._hist_request = None
        self._hist_results = {}
        hidden = getattr(self.win.hub, "power_page_hidden", None)
        if hidden is not None:
            hidden(self)

    # -- preserved charge-limit callbacks ------------------------------------------------------

    def _limit_failed(self, exc) -> None:
        self.limit_err.set_subtitle(
            "Not available right now: the charge limit could not be read. Check that the "
            "asusd service is running, then press Refresh.")
        self.limit_err.set_visible(True)
        self.limit_err.set_tooltip_text(str(exc))
        self.scale.set_sensitive(False)
        self.presets.set_sensitive(False)
        self.oneshot.set_sensitive(False)

    def _got_limit(self, value: int) -> None:
        self.limit_err.set_visible(False)
        self.limit_err.set_tooltip_text(None)
        self.scale.set_sensitive(True)
        self.presets.set_sensitive(True)
        self.oneshot.set_sensitive(True)
        self.current = value
        self._quiet = True
        self.adj.set_value(value)
        self._quiet = False
        self._label(value)

    def _health_failed(self, exc) -> None:
        self.h_val.set_label("Unavailable")
        self.h_val.set_tooltip_text(str(exc))
        self.level.set_value(0)
        self.cap_val.set_label(DASH)
        self.cyc_val.set_label(DASH)

    def _label(self, v: int) -> None:
        self.big.set_label(f"{v}%")
        self.hint.set_label(charge_limit_hint(v))

    def _changed(self, adj) -> None:
        v = int(round(adj.get_value()))
        self._label(v)
        if self._quiet:
            return
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(700, self._commit)

    def _commit(self):
        self._pending = None
        try:
            v = validate_charge_limit(self.adj.get_value())
        except Exception as exc:
            self.win.toast_error(exc)
            return False
        if v != self.current:
            def after(_=None, v=v):
                self.current = v
            self.win.run_write(lambda: self.ctl.set_battery_threshold(v),
                               f"Charge limit set to {v}%", after=after,
                               on_error=self.refresh)
        return False

    def _oneshot(self) -> None:
        self.win.run_write(self.ctl.one_shot_full_charge,
                           "Charging to 100% this time", after=self.refresh)

    def _got_health(self, h) -> None:
        hp = h.health_percent
        self.h_val.set_label(f"{fmt_num(hp)}% · {health_label(hp)}")
        self.level.set_value(hp)
        self.cap_val.set_label(f"{h.energy_full_uwh / 1e6:.1f} of "
                               f"{h.energy_full_design_uwh / 1e6:.1f} Wh")
        self.cyc_val.set_label("Not reported" if not h.cycle_count else str(h.cycle_count))

    # -- live telemetry --------------------------------------------------------------------------

    def on_telemetry(self, t) -> None:
        st = (t.battery_status or DASH).lower()
        self.now_val.set_label(f"{fmt_num(t.battery_percent)}% · {st} · "
                               f"{fmt_num(t.battery_power_w, 1)} W")
        self._update_card(t)

    def _update_card(self, t) -> None:
        info = self.win.hub.info.get("battery") or {}
        pct = t.battery_percent
        self.card_big.set_label(DASH if pct is None else f"{pct}%")
        self.card_state.set_label(state_text(t.battery_status))
        w = t.battery_power_w
        self.card_w.set_label(DASH if w is None else f"{fmt_num(w, 1)} W")
        v = info.get("voltage")
        self.card_v.set_label(DASH if v is None else f"{fmt_num(v, 2)} V")
        cpu = getattr(t, "cpu_power_w", None)
        self.card_cpu.set_label(DASH if cpu is None else f"{fmt_num(cpu, 1)} W")

    # -- UPower inventory -----------------------------------------------------------------------

    def on_power_inventory(self, devices, status_text) -> None:
        devices = [d for d in (devices or ()) if getattr(d, "present", None) is not False]
        self._laptops = [d for d in devices if device_kind(d) == "laptop"]
        self._laptop_map = {d.object_path: d for d in self._laptops}
        self._peripherals = [d for d in devices if device_kind(d) == "peripheral"]
        self._ups = [d for d in devices if device_kind(d) == "ups"]
        self._inventory_status = status_text or ""
        self._rebuild_device_rows()
        have = bool(self._laptops or self._peripherals or self._ups)
        self._set_history_available(have)
        if have:
            self._rebuild_selector()
        else:
            cancel = getattr(self.win.hub, "cancel_power_history", None)
            if cancel is not None:
                cancel()
            self._hist_entries = []
            self._hist_request = None
            self._hist_gen += 1  # drop any in-flight responses
            self._hist_results = {}
            self.hist_selector.set_sensitive(False)  # nothing left to select

    def _device_row(self, d) -> Adw.ActionRow:
        row = Adw.ActionRow(title=device_name(d), subtitle=device_subtitle(d), use_markup=False)
        row.set_subtitle_lines(1)
        val = label(level_text(d), ("numeric",), xalign=1.0)
        val.add_css_class("dim-label")
        row.add_suffix(val)
        return row

    def _rebuild_device_rows(self) -> None:
        laptops = self._laptops if len(self._laptops) > 1 else []
        for group, devices, store in ((self.batteries_group, laptops, "battery_rows"),
                                      (self.periph_group, self._peripherals, "periph_rows"),
                                      (self.ups_group, self._ups, "ups_rows")):
            for row in getattr(self, store):  # rows added through group.add()
                group.remove(row)
            rows = [self._device_row(d) for d in devices]
            for row in rows:
                group.add(row)
            setattr(self, store, rows)
            group.set_visible(bool(devices))

    def _rebuild_selector(self) -> None:
        entries: list[dict] = []
        for d in self._laptops:
            entries.append({"key": d.object_path, "name": device_name(d),
                            "kind": "laptop", "device": d})
        if len(self._laptops) >= 2:
            entries.append({"key": AGGREGATE_KEY, "name": "All batteries",
                            "kind": "aggregate", "device": None})
        for d in self._peripherals:
            entries.append({"key": d.object_path, "name": device_name(d),
                            "kind": "peripheral", "device": d})
        for d in self._ups:
            entries.append({"key": d.object_path, "name": device_name(d),
                            "kind": "ups", "device": d})
        self._disambiguate(entries)
        current = self._current_entry()
        prev = current["key"] if current else None
        old_model = [(e["key"], e["name"]) for e in self._hist_entries]
        self._hist_entries = entries
        if old_model == [(e["key"], e["name"]) for e in entries]:
            self._refresh_history()
            return
        self._hist_rebuilding = True
        try:
            self.hist_selector.set_model(Gtk.StringList.new([e["name"] for e in entries]))
            self.hist_selector.set_sensitive(bool(entries))
            idx = next((i for i, e in enumerate(entries) if e["key"] == prev), 0)
            self.hist_selector.set_selected(idx)
        finally:
            self._hist_rebuilding = False
        self._refresh_history()

    @staticmethod
    def _disambiguate(entries: list[dict]) -> None:
        """Append the native name to distinguish devices with repeated labels."""
        seen: set[str] = set()
        for e in entries:
            name = e["name"]
            if name in seen and e["device"] is not None:
                base = _path_base(e["device"])
                e["name"] = f"{name} ({base})" if base and base != name else name
            seen.add(e["name"])

    def _set_history_available(self, have: bool) -> None:
        self.hist_group.set_visible(True)  # the inventory answered: note or content
        self.hist_note.set_visible(not have)
        if not have:
            text = ("No battery devices reported." if self._inventory_status.startswith("UPower:")
                    else "History needs UPower.")
            self.hist_note.set_label(text)
        self.hist_head.set_visible(have)
        if not have:
            self.hist_chart_holder.set_visible(False)
            self.hist_legend.set_visible(False)
            self.hist_summary.set_visible(False)
            self.hist_empty.set_visible(False)

    # -- history fetch + render --------------------------------------------------------------

    def _current_entry(self) -> dict | None:
        if not self._hist_entries:
            return None
        idx = self.hist_selector.get_selected()
        if idx < 0 or idx >= len(self._hist_entries):
            return None
        return self._hist_entries[idx]

    def _on_hist_device(self, *_a) -> None:
        if self._hist_rebuilding:
            return  # the selector rebuild restores the selection itself
        self._refresh_history()

    def _on_range_toggled(self, b: Gtk.ToggleButton, secs: int) -> None:
        if self._chip_guard:
            return
        if not b.get_active():
            self._chip_guard = True
            b.set_active(True)
            self._chip_guard = False
            return
        self._chip_guard = True
        for other in self.chip_buttons:
            if other is not b and other.get_active():
                other.set_active(False)
        self._chip_guard = False
        if secs == self._range_s:
            return
        self._range_s = secs
        self._refresh_history(force=True)

    def _refresh_history(self, force: bool = False) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        devices = self._laptops if entry["kind"] == "aggregate" else [entry["device"]]
        identity = tuple((d.object_path, getattr(d, "serial", None), getattr(d, "native_path", ""))
                         for d in devices)
        want = (entry["key"], self._range_s, identity)
        same = want == self._hist_request
        now = time.monotonic()
        if not force and same and now < self._hist_refresh_at:
            return
        self._hist_request = want
        self._hist_refresh_at = now + 60.0
        self._hist_gen += 1
        gen = self._hist_gen
        self._hist_results = {}
        self._hist_errors.clear()
        if not same:
            cancel = getattr(self.win.hub, "cancel_power_history", None)
            if cancel is not None:
                cancel()
            self.hist_legend.set_visible(False)
            self.hist_summary.set_visible(False)
            self.hist_chart_holder.set_visible(False)
            self.hist_empty.set_label("Loading battery history…")
            self.hist_empty.set_visible(True)
        if entry["kind"] == "aggregate":
            keys = [(d.object_path, s) for d in self._laptops for s in HISTORY_SERIES]
        else:
            keys = [(entry["key"], s) for s in HISTORY_SERIES]
        self._hist_expected = keys
        for dev_key, series in keys:
            self._request_history(gen, dev_key, series)

    def _request_history(self, gen: int, dev_key: str, series: str) -> None:
        fn = getattr(self.win.hub, "request_power_history", None)
        if fn is None:
            self._history_failed(gen, (dev_key, series), None)
            return
        fn(history_key(dev_key, series), self._range_s,
           lambda pts, g=gen, k=(dev_key, series): self._got_history(g, k, pts),
           lambda exc, g=gen, k=(dev_key, series): self._history_failed(g, k, exc))

    def _got_history(self, gen: int, key: tuple, points) -> None:
        if gen != self._hist_gen:
            return  # a newer selection superseded this response
        self._hist_results[key] = self._normalize(points, key[1])
        self._render_if_complete()

    def _history_failed(self, gen: int, key: tuple, _exc) -> None:
        if gen != self._hist_gen:
            return
        self._hist_results[key] = []  # a failed series is a gap, not a fabricated zero
        self._hist_errors.add(key)
        self._render_if_complete()

    def _render_if_complete(self) -> None:
        if self._hist_expected and all(k in self._hist_results
                                       for k in self._hist_expected):
            self._render_history()

    def _normalize(self, raw_points, series: str) -> list[tuple]:
        """Convert normalized measurements to timestamp, value and state triples."""
        try:
            from ...telemetry.power_history import normalize_history
        except ImportError:
            return []
        field = SERIES_FIELD[series]
        out: list[tuple] = []
        for p in normalize_history(list(raw_points or ()), series):
            t = getattr(p, "t", None)
            v = getattr(p, field, None)
            if v is None:
                v = getattr(p, "value", None)  # tolerate a raw (t, value) point shape
            if isinstance(t, bool) or not isinstance(t, (int, float)):
                continue
            if isinstance(v, float) and not math.isfinite(v):
                v = None
            out.append((float(t), None if v is None else float(v),
                        _state_str(getattr(p, "state", None))))
        return out

    def _render_history(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        per_dev: dict[str, dict[str, list]] = {}
        for (dev_key, series), triples in self._hist_results.items():
            per_dev.setdefault(dev_key, {})[series] = triples
        if entry["kind"] == "aggregate":
            weights = {k: battery_weight(self._laptop_map.get(k)) for k in per_dev}
            parts = aggregate_history(per_dev, weights)
        else:
            parts = per_dev.get(entry["key"], {})
        rows = merge_series(parts)
        self._show_history_rows(rows)

    def _show_history_rows(self, rows: list[dict]) -> None:
        end = wall_time()
        rows = [row for row in rows if end - self._range_s <= row["t"] <= end]
        lines = summary_lines(rows)
        empty = not lines
        warning = bool(self._hist_errors)
        message = (("Could not load battery history." if empty else
                    "Some history could not be loaded.") if warning else EMPTY_TEXT)
        self.hist_empty.set_label(message)
        self.hist_empty.set_visible(empty or warning)
        self.hist_chart_holder.set_visible(not empty)
        self.hist_legend.set_visible(not empty)
        self.hist_summary.set_visible(not empty)
        if empty:
            if self._chart is not None:
                self._chart.set_empty_text(message)
            for _row, lab in self._summary_rows.values():
                lab.set_label(DASH)
            return
        if self._chart is not None:
            series = {f: [(r["t"], r.get(f)) for r in rows]
                      for f in ("percent", "watts", "voltage")}
            gap_s = max(120.0, min(7200.0, self._range_s / 10))
            self._chart.set_data(end, self._range_s, series, build_bands(rows, gap_s))
        for field, (row, lab) in self._summary_rows.items():
            if field not in lines:
                row.set_visible(False)
                continue
            row.set_visible(True)
            latest, lo, hi = lines[field]
            lab.set_label(f"latest {_fmt_series(field, latest)} · "
                          f"min {_fmt_series(field, lo)} · max {_fmt_series(field, hi)}")
