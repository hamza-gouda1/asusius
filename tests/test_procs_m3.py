# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Milestone 3 process backend: ProcDetail inspection, pidfd kill, renice.

Fake-root tests hand-build /proc trees in tempdirs and always patch the OS
primitives (os.pidfd_open, the pidfd_send_signal syscall, os.kill,
os.setpriority): a fake /proc PID must never become a real signal or priority
target. The unmocked tests run only against the real /proc on processes this
module spawned itself, and only ever lower priority (raise the nice value).
"""
import errno
import os
import pwd
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from asusius.telemetry import procs
from asusius.telemetry.procs import ProcDetail, alive, inspect_process, kill, parse_pid_stat, \
    set_priority
from asusius.telemetry.util import read_bytes

BTIME = 1_700_000_000
NOW = 1_700_018_450.0
DEFAULT_STATUS = "Name:\tsleeper\nState:\tS\nVmRSS:\t2048 kB\nVmHWM:\t4096 kB\nVmSwap:\t64 kB\n"
DEFAULT_CGROUP = ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
                  "app-gnome-org.demo.App-7.scope\n")
DEFAULT_STATM = "1024 512 128 10 0 512 0\n"


def stat_text(pid: int, *, comm: str = "sleeper", state: str = "S", ppid: int = 1,
              utime: int = 10, stime: int = 5, threads: int = 1, start: int = 12345,
              nice: int = 0, rss_pages: int = 512) -> str:
    """A /proc/<pid>/stat line in the same field layout the repo's fake world writes."""
    return (f"{pid} ({comm}) {state} {ppid} {pid} {pid} 0 -1 4194560 0 0 0 0 {utime} {stime} "
            f"0 0 20 {nice} {threads} 0 {start} 0 {rss_pages} "
            "18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 3 0 0 0 0 0\n")


def make_proc(root: Path, pid: int, *, start: int = 12345, nice: int = 0,
              comm: str = "sleeper", state: str = "S", ppid: int = 1,
              cmdline: bytes | None = b"/usr/bin/sleeper\0--flag\0",
              status: str | None = DEFAULT_STATUS, statm: str | None = DEFAULT_STATM,
              cgroup: str | None = DEFAULT_CGROUP) -> None:
    """Hand-build one /proc/<pid> directory (repo style: per-pid trees in a tempdir)."""
    d = root / "proc" / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "stat").write_text(stat_text(pid, comm=comm, state=state, ppid=ppid, start=start,
                                      nice=nice))
    if cmdline is not None:
        (d / "cmdline").write_bytes(cmdline)
    if status is not None:
        (d / "status").write_text(status)
    if statm is not None:
        (d / "statm").write_text(statm)
    if cgroup is not None:
        (d / "cgroup").write_text(cgroup)


def make_world(root: Path, *, btime: int | None = BTIME) -> None:
    """The /proc/stat btime line inspect_process is allowed to read."""
    (root / "proc").mkdir(parents=True, exist_ok=True)
    text = "cpu  1 2 3 4 5 6 7 8 0 0\n"
    if btime is not None:
        text += f"btime {btime}\n"
    (root / "proc" / "stat").write_text(text + "procs_running 1\n")


def spare_fd() -> int:
    """A real, closeable fd so mocked pidfd_open results survive the real os.close()."""
    return os.dup(0)


def read_stat(path: str) -> tuple | None:
    data = read_bytes(path, 4096)
    return parse_pid_stat(data) if data else None


class ParseNiceTests(unittest.TestCase):
    def test_nice_is_appended_as_ninth_field(self):
        st = parse_pid_stat(stat_text(500, nice=13).encode())
        self.assertEqual(len(st), 9)
        self.assertEqual(st[8], 13)

    def test_first_eight_indices_unchanged(self):
        data = (Path(__file__).parent / "fixtures" / "telemetry" / "pid_stat").read_bytes()
        self.assertEqual(parse_pid_stat(data)[:8],
                         ("Isolated Web Co", "S", 1700, 750, 250, 6, 99999, 51200))

    def test_fixture_nice_parsed(self):
        data = (Path(__file__).parent / "fixtures" / "telemetry" / "pid_stat").read_bytes()
        self.assertEqual(parse_pid_stat(data)[8], 0)  # field 19 of the fixture line

    def test_weird_comm_line(self):
        weird = b"7 (a) b (c)) R 1 1 1 0 -1 0 0 0 0 0 1 2 0 0 20 5 1 0 5 0 9 0\n"
        st = parse_pid_stat(weird)
        self.assertEqual(st[0], "a) b (c)")
        self.assertEqual(st[8], 5)

    def test_short_data_still_none(self):
        self.assertIsNone(parse_pid_stat(b"1 (x) S 0 1 1 0 -1 0\n"))


