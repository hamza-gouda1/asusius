# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""UPower devices and history from org.freedesktop.UPower (system bus), on demand.

A separate client, never on the sampler path: power.py stays D-Bus-free. gi is
imported only inside GioUpowerTransport, so importing this module needs neither
PyGObject nor a running D-Bus. UpowerTransport also supports in-memory tests.
Inventory reports a status string. History distinguishes a missing series
from a failed request.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

UPOWER = "org.freedesktop.UPower"
UPOWER_PATH = "/org/freedesktop/UPower"
DEVICE_IFACE = "org.freedesktop.UPower.Device"
DBUS_IFACE = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

CALL_TIMEOUT_MS = 2000  # per D-Bus call
CONNECT_TIMEOUT_S = 3.0  # Gio has no connect timeout, so the attempt is abandoned
MAX_TIMESPAN_S = 604800  # 7 days: the farthest back GetHistory is asked for
MIN_RESOLUTION_S = 30

# The composite DisplayDevice is identified by its object path.
KIND_BY_ID: dict[int, str] = {
    0: "unknown", 1: "line_power", 2: "laptop", 3: "ups", 11: "unknown",
    **dict.fromkeys((4, *range(5, 11), *range(12, 29)), "peripheral"),
}
KIND_LABELS = {"laptop": "Laptop battery", "ups": "UPS", "display": "Display device",
               "line_power": "AC adapter", "peripheral": "Peripheral",
               "unknown": "Unknown device"}

# UpDeviceState ids -> the raw state strings used across the app (the page maps
# them to readable text; the data layer passes them through untouched).
STATE_BY_ID: dict[int, str] = {
    0: "unknown", 1: "charging", 2: "discharging", 3: "empty", 4: "fully-charged",
    5: "pending-charge", 6: "pending-discharge",
}

HISTORY_SERIES = ("charge", "rate", "voltage")
DISPLAY_DEVICE = UPOWER_PATH + "/devices/DisplayDevice"
COARSE_LEVELS = {3: "Low", 4: "Critical", 6: "Normal", 7: "High", 8: "Full"}


def state_text(state: int) -> str:
    """Raw UPower state id -> the state string; unknown ids read as "unknown"."""
    return STATE_BY_ID.get(int(state), "unknown")


def kind_label(kind: str) -> str:
    """Collapsed kind -> a readable label for lists and tooltips."""
    return KIND_LABELS.get(kind, KIND_LABELS["unknown"])


class UpowerError(RuntimeError):
    """A UPower call failed (timeout, transport error, unreadable reply)."""


class UpowerUnavailable(UpowerError):
    """UPower, PyGObject or the system bus is not there at all."""


class UpowerTransport(Protocol):
    """The D-Bus seam (mirrors backends.dbus.Transport): prod uses Gio, tests fake
    it. Implementations own their own timeouts and error wrapping."""

    def name_has_owner(self, name: str) -> bool: ...

    def call(self, dest: str, path: str, iface: str, method: str,
             sig: str | None = None, args: tuple = ()) -> Any: ...

    def get_all(self, dest: str, path: str, iface: str) -> dict[str, Any]: ...


