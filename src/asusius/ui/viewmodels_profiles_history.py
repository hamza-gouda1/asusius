# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Profile labels and telemetry history viewmodels."""

from __future__ import annotations

from collections import deque

from ..core import (
    Profile,
)

PROFILE_INFO: dict[Profile, tuple[str, str, str]] = {
    Profile.QUIET: ("Quiet", "Cool and silent, lowest power", "power-profile-power-saver-symbolic"),
    Profile.BALANCED: (
        "Balanced",
        "Everyday mix of speed and noise",
        "power-profile-balanced-symbolic",
    ),
    Profile.PERFORMANCE: (
        "Performance",
        "Full power, fans work harder",
        "power-profile-performance-symbolic",
    ),
}
PROFILE_ORDER = [Profile.QUIET, Profile.BALANCED, Profile.PERFORMANCE]


# -- telemetry history ---------------------------------------------------------------


class History:
    """Time-windowed samples for a sparkline."""

    def __init__(self, window_s: float = 120.0, maxlen: int = 1024) -> None:
        self.window_s = window_s
        self._data: deque[tuple[float, float]] = deque(maxlen=maxlen)

    def push(self, t: float, value: float | None) -> None:
        if value is None:
            return
        if self._data and t < self._data[-1][0]:
            return  # clocks never run backwards here
        self._data.append((t, float(value)))
        self._trim(t)

    def _trim(self, now: float) -> None:
        while self._data and now - self._data[0][0] > self.window_s:
            self._data.popleft()

    def __len__(self) -> int:
        return len(self._data)

    @property
    def last(self) -> float | None:
        return self._data[-1][1] if self._data else None

    def samples(self) -> list[tuple[float, float]]:
        return list(self._data)

    def normalized(self, now: float) -> list[tuple[float, float]]:
        """(x, v) with x in [0, 1] across the window ending at `now`."""
        start = now - self.window_s
        return [((t - start) / self.window_s, v) for t, v in self._data if t >= start]

    def bounds(self, floor: float | None = 0.0, min_span: float = 10.0) -> tuple[float, float]:
        """Y range with headroom; at least `min_span` tall so flat lines stay calm."""
        if not self._data:
            return (floor or 0.0, (floor or 0.0) + min_span)
        vals = [v for _, v in self._data]
        lo, hi = min(vals), max(vals)
        if hi - lo < min_span:
            mid = (hi + lo) / 2
            lo, hi = mid - min_span / 2, mid + min_span / 2
        pad = (hi - lo) * 0.12
        lo, hi = lo - pad, hi + pad
        if floor is not None and lo < floor:
            hi += floor - lo
            lo = floor
        return lo, hi


def smooth_values(points: list[tuple[float, float]], radius: int = 1) -> list[tuple[float, float]]:
    """Centred moving average over values (keeps x); calms integer sensor steps."""
    if radius <= 0 or len(points) < 3:
        return list(points)
    out = []
    n = len(points)
    for i, (x, _v) in enumerate(points):
        lo, hi = max(0, i - radius), min(n, i + radius + 1)
        out.append((x, sum(v for _, v in points[lo:hi]) / (hi - lo)))
    return out


def smooth_path(points: list[tuple[float, float]]) -> list[tuple[float, ...]]:
    """Catmull-Rom through points -> cubic Bezier segments (x1,y1,x2,y2,x,y)."""
    segs = []
    n = len(points)
    for i in range(n - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1, p2 = points[i], points[i + 1]
        p3 = points[i + 2] if i + 2 < n else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        segs.append((*c1, *c2, *p2))
    return segs


def battery_icon(percent: int | None, status: str | None) -> str:
    if percent is None:
        return "ac-adapter-symbolic"
    level = max(0, min(100, int(round(percent / 10.0)) * 10))
    st = (status or "").lower()
    if st == "full" or (level == 100 and st in ("charging", "not charging")):
        return "battery-level-100-charged-symbolic"
    suffix = "-charging" if st in ("charging", "not charging") else ""
    return f"battery-level-{level}{suffix}-symbolic"


def power_state_text(status: str | None, percent: int | None) -> str:
    if percent is None:
        return "AC"
    st = (status or "").lower()
    if st in ("charging",):
        return f"{percent}% · charging"
    if st in ("not charging", "full"):
        return f"{percent}% · plugged in"
    return f"{percent}%"
