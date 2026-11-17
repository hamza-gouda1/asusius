# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Process walk over /proc: CPU %, memory, disk I/O, app grouping by systemd scope.

Runs only while the Processes page is visible (every 2 s). Per process it reads
stat + statm each pass, io for processes we own, and cmdline/cgroup once per process
lifetime (cached by pid and start time). Records are reused between passes.
inspect_process takes a one-shot ProcDetail snapshot for the details dialog, bound
to (pid, start); kill prefers pidfd so a reused PID can never be signalled by
mistake, and set_priority renices under the same identity check.
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import pwd
import re
import time
from dataclasses import dataclass, field

from .util import join, read_bytes

CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_APP_RE = re.compile(r"^app-(?:(?:gnome|kde|flatpak|snap|xfce|dbus-:[0-9.]+|"
                     r"[a-z0-9]+launcher)-)?(.+?)(?:-[0-9]+\.scope|@[^.]*\.service|\.service"
                     r"|\.scope|\.slice)$")
_PIDFD_SEND_SIGNAL = 424  # pidfd_send_signal(2) syscall number on x86_64 and aarch64
_PIDFD_ARCHES = frozenset({"x86_64", "aarch64"})


def parse_pid_stat(data: bytes) -> tuple | None:
    """(comm, state, ppid, utime, stime, threads, starttime, rss_pages, nice) of /proc/pid/stat.

    Indices 0-7 feed the walker, kill and alive; st[8] is the nice value (field 19).
    None when the comm parens or the required fields are missing."""
    r = data.rfind(b")")
    l = data.find(b"(")
    if not 0 <= l < r:
        return None
    comm = data[l + 1:r].decode(errors="replace")
    f = data[r + 2:].split()
    if len(f) < 22:
        return None
    try:
        return (comm, f[0].decode(), int(f[1]), int(f[11]), int(f[12]), int(f[17]), int(f[19]),
                int(f[21]), int(f[16]))
    except ValueError:
        return None


def parse_io(data: bytes) -> tuple[int, int] | None:
    """(read_bytes, write_bytes) at the storage layer from /proc/pid/io."""
    r = w = None
    for line in data.split(b"\n"):
        if line.startswith(b"read_bytes:"):
            r = int(line[11:])
        elif line.startswith(b"write_bytes:"):
            w = int(line[12:])
    return None if r is None or w is None else (r, w)


def parse_vmswap(data: bytes) -> int | None:
    """Swapped-out bytes of a process: the VmSwap line of /proc/pid/status (kB there).
    None when the line is absent (kernel threads, or a kernel without swap accounting)."""
    i = data.find(b"\nVmSwap:")
    if i < 0:
        return None
    line = data[i + 1:data.find(b"\n", i + 1)]
    try:
        return int(line.split()[1]) * 1024
    except (IndexError, ValueError):
        return None


def unescape_unit(s: str) -> str:
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)


def app_from_cgroup(text: str) -> tuple[str | None, str | None]:
    """(application id, unit) from /proc/pid/cgroup, per the systemd XDG naming scheme:
    app[-<launcher>]-<ApplicationID>[-<RANDOM>].scope or ...[@<RANDOM>].service."""
    line = ""
    for ln in text.splitlines():
        if ln.startswith("0::"):
            line = ln[3:]
            break
    else:
        line = text.splitlines()[0].split(":", 2)[-1] if text.strip() else ""
    parts = [p for p in line.split("/") if p]
    unit = parts[-1] if parts else None
    for p in reversed(parts):
        if p.startswith("app-"):
            m = _APP_RE.match(p)
            if m:
                app = unescape_unit(m.group(1))
                return app, unit
        if p.startswith("snap."):
            return "snap." + p.split(".")[1], unit
    return None, unit


class Proc:
    __slots__ = ("pid", "ppid", "start", "name", "cmd", "uid", "state", "threads", "cpu",
                 "mem", "mem_partial", "rss", "swap", "io_read", "io_write", "gpu", "vram",
                 "app", "unit", "kthread", "_ticks", "_io", "_t", "seen")

    def __init__(self, pid: int, start: int) -> None:
        self.pid, self.start = pid, start
        self.name = self.cmd = ""
        self.uid = -1
        self.state = ""
        self.ppid = self.threads = 0
        self.cpu = self.io_read = self.io_write = None
        self.mem = self.rss = 0
        self.mem_partial = False  # statm unreadable this pass: mem fell back to rss
        self.swap = None
        self.gpu = self.vram = None
        self.app = self.unit = None
        self.kthread = False
        self._ticks = None
        self._io = None
        self._t = 0.0
        self.seen = 0


