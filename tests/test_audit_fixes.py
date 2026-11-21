# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Regression tests for verified Batch 2 backend, telemetry, updates, and agent fixes."""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from asusius import dbusutil
from asusius.agent import mcp
from asusius.backends.cli import CliBackend
from asusius.backends.fake import build_fake_tree
from asusius.backends.sysfs import SysfsBackend
from asusius.controller import Controller
from asusius.core import (
    BackendError,
    BackendUnavailable,
    GpuMode,
    ValidationError,
)
from asusius.daemon import Daemon
from asusius.telemetry import Sampler, Store, disk_analyzer, pci
from asusius.telemetry.gpu_nvidia import IDLE_SAMPLES, NvidiaGpu
from asusius.telemetry.cpu import CpuReader
from asusius.telemetry.network import parse_net_dev
from asusius.telemetry.power import Power
from asusius.telemetry.procs import ProcessWalker, unescape_unit
from asusius.telemetry.sensors import Sensors
from asusius.telemetry.storage import parse_diskstats
from asusius.telemetry.upower import GioUpowerTransport, UpowerUnavailable
from asusius.updates import check, flatpak
from asusius.updates.model import Report, SourceResult, UpdateItem
from asusius.updates.scheduler import UpdateScheduler


def _w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class NvidiaGpuTests(unittest.TestCase):
    def _gpu(self, tmp: Path) -> NvidiaGpu:
        dev = tmp / "sys/bus/pci/devices/0000:01:00.0"
        _w(dev / "class", "0x030000\n")
        _w(dev / "vendor", "0x10de\n")
        _w(dev / "device", "0x28e0\n")
        _w(dev / "power/runtime_status", "active\n")
        with mock.patch("asusius.telemetry.gpu_nvidia.Nvml", return_value=mock.Mock()):
            gpu = NvidiaGpu(str(tmp))
        gpu.backend = "nvidia-smi"
        gpu.reason = "nvmlInit failed"
        return gpu

    def test_smi_rate_limit_holds_values_without_ring_gaps_or_idle_ticks(self):
        with tempfile.TemporaryDirectory() as d:
            gpu = self._gpu(Path(d))
            store = Store(20)
            sample_csv = "GeForce, 550.54, 12, 5, 1024, 8192, 1500, 1500, 48, 22.5, 140, 30, P0\n"
            with mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
                    mock.patch("asusius.backends.util.run_cmd",
                               return_value=sample_csv) as run:
                for t in (10.0, 11.0, 12.0, 13.0, 14.0):
                    vals, info = {}, {}
                    gpu.sample(t, vals, info)
                    store.push(t, vals)
                self.assertEqual(run.call_count, 1)
                self.assertIsNone(gpu.reason)
                self.assertIsNone(info["dgpu"].get("reason"))
                self.assertEqual(store.last("dgpu.temp"), 48.0)
                self.assertEqual(store.last("dgpu.util"), 12.0)
                self.assertEqual(gpu._idle, 0)

    def test_missing_utilization_never_counts_as_idle(self):
        with tempfile.TemporaryDirectory() as d:
            gpu = self._gpu(Path(d))
            gpu.backend = "nvml"
            nv = mock.Mock()
            nv.init.return_value = True
            nv.handle.return_value = object()
            nv.static.return_value = {"name": "GPU"}
            nv.query.return_value = {"util": None, "mem_util": None, "temp": 50.0}
            nv.processes.return_value = {}
            gpu.nvml = nv
            for t in range(1, IDLE_SAMPLES + 3):
                vals, info = {}, {}
                gpu.sample(float(t), vals, info)
            self.assertEqual(gpu._idle, 0)
            nv.shutdown.assert_not_called()


