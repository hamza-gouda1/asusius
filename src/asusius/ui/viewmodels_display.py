# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Display mode parsing and monitor configuration viewmodels."""

from __future__ import annotations

from dataclasses import dataclass

from ..core import (
    ValidationError,
)

# -- display -----------------------------------------------------------------------------------


@dataclass
class DisplayMode:
    id: str
    width: int
    height: int
    refresh: float
    current: bool = False
    preferred: bool = False

    @property
    def label(self) -> str:
        return (
            f"{self.refresh:.0f} Hz"
            if abs(self.refresh - round(self.refresh)) < 0.05
            else f"{self.refresh:.2f} Hz"
        )


@dataclass
class Monitor:
    connector: str
    name: str
    builtin: bool
    modes: list[DisplayMode]
    vrr_supported: bool = False

    @property
    def current(self) -> DisplayMode | None:
        return next((m for m in self.modes if m.current), None)

    def refresh_options(self) -> list[DisplayMode]:
        """Modes at the current resolution, one per distinct refresh rate, fastest first."""
        cur = self.current
        if cur is None:
            return []
        seen: dict[int, DisplayMode] = {}
        for m in self.modes:
            if (m.width, m.height) != (cur.width, cur.height):
                continue
            key = round(m.refresh * 10)
            if key not in seen or m.current:
                seen[key] = m
        return sorted(seen.values(), key=lambda m: -m.refresh)


def parse_display_state(state) -> list[Monitor]:
    """Parse org.gnome.Mutter.DisplayConfig.GetCurrentState() output."""
    _serial, monitors, _logical, _props = state
    out = []
    for spec, modes, props in monitors:
        connector, vendor, product, _serialno = spec
        ms, vrr = [], False
        for mid, w, h, r, _ps, _scales, mp in modes:
            if mp.get("refresh-rate-mode") == "variable":
                vrr = True  # GNOME 46+ lists VRR as separate modes
                continue
            ms.append(
                DisplayMode(
                    mid,
                    int(w),
                    int(h),
                    float(r),
                    bool(mp.get("is-current")),
                    bool(mp.get("is-preferred")),
                )
            )
        name = props.get("display-name") or f"{vendor} {product}".strip()
        out.append(
            Monitor(
                connector,
                name,
                bool(props.get("is-builtin")),
                ms,
                vrr or bool(props.get("is-vrr-allowed", False)),
            )
        )
    return out


def build_apply_config(state, connector: str, mode_id: str):
    """Logical-monitor list for ApplyMonitorsConfig keeping everything but one mode."""
    _serial, monitors, logical, _props = state
    current_mode = {}
    for spec, modes, _p in monitors:
        cur = next((m[0] for m in modes if m[6].get("is-current")), None)
        current_mode[spec[0]] = cur
    if connector not in current_mode:
        raise ValidationError(f"unknown monitor {connector}")
    out = []
    for x, y, scale, transform, primary, mons, _lp in logical:
        entries = []
        for spec in mons:
            conn = spec[0]
            mid = mode_id if conn == connector else current_mode.get(conn)
            if mid is None:
                raise ValidationError(f"monitor {conn} has no current mode")
            entries.append((conn, mid, {}))
        out.append((int(x), int(y), float(scale), int(transform), bool(primary), entries))
    return out