class ProcessWalker:
    def __init__(self, root: str = "/", ncpu: int | None = None) -> None:
        self.root = root
        self.proc = join(root, "proc")
        self.ncpu = ncpu or os.cpu_count() or 1
        self.table: dict[int, Proc] = {}
        self.gen = 0
        self.my_uid = os.getuid()

    def _meta(self, p: Proc, base: str, comm: str) -> None:
        raw = read_bytes(base + "/cmdline", 1 << 15) or b""
        args = raw.rstrip(b"\0").split(b"\0") if raw else []
        p.cmd = " ".join(a.decode(errors="replace") for a in args)[:512]
        name = comm
        if args:
            first = re.split(r"[/\\]", args[0].decode(errors="replace").split(" ")[0])[-1]
            if first and (first.startswith(comm) or len(comm) >= 15):
                name = first
        p.name = name
        p.kthread = not args and (p.ppid == 2 or p.pid == 2)
        try:
            p.uid = os.stat(base).st_uid
        except OSError:
            p.uid = -1
        cg = read_bytes(base + "/cgroup", 4096)
        if cg:
            p.app, p.unit = app_from_cgroup(cg.decode(errors="replace"))

    def walk(self, now: float, gpu_pct: dict[int, float] | None = None,
             vram: dict[int, dict] | None = None) -> list[Proc]:
        self.gen += 1
        gen = self.gen
        table = self.table
        proc = self.proc
        scale = 100.0 / (CLK_TCK * self.ncpu)  # ticks/s -> % of the whole CPU
        out = []
        try:
            names = os.listdir(proc)
        except OSError:
            return out
        for name in names:
            if not name.isdigit():
                continue
            pid = int(name)
            base = proc + "/" + name
            data = read_bytes(base + "/stat", 4096)
            if not data:
                continue
            st = parse_pid_stat(data)
            if st is None:
                continue
            # _nice (st[8]) is read by inspect_process; the walker has no per-pass use for it
            comm, state, ppid, ut, stt, threads, start, rss_pages, _nice = st
            p = table.get(pid)
            if p is None or p.start != start:
                p = Proc(pid, start)
                p.ppid = ppid
                table[pid] = p
                self._meta(p, base, comm)
            p.seen = gen
            p.state, p.ppid, p.threads = state, ppid, threads
            ticks = ut + stt
            if p._ticks is not None and now > p._t:
                p.cpu = max(0.0, (ticks - p._ticks) / (now - p._t) * scale)
            dt = now - p._t
            p._ticks = ticks
            sm = read_bytes(base + "/statm", 256)
            if sm:
                f = sm.split()
                resident, shared = int(f[1]), int(f[2])
                p.rss = resident * PAGE
                p.mem = max(0, resident - shared) * PAGE
                p.mem_partial = False
            else:
                p.rss = rss_pages * PAGE
                p.mem = p.rss
                p.mem_partial = True
            if not p.kthread:
                stx = read_bytes(base + "/status", 4096)
                p.swap = parse_vmswap(stx) if stx else None
            if p.uid == self.my_uid:
                io = read_bytes(base + "/io", 1024)
                cur = parse_io(io) if io else None
                if cur and p._io is not None and dt > 0 and cur[0] >= p._io[0] \
                        and cur[1] >= p._io[1]:
                    p.io_read = (cur[0] - p._io[0]) / dt
                    p.io_write = (cur[1] - p._io[1]) / dt
                p._io = cur
            p._t = now
            p.gpu = gpu_pct.get(pid) if gpu_pct else None
            v = vram.get(pid) if vram else None
            if v:
                p.vram = v.get("vram")
                if v.get("sm") is not None:
                    p.gpu = max(p.gpu or 0.0, v["sm"])
            else:
                p.vram = None
            out.append(p)
        for pid in [k for k, v in table.items() if v.seen != gen]:
            del table[pid]
        return out

    def clear(self) -> None:
        self.table.clear()