class WalkerUnpackTests(unittest.TestCase):
    def test_walk_survives_the_nine_field_stat(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            make_proc(root, 4242, start=77)
            w = procs.ProcessWalker(str(root))
            out = w.walk(100.0)
            self.assertEqual(len(out), 1)
            self.assertEqual((out[0].pid, out[0].start, out[0].name), (4242, 77, "sleeper"))


class InspectTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        make_world(self.root)

    def inspect(self, pid: int, start: int = 12345) -> ProcDetail:
        return inspect_process(pid, start, root=str(self.root), now=NOW)

    def test_ok_snapshot_has_all_fields(self):
        make_proc(self.root, 500)
        d = self.inspect(500)
        self.assertIsInstance(d, ProcDetail)
        self.assertEqual(d.status, "ok")
        self.assertEqual((d.pid, d.start), (500, 12345))
        self.assertEqual(d.comm, "sleeper")
        self.assertEqual(d.name, "sleeper")
        self.assertEqual(d.argv0, "/usr/bin/sleeper")
        self.assertEqual(d.args, ["/usr/bin/sleeper", "--flag"])
        self.assertEqual(d.args_count, 1)
        self.assertEqual((d.ppid, d.state, d.threads), (1, "S", 1))
        self.assertEqual(d.nice, 0)
        self.assertEqual(d.uid, os.getuid())
        self.assertEqual(d.user, pwd.getpwuid(os.getuid()).pw_name)
        self.assertAlmostEqual(d.uptime_s, NOW - (BTIME + 12345 / procs.CLK_TCK))
        self.assertEqual(d.rss, 2048 * 1024)
        self.assertEqual(d.hwm, 4096 * 1024)
        self.assertEqual(d.swap, 64 * 1024)
        self.assertEqual(d.private, (512 - 128) * procs.PAGE)
        self.assertEqual(d.shared, 128 * procs.PAGE)
        self.assertEqual(d.app, "org.demo.App")
        self.assertEqual(d.unit, "app-gnome-org.demo.App-7.scope")
        self.assertEqual(d.cgroup, "/user.slice/user-1000.slice/user@1000.service/app.slice/"
                                   "app-gnome-org.demo.App-7.scope")

    def test_kernel_thread_empty_cmdline(self):
        make_proc(self.root, 2, comm="kthreadd", state="S", ppid=0, cmdline=b"", cgroup="0::/\n",
                  status=None, statm=None)
        d = self.inspect(2)
        self.assertEqual(d.status, "ok")
        self.assertEqual(d.argv0, "")
        self.assertEqual(d.args, [])
        self.assertEqual(d.args_count, 0)
        self.assertEqual(d.name, "kthreadd")
        self.assertEqual(d.uid, os.getuid())
        self.assertEqual(d.user, pwd.getpwuid(os.getuid()).pw_name)
        self.assertIsNone(d.rss)
        self.assertIsNone(d.hwm)
        self.assertIsNone(d.private)
        self.assertIsNone(d.shared)
        self.assertIsNone(d.app)
        self.assertIsNone(d.unit)
        self.assertEqual(d.cgroup, "/")

    def test_missing_process_is_exited_with_nothing_fabricated(self):
        d = self.inspect(999)
        self.assertEqual(d.status, "exited")
        self.assertEqual((d.pid, d.start), (999, 12345))
        for f in ("comm", "name", "argv0", "args", "args_count", "uid", "user", "ppid", "state",
                  "threads", "uptime_s", "nice", "app", "unit", "cgroup", "rss", "hwm", "swap",
                  "private", "shared"):
            self.assertIsNone(getattr(d, f), f)

    def test_zombie_is_exited(self):
        make_proc(self.root, 501, state="Z", comm="dead", cmdline=b"", status=None, statm=None)
        d = self.inspect(501)
        self.assertEqual(d.status, "exited")
        self.assertIsNone(d.state)
        self.assertIsNone(d.comm)
        self.assertIsNone(d.argv0)
        self.assertIsNone(d.rss)

    def test_pid_reuse_is_changed(self):
        make_proc(self.root, 502, start=99999, comm="other")
        d = self.inspect(502)
        self.assertEqual(d.status, "changed")
        self.assertIsNone(d.comm)
        self.assertIsNone(d.args)

    def test_pid_reuse_while_reading_discards_the_snapshot(self):
        make_proc(self.root, 502)
        original = procs.read_bytes

        def replace_during_read(path, limit):
            result = original(path, limit)
            if path.endswith("/502/cmdline"):
                make_proc(self.root, 502, start=99999, comm="replacement")
            return result

        with mock.patch.object(procs, "read_bytes", side_effect=replace_during_read):
            d = self.inspect(502)
        self.assertEqual(d.status, "changed")
        self.assertIsNone(d.args)
        self.assertIsNone(d.comm)

    def test_missing_sources_are_none_not_zero(self):
        make_proc(self.root, 503, status=None, statm=None, cgroup=None)
        (self.root / "proc" / "stat").write_text("cpu  1 2 3 4 5 6 7 8 0 0\n")  # no btime
        d = self.inspect(503)
        self.assertEqual(d.status, "ok")
        self.assertIsNone(d.rss)
        self.assertIsNone(d.hwm)
        self.assertIsNone(d.swap)
        self.assertIsNone(d.private)
        self.assertIsNone(d.shared)
        self.assertIsNone(d.app)
        self.assertIsNone(d.unit)
        self.assertIsNone(d.cgroup)
        self.assertIsNone(d.uptime_s)
        self.assertEqual(d.argv0, "/usr/bin/sleeper")  # cmdline is still readable

    def test_garbled_status_lines_are_none(self):
        make_proc(self.root, 504, status="Name:\tx\nVmRSS:\tbig kB\nVmHWM:\t\n")
        d = self.inspect(504)
        self.assertIsNone(d.rss)
        self.assertIsNone(d.hwm)
        self.assertIsNone(d.swap)

    def test_unknown_uid_falls_back_to_uid_n(self):
        make_proc(self.root, 505)
        with mock.patch("pwd.getpwuid", side_effect=KeyError("no such uid")):
            d = self.inspect(505)
        self.assertEqual(d.uid, os.getuid())
        self.assertEqual(d.user, f"uid {os.getuid()}")

    def test_unreadable_process_dir_leaves_uid_none(self):
        make_proc(self.root, 506)
        with mock.patch("os.stat", side_effect=OSError(13, "denied")):
            d = self.inspect(506)
        self.assertIsNone(d.uid)
        self.assertIsNone(d.user)

    def test_never_reads_environ_and_only_allowed_paths(self):
        make_proc(self.root, 507)
        (self.root / "proc" / "507" / "environ").write_bytes(b"SECRET=1\0")
        seen = []
        real = procs.read_bytes

        def spy(path, limit=1 << 20):
            seen.append(path)
            return real(path, limit)

        with mock.patch.object(procs, "read_bytes", spy):
            d = self.inspect(507)
        allowed = {str(self.root / "proc" / "stat"),
                   *(str(self.root / "proc" / "507" / n)
                     for n in ("stat", "status", "statm", "cmdline", "cgroup"))}
        self.assertTrue(seen, "inspect_process must read through read_bytes")
        self.assertEqual(set(seen), allowed)
        self.assertNotIn("SECRET", repr(d))
        self.assertEqual(d.args, ["/usr/bin/sleeper", "--flag"])  # cmdline only, no environ

    def test_args_never_appear_in_repr(self):
        make_proc(self.root, 508, cmdline=b"/usr/bin/app\0--token=SECRETVALUE\0")
        d = self.inspect(508)
        self.assertEqual(d.args, ["/usr/bin/app", "--token=SECRETVALUE"])
        self.assertNotIn("SECRETVALUE", repr(d))
        self.assertIn("SECRETVALUE", str(d.args))  # in memory only, for the Copy action

    def test_now_defaults_to_time_time(self):
        make_proc(self.root, 509)
        d = inspect_process(509, 12345, root=str(self.root))
        self.assertIsInstance(d.uptime_s, float)


class KillFakeRootTests(unittest.TestCase):
    """Fake /proc trees: OS primitives are always patched here, so a fake PID can
    never reach a real signal."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        make_world(self.root)
        make_proc(self.root, 5000, start=777)

    def test_pidfd_path_sends_the_signal_through_the_fd(self):
        libc = mock.MagicMock()
        libc.syscall.return_value = 0
        with mock.patch("os.pidfd_open", return_value=spare_fd()) as pfd, \
                mock.patch("ctypes.CDLL", return_value=libc) as cdll, \
                mock.patch("os.kill") as okill:
            kill(5000, 777, signal.SIGTERM, root=str(self.root))
        pfd.assert_called_once_with(5000)
        cdll.assert_called_once_with(None, use_errno=True)
        libc.syscall.assert_called_once_with(procs._PIDFD_SEND_SIGNAL, pfd.return_value,
                                             signal.SIGTERM, None, 0)
        okill.assert_not_called()

    def test_pidfd_esrch_is_process_lookup_error(self):
        libc = mock.MagicMock()
        libc.syscall.return_value = -1
        with mock.patch("os.pidfd_open", return_value=spare_fd()), \
                mock.patch("ctypes.CDLL", return_value=libc), \
                mock.patch("ctypes.get_errno", return_value=errno.ESRCH), \
                mock.patch("os.kill") as okill:
            with self.assertRaises(ProcessLookupError):
                kill(5000, 777, signal.SIGTERM, root=str(self.root))
        okill.assert_not_called()

    def test_pidfd_eperm_is_permission_error_without_fallback(self):
        libc = mock.MagicMock()
        libc.syscall.return_value = -1
        with mock.patch("os.pidfd_open", return_value=spare_fd()), \
                mock.patch("ctypes.CDLL", return_value=libc), \
                mock.patch("ctypes.get_errno", return_value=errno.EPERM), \
                mock.patch("os.kill") as okill:
            with self.assertRaises(PermissionError):
                kill(5000, 777, signal.SIGTERM, root=str(self.root))
        okill.assert_not_called()

    def test_pidfd_enosys_falls_back_to_checked_kill(self):
        libc = mock.MagicMock()
        libc.syscall.return_value = -1
        with mock.patch("os.pidfd_open", return_value=spare_fd()), \
                mock.patch("ctypes.CDLL", return_value=libc), \
                mock.patch("ctypes.get_errno", return_value=errno.ENOSYS), \
                mock.patch("os.kill") as okill:
            kill(5000, 777, signal.SIGUSR1, root=str(self.root))
        okill.assert_called_once_with(5000, signal.SIGUSR1)

    def test_pidfd_open_oserror_falls_back(self):
        with mock.patch("os.pidfd_open",
                        side_effect=OSError(errno.ENOSYS, "pidfd_open unavailable")), \
                mock.patch("os.kill") as okill:
            kill(5000, 777, signal.SIGTERM, root=str(self.root))
        okill.assert_called_once_with(5000, signal.SIGTERM)

    def test_pidfd_open_missing_process_raises(self):
        with mock.patch("os.pidfd_open", side_effect=ProcessLookupError), \
                mock.patch("os.kill") as okill:
            with self.assertRaises(ProcessLookupError):
                kill(5000, 777, signal.SIGTERM, root=str(self.root))
        okill.assert_not_called()

    def test_start_mismatch_after_open_refuses(self):
        make_proc(self.root, 5000, start=888)  # the PID now belongs to another start
        with mock.patch("os.pidfd_open", return_value=spare_fd()), \
                mock.patch("os.kill") as okill:
            with self.assertRaises(ProcessLookupError):
                kill(5000, 777, signal.SIGTERM, root=str(self.root))
        okill.assert_not_called()

    def test_vanished_after_open_refuses(self):
        (self.root / "proc" / "5000" / "stat").unlink()
        with mock.patch("os.pidfd_open", return_value=spare_fd()), \
                mock.patch("os.kill") as okill:
            with self.assertRaises(ProcessLookupError):
                kill(5000, 777, signal.SIGTERM, root=str(self.root))
        okill.assert_not_called()

    def test_malformed_stat_after_open_closes_the_pidfd(self):
        stat = self.root / "proc" / "5000" / "stat"
        stat.write_bytes(stat.read_bytes().replace(b" 777 ", b" invalid "))
        fd = spare_fd()
        with (mock.patch("os.pidfd_open", return_value=fd),
              mock.patch("os.kill") as okill):
            with self.assertRaises(ProcessLookupError):
                kill(5000, 777, signal.SIGTERM, root=str(self.root))
        okill.assert_not_called()
        with self.assertRaises(OSError) as closed:
            os.fstat(fd)
        self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_other_arch_never_attempts_the_syscall(self):
        libc = mock.MagicMock()
        with mock.patch("platform.machine", return_value="riscv64"), \
                mock.patch("os.pidfd_open", return_value=spare_fd()), \
                mock.patch("ctypes.CDLL", return_value=libc), \
                mock.patch("os.kill") as okill:
            kill(5000, 777, signal.SIGTERM, root=str(self.root))
        libc.syscall.assert_not_called()
        okill.assert_called_once_with(5000, signal.SIGTERM)

    def test_fallback_rechecks_start_before_kill(self):
        make_proc(self.root, 5001, start=888)
        with mock.patch("os.pidfd_open", side_effect=OSError(errno.ENOSYS, "no pidfd")), \
                mock.patch("os.kill") as okill:
            with self.assertRaises(ProcessLookupError):
                kill(5001, 777, signal.SIGTERM, root=str(self.root))
        okill.assert_not_called()

    def test_init_and_self_refused_before_any_os_interaction(self):
        seen = []
        real = procs.read_bytes

        def spy(path, limit=1 << 20):
            seen.append(path)
            return real(path, limit)

        with mock.patch.object(procs, "read_bytes", spy), \
                mock.patch("os.pidfd_open") as pfd, \
                mock.patch("os.kill") as okill, \
                mock.patch("ctypes.CDLL") as cdll:
            for target in (1, os.getpid()):
                with self.assertRaises(PermissionError):
                    kill(target, 12345, signal.SIGTERM, root=str(self.root))
        self.assertEqual(seen, [])
        pfd.assert_not_called()
        okill.assert_not_called()
        cdll.assert_not_called()


class KillRealProcessTests(unittest.TestCase):
    """Unmocked kill() against the real /proc, on children this test spawned and owns."""

    def spawn(self) -> tuple:
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self.reap, p)
        st = read_stat(f"/proc/{p.pid}/stat")
        self.assertIsNotNone(st)
        return p, p.pid, st[6]

    @staticmethod
    def reap(p) -> None:
        if p.poll() is None:
            p.kill()
        p.wait()

    @staticmethod
    def state_of(pid: int, start: int) -> str | None:
        st = read_stat(f"/proc/{pid}/stat")
        return None if st is None or st[6] != start else st[1]

    def wait_state(self, pid: int, start: int, want: str) -> None:
        deadline = time.monotonic() + 5
        while self.state_of(pid, start) != want and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.state_of(pid, start), want)

    def test_stop_continue_term_through_real_pidfd(self):
        p, pid, start = self.spawn()
        kill(pid, start, signal.SIGSTOP)
        self.wait_state(pid, start, "T")
        self.assertTrue(alive(pid, start))  # stopped is not dead
        kill(pid, start, signal.SIGCONT)
        deadline = time.monotonic() + 5
        while self.state_of(pid, start) == "T" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertNotEqual(self.state_of(pid, start), "T")
        kill(pid, start, signal.SIGTERM)
        p.wait(timeout=10)
        self.assertFalse(alive(pid, start))

    def test_kill_after_exit_raises_process_lookup_error(self):
        p, pid, start = self.spawn()
        kill(pid, start, signal.SIGTERM)
        p.wait(timeout=10)
        with self.assertRaises(ProcessLookupError):
            kill(pid, start, signal.SIGTERM)

    def test_wrong_start_refused_on_real_process(self):
        p, pid, start = self.spawn()
        with self.assertRaises(ProcessLookupError):
            kill(pid, start + 1, signal.SIGTERM)
        self.assertTrue(alive(pid, start))  # untouched

    def test_fallback_kill_on_real_process(self):
        p, pid, start = self.spawn()
        with mock.patch("platform.machine", return_value="riscv64"):  # force the os.kill path
            kill(pid, start, signal.SIGSTOP)
        self.wait_state(pid, start, "T")

    def test_init_and_self_refused_unmocked(self):
        with self.assertRaises(PermissionError):
            kill(1, 0, signal.SIGTERM)
        with self.assertRaises(PermissionError):
            kill(os.getpid(), 0, signal.SIGTERM)


class SetPriorityFakeRootTests(unittest.TestCase):
    """Fake /proc trees: os.setpriority is always patched here."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        make_world(self.root)
        make_proc(self.root, 6000, start=42, nice=5)

    def renice_effect(self, pid: int = 6000):
        """os.setpriority stand-in that applies the change to the fake stat file."""
        def apply(_which, _who, nice):
            path = self.root / "proc" / str(pid) / "stat"
            st = parse_pid_stat(path.read_bytes())
            path.write_text(stat_text(pid, comm=st[0], state=st[1], ppid=st[2], utime=st[3],
                                      stime=st[4], threads=st[5], start=st[6],
                                      rss_pages=st[7], nice=nice))
        return apply

    def test_applies_and_returns_the_nice_in_effect(self):
        with mock.patch("os.setpriority", side_effect=self.renice_effect()) as sp:
            out = set_priority(6000, 42, 12, root=str(self.root))
        self.assertEqual(out, 12)
        sp.assert_called_once_with(os.PRIO_PROCESS, 6000, 12)
        self.assertEqual(read_stat(str(self.root / "proc" / "6000" / "stat"))[8], 12)

    def test_range_enforced_before_any_os_call(self):
        with mock.patch("os.setpriority") as sp:
            for bad in (-21, 20, -100, 1000):
                with self.assertRaises(ValueError):
                    set_priority(6000, 42, bad, root=str(self.root))
        sp.assert_not_called()

    def test_range_bounds_are_valid(self):
        for good in (-20, 19):
            with mock.patch("os.setpriority", side_effect=self.renice_effect()):
                self.assertEqual(set_priority(6000, 42, good, root=str(self.root)), good)

    def test_identity_mismatch_refuses(self):
        with mock.patch("os.setpriority") as sp:
            with self.assertRaises(ProcessLookupError):
                set_priority(6000, 43, 10, root=str(self.root))
        sp.assert_not_called()

    def test_missing_process_refuses(self):
        with mock.patch("os.setpriority") as sp:
            with self.assertRaises(ProcessLookupError):
                set_priority(6999, 42, 10, root=str(self.root))
        sp.assert_not_called()

    def test_protected_targets_first_even_with_out_of_range_nice(self):
        with mock.patch("os.setpriority") as sp:
            for target in (1, os.getpid()):
                with self.assertRaises(PermissionError):
                    set_priority(target, 0, 999, root=str(self.root))
        sp.assert_not_called()

    def test_eacces_is_permission_error_with_honest_hint(self):
        with mock.patch("os.setpriority", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(PermissionError) as cm:
                set_priority(6000, 42, -5, root=str(self.root))
        self.assertIn("privilege", str(cm.exception))

    def test_readback_identity_lost(self):
        def reuse(_which, _who, _nice):
            (self.root / "proc" / "6000" / "stat").write_text(stat_text(6000, start=43, nice=12))

        with mock.patch("os.setpriority", side_effect=reuse):
            with self.assertRaises(ProcessLookupError):
                set_priority(6000, 42, 12, root=str(self.root))

    def test_readback_stat_gone(self):
        def vanish(_which, _who, _nice):
            (self.root / "proc" / "6000" / "stat").unlink()

        with mock.patch("os.setpriority", side_effect=vanish):
            with self.assertRaises(ProcessLookupError):
                set_priority(6000, 42, 12, root=str(self.root))

    def test_readback_nice_unchanged_is_oserror(self):
        with mock.patch("os.setpriority"):  # reports success but changes nothing
            with self.assertRaises(OSError) as cm:
                set_priority(6000, 42, 12, root=str(self.root))
        self.assertNotIsInstance(cm.exception, ProcessLookupError)
        self.assertIn("did not take effect", str(cm.exception))


class SetPriorityRealProcessTests(unittest.TestCase):
    """Unmocked set_priority() on the real /proc: lowering only (raising is privileged)."""

    def test_lowering_priority_unmocked(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            st = read_stat(f"/proc/{p.pid}/stat")
            self.assertIsNotNone(st)
            pid, start, initial = p.pid, st[6], st[8]
            target = min(19, max(initial, 0) + 10)  # only ever raise the nice value
            out = set_priority(pid, start, target)
            self.assertEqual(out, target)
            st2 = read_stat(f"/proc/{pid}/stat")
            self.assertIsNotNone(st2)
            self.assertEqual(st2[8], target)
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()


if __name__ == "__main__":
    unittest.main()