class GioUpowerTransport:
    """Default transport: synchronous Gio calls on the system bus, off the UI thread.

    PyGObject is imported on first use, one connection is reused, connects are
    bounded by CONNECT_TIMEOUT_S (Gio has no connect timeout, so the attempt runs on
    a daemon thread and is abandoned on expiry) and every call by CALL_TIMEOUT_MS.
    """

    def __init__(self) -> None:
        self._gi: tuple | None = None
        self._conn: Any | None = None
        self._connection_lock = threading.Lock()

    def _mods(self) -> tuple:
        if self._gi is None:
            try:
                import gi
                gi.require_version("Gio", "2.0")
                from gi.repository import Gio, GLib
            except (ImportError, ValueError) as exc:
                raise UpowerUnavailable(f"PyGObject/Gio not available: {exc}") from None
            self._gi = (Gio, GLib)
        return self._gi

    def _connection(self):
        with self._connection_lock:
            return self._connect()

    def _connect(self):
        Gio, _GLib = self._mods()
        if self._conn is not None and getattr(self._conn, "is_closed", lambda: False)():
            self._conn = None
        if self._conn is None:
            box, err, done = [], [], threading.Event()
            cancel = Gio.Cancellable()

            def connect() -> None:
                try:
                    box.append(Gio.bus_get_sync(Gio.BusType.SYSTEM, cancel))
                except BaseException as exc:  # GLib.Error or anything else
                    err.append(exc)
                finally:
                    done.set()

            threading.Thread(target=connect, daemon=True,
                             name="asusius-upower-connect").start()
            if not done.wait(CONNECT_TIMEOUT_S):
                cancel.cancel()
                raise UpowerUnavailable(
                    f"system bus connect exceeded {CONNECT_TIMEOUT_S:g}s") from None
            if err:
                raise UpowerUnavailable(f"system bus unavailable: {_msg(err[0])}") from None
            self._conn = box[0]
        return self._conn

    @staticmethod
    def _wrap(exc: BaseException) -> UpowerError:
        msg = _msg(exc)
        if any(k in msg for k in ("ServiceUnknown", "NameHasNoOwner", "UnknownObject",
                                  "UnknownMethod", "UnknownProperty", "UnknownInterface")):
            return UpowerUnavailable(msg)
        if "Timeout" in msg or "timed out" in msg.lower():
            return UpowerError(f"D-Bus timeout: {msg}")
        return UpowerError(msg)

    def call(self, dest: str, path: str, iface: str, method: str,
             sig: str | None = None, args: tuple = ()) -> Any:
        Gio, GLib = self._mods()
        params = GLib.Variant(sig, args) if sig else None
        try:
            res = self._connection().call_sync(
                dest, path, iface, method, params, None, Gio.DBusCallFlags.NO_AUTO_START,
                CALL_TIMEOUT_MS, None)
        except Exception as exc:
            raise self._wrap(exc) from None
        return res.unpack() if res is not None else None

    def get_all(self, dest: str, path: str, iface: str) -> dict[str, Any]:
        Gio, GLib = self._mods()
        try:
            res = self._connection().call_sync(
                dest, path, PROPS_IFACE, "GetAll", GLib.Variant("(s)", (iface,)), None,
                Gio.DBusCallFlags.NO_AUTO_START, CALL_TIMEOUT_MS, None)
        except Exception as exc:
            raise self._wrap(exc) from None
        return res.unpack()[0] if res is not None else {}

    def name_has_owner(self, name: str) -> bool:
        Gio, GLib = self._mods()
        try:
            res = self._connection().call_sync(
                DBUS_IFACE, DBUS_PATH, DBUS_IFACE, "NameHasOwner",
                GLib.Variant("(s)", (name,)), None, Gio.DBusCallFlags.NONE,
                CALL_TIMEOUT_MS, None)
        except Exception as exc:
            raise self._wrap(exc) from None
        return bool(res.unpack()[0]) if res is not None else False


@dataclass
class UpowerDeviceInfo:
    """One device. The serial is used in memory to distinguish physical batteries."""
    object_path: str
    native_path: str = ""  # sysfs dir as UPower reports it; "" when it gives none
    kind: str = "unknown"
    present: bool | None = None
    percent: float | None = None  # 0-100, genuine 0 kept distinct from None
    state: str | None = None      # raw state text ("charging", "discharging", ...)
    watts: float | None = None    # magnitude >= 0
    voltage: float | None = None  # V
    energy: float | None = None         # Wh
    energy_full: float | None = None    # Wh
    energy_design: float | None = None  # Wh
    cycles: int | None = None
    type_text: str = ""
    model: str | None = None
    vendor: str | None = None
    serial: str | None = field(default=None, repr=False)
    coarse_level: str | None = None


@dataclass
class UpowerHistoryPoint:
    """One raw history row as UPower returned it; normalize_history refines it."""
    t: int      # UNIX seconds
    value: float
    state: int  # raw state id (see state_text); 0 ("unknown") for pair-shaped rows