@dataclass
class ProcDetail:
    """One-shot inspector snapshot for a single (pid, start) identity.

    Every field is None when its /proc source is missing or unreadable — never a
    fabricated zero. status is "ok", "exited" (stat gone, or a zombie) or
    "changed" (another process owns the PID now). args is in-memory only, for
    the explicit Copy action: it is never logged, rendered or screenshotted, so
    the dataclass repr hides it.
    """

    pid: int
    start: int
    status: str
    comm: str | None = None
    name: str | None = None
    argv0: str | None = None
    args: list[str] | None = field(default=None, repr=False)
    args_count: int | None = None
    uid: int | None = None
    user: str | None = None
    ppid: int | None = None
    state: str | None = None
    threads: int | None = None
    uptime_s: float | None = None
    nice: int | None = None
    app: str | None = None
    unit: str | None = None
    cgroup: str | None = None
    rss: int | None = None
    hwm: int | None = None
    swap: int | None = None
    private: int | None = None
    shared: int | None = None


def _vm_bytes(data: bytes, key: bytes) -> int | None:
    """Bytes for a "VmXXX:\t<N> kB" line of /proc/pid/status; None when absent or garbled."""
    for line in data.split(b"\n"):
        if line.startswith(key):
            try:
                return int(line[len(key):].split()[0]) * 1024
            except (IndexError, ValueError):
                return None
    return None


def inspect_process(pid: int, start: int, root: str = "/",
                    now: float | None = None) -> ProcDetail:
    """Snapshot one process for the details dialog, identity-bound to (pid, start).

    Reads /proc/stat (for btime) and the inspected pid's stat, status, statm,
    cmdline and cgroup — nothing else, never environ. Missing or unreadable
    sources stay None. Kernel threads (empty cmdline) report argv0 "" and no
    arguments. uptime_s is relative to now (time.time() by default).
    """
    if now is None:
        now = time.time()
    d = ProcDetail(pid=pid, start=start, status="exited")
    base = join(root, f"proc/{pid}")
    data = read_bytes(base + "/stat", 4096)
    st = parse_pid_stat(data) if data else None
    if st is None:
        return d  # gone: nothing to read, nothing to fabricate
    if st[6] != start:
        d.status = "changed"
        return d
    if st[1] == "Z":
        return d
    d.status = "ok"
    d.comm, d.state, d.ppid, d.threads = st[0], st[1], st[2], st[5]
    d.nice = st[8] if len(st) > 8 else None
    d.name = st[0]
    raw = read_bytes(base + "/cmdline", 1 << 15)
    if raw is None:
        d.args = d.argv0 = d.args_count = None
    else:
        args = [a.decode(errors="replace")
                for a in (raw.rstrip(b"\0").split(b"\0") if raw else [])]
        d.args = args
        d.argv0 = args[0] if args else ""
        d.args_count = max(0, len(args) - 1)
        if args:
            first = re.split(r"[/\\]", args[0].split(" ")[0])[-1]
            if first and (first.startswith(st[0]) or len(st[0]) >= 15):
                d.name = first
    try:
        d.uid = os.stat(base).st_uid
    except OSError:
        d.uid = None
    if d.uid is not None:
        try:
            d.user = pwd.getpwuid(d.uid).pw_name
        except (KeyError, OSError):
            d.user = f"uid {d.uid}"
    stx = read_bytes(base + "/status", 4096)
    if stx:
        d.rss = _vm_bytes(stx, b"VmRSS:")
        d.hwm = _vm_bytes(stx, b"VmHWM:")
        d.swap = _vm_bytes(stx, b"VmSwap:")
    sm = read_bytes(base + "/statm", 256)
    if sm:
        f = sm.split()
        try:
            resident, shared = int(f[1]), int(f[2])
        except (IndexError, ValueError):
            resident = shared = None
        if resident is not None:
            d.private = max(0, resident - shared) * PAGE
            d.shared = shared * PAGE
    cg = read_bytes(base + "/cgroup", 4096)
    if cg:
        text = cg.decode(errors="replace")
        d.app, d.unit = app_from_cgroup(text)
        for ln in text.splitlines():
            if ln.startswith("0::"):
                d.cgroup = ln[3:]
                break
    bdata = read_bytes(join(root, "proc/stat"), 1 << 16)
    if bdata:
        for line in bdata.split(b"\n"):
            if line.startswith(b"btime"):
                try:
                    btime = int(line.split()[1])
                except (IndexError, ValueError):
                    btime = None
                break
        else:
            btime = None
        if btime is not None:
            d.uptime_s = now - (btime + st[6] / CLK_TCK)
    data = read_bytes(base + "/stat", 4096)
    confirmed = parse_pid_stat(data) if data else None
    if confirmed is None:
        return ProcDetail(pid, start, "exited")
    if confirmed[6] != start:
        return ProcDetail(pid, start, "changed")
    if confirmed[1] == "Z":
        return ProcDetail(pid, start, "exited")
    return d


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _pidfd_send_signal(fd: int, sig: int) -> int:
    """pidfd_send_signal(2) as raw syscall 424, on the architectures that define it there.

    Returns 0 on success, the errno on failure, or -1 when the call could not be
    attempted at all (other architecture, no libc syscall wrapper) so the caller
    falls back to a revalidated os.kill.
    """
    if platform.machine() not in _PIDFD_ARCHES:
        return -1
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        ret = libc.syscall(_PIDFD_SEND_SIGNAL, fd, sig, None, 0)
    except (AttributeError, OSError):
        return -1
    return 0 if ret == 0 else (ctypes.get_errno() or -1)


