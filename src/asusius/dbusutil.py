# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Small synchronous D-Bus helpers over Gio, for worker threads (updates, services).

Every call has a timeout. `interactive=True` sets ALLOW_INTERACTIVE_AUTHORIZATION so a
system service may ask polkit, which shows the desktop's own authentication dialog; this
module never escalates privileges itself.
"""
from __future__ import annotations

import threading
from typing import Any

CONNECT_TIMEOUT_S = 3.0
_BUS_LOCK = threading.Lock()
_IN_FLIGHT: dict[str, tuple[threading.Event, list, list, Any, list[bool]]] = {}
_LATE_BUS: dict[str, Any] = {}


class DBusUnavailable(RuntimeError):
    """PyGObject or the bus is missing."""


def _gio():
    try:
        import gi  # noqa: F401
        from gi.repository import Gio, GLib
    except (ImportError, ValueError) as exc:  # pragma: no cover - depends on the system
        raise DBusUnavailable(f"PyGObject unavailable: {exc}") from None
    return Gio, GLib


def connect_bus_sync(kind: str = "system", timeout_s: float = CONNECT_TIMEOUT_S):
    """Connect to the shared Gio bus with a bounded wait and no duplicate connect threads."""
    Gio, GLib = _gio()
    bus_type = Gio.BusType.SYSTEM if kind == "system" else Gio.BusType.SESSION
    with _BUS_LOCK:
        late = _LATE_BUS.pop(kind, None)
        if late is not None and not getattr(late, "is_closed", lambda: False)():
            return late
        flight = _IN_FLIGHT.get(kind)
        if flight is not None and flight[0].is_set():
            _done, box, _err, _cancel, _timed_out = _IN_FLIGHT.pop(kind)
            if box:
                conn = box[0]
                if not getattr(conn, "is_closed", lambda: False)():
                    return conn
            flight = None
        if flight is None:
            done = threading.Event()
            box: list = []
            err: list = []
            cancel = Gio.Cancellable()
            timed_out = [False]
            flight = (done, box, err, cancel, timed_out)
            _IN_FLIGHT[kind] = flight

            def worker() -> None:
                try:
                    conn = Gio.bus_get_sync(bus_type, cancel)
                except BaseException as exc:
                    err.append(exc)
                else:
                    box.append(conn)
                    with _BUS_LOCK:
                        if timed_out[0]:
                            # Never close Gio's shared singleton connection; reuse it next call.
                            _LATE_BUS[kind] = conn
                finally:
                    with _BUS_LOCK:
                        if _IN_FLIGHT.get(kind) is flight and (not timed_out[0] or err):
                            _IN_FLIGHT.pop(kind, None)
                    done.set()

            threading.Thread(target=worker, daemon=True,
                             name=f"asusius-dbus-connect-{kind}").start()
        else:
            done, box, err, cancel, timed_out = flight
    if not done.wait(timeout_s):
        timed_out[0] = True
        cancel.cancel()
        raise DBusUnavailable(f"{kind} bus connect exceeded {timeout_s:g}s") from None
    with _BUS_LOCK:
        if _IN_FLIGHT.get(kind) is flight:
            _IN_FLIGHT.pop(kind, None)
    if err:
        exc = err[0]
        if isinstance(exc, GLib.Error):
            raise DBusUnavailable(f"{kind} bus: {exc.message}") from None
        msg = getattr(exc, "message", None) or str(exc)
        raise DBusUnavailable(f"{kind} bus: {msg}") from None
    if not box:
        raise DBusUnavailable(f"{kind} bus unavailable")
    return box[0]


def bus(kind: str = "system"):
    return connect_bus_sync(kind)


def call(kind: str, name: str, path: str, iface: str, method: str, args=None,
         reply: str | None = None, timeout_ms: int = 10000, interactive: bool = False,
         conn=None, fd_list=None):
    """Call and return the unpacked reply tuple. Raises GLib.Error on a D-Bus error."""
    Gio, GLib = _gio()
    conn = conn or bus(kind)
    flags = Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION if interactive \
        else Gio.DBusCallFlags.NONE
    rtype = GLib.VariantType(reply) if reply else None
    if fd_list is not None:
        res, _out = conn.call_with_unix_fd_list_sync(name, path, iface, method, args, rtype,
                                                     flags, timeout_ms, fd_list, None)
    else:
        res = conn.call_sync(name, path, iface, method, args, rtype, flags, timeout_ms, None)
    return res.unpack() if res is not None else ()


def get_property(kind: str, name: str, path: str, iface: str, prop: str,
                 timeout_ms: int = 5000, conn=None):
    _Gio, GLib = _gio()
    (val,) = call(kind, name, path, "org.freedesktop.DBus.Properties", "Get",
                  GLib.Variant("(ss)", (iface, prop)), "(v)", timeout_ms, conn=conn)
    return val


def has_owner(kind: str, name: str, conn=None) -> bool:
    """True when the name has an owner or can be activated (does not activate it)."""
    _Gio, GLib = _gio()
    try:
        (owned,) = call(kind, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                        "org.freedesktop.DBus", "NameHasOwner", GLib.Variant("(s)", (name,)),
                        "(b)", 3000, conn=conn)
        if owned:
            return True
        (names,) = call(kind, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                        "org.freedesktop.DBus", "ListActivatableNames", None, "(as)", 3000,
                        conn=conn)
        return name in names
    except (GLib.Error, DBusUnavailable):
        return False


def error_name(exc) -> str:
    """'org.freedesktop.fwupd.NothingToDo' from a GLib.Error raised by a remote call."""
    try:
        from gi.repository import Gio
        return Gio.DBusError.get_remote_error(exc) or ""
    except Exception:
        return ""


def error_text(exc) -> str:
    try:
        from gi.repository import Gio
        Gio.DBusError.strip_remote_error(exc)
    except Exception:
        pass
    return getattr(exc, "message", None) or str(exc)
