# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Fan curve editing geometry and conversion helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..core import (
    FanCurve,
    FanPoint,
)

# -- fan curve editor -----------------------------------------------------------------------


def pwm_to_pct(pwm: float) -> float:
    return pwm * 100.0 / FanCurve.PWM_RANGE[1]


def pct_to_pwm(pct: float) -> int:
    return int(round(max(0.0, min(100.0, pct)) * FanCurve.PWM_RANGE[1] / 100.0))


class FanCurveModel:
    """Editable 8-point curve. Every drag is clamped so the curve stays valid."""

    def __init__(self, curve: FanCurve) -> None:
        self.fan = curve.fan
        self.original = [(p.temp, p.pwm) for p in curve.points]
        self.points: list[tuple[int, int]] = list(self.original)
        self.enabled = curve.enabled
        self.original_enabled = curve.enabled

    @property
    def dirty(self) -> bool:
        return self.points != self.original or self.enabled != self.original_enabled

    def revert(self) -> None:
        self.points = list(self.original)
        self.enabled = self.original_enabled

    def commit(self) -> None:
        self.original = list(self.points)
        self.original_enabled = self.enabled

    def limits_for(self, i: int) -> tuple[int, int, int, int]:
        """(temp_lo, temp_hi, pwm_lo, pwm_hi) allowed for point i."""
        lo_t, hi_t = FanCurve.TEMP_RANGE
        lo_p, hi_p = FanCurve.PWM_RANGE
        n = len(self.points)
        # leave room for the remaining points on each side (temps strictly increase)
        t_lo = self.points[i - 1][0] + 1 if i > 0 else lo_t
        t_hi = self.points[i + 1][0] - 1 if i < n - 1 else hi_t
        t_lo = max(t_lo, lo_t + i)
        t_hi = min(t_hi, hi_t - (n - 1 - i))
        p_lo = self.points[i - 1][1] if i > 0 else lo_p
        p_hi = self.points[i + 1][1] if i < n - 1 else hi_p
        return t_lo, t_hi, p_lo, p_hi

    def drag(self, i: int, temp: float, pwm: float) -> tuple[int, int]:
        t_lo, t_hi, p_lo, p_hi = self.limits_for(i)
        t = int(round(max(t_lo, min(t_hi, temp))))
        p = int(round(max(p_lo, min(p_hi, pwm))))
        self.points[i] = (t, p)
        return self.points[i]

    def nearest(
        self, temp: float, pwm: float, max_dist: float, scale: tuple[float, float] = (1.0, 1.0)
    ) -> int | None:
        """Index of the point closest to (temp, pwm) in scaled units, within max_dist."""
        best, best_d = None, max_dist
        for i, (t, p) in enumerate(self.points):
            d = math.hypot((t - temp) * scale[0], (p - pwm) * scale[1])
            if d <= best_d:
                best, best_d = i, d
        return best

    def to_curve(self) -> FanCurve:
        return FanCurve(self.fan, [FanPoint(t, p) for t, p in self.points], self.enabled).validate()


@dataclass(frozen=True)
class CurveGeometry:
    """Maps curve data (temp, pwm) to canvas pixels and back."""

    width: float
    height: float
    left: float = 44.0
    right: float = 16.0
    top: float = 16.0
    bottom: float = 30.0

    @property
    def plot_w(self) -> float:
        return max(1.0, self.width - self.left - self.right)

    @property
    def plot_h(self) -> float:
        return max(1.0, self.height - self.top - self.bottom)

    def to_px(self, temp: float, pwm: float) -> tuple[float, float]:
        lo_t, hi_t = FanCurve.TEMP_RANGE
        x = self.left + (temp - lo_t) / (hi_t - lo_t) * self.plot_w
        y = self.top + (1 - pwm / FanCurve.PWM_RANGE[1]) * self.plot_h
        return x, y

    def to_data(self, x: float, y: float) -> tuple[float, float]:
        lo_t, hi_t = FanCurve.TEMP_RANGE
        temp = lo_t + (x - self.left) / self.plot_w * (hi_t - lo_t)
        pwm = (1 - (y - self.top) / self.plot_h) * FanCurve.PWM_RANGE[1]
        return temp, pwm

    @property
    def scale(self) -> tuple[float, float]:
        """Pixels per unit of (temp, pwm), for hit testing."""
        lo_t, hi_t = FanCurve.TEMP_RANGE
        return self.plot_w / (hi_t - lo_t), self.plot_h / FanCurve.PWM_RANGE[1]


def bubble_rect(
    x: float, y: float, tw: float, th: float, w: float, h: float, left: float, right: float
) -> tuple[float, float, float, float]:
    """Where the fan-curve readout bubble ("62° · 62%") goes for a point at (x, y) on a
    w×h canvas: centred above the point (below it when there is no room), kept over the
    plot horizontally, and never outside the canvas, so it is not clipped at an edge."""
    bw, bh = tw + 16.0, th + 8.0
    bx = min(max(x - bw / 2, left), w - right - bw)
    bx = max(2.0, min(bx, w - bw - 2.0))
    by = y - th - 22.0
    if by <= 2.0:
        by = y + 14.0
    by = max(2.0, min(by, h - bh - 2.0))
    return bx, by, bw, bh