def kill(pid: int, start: int, sig: int, root: str = "/") -> None:
    """Signal pid only if it is still the same process (start time matches).

    Protected targets (init and this app itself) are refused before any OS
    interaction. The preferred path opens a pidfd first: the fd pins the
    process, so PID reuse after the open cannot redirect the signal, and the
    stat re-check right after the open closes the read-to-open race. Where
    pidfd is unavailable (old kernel, seccomp, other architecture) the signal
    goes through os.kill behind an immediate stat re-check instead.
    """
    if pid <= 1 or pid == os.getpid():
        raise PermissionError("refusing to signal init or this app")
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        raise
    except AttributeError:
        fd = None
    except OSError as exc:
        if exc.errno not in (errno.ENOSYS, errno.EINVAL, errno.EPERM):
            raise
        fd = None
    if fd is not None:
        try:
            if not alive(pid, start, root):
                raise ProcessLookupError(f"process {pid} has already exited")
            err = _pidfd_send_signal(fd, sig)
        finally:
            _close_fd(fd)
        if err == 0:
            return
        if err == errno.ESRCH:
            raise ProcessLookupError(f"process {pid} has already exited")
        if err == errno.EPERM:
            raise PermissionError(f"not permitted to signal process {pid}")
        if err not in (-1, errno.ENOSYS):
            raise OSError(err, os.strerror(err))
    # The legacy numeric-PID API has a remaining race after this check.
    if not alive(pid, start, root):
        raise ProcessLookupError(f"process {pid} has already exited")
    os.kill(pid, sig)


def set_priority(pid: int, start: int, nice: int, root: str = "/") -> int:
    """Renice pid only if it is still the same process; returns the nice in effect.

    Refuses init and this app itself, rejects nice values outside [-20, 19],
    and re-reads stat after os.setpriority to confirm both the identity and the
    result. Lowering a priority (raising the nice value) is unprivileged;
    raising one may need privileges and fails with PermissionError.
    """
    if pid <= 1 or pid == os.getpid():
        raise PermissionError("refusing to renice init or this app")
    if not -20 <= nice <= 19:
        raise ValueError(f"nice {nice} outside [-20, 19]")
    path = join(root, f"proc/{pid}/stat")
    data = read_bytes(path, 4096)
    st = parse_pid_stat(data) if data else None
    if st is None or st[6] != start or st[1] == "Z":
        raise ProcessLookupError(f"process {pid} has already exited")
    try:
        os.setpriority(os.PRIO_PROCESS, pid, nice)
    except PermissionError as e:
        raise PermissionError("renicing the process failed: raising a priority may need "
                              "privileges") from e
    data = read_bytes(path, 4096)
    st = parse_pid_stat(data) if data else None
    if st is None or st[6] != start or st[1] == "Z":
        raise ProcessLookupError(f"process {pid} has already exited")
    if st[8] != nice:
        raise OSError(f"nice value did not take effect (still {st[8]})")
    return st[8]


def alive(pid: int, start: int, root: str = "/") -> bool:
    data = read_bytes(join(root, f"proc/{pid}/stat"), 4096)
    st = parse_pid_stat(data) if data else None
    return st is not None and st[6] == start and st[1] != "Z"
