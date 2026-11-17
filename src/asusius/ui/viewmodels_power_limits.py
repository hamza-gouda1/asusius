# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Power limit presentation and value snapping helpers."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..core import (
    PowerLimit,
)

# -- power limits -----------------------------------------------------------------------

LIMIT_INFO: dict[str, tuple[str, str]] = {
    "ppt_pl1_spl": ("CPU sustained power", "Long-term package limit (PL1)"),
    "ppt_pl2_sppt": ("CPU boost power", "Short-term package limit (PL2)"),
    "ppt_pl3_fppt": ("CPU peak power", "Very short boost limit"),
    "ppt_apu_sppt": ("APU boost power", "Short-term APU limit"),
    "ppt_platform_sppt": ("Platform boost power", "Short-term platform limit"),
    "nv_dynamic_boost": ("GPU dynamic boost", "Extra power shifted from CPU to GPU"),
    "nv_temp_target": ("GPU temperature target", "dGPU throttles above this"),
    "nv_tgp": ("GPU extra TGP", "Added on top of the base TGP"),
}


@dataclass
class LimitRow:
    name: str
    title: str
    subtitle: str
    value: int
    min: int
    max: int
    step: int
    default: int | None
    unit: str


@dataclass
class PowerLimitsView:
    available: bool
    reason: str = ""
    rows: list[LimitRow] = field(default_factory=list)


def explain_unavailable(cap_info: dict | None) -> str:
    """Pick the most useful reason from a capabilities() entry."""
    if not cap_info:
        return "No backend reported this feature."
    reasons = [r for r in (cap_info.get("unavailable") or {}).values() if r]
    for r in reasons:  # D-Bus/asusd reasons describe the firmware state best
        if "PPT" in r or "EnablePptGroup" in r or "firmware" in r:
            return r[0].upper() + r[1:]
    return (reasons[0][0].upper() + reasons[0][1:]) if reasons else "Unavailable."


def power_limits_view(cap_info: dict | None, limits: list[PowerLimit] | None) -> PowerLimitsView:
    if not cap_info or not cap_info.get("available") or not limits:
        return PowerLimitsView(False, explain_unavailable(cap_info))
    rows = []
    for lim in limits:
        if not lim.bounded or lim.current is None:
            continue
        title, sub = LIMIT_INFO.get(lim.name, (lim.display_name or lim.name, lim.name))
        unit = "°C" if "temp" in lim.name else "W"
        rows.append(
            LimitRow(
                lim.name,
                title,
                sub,
                lim.current,
                lim.min,
                lim.max,  # type: ignore
                max(1, lim.increment),
                lim.default,
                unit,
            )
        )
    if not rows:
        return PowerLimitsView(False, "Firmware exposes the limits but no readable values.")
    order = list(LIMIT_INFO)
    rows.sort(key=lambda r: order.index(r.name) if r.name in order else len(order))
    return PowerLimitsView(True, "", rows)


def snap_to_step(value: float, lo: int, hi: int, step: int) -> int:
    step = max(1, step)
    v = lo + round((value - lo) / step) * step
    return int(max(lo, min(hi, v)))
