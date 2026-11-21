# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Display formatting for telemetry values (GTK-free, unit-tested)."""
from __future__ import annotations

import math

DASH = "–"


def ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def num(v, digits: int = 0) -> str:
    return f"{v:.{digits}f}" if ok(v) else DASH


def pct(v, digits: int = 0) -> str:
    return f"{v:.{digits}f} %" if ok(v) else DASH


def _scaled(v, units: tuple[str, ...], base: float, digits: int,
            *, zero_digits_first: bool = True) -> str:
    if not ok(v):
        return DASH
    v = float(v)
    for idx, unit in enumerate(units):
        d = 0 if (idx == 0 and zero_digits_first) else digits
        if idx == len(units) - 1 or round(abs(v), d) < base:
            return f"{v:.{d}f} {unit}"
        v /= base
    return DASH  # pragma: no cover


class StickyUnit:
    """Unit formatter with hysteresis so values near a unit boundary stay steady."""

    def __init__(self, units: tuple[str, ...], base: float, digits: int = 1,
                 hysteresis: float = 1.25, *, zero_digits_first: bool = True) -> None:
        self.units = units
        self.base = base
        self.digits = digits
        self.hysteresis = hysteresis
        self.zero_digits_first = zero_digits_first
        self._idx: int | None = None

    def _digits_for(self, idx: int) -> int:
        return 0 if (idx == 0 and self.zero_digits_first) else self.digits

    def _pick(self, abs_v: float) -> int:
        for idx in range(len(self.units)):
            d = self._digits_for(idx)
            if idx == len(self.units) - 1 or round(abs_v, d) < self.base:
                return idx
            abs_v /= self.base
        return len(self.units) - 1  # pragma: no cover

    def __call__(self, v) -> str:
        if not ok(v):
            return DASH
        val = float(v)
        av = abs(val)
        if self._idx is None:
            self._idx = self._pick(av)
        else:
            idx = self._idx
            while idx < len(self.units) - 1:
                d = self._digits_for(idx)
                if round(av / (self.base ** idx), d) >= self.base:
                    idx += 1
                else:
                    break
            while idx > 0 and av < (self.base ** idx) / self.hysteresis:
                idx -= 1
            self._idx = idx
        idx = self._idx
        d = self._digits_for(idx)
        return f"{val / (self.base ** idx):.{d}f} {self.units[idx]}"


def sticky_bytes(digits: int = 1, hysteresis: float = 1.25) -> StickyUnit:
    return StickyUnit(("B", "KiB", "MiB", "GiB", "TiB"), 1024.0, digits, hysteresis)


def sticky_rate(digits: int = 1, hysteresis: float = 1.25) -> StickyUnit:
    return StickyUnit(("B/s", "kB/s", "MB/s", "GB/s"), 1000.0, digits, hysteresis)


def bytes_(v, digits: int = 1) -> str:
    """Binary units, as `free -h` and file managers' 'GiB' figures."""
    return _scaled(v, ("B", "KiB", "MiB", "GiB", "TiB"), 1024.0, digits)


def split_bytes(v, digits: int = 1) -> tuple[str, str]:
    s = bytes_(v, digits)
    return tuple(s.split(" ", 1)) if " " in s else (s, "")  # type: ignore[return-value]


def rate(v, digits: int = 1) -> str:
    """Bytes per second in decimal units (kB/s, MB/s), like network tools."""
    return _scaled(v, ("B/s", "kB/s", "MB/s", "GB/s"), 1000.0, digits)


def bits(v) -> str:
    if not ok(v):
        return DASH
    return _scaled(float(v) * 8, ("bit/s", "kbit/s", "Mbit/s", "Gbit/s"), 1000.0, 1)


def mhz(v) -> str:
    if not ok(v):
        return DASH
    v = float(v)
    return f"{v / 1000:.2f} GHz" if round(v) >= 1000 else f"{v:.0f} MHz"


def temp(v) -> str:
    return f"{v:.0f} °C" if ok(v) else DASH


def watts(v, digits: int = 1) -> str:
    return f"{v:.{digits}f} W" if ok(v) else DASH


def rpm(v) -> str:
    return f"{int(v):,}".replace(",", " ") + " rpm" if ok(v) else DASH


def duration(seconds) -> str:
    if not ok(seconds):
        return DASH
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d} d {h} h {m} min"
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def hours(h) -> str:
    if not ok(h) or h < 0:
        return DASH
    m = int(round(h * 60))
    if m == 0:
        return "0 min"
    if m < 60:
        return f"{m} min"
    return f"{m // 60} h {m % 60:02d} min"


def count(v) -> str:
    if not ok(v):
        return DASH
    v = float(v)
    if round(v / 1e3) >= 1000:
        return f"{v / 1e6:.1f} M"
    if round(v) >= 1e4:
        return f"{v / 1e3:.0f} k"
    return f"{v:.0f}"


def axis_rate(v) -> str:
    """Short axis label for a bytes/s value."""
    if v >= 1e9:
        return f"{v / 1e9:g} GB/s"
    if v >= 1e6:
        return f"{v / 1e6:g} MB/s"
    if v >= 1e3:
        return f"{v / 1e3:g} kB/s"
    return f"{v:g} B/s"


def axis_bytes(v) -> str:
    if v >= 1 << 30:
        return f"{v / (1 << 30):g} GiB"
    if v >= 1 << 20:
        return f"{v / (1 << 20):g} MiB"
    return f"{v / 1024:g} KiB" if v else "0"
