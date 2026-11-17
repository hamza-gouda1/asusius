# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Battery charge-limit validation and health labels."""

from __future__ import annotations

from ..core import (
    ValidationError,
)

# -- battery -----------------------------------------------------------------------------

CHARGE_PRESETS = (60, 80, 100)
CHARGE_RANGE = (20, 100)


def validate_charge_limit(value: float) -> int:
    v = int(round(value))
    lo, hi = CHARGE_RANGE
    if not lo <= v <= hi:
        raise ValidationError(f"charge limit must be {lo}-{hi}%")
    return v


def charge_limit_hint(value: int) -> str:
    if value <= 60:
        return "Best for battery lifespan when mostly plugged in"
    if value <= 85:
        return "Good balance of lifespan and runtime"
    return "Full capacity, faster wear when kept at 100%"


def health_label(health_pct: float) -> str:
    if health_pct >= 90:
        return "Excellent"
    if health_pct >= 80:
        return "Good"
    if health_pct >= 65:
        return "Fair"
    return "Worn"