class UpowerClient:
    """Inventory and history through a transport, with failures as status text.

    .status always holds the last outcome and .devices the last successful
    inventory (the cache between refreshes). One pass per call, no retries: the
    caller decides when to ask again.
    """

    def __init__(self, transport: UpowerTransport | None = None) -> None:
        self.t: UpowerTransport = transport or GioUpowerTransport()
        self.status = "UPower not queried yet"
        self.devices: list[UpowerDeviceInfo] = []

    def refresh_inventory(self) -> tuple[list[UpowerDeviceInfo], str]:
        """All UPower devices (DisplayDevice included; the page filters it out)."""
        self.devices = []
        try:
            if not self.t.name_has_owner(UPOWER):
                self.status = "UPower not available"
                return [], self.status
            (paths,) = self.t.call(UPOWER, UPOWER_PATH, UPOWER, "EnumerateDevices") \
                or ([],)
        except Exception as exc:
            self.status = _status_for(exc)
            return [], self.status
        unreadable = 0
        for path in paths or []:
            try:
                props = self.t.get_all(UPOWER, path, DEVICE_IFACE)
            except Exception:
                unreadable += 1
                continue
            if not isinstance(props, dict) or not props:
                unreadable += 1  # a device with no readable properties is no row
                continue
            self.devices.append(_device_from_props(path, props))
        n = len(self.devices)
        self.status = f"UPower: {n} device{'s' if n != 1 else ''}"
        if unreadable:
            self.status += f", {unreadable} unreadable"
        return list(self.devices), self.status

    def get_history(self, object_path: str, series: str, timespan_s: float,
                    resolution: int) -> list[UpowerHistoryPoint]:
        """Request a bounded range and approximate point count.

        An unsupported or empty series returns no rows. Transport failures raise.
        """
        if series not in HISTORY_SERIES:
            raise ValueError(f"series must be one of {HISTORY_SERIES}, not {series!r}")
        timespan = max(1, min(int(timespan_s), MAX_TIMESPAN_S))
        resolution = min(600, max(MIN_RESOLUTION_S, int(resolution)))
        try:
            if not self.t.name_has_owner(UPOWER):
                raise UpowerUnavailable("UPower not available")
            (rows,) = self.t.call(UPOWER, object_path, DEVICE_IFACE, "GetHistory",
                                  "(suu)", (series, timespan, resolution)) or ([],)
        except UpowerError as exc:
            if "device has no history" in str(exc).lower():
                return []
            raise
        return _history_points(rows)


def _msg(exc: BaseException) -> str:
    return getattr(exc, "message", None) or str(exc)


def _status_for(exc: Exception) -> str:
    if isinstance(exc, UpowerUnavailable):
        return "UPower not available"
    return f"UPower query failed: {_msg(exc)}"


def _num(v: Any) -> float | None:
    """A finite float, or None (absent, non-numeric, NaN, inf)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any, default: int | None = None) -> int | None:
    if v is None or isinstance(v, bool):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None  # "" reads as unset


def _percent(v: Any) -> float | None:
    """0-100; UPower's -1 "unknown" sentinel (and anything negative) is None."""
    f = _num(v)
    return None if f is None or f < 0 else min(f, 100.0)


def _nonneg(v: Any) -> float | None:
    f = _num(v)
    return None if f is None or f < 0 else f


def _magnitude(v: Any) -> float | None:
    """A rate magnitude, sign folded like power.py does for power_now."""
    f = _num(v)
    return None if f is None else abs(f)


def _count(v: Any) -> int | None:
    i = _int(v)
    return None if i is None or i < 0 else i  # UPower uses -1 for unknown


def _device_from_props(object_path: str, props: dict[str, Any]) -> UpowerDeviceInfo:
    kind = KIND_BY_ID.get(_int(props.get("Type"), 0), "unknown")
    if object_path == DISPLAY_DEVICE:
        kind = "display"
    elif kind == "laptop" and props.get("PowerSupply") is False:
        kind = "peripheral"
    sid = _int(props.get("State"), None)
    battery_level = _int(props.get("BatteryLevel"), None)
    return UpowerDeviceInfo(
        object_path=object_path,
        native_path=_str(props.get("NativePath")) or "",
        kind=kind,
        present=_bool(props.get("IsPresent")),
        percent=_percent(props.get("Percentage")) if battery_level in (None, 1) else None,
        state=None if sid is None else state_text(sid),
        watts=_magnitude(props.get("EnergyRate")),
        voltage=_nonneg(props.get("Voltage")) or None,
        energy=_nonneg(props.get("Energy")),
        energy_full=_nonneg(props.get("EnergyFull")),
        energy_design=_nonneg(props.get("EnergyFullDesign")),
        cycles=_count(props.get("ChargeCycles", props.get("CycleCount"))),
        type_text=kind_label(kind),
        model=_str(props.get("Model")),
        vendor=_str(props.get("Vendor")),
        serial=_str(props.get("Serial")),
        coarse_level=COARSE_LEVELS.get(battery_level),
    )


def _history_points(rows: Any) -> list[UpowerHistoryPoint]:
    """(t, value, state) triples, (t, value) pairs from older daemons, or junk."""
    out: list[UpowerHistoryPoint] = []
    for row in rows or ():
        try:
            if len(row) >= 3:
                out.append(UpowerHistoryPoint(t=int(row[0]), value=float(row[1]),
                                              state=int(row[2])))
            elif len(row) == 2:
                out.append(UpowerHistoryPoint(t=int(row[0]), value=float(row[1]),
                                              state=0))
        except (TypeError, ValueError, IndexError):
            continue
    return out