class SchedulerAndLockTests(unittest.TestCase):
    def test_after_check_does_not_block_status_during_slow_notify(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"ASUSIUS_CACHE_DIR": d}):
            started = threading.Event()
            release = threading.Event()

            def slow_notify(_summary, _body):
                started.set()
                release.wait(timeout=2.0)
                return 1

            sched = UpdateScheduler(notifier=slow_notify, first_delay_s=0.0)
            rep = Report(
                checked_ts=100.0,
                sources=[SourceResult("apt", items=[
                    UpdateItem("apt:pkg", "apt", "pkg", "1", "2")
                ])],
            )
            t = threading.Thread(target=sched.after_check, args=(rep,))
            t.start()
            self.assertTrue(started.wait(timeout=1.0))
            got_status = threading.Event()

            def read_status():
                sched.status()
                got_status.set()

            reader = threading.Thread(target=read_status)
            reader.start()
            self.assertTrue(got_status.wait(timeout=0.5))
            release.set()
            t.join(timeout=2.0)
            reader.join(timeout=2.0)

    def test_update_locks_are_private_and_is_checking_safe(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / "cache" / "asusius"
            custom = root / "custom"
            custom.mkdir(mode=0o755)
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(root / "cache")}):
                self.assertFalse(check.is_checking())
                self.assertFalse((cache / "updates.lock").exists())
                old_umask = os.umask(0)
                try:
                    with check.check_lock() as got:
                        self.assertTrue(got)
                        self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o700)
                        self.assertEqual(
                            stat.S_IMODE((cache / "updates.lock").stat().st_mode), 0o600)
                    check.update_cache(lambda doc: doc.setdefault("x", 1))
                finally:
                    os.umask(old_umask)
                self.assertEqual(
                    stat.S_IMODE((cache / "updates.json.wlock").stat().st_mode), 0o600)

                target = root / "target"
                target.write_text("keep")
                sym = custom / "updates.lock"
                sym.symlink_to(target)
                with self.assertRaises(OSError):
                    with check.check_lock(sym):
                        pass
                self.assertEqual(target.read_text(), "keep")

    def test_daemon_logs_update_scheduler_errors(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            root = build_fake_tree(tmp / "root")
            with mock.patch.dict(os.environ, {
                "ASUSIUS_FAKE_ROOT": str(root),
                "ASUSIUS_CONFIG_DIR": str(tmp / "cfg"),
                "ASUSIUS_RUNTIME_DIR": str(tmp / "run"),
            }):
                sched = mock.Mock()
                sched.tick.side_effect = RuntimeError("cache broken")
                sched.status.return_value = {}
                dmon = Daemon(Controller([SysfsBackend(root)]), updates=sched, dry_run=True)
                stop = threading.Event()
                err = io.StringIO()
                threading.Timer(0.1, stop.set).start()
                with contextlib.redirect_stderr(err):
                    dmon.run(stop)
                self.assertIn("updates tick failed", err.getvalue())
                self.assertIn("cache broken", err.getvalue())


class ControllerDryRunTests(unittest.TestCase):
    def test_dry_run_rejects_readonly_sysfs_and_validates_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            root = build_fake_tree(Path(d) / "root")
            ctl = Controller([SysfsBackend(root)])
            with self.assertRaises(BackendUnavailable):
                ctl.set_gpu_mode(GpuMode.INTEGRATED, dry_run=True)
            with self.assertRaises(BackendUnavailable):
                ctl.one_shot_full_charge(dry_run=True)
            with self.assertRaises(ValidationError):
                ctl.set_kbd_brightness(9999, dry_run=True)
            with self.assertRaises(ValidationError):
                ctl.set_attribute("panel_overdrive", 9, dry_run=True)
            ok = ctl.set_kbd_brightness(2, dry_run=True)
            self.assertFalse(ok.applied)
            self.assertIn("would set keyboard brightness 2", ok.detail)

    def test_cli_probe_rejects_generic_backend_error(self):
        b = CliBackend()
        with mock.patch("shutil.which", return_value="/usr/bin/asusctl"), \
                mock.patch("asusius.backends.cli.run_cmd",
                           side_effect=BackendError("asusctl timed out")):
            caps = b.probe()
        self.assertTrue(all(not s.available for s in caps.values()))


class DbusTimeoutAndLeakTests(unittest.TestCase):
    def test_dbusutil_reuses_single_inflight_and_caches_late_singleton(self):
        gate = threading.Event()
        calls = 0
        fake_conn = mock.Mock(is_closed=lambda: False)

        def slow_bus(_bus_type, _cancel):
            nonlocal calls
            calls += 1
            gate.wait(timeout=2.0)
            return fake_conn

        fake_gio = types.SimpleNamespace(
            BusType=types.SimpleNamespace(SYSTEM=1, SESSION=2),
            Cancellable=lambda: mock.Mock(cancel=mock.Mock()),
            bus_get_sync=slow_bus,
        )
        fake_glib = types.SimpleNamespace(Error=RuntimeError)
        with mock.patch.object(dbusutil, "_gio", return_value=(fake_gio, fake_glib)), \
                mock.patch.dict(dbusutil._IN_FLIGHT, {}, clear=True), \
                mock.patch.dict(dbusutil._LATE_BUS, {}, clear=True):
            with self.assertRaises(dbusutil.DBusUnavailable):
                dbusutil.connect_bus_sync("system", timeout_s=0.02)
            with self.assertRaises(dbusutil.DBusUnavailable):
                dbusutil.connect_bus_sync("system", timeout_s=0.02)
            self.assertEqual(calls, 1)
            gate.set()
            time.sleep(0.05)
            got = dbusutil.connect_bus_sync("system", timeout_s=0.2)
            self.assertIs(got, fake_conn)
            fake_conn.close.assert_not_called()

    def test_upower_connect_reuses_single_inflight_attempt(self):
        gate = threading.Event()
        calls = 0
        fake_conn = mock.Mock(is_closed=lambda: False)

        def slow_bus(_bus_type, _cancel):
            nonlocal calls
            calls += 1
            gate.wait(timeout=2.0)
            return fake_conn

        fake_gio = types.SimpleNamespace(
            BusType=types.SimpleNamespace(SYSTEM=1),
            Cancellable=lambda: mock.Mock(cancel=mock.Mock()),
            bus_get_sync=slow_bus,
        )
        tr = GioUpowerTransport()
        tr._gi = (fake_gio, types.SimpleNamespace(Error=RuntimeError))
        with mock.patch("asusius.telemetry.upower.CONNECT_TIMEOUT_S", 0.02):
            with self.assertRaises(UpowerUnavailable):
                tr._connect()
            with self.assertRaises(UpowerUnavailable):
                tr._connect()
            self.assertEqual(calls, 1)
            gate.set()
            time.sleep(0.05)
            self.assertIs(tr._connect(), fake_conn)
            fake_conn.close.assert_not_called()


class ParserAndTelemetryEdgeTests(unittest.TestCase):
    def test_net_dev_and_diskstats_skip_malformed_lines(self):
        net = parse_net_dev(
            b"Inter-|\n face |...\n"
            b" bad0: a b c d e f g h i j\n"
            b" eth0: 10 2 0 0 0 0 0 0 20 3\n"
        )
        self.assertEqual(net, {"eth0": (10, 2, 20, 3)})
        disks = parse_diskstats(
            b" 8 0 bad x 0 0 0 0 0 0 0 0 0\n"
            b" 8 0 sda 1 0 2 3 4 0 5 6 0 7 0\n"
        )
        self.assertEqual(disks, {"sda": (1, 2, 4, 5, 7, 3, 6)})

    def test_procs_utf8_unescape_and_garbled_statm(self):
        self.assertEqual(unescape_unit(r"app-gnome-caf\xc3\xa9"), "app-gnome-café")
        with tempfile.TemporaryDirectory() as d:
            proc = Path(d) / "proc/123"
            _w(proc / "stat", "123 (app) S 1 1 1 0 -1 0 0 0 0 0 1 1 0 0 20 0 1 0 1 100 5 0\n")
            _w(proc / "statm", "bad\n")
            _w(proc / "comm", "app\n")
            _w(proc / "cmdline", "app\0")
            rows = ProcessWalker(d).walk(1.0)
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0].mem_partial)

    def test_disk_analyzer_octal_mountinfo_utf8_and_spaces(self):
        line = ("1 1 0:1 / /mnt/caf\\303\\251\\040dir rw,relatime - ext4 /dev/sda1 rw\n")
        with mock.patch("builtins.open", return_value=io.StringIO(line)):
            mounts = disk_analyzer._nested_mounts("/mnt")
        self.assertEqual(mounts, {"/mnt/café dir"})

    def test_power_unreadable_adapter_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            ac = Path(d) / "sys/class/power_supply/AC0"
            _w(ac / "type", "Mains\n")
            vals, info = {}, {}
            Power(d).sample(1.0, vals, info)
            self.assertIsNone(info["battery"]["ac_online"])

    def test_sensors_nvme_fallback_never_fabricates_device_and_rapl_tuple(self):
        with tempfile.TemporaryDirectory() as d:
            hw = Path(d) / "sys/class/hwmon/hwmon4"
            _w(hw / "name", "nvme\n")
            (hw / "device").mkdir()
            _w(hw / "temp1_input", "35000\n")
            rapl = Path(d) / "sys/class/powercap/intel-rapl:0"
            _w(rapl / "energy_uj", "1000000\n")
            _w(rapl / "max_energy_range_uj", "100000000\n")
            s = Sensors(d)
            vals, info = {}, {}
            s.sample(1.0, vals, info)
            self.assertNotIn("device", info["sensors"]["nvme"])
            self.assertIn("hwmon4", info["sensors"]["nvme"])
            _w(rapl / "energy_uj", "11000000\n")
            vals, info = {}, {}
            s.sample(2.0, vals, info)
            self.assertAlmostEqual(vals["power.cpu"], 10.0)
            self.assertEqual(s._rapl_prev, (11000000, 2.0))

    def test_pci_name_falls_through_to_second_file(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = Path(d) / "pci1.ids"
            p2 = Path(d) / "pci2.ids"
            p1.write_text("1002  AMD\n\t0001  Other\n")
            p2.write_text("1002  AMD\n\t747e  Radeon RX\n")
            with mock.patch.object(pci, "PCI_IDS", (str(p1), str(p2))), \
                    mock.patch.dict(pci._names, {}, clear=True):
                self.assertEqual(pci.pci_name("1002", "747e"), "Radeon RX")

    def test_sampler_dual_amd_without_boot_vga(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for card, addr, vram in (("card0", "0000:03:00.0", 8 << 30),
                                     ("card1", "0000:04:00.0", 512 << 20)):
                dev = root / "sys/class/drm" / card / "device"
                _w(dev / "driver_name", "amdgpu\n")
                _w(dev / "address", addr + "\n")
                _w(dev / "boot_vga", "0\n")
                _w(dev / "mem_info_vram_total", f"{vram}\n")
            s = Sampler(str(root))
            self.assertEqual(s.igpu.info["card_name"], "card1")
            self.assertEqual(s.dgpu.dev["addr"], "0000:03:00.0")


class FlatpakAndMcpTests(unittest.TestCase):
    def test_flatpak_preserves_partial_error_with_items(self):
        list_cp = mock.Mock(returncode=0, stdout="org.app\t1.0\tstable\tx86_64\tuser\n")
        sys_cp = mock.Mock(returncode=0, stdout="org.sys\t2.0\tstable\tx86_64\tflathub\t10 MB\n")
        usr_cp = mock.Mock(returncode=1, stdout="", stderr="remote unreachable")
        with mock.patch("shutil.which", return_value="/usr/bin/flatpak"), \
                mock.patch("asusius.updates.flatpak._run",
                           side_effect=[list_cp, sys_cp, usr_cp]):
            res = flatpak.check()
        self.assertEqual(len(res.items), 1)
        self.assertIn("user: remote unreachable", res.error)

    def test_mcp_exact_max_line_boundary_and_broken_pipe(self):
        exact = b"x" * (mcp.MAX_LINE - 1) + b"\n"
        over_one = b"y" * mcp.MAX_LINE + b"\n"
        after = b'{"ok":1}\n'
        stream = io.BytesIO(exact + over_one + after)
        got = list(mcp._readlines(stream))
        self.assertEqual(got, [exact, None, after])

        class PipeOut:
            def write(self, _data):
                raise BrokenPipeError()

            def flush(self):
                pass

        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode() + b"\n"
        rc = mcp.serve(service=mock.Mock(), stdin=io.BytesIO(req), stdout=PipeOut())
        self.assertEqual(rc, 0)

    def test_cpu_reader_publishes_governor_without_full_policy(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _w(root / "proc/stat", "cpu  10 0 5 100 0 0 0 0\ncpu0 10 0 5 100 0 0 0 0\n")
            _w(root / "proc/loadavg", "0.10 0.10 0.10 1/100 100\n")
            _w(root / "proc/uptime", "10.0 5.0\n")
            _w(root / "sys/devices/system/cpu/online", "0\n")
            _w(root / "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", "powersave\n")
            reader = CpuReader(str(root))
            self.assertFalse(reader.want_policy)
            vals, info = {}, {}
            reader.sample(1.0, vals, info)
            self.assertEqual(info["cpu"]["governor"], "powersave")
            _w(root / "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", "performance\n")
            reader.sample(3.0, vals, info)
            self.assertEqual(info["cpu"]["governor"], "powersave")
            reader.sample(7.0, vals, info)
            self.assertEqual(info["cpu"]["governor"], "performance")


if __name__ == "__main__":
    unittest.main()
