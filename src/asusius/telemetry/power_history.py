# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""History normalisation and the local voltage log for the power page.

`normalize_history` turns raw UPower points (or point-like objects) into sorted,
deduplicated, finite-only HistoryPoint rows: a None field is a chart gap, never a
fabricated zero, and gaps are never interpolated across. `LocalVoltageStore` logs
the voltage a battery reports while discharging, so the voltage graph has data on
systems where UPower keeps no voltage history of its own (and where the history it
does keep is lost on every reboot). No gi, no D-Bus here.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .upower import state_text

MAX_POINTS = 2000  # newest points kept per device: bounded persistence
SERIES_FIELD = {"charge": "percent", "rate": "watts", "voltage": "voltage"}


@dataclass
class HistoryPoint:
    """One history row. A None field is missing data: charts draw a gap and
    summaries show "-", never a zero. state is the raw UPower state text."""
    t: float  # UNIX seconds
    percent: float | None = None
    watts: float | None = None
    voltage: float | None = None
    state: str | None = None


def normalize_history(points: Any, series: str = "charge") -> list[HistoryPoint]:
    """Sort by t, drop duplicate timestamps (the first seen wins), keep gaps as gaps.

    Accepts raw UPower triples ((t, value, state) and older (t, value) pairs),
    UpowerHistoryPoint / HistoryPoint objects and dicts with t/percent/watts/voltage/
    state keys (a "value" key is mapped onto the field named by series: "charge" ->
    percent, "rate" -> watts, "voltage" -> voltage). Non-finite values become None;
    rows without a finite t are dropped. No interpolation, no extrapolation.
    """
    field = SERIES_FIELD.get(series)
    if field is None:
        raise ValueError(f"series must be one of {sorted(SERIES_FIELD)}, not {series!r}")
    rows: list[HistoryPoint] = []
    for p in points or ():
        if isinstance(p, (tuple, list, dict)) or hasattr(p, "t"):
            unpacked = _unpack(p, field)
        else:
            continue
        if unpacked is None:
            continue
        t, vals, state = unpacked
        t = _finite(t)
        if t is None:
            continue
        percent = _finite(vals["percent"])
        voltage = _finite(vals["voltage"])
        watts = _finite(vals["watts"])
        if watts is not None:
            if state == "charging":
                watts = -abs(watts)
            elif state == "discharging":
                watts = abs(watts)
            elif watts != 0 and state not in (None, "unknown"):
                watts = None
        rows.append(HistoryPoint(t=t,
                                 percent=percent if percent is None or 0 <= percent <= 100 else None,
                                 watts=watts,
                                 voltage=voltage if voltage is None or voltage > 0 else None,
                                 state=state if isinstance(state, str) else None))
    rows.sort(key=lambda r: r.t)  # stable: equal ts keep input order, first wins
    out: list[HistoryPoint] = []
    for r in rows:
        if out and out[-1].t == r.t:
            continue  # duplicate timestamp: the first row seen for that t stays
        out.append(r)
    return out


def _state_of(state: Any) -> str | None:
    """Raw state id (int) -> text; text passes through; anything else is None."""
    if state is None or isinstance(state, str):
        return state
    try:
        return state_text(int(state))
    except (TypeError, ValueError):
        return None


def _unpack(p: Any, field: str) -> tuple[Any, dict[str, Any], Any] | None:
    """One accepted shape -> (t, {percent, watts, voltage}, state)."""
    if isinstance(p, (tuple, list)):
        if len(p) < 2:
            return None
        vals = {"percent": None, "watts": None, "voltage": None}
        vals[field] = p[1]
        return p[0], vals, _state_of(p[2] if len(p) > 2 else None)
    if isinstance(p, dict):
        vals = {k: p.get(k) for k in ("percent", "watts", "voltage")}
        if vals.get(field) is None and p.get("value") is not None:
            vals[field] = p["value"]
        return p.get("t"), vals, _state_of(p.get("state"))
    vals = {"percent": getattr(p, "percent", None), "watts": getattr(p, "watts", None),
            "voltage": getattr(p, "voltage", None)}
    if vals.get(field) is None and hasattr(p, "value"):  # UpowerHistoryPoint
        vals[field] = getattr(p, "value", None)
    return getattr(p, "t", None), vals, _state_of(getattr(p, "state", None))


