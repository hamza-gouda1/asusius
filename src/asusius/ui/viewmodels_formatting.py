# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Formatting and presentation helpers shared by UI pages."""

from __future__ import annotations

import math
import re

from . import prefs
from ..core import (
    BackendError,
    BackendUnavailable,
    PrivilegeRequired,
    Unsupported,
    ValidationError,
)

DASH = "–"

# -- formatting ---------------------------------------------------------------


def fmt_num(value: float | None, digits: int = 0) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return DASH
    return f"{value:.{digits}f}"


_NB_RE = re.compile(r"(?<=[\d%]) (?=[\d°%A-Za-zµ/])")


def nb(text: str) -> str:
    """No-break spaces inside number groups and between a number and its unit
    ("5 300 rpm", "12.4 °C", "42 %"), so a wrapping label never splits a reading across
    two lines. Spaces between words are left alone."""
    return _NB_RE.sub("\u00a0", text)


def norm_app_key(text: str) -> str:
    """One comparable form for an application id, desktop file id, StartupWMClass or
    executable name: lower case, no .desktop, only the last part of a reverse-DNS id,
    no separators ("app-gnome-asusius\\x2dgui" scope -> "asusiusgui")."""
    t = text.strip().lower()
    if t.endswith(".desktop"):
        t = t[:-8]
    if t.count(".") >= 2:
        t = t.rsplit(".", 1)[-1]
    return re.sub(r"[^a-z0-9]", "", t)


OWN_APP_ID = "io.github.hamzagouda.Asusius"
OWN_APP_KEYS = {norm_app_key(k) for k in (OWN_APP_ID, "asusius", "asusius-gui")}


def desktop_index(entries) -> dict[str, object]:
    """key -> app info for (desktop id, StartupWMClass, executable basename) triples,
    first entry wins. `entries` yields (info, desktop_id, wm_class, exec_line)."""
    out: dict[str, object] = {}
    for info, did, wm, exec_line in entries:
        keys = [did, wm or ""]
        first = (exec_line or "").split(" ")[0]
        keys.append(first.rsplit("/", 1)[-1])
        for k in keys:
            nk = norm_app_key(k)
            if nk and nk not in out:
                out[nk] = info
    return out


def signal_refusal(pid: int | None, own_pids, kind: str = "proc") -> str | None:
    """Why Stop / Force Stop must not signal this target (None when it may)."""
    if pid == 1:
        return "PID 1 is the init system: stopping it would take the session down"
    if pid is not None and pid in own_pids:
        return "This is Asusius itself"
    return None


def column_totals(cpu, mem_pct, swap_pct, drive_pct, gpu_pct, vram_pct) -> dict[str, str]:
    """Header second lines like Mission Center's ("4%"); '' while unknown."""

    def f(v):
        return "" if v is None else f"{v:.0f}%"

    return {
        "cpu": f(cpu),
        "mem": f(mem_pct),
        "swap": f(swap_pct),
        "disk": f(drive_pct),
        "gpu": f(gpu_pct),
        "vram": f(vram_pct),
    }


def fmt_rpm(value: int | None) -> str:
    return DASH if value is None else f"{int(value):,}".replace(",", " ")


def to_display_temp(celsius: float | None, unit: str | None = None) -> float | None:
    if celsius is None or (isinstance(celsius, float) and math.isnan(celsius)):
        return None
    u = (unit or prefs.get_temp_unit()).upper()
    return celsius * 9.0 / 5.0 + 32.0 if u == "F" else float(celsius)


def temp_unit_label(unit: str | None = None) -> str:
    u = (unit or prefs.get_temp_unit()).upper()
    return "°F" if u == "F" else "°C"


def temp_value(celsius: float | None, digits: int = 0, unit: str | None = None) -> str:
    v = to_display_temp(celsius, unit)
    return DASH if v is None else f"{v:.{digits}f}"


def fmt_temp(celsius: float | None, digits: int = 0, unit: str | None = None) -> str:
    v = to_display_temp(celsius, unit)
    if v is None:
        return DASH
    return nb(f"{v:.{digits}f} {temp_unit_label(unit)}")


def humanize_error(exc: BaseException) -> str:
    """One short, honest sentence for a toast."""
    msg = str(exc).strip() or type(exc).__name__
    if isinstance(exc, PrivilegeRequired):
        return f"Needs administrator rights ({msg})"
    if isinstance(exc, ValidationError):
        return f"Rejected: {msg}"
    if isinstance(exc, (BackendUnavailable, Unsupported)):
        return f"Not available: {msg}"
    if isinstance(exc, BackendError):
        return f"Failed: {msg}"
    return f"Error: {msg}"


# -- profiles ---------------------------------------------------------------------