def _finite(v: Any) -> float | None:
    """A finite float, or None (missing, non-numeric, NaN, inf)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def device_key(native_path: str, model: str | None) -> str:
    """Stable per-device file key: a hash of native_path and model only. Serials
    never enter the key or the stored content."""
    raw = f"{native_path or ''}\x1f{model or ''}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:16]


class LocalVoltageStore:
    """Per-device voltage history under $XDG_STATE_HOME/asusius/voltage/.

    One JSON array file per device, named by device_key(native_path, model); each
    entry is {"t", "voltage", "percent", "state"} and no serial is ever stored.
    Writes are atomic (tmp + os.replace) under an exclusive fcntl lock on a
    per-device .lock file. A corrupt file is treated as empty and rebuilt by the
    next append: history older than the corruption is lost rather than wrong.

    Sampling trigger: call record() each tick with whatever the device reports; a
    point is appended only when voltage and percent are both finite and the raw
    state is "discharging" (on AC the draw says nothing about the battery). When a
    device is absent the caller stops calling record() and nothing is read, written
    or created, so there is no file churn for devices that never report.
    """

    def __init__(self, base_dir: str | os.PathLike | None = None,
                 clock: Callable[[], float] | None = None,
                 max_points: int = MAX_POINTS) -> None:
        self._base = Path(base_dir) if base_dir else _default_base()
        self._clock = clock or time.time
        self._max = max(1, int(max_points))

    def record(self, native_path: str, model: str | None, voltage: float | None,
               percent: float | None, state: str | None = None,
               t: float | None = None) -> bool:
        """Append one point when it passes the sampling trigger; True when written."""
        v, p = _finite(voltage), _finite(percent)
        if v is None or p is None or state != "discharging":
            return False
        t = self._clock() if t is None else float(t)
        if not math.isfinite(t):
            return False
        path = self._path(native_path, model)
        with _locked(path):  # read, append, cap and replace as one critical section
            points = _load_json(path)
            points.append({"t": t, "voltage": v, "percent": p, "state": state})
            pts = normalize_history(points, series="voltage")
            if len(pts) > self._max:
                pts = pts[-self._max:]  # newest kept
            rows = [{"t": x.t, "voltage": x.voltage, "percent": x.percent,
                     "state": x.state} for x in pts]
            return _write_json(path, rows)

    def load(self, native_path: str, model: str | None) -> list[HistoryPoint]:
        """Stored points for one device, oldest first; [] when absent or corrupt."""
        path = self._path(native_path, model)
        if not path.exists():
            return []  # absent: no lock file, no directory, no churn
        with _locked(path):
            points = _load_json(path)
        return normalize_history(points, series="voltage")

    def path_for(self, native_path: str, model: str | None) -> Path:
        """The file this device maps to (tests and diagnostics)."""
        return self._path(native_path, model)

    def _path(self, native_path: str, model: str | None) -> Path:
        return self._base / f"{device_key(native_path, model)}.json"


def _default_base() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "asusius" / "voltage"


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.with_name(path.name + ".lock"),
                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("voltage lock is not a regular file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _load_json(path: Path) -> list[dict]:
    """The stored array, or [] (corrupt, truncated or not a list): rebuild, not fail."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd) as f:
            meta = os.fstat(f.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_size > 1024 * 1024:
                return []
            data = json.loads(f.read(1024 * 1024 + 1))
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _write_json(path: Path, rows: list[dict]) -> bool:
    """Atomic replace; a failed write keeps the previous generation and returns False."""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8",
                                         prefix=".voltage-", delete=False) as f:
            tmp = f.name
            json.dump(rows, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return False
    return True
