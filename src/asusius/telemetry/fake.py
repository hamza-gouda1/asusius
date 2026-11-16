# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""A fake /proc + /sys for the telemetry layer (development and screenshots only).

`ensure(root)` adds the files the readers need to a tree made by
backends.fake.build_fake_tree — including the deterministic disk-analyzer demo tree
under `FAKE_HOME` (nested dirs, a hardlinked pair, sparse "large" files, a .cache and
a node_modules dir) and the nvme0n1 metadata files (model/serial/wwn/scheduler) the
Storage reader reports. `FakeWorld.advance(now)` moves the counters like a short
gaming burst so graphs have shape. Everything is written inside `root`.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

from .upower import HISTORY_SERIES, UpowerClient

NCPU = 24
P_THREADS = 16
CLK = 100
PAGE = 4096
GIB = 1 << 30
MEM_TOTAL = 16 * GIB - 420 * (1 << 20)
ZRAM_TABLE_BYTES = 64 * PAGE  # zram0's fixed table overhead on top of the compressed pool

# ---- disk analyzer demo tree (pages + tests; deterministic sizes) ----------------
# FAKE_HOME stands in for the "/home" filesystem: a page points scan_path() at
# <root>/FAKE_HOME and gets the same tree on every run. Files listed as sparse are
# created with one written byte + truncate (large apparent size, ~4 KiB on disk), so
# the allocated-vs-apparent difference is visible without burning real space. dup1.bin
# and dup2.bin are one inode hardlinked under two names (the scanner must count it
# once). The fake root cannot create real distinct st_dev mounts without root, so
# mount variety lives in FAKE_MOUNTS metadata; the st_dev boundary rule itself is
# unit-tested by injecting a foreign st_dev at the DirEntry layer (test_disk_analyzer).
FAKE_HOME = "fake/home/user"
NVME_MODEL = "WD PC SN5000S SDEQNSJ-512G-1002"
NVME_SERIAL = "WD-5QJ4V7LR"
NVME_WWN = "eui.0badb0d5512g1002"
NVME_SCHEDULER = "none [mq-deadline] kyber bfq"
# (path relative to FAKE_HOME, apparent size in bytes, sparse?)
FAKE_HOME_FILES: tuple[tuple[str, int, bool], ...] = (
    (".cache/mozilla/cache.bin", 3 << 20, False),
    (".cache/thumbs.db", 64 << 10, False),
    ("Documents/notes.md", 12 << 10, False),
    ("Documents/report.pdf", 2 << 20, False),
    ("Downloads/ubuntu-24.04.iso", 128 << 20, True),
    ("Projects/app/__pycache__/main.cpython-311.pyc", 1 << 10, False),
    ("Projects/app/bundle.node", 72 << 20, True),
    ("Projects/app/node_modules/leftpad/index.js", 2 << 10, False),
    ("Projects/app/node_modules/leftpad/leftpad.node", 68 << 20, True),
    ("Projects/app/src/main.py", 8 << 10, False),
    ("Projects/app/src/deep/deeper/leaf.py", 4 << 10, False),
    ("VMs/win10.qcow2", 96 << 20, True),
    ("Videos/capture.mkv", 96 << 20, True),
    ("dup1.bin", 48 << 20, True),
    ("swap.img", 64 << 20, True),
)
# Multiple mounts with distinct st_dev semantics: every mount point is its own
# filesystem in the real world. The fake tree models them as metadata only (see the
# FAKE_HOME comment); "/home" below is the mount whose content FAKE_HOME stands in for.
FAKE_MOUNTS: tuple[dict, ...] = (
    {"source": "/dev/nvme0n1p5", "mount": "/", "fstype": "ext4", "size": 468 * GIB,
     "used": 291 * GIB, "avail": 153 * GIB, "disk": "nvme0n1"},
    {"source": "/dev/nvme0n1p1", "mount": "/boot/efi", "fstype": "vfat",
     "size": 256 << 20, "used": 38 << 20, "avail": 218 << 20, "disk": "nvme0n1"},
    {"source": "/dev/nvme0n1p6", "mount": "/home", "fstype": "ext4",
     "size": 512 * GIB, "used": 300 * GIB, "avail": 212 * GIB, "disk": "nvme0n1"},
)

# (pid, ppid, comm, cmdline, cgroup unit, base cpu % of one core, private MiB, gpu)
PROCS = [
    (1, 0, "systemd", "/sbin/init splash", "init.scope", 0.1, 12, None),
    (2, 0, "kthreadd", "", "", 0.0, 0, None),
    (85, 2, "kworker/3:1-events", "", "", 0.3, 0, None),
    (412, 1, "systemd-journal", "/usr/lib/systemd/systemd-journald",
     "systemd-journald.service", 0.2, 30, None),
    (1180, 1, "systemd", "/usr/lib/systemd/systemd --user",
     "user@1000.service/init.scope", 0.0, 9, None),
    (1240, 1180, "pipewire", "/usr/bin/pipewire",
     "user@1000.service/session.slice/pipewire.service", 1.4, 22, None),
    (1252, 1180, "wireplumber", "/usr/bin/wireplumber",
     "user@1000.service/session.slice/wireplumber.service", 0.2, 18, None),
    (1460, 1180, "gnome-shell", "/usr/bin/gnome-shell",
     "user@1000.service/session.slice/org.gnome.Shell@wayland.service", 6.0, 380, "igpu"),
    (2101, 1180, "firefox", "/usr/lib/firefox/firefox",
     "user@1000.service/app.slice/app-gnome-firefox-2101.scope", 9.0, 640, "igpu"),
    (2150, 2101, "Isolated Web Co", "/usr/lib/firefox/firefox -contentproc -isForBrowser",
     "user@1000.service/app.slice/app-gnome-firefox-2101.scope", 14.0, 420, None),
    (2163, 2101, "Isolated Web Co", "/usr/lib/firefox/firefox -contentproc -isForBrowser",
     "user@1000.service/app.slice/app-gnome-firefox-2101.scope", 4.0, 260, None),
    (2170, 2101, "RDD Process", "/usr/lib/firefox/firefox -contentproc rdd",
     "user@1000.service/app.slice/app-gnome-firefox-2101.scope", 2.0, 40, "igpu"),
    (2201, 2101, "Web Content", "/usr/lib/firefox/firefox -contentproc",
     "user@1000.service/app.slice/app-gnome-firefox-2101.scope", 0.5, 90, None),
    (2610, 1180, "code", "/usr/share/code/code --unity-launch",
     "user@1000.service/app.slice/app-gnome-code-2610.scope", 3.0, 310, "igpu"),
    (2633, 2610, "code", "/usr/share/code/code --type=renderer",
     "user@1000.service/app.slice/app-gnome-code-2610.scope", 5.0, 520, None),
    (2655, 2610, "code", "/usr/share/code/code --type=utility",
     "user@1000.service/app.slice/app-gnome-code-2610.scope", 1.0, 180, None),
    (3010, 1180, "nautilus", "/usr/bin/nautilus --gapplication-service",
     "user@1000.service/app.slice/app-gnome-org.gnome.Nautilus-3010.scope", 0.1, 95, None),
    (3330, 1180, "gnome-terminal-", "/usr/libexec/gnome-terminal-server",
     "user@1000.service/app.slice/app-gnome-org.gnome.Terminal-3330.scope", 0.4, 60, None),
    (3351, 3330, "bash", "bash", "user@1000.service/app.slice/app-gnome-org.gnome.Terminal-"
     "3330.scope", 0.0, 5, None),
    (3420, 1180, "steam", "/home/user/.steam/ubuntu12_32/steam",
     "user@1000.service/app.slice/app-gnome-steam-3420.scope", 2.0, 410, None),
    (3488, 3420, "Cyberpunk2077.e", "Z:\\Cyberpunk2077\\bin\\x64\\Cyberpunk2077.exe",
     "user@1000.service/app.slice/app-gnome-steam-3420.scope", 160.0, 5200, "dgpu"),
    (3502, 3420, "wineserver", "/usr/bin/wineserver",
     "user@1000.service/app.slice/app-gnome-steam-3420.scope", 3.0, 30, None),
    (4100, 1180, "python3", "python3 -m asusius.ui",
     "user@1000.service/app.slice/app-gnome-io.github.hamzagouda.Asusius-4100.scope",
     0.6, 70, "igpu"),
]


def _w(path: Path, text) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{text}\n")


def _sparse(path: Path, apparent: int) -> None:
    """Sparse file: apparent size `apparent`, one written byte (~4 KiB on disk)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0")
        f.truncate(apparent)


def load_at(t: float) -> float:
    return max(0.0, min(1.0, 0.5 + 0.45 * math.sin(t / 14.0) + 0.15 * math.sin(t / 3.1)))


def ensure(root: str | Path) -> None:
    """Add telemetry files to a fake tree (idempotent)."""
    root = Path(root)
    if (root / "fake/telemetry-ready").exists():
        return
    cpu = root / "sys/devices/system/cpu"
    _w(cpu / "online", f"0-{NCPU - 1}")
    _w(root / "sys/devices/cpu_core/cpus", f"0-{P_THREADS - 1}")
    _w(root / "sys/devices/cpu_atom/cpus", f"{P_THREADS}-{NCPU - 1}")
    for i in range(NCPU):
        d = cpu / f"cpu{i}"
        p = i < P_THREADS
        core = (i // 2) * 4 if p else 32 + (i - P_THREADS)
        _w(d / "topology/core_id", core)
        _w(d / "topology/physical_package_id", 0)
        _w(d / "topology/thread_siblings_list", f"{i - i % 2}-{i - i % 2 + 1}" if p else i)
        _w(d / "cpufreq/cpuinfo_max_freq", 5_000_000 if p else 3_700_000)
        _w(d / "cpufreq/cpuinfo_min_freq", 800_000)
        _w(d / "cpufreq/base_frequency", 2_200_000 if p else 1_600_000)
        _w(d / "cpufreq/scaling_cur_freq", 800_000)
        _w(d / "cpufreq/scaling_driver", "intel_pstate")
        _w(d / "cpufreq/scaling_governor", "powersave")
        _w(d / "cpufreq/energy_performance_preference",
           "balance_performance" if p else "power")  # two policy groups: 0-15 and 16-23
        # §2.1 cache layout: L1d/L1i private per core, L2 shared per P pair or private
        # per E core, L3 Unified shared by every core (identical content under cpuN)
        cache = d / "cache"
        _w(cache / "index0/level", 1)
        _w(cache / "index0/type", "Data")
        _w(cache / "index0/size", "48K")
        _w(cache / "index1/level", 1)
        _w(cache / "index1/type", "Instruction")
        _w(cache / "index1/size", "32K")
        _w(cache / "index2/level", 2)
        _w(cache / "index2/type", "Data")
        if p:
            lo = i - i % 2
            _w(cache / "index2/size", "4096 K")
            _w(cache / "index2/shared_cpu_list", f"{lo}-{lo + 1}")
        else:
            _w(cache / "index2/size", "2048 K")
            _w(cache / "index2/shared_cpu_list", "")
        _w(cache / "index3/level", 3)
        _w(cache / "index3/type", "Unified")
        _w(cache / "index3/size", "24576 K")
        _w(cache / "index3/shared_cpu_list", "0-23")
    _w(cpu / "intel_pstate/status", "active")
    _w(cpu / "intel_pstate/no_turbo", 0)
    _w(root / "proc/cpuinfo", "processor\t: 0\nmodel name\t: Intel(R) Core(TM) i7-14650HX\n")
    _w(root / "proc/pressure/memory",
       "some avg10=0.00 avg60=0.00 avg300=0.12 total=42130000\n"
       "full avg10=0.00 avg60=0.00 avg300=0.03 total=9120000\n")
    hw = root / "sys/class/hwmon/hwmon7"
    _w(hw / "temp1_label", "Package id 0")
    cores = sorted({(i // 2) * 4 for i in range(P_THREADS)} | set(range(32, 40)))
    for n, core in enumerate(cores, start=2):
        _w(hw / f"temp{n}_label", f"Core {core}")
        _w(hw / f"temp{n}_input", 50000)
    nv = root / "sys/class/hwmon/hwmon4"
    _w(nv / "name", "nvme")
    for n, lab in enumerate(("Composite", "Sensor 1", "Sensor 2"), 1):
        _w(nv / f"temp{n}_label", lab)
        _w(nv / f"temp{n}_input", 38850 + 4000 * (n == 2))
    (nv / "device").mkdir(parents=True, exist_ok=True)
    (nv / "device/nvme0n1").mkdir(exist_ok=True)
    _w(root / "sys/class/hwmon/hwmon6/name", "spd5118")
    _w(root / "sys/class/hwmon/hwmon6/temp1_input", 43500)
    _w(root / "sys/class/hwmon/hwmon2/name", "acpitz")
    _w(root / "sys/class/hwmon/hwmon2/temp1_input", 27800)
    bat = root / "sys/class/power_supply/BAT0"
    _w(bat / "type", "Battery")
    _w(bat / "voltage_now", 15_820_000)
    _w(bat / "energy_now", 40_380_000)
    _w(bat / "model_name", "GU60558")
    _w(root / "sys/class/power_supply/ADP0/type", "Mains")
    _w(root / "sys/class/power_supply/ADP0/online", 0)
    blk = root / "sys/block/nvme0n1"
    _w(blk / "device/model", NVME_MODEL)
    _w(blk / "device/serial", NVME_SERIAL)
    _w(blk / "device/wwid", NVME_WWN)
    _w(blk / "size", 1_000_215_216)
    _w(blk / "queue/rotational", 0)
    _w(blk / "queue/scheduler", NVME_SCHEDULER)
    _w(blk / "removable", 0)
    (root / "sys/block/loop0").mkdir(parents=True, exist_ok=True)
    # zram swap devices (separate from the physical swap and from zswap): zram0 carries
    # the load-animated pool, zram1 sits idle with a measured all-zero mm_stat
    zorig = int(1.75 * GIB)
    zcompr = zorig // 4
    _w(root / "sys/block/zram0/mm_stat",
       f"{zorig} {zcompr} {int(zcompr * 1.1) + ZRAM_TABLE_BYTES} 0 0 0 0 0")
    _w(root / "sys/block/zram1/mm_stat", "0 0 0 0 0 0 0 0")
    for name, wifi, state in (("wlp0s20f3", True, "up"), ("enp0s31f6", False, "down"),
                              ("docker0", None, "down")):
        d = root / "sys/class/net" / name
        _w(d / "type", 1)
        _w(d / "operstate", state)
        _w(d / "speed", -1)
        if wifi is not None:
            (d / "device").mkdir(parents=True, exist_ok=True)
        if wifi:
            (d / "wireless").mkdir(parents=True, exist_ok=True)
    _w(root / "sys/class/net/lo/type", 772)
    card = root / "sys/class/drm/card1"
    for k, v in (("act", 0), ("cur", 700), ("max", 1600), ("min", 300), ("RP0", 1600),
                 ("boost", 1600)):
        _w(card / f"gt_{k}_freq_mhz", v)
    _w(card / "gt/gt0/rc6_residency_ms", 0)
    _w(card / "gt/gt0/throttle_reason_status", 0)
    _w(card / "device/driver_name", "i915")
    _w(card / "device/address", "0000:00:02.0")
    _w(card / "device/device", "0xa78b")
    # iGPU PCIe link, read through the card's device dir while runtime PM allows it
    _w(card / "device/current_link_speed", "8.0 GT/s")
    _w(card / "device/max_link_speed", "16.0 GT/s")
    _w(card / "device/current_link_width", 8)
    _w(card / "device/max_link_width", 16)
    pci = root / "sys/bus/pci/devices"
    _w(pci / "0000:00:02.0/device", "0xa78b")
    _w(pci / "0000:01:00.0/device", "0x2dd8")
    _w(pci / "0000:00:02.0/power_state", "D0")
    _w(pci / "0000:01:00.0/power_state", "D0")
    # dGPU PCIe link files on its PCI device dir; read only while the GPU is awake
    dg = pci / "0000:01:00.0"
    _w(dg / "current_link_speed", "16.0 GT/s")
    _w(dg / "max_link_speed", "16.0 GT/s")
    _w(dg / "current_link_width", 16)
    _w(dg / "max_link_width", 16)
    if not (dg / "power/runtime_status").exists():
        _w(dg / "power/runtime_status", "active")  # awake so QA reads the link
    _w(root / "fake/mounts.json", json.dumps(FAKE_MOUNTS))
    home = root / FAKE_HOME
    for rel, size, sparse in FAKE_HOME_FILES:
        p = home / rel
        if sparse:
            _sparse(p, size)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(bytes(size))
    dup1 = home / "dup1.bin"
    if not (home / "dup2.bin").exists():  # one inode, two names; the scanner dedups it
        os.link(dup1, home / "dup2.bin")
    for pid, ppid, comm, cmd, unit, _cpu, mem, gpu in PROCS:
        d = root / "proc" / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + (b"\0" if cmd else b""))
        _w(d / "cgroup", f"0::/user.slice/user-1000.slice/{unit}" if unit.startswith("user@")
           else (f"0::/system.slice/{unit}" if unit else "0::/"))
        (d / "fd").mkdir(exist_ok=True)
        (d / "fdinfo").mkdir(exist_ok=True)
        if gpu == "igpu":
            link = d / "fd/9"
            if not link.is_symlink():
                os.symlink("/dev/dri/renderD129", link)
    (root / "proc/net").mkdir(parents=True, exist_ok=True)
    _w(root / "fake/telemetry-ready", 1)


class FakeWorld:
    """Integrates fake load into monotonic counters (jiffies, sectors, bytes, ns)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        ensure(self.root)
        self.t: float | None = None
        self.cpu = [[0] * 8 for _ in range(NCPU)]  # user nice system idle iowait irq soft st
        self.ctxt = self.intr = 0
        self.disk = [0, 0, 0, 0, 0, 0, 0]
        self.net = {"wlp0s20f3": [0, 0, 0, 0], "enp0s31f6": [0, 0, 0, 0],
                    "docker0": [0, 0, 0, 0]}
        self.proc_ticks = {p[0]: 0 for p in PROCS}
        self.proc_io = {p[0]: [0, 0] for p in PROCS}
        self.render_ns = {p[0]: 0 for p in PROCS}
        self.rc6_ms = 0
        self.uptime = 18_450.0

    def advance(self, now: float) -> None:
        if self.t is None:
            self.t = now - 1.0
        dt = max(0.0, now - self.t)
        self.t = now
        self.uptime += dt
        L = load_at(now)
        r = self.root
        lines = []
        tot = [0] * 8
        for i, c in enumerate(self.cpu):
            p = i < P_THREADS
            u = min(0.98, 0.04 + L * (0.75 if p else 0.45) * (0.6 + 0.4 * math.sin(i * 1.7 +
                                                                                      now / 5)))
            ticks = dt * CLK
            c[0] += int(ticks * u * 0.8)
            c[2] += int(ticks * u * 0.2)
            c[4] += int(ticks * 0.004)
            c[3] += int(ticks * (1 - u))
            mhz = 800 + (4200 if p else 2900) * min(1.0, u * 1.1)
            _w(r / f"sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq", int(mhz * 1000))
            for k in range(8):
                tot[k] += c[k]
            lines.append(f"cpu{i} " + " ".join(map(str, c)) + " 0 0")
        self.ctxt += int(dt * (9000 + 30000 * L))
        self.intr += int(dt * (6000 + 20000 * L))
        (r / "proc/stat").write_text(
            "cpu  " + " ".join(map(str, tot)) + " 0 0\n" + "\n".join(lines) +
            f"\nintr {self.intr} 0\nctxt {self.ctxt}\nbtime 1792705358\nprocesses 1096000\n"
            f"procs_running {1 + int(L * 9)}\nprocs_blocked 0\n")
        _w(r / "proc/loadavg", f"{0.6 + 7 * L:.2f} {0.5 + 5 * L:.2f} {1.9:.2f} "
           f"{1 + int(L * 9)}/1874 41022")
        _w(r / "proc/uptime", f"{self.uptime:.2f} {self.uptime * 20:.2f}")
        hw = r / "sys/class/hwmon/hwmon7"
        pkg = 48 + 34 * L
        _w(hw / "temp1_input", int(pkg * 1000))
        for n in range(2, 18):
            _w(hw / f"temp{n}_input", int((pkg - 6 + 5 * math.sin(n + now / 7)) * 1000))
        _w(r / "sys/class/hwmon/hwmon8/fan1_input", int(2600 + 3200 * L))
        _w(r / "sys/class/hwmon/hwmon8/fan2_input", int(2300 + 3000 * L))
        used = int(MEM_TOTAL * (0.48 + 0.22 * L))
        free = int(MEM_TOTAL * 0.09)
        cached = MEM_TOTAL - used - free
        kb = 1024
        (r / "proc/meminfo").write_text(
            f"MemTotal: {MEM_TOTAL // kb} kB\nMemFree: {free // kb} kB\n"
            f"MemAvailable: {(free + cached) // kb} kB\nBuffers: {180 << 10} kB\n"
            f"Cached: {(cached - (500 << 20)) // kb} kB\nSwapCached: {72 << 10} kB\n"
            f"Shmem: {1300 << 10} kB\nSReclaimable: {320 << 10} kB\nSUnreclaim: {210 << 10} kB\n"
            f"Dirty: {int(2 + 30 * L) << 10} kB\nWriteback: 0 kB\nAnonPages: {used // kb} kB\n"
            f"Mapped: {900 << 10} kB\nSwapTotal: {4 << 20} kB\n"
            f"SwapFree: {int((4 << 20) * (0.62 - 0.05 * L))} kB\nZswap: 0 kB\nZswapped: 0 kB\n"
            f"CommitLimit: {12 << 20} kB\nCommitted_AS: {int((14 + 6 * L) * (1 << 20))} kB\n")
        # zram pool tracks the workload (orig ≈ (0.5+2.5·L) GiB, ~4× compression,
        # mem_used_total ≈ pool + table overhead); zram1 stays a measured zero
        zorig = int((0.5 + 2.5 * L) * GIB)
        zcompr = max(1, zorig // 4)
        _w(r / "sys/block/zram0/mm_stat",
           f"{zorig} {zcompr} {int(zcompr * 1.1) + ZRAM_TABLE_BYTES} 0 0 0 0 0")
        d = self.disk
        rd = (2 + 180 * L ** 3) * (1 << 20) * dt
        wr = (1 + 40 * L * abs(math.sin(now / 9))) * (1 << 20) * dt
        d[0] += int(rd / 65536) + 1
        d[1] += int(rd / 512)
        d[2] += int(wr / 32768) + 1
        d[3] += int(wr / 512)
        d[4] += int(dt * 1000 * min(1.0, 0.02 + 0.5 * L ** 2))
        d[5] += max(1, int(rd / 65536 * (0.4 + L)))
        d[6] += max(1, int(wr / 32768 * (0.8 + L)))
        (r / "proc/diskstats").write_text(
            f" 259 0 nvme0n1 {d[0]} 0 {d[1]} {d[5]} {d[2]} 0 {d[3]} {d[6]} 0 {d[4]} "
            f"{d[5] + d[6]} 0 0 0 0 0 0\n"
            f" 259 3 nvme0n1p5 {d[0]} 0 {d[1]} {d[5]} {d[2]} 0 {d[3]} {d[6]} 0 {d[4]} "
            f"{d[5] + d[6]} 0 0 0 0\n"
            "   7 0 loop0 3 0 24 0 0 0 0 0 0 1 0 0 0 0 0 0 0\n")
        w = self.net["wlp0s20f3"]
        w[0] += int(dt * (40e3 + 4.2e6 * L * (0.5 + 0.5 * math.sin(now / 4))))
        w[1] += int(dt * 900)
        w[2] += int(dt * (12e3 + 0.6e6 * L))
        w[3] += int(dt * 400)
        rows = []
        for name, v in [("lo", [123456, 900, 123456, 900])] + list(self.net.items()):
            rows.append(f"{name:>8}: {v[0]} {v[1]} 0 0 0 0 0 0 {v[2]} {v[3]} 0 0 0 0 0 0")
        (r / "proc/net/dev").write_text(
            "Inter-|   Receive\n face |bytes    packets\n" + "\n".join(rows) + "\n")
        (r / "proc/net/wireless").write_text(
            "Inter-| sta-|   Quality\n face | tus | link level noise\n"
            " wlp0s20f3: 0000   58.  -52.  -256        0      0      0      0      0        0\n")
        # iGPU
        card = r / "sys/class/drm/card1"
        busy = 0.10 + 0.35 * L
        self.rc6_ms += int(dt * 1000 * (1 - busy - 0.08))
        _w(card / "gt/gt0/rc6_residency_ms", self.rc6_ms)
        _w(card / "gt_act_freq_mhz", int(300 + 1100 * busy))
        _w(card / "gt_cur_freq_mhz", int(350 + 1150 * busy))
        # processes
        game_dgpu = 0.25 + 0.7 * L
        for pid, ppid, comm, cmd, unit, cpu_pct, mem, gpu in PROCS:
            k = cpu_pct * (0.4 + 1.2 * L) if pid in (3488, 2150, 1460) else cpu_pct
            self.proc_ticks[pid] += int(dt * CLK * k / 100 + 0.5)
            ticks = self.proc_ticks[pid]
            pd = r / "proc" / str(pid)
            rss = int(mem * (1 << 20) * (1 + (0.1 * L if pid == 3488 else 0)) / PAGE)
            (pd / "stat").write_text(
                f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194560 0 0 0 0 {ticks * 3 // 4} "
                f"{ticks // 4} 0 0 20 0 {6 if rss else 1} 0 {1000 + pid} 0 {rss} "
                "18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 3 0 0 0 0 0\n")
            shared = rss // 6
            (pd / "statm").write_text(f"{rss * 3} {rss + shared} {shared} 10 0 {rss} 0\n")
            io = self.proc_io[pid]
            if pid in (3488, 2610, 2101):
                io[0] += int(dt * L * (9e6 if pid == 3488 else 2e5))
                io[1] += int(dt * L * (1.5e6 if pid == 3488 else 8e4))
            (pd / "status").write_text(
                f"Name:\t{comm}\nVmRSS:\t{rss * PAGE // 1024} kB\n"
                f"VmSwap:\t{(mem * 1024 // 3) if mem else 0} kB\n")
            (pd / "io").write_text(f"rchar: {io[0]}\nwchar: {io[1]}\nread_bytes: {io[0]}\n"
                                   f"write_bytes: {io[1]}\n")
            if gpu == "igpu":
                share = {1460: 0.55, 2101: 0.3, 2170: 0.1}.get(pid, 0.05)
                self.render_ns[pid] += int(dt * 1e9 * busy * share)
                (pd / "fdinfo/9").write_text(
                    "pos:\t0\nflags:\t02104002\ndrm-driver:\ti915\n"
                    f"drm-client-id:\t{pid}\ndrm-pdev:\t0000:00:02.0\n"
                    f"drm-total-system0:\t{mem * 300} KiB\n"
                    f"drm-engine-render:\t{self.render_ns[pid]} ns\n"
                    f"drm-engine-copy:\t{self.render_ns[pid] // 50} ns\n"
                    f"drm-engine-video:\t{self.render_ns[pid] // 9 if pid == 2170 else 0} ns\n"
                    "drm-engine-video-enhance:\t0 ns\n")
        _w(r / "sys/class/power_supply/BAT0/power_now", int((14 + 46 * L) * 1e6))
        vram = int((2.1 + 3.6 * L) * GIB)
        (r / "fake/nvml.json").write_text(json.dumps({
            "static": {"name": "NVIDIA GeForce RTX 5050 Laptop GPU",
                       "driver_version": "580.178.04", "max_gr_mhz": 3090,
                       "max_mem_mhz": 12001},
            "query": {"util": round(100 * game_dgpu), "mem_util": round(60 * game_dgpu),
                      "vram_used": vram, "vram_total": 8151 << 20, "gr_mhz": int(
                          1200 + 1500 * game_dgpu), "mem_mhz": 12001, "video_mhz": 1800,
                      "temp": int(46 + 32 * L), "power": round(12 + 78 * game_dgpu, 1),
                      "power_limit": 100.0, "fan": None, "pstate": 0 if L > 0.15 else 2,
                      "enc": round(15 * L), "dec": round(8 * L)},
            "procs": {"3488": {"type": "C+G", "vram": vram - (380 << 20),
                               "sm": round(100 * game_dgpu)},
                      "1460": {"type": "G", "vram": 2 << 20, "sm": None}}}))


# ---- fake UPower (tests and QA; injected into ui.live.LiveHub via upower=) ----------

# A fixed epoch base so every generated timestamp — and therefore every QA
# screenshot — is deterministic across runs.
UPOWER_BASE_T = 1_810_000_000.0
UPOWER_CHARGING, UPOWER_DISCHARGING = 1, 2  # UpDeviceState ids (telemetry.upower)

UPOWER_BAT0 = "/org/freedesktop/UPower/devices/battery_BAT0"
UPOWER_BAT1 = "/org/freedesktop/UPower/devices/battery_BAT1"
UPOWER_AC = "/org/freedesktop/UPower/devices/line_power_AC"
UPOWER_MOUSE = "/org/freedesktop/UPower/devices/mouse_0003o046Do101Ax0006"
UPOWER_UPS = "/org/freedesktop/UPower/devices/ups_dev"
UPOWER_DUP = "/org/freedesktop/UPower/devices/battery_BAT0b"
UPOWER_SYSFS_BAT0 = "/sys/devices/LNXSYSTM:00/LNXSYBUS:00/PNP0C0A:00/power_supply/BAT0"
UPOWER_SYSFS_BAT1 = "/sys/devices/LNXSYSTM:00/LNXSYBUS:00/PNP0C0A:00/power_supply/BAT1"


class FakeUpowerTransport:
    """In-memory org.freedesktop.UPower implementing A's UpowerTransport seam.

    Devices are raw property dicts shaped like GetAll(org.freedesktop.UPower.Device)
    replies; history rows are (t, value, state) triples keyed by (object path, series
    name). Mutating the dicts between client calls drives hotplug / removal / restart
    drills in tests and the QA harness. No D-Bus, no gi, no threads.
    """

    def __init__(self, owner: bool = True) -> None:
        self.owner = owner  # NameHasOwner answer; False simulates an absent service
        self.devices: dict[str, dict] = {}               # object path -> properties
        self.history: dict[tuple[str, str], list] = {}   # (object path, series) -> rows
        self.calls: list[str] = []

    # -- fixture editing -----------------------------------------------------------------
    def add(self, object_path: str, **props) -> dict:
        self.devices[object_path] = dict(props)
        return self.devices[object_path]

    def remove(self, object_path: str) -> None:
        """Hotplug-out: the device disappears from the next EnumerateDevices."""
        self.devices.pop(object_path, None)
        for key in [k for k in self.history if k[0] == object_path]:
            del self.history[key]

    def set_history(self, object_path: str, series: str, rows: list) -> None:
        self.history[(object_path, series)] = list(rows)

    # -- the UpowerTransport protocol ----------------------------------------------------
    def name_has_owner(self, name: str) -> bool:
        self.calls.append("name_has_owner")
        return self.owner

    def call(self, dest: str, path: str, iface: str, method: str,
             sig: str | None = None, args: tuple = ()):
        self.calls.append(method)
        if method == "EnumerateDevices":
            return (sorted(self.devices),)
        if method == "GetHistory":
            assert sig == "(suu)" and args[0] in HISTORY_SERIES
            series = args[0]
            return (list(self.history.get((path, series), [])),)
        raise AssertionError(f"unexpected D-Bus call {method}")

    def get_all(self, dest: str, path: str, iface: str) -> dict:
        self.calls.append("get_all")
        return dict(self.devices.get(path, {}))


def upower_battery_props(native_path: str = UPOWER_SYSFS_BAT0, model: str = "GU60558",
                         percent: float | None = 87.5, state: int = UPOWER_DISCHARGING,
                         watts: float | None = 12.4, voltage: float | None = 15.7,
                         energy: float | None = 44.6, energy_full: float | None = 51.3,
                         energy_design: float | None = 60.0, cycles: int | None = 128,
                         present: bool = True) -> dict:
    """One laptop battery as UPower's GetAll returns it (identity = native_path +
    model; the serial is deliberately omitted from the fixtures)."""
    props: dict = {"NativePath": native_path, "Type": 2, "State": state,
                   "Model": model, "Vendor": "ASUSTeK", "IsPresent": present}
    if percent is not None:
        props["Percentage"] = percent
    if watts is not None:
        props["EnergyRate"] = watts
    if voltage is not None:
        props["Voltage"] = voltage
    if energy is not None:
        props["Energy"] = energy
    if energy_full is not None:
        props["EnergyFull"] = energy_full
    if energy_design is not None:
        props["EnergyFullDesign"] = energy_design
    if cycles is not None:
        props["CycleCount"] = cycles
    return props


def upower_battery_history(hours: float = 24.0, step_s: float = 300.0,
                           base_t: float = UPOWER_BASE_T, gap: bool = True,
                           max_points: int | None = None) -> dict[str, list]:
    """Deterministic charge/rate/voltage history over the last `hours`.

    The story: plugged in and charging for the older half (percent rising to 100,
    high rate, ~16.1 V), then discharging for the newer half (percent falling, the
    draw wandering around 12-24 W, voltage sagging). A gap sits in the middle —
    UPower lost samples there and the chart must break line and fill, never
    interpolate. Newest point lands exactly on base_t; rows are oldest first.
    max_points keeps only the newest points (the "short" scenario).
    """
    n = max(2, int(hours * 3600 / step_s))
    charge: list[tuple] = []
    rate: list[tuple] = []
    voltage: list[tuple] = []
    for i in range(n + 1):
        f = i / n
        t = base_t - (n - i) * step_s
        if gap and 0.45 <= f < 0.55:  # the hole: no rows at all here
            continue
        if f < 0.5:  # charging stretch
            state = UPOWER_CHARGING
            pct = min(100.0, 55.0 + 90.0 * f)
            w = 28.0 + 4.0 * math.sin(i * 0.7)
            v = 16.10 + 0.05 * math.sin(i * 0.3)
        else:        # discharging stretch
            state = UPOWER_DISCHARGING
            pct = max(38.0, 100.0 - 62.0 * (f - 0.5) * 2)
            w = 15.0 + 5.0 * math.sin(i * 0.9) + 3.0 * math.sin(i * 0.23)
            v = 15.80 - 0.9 * (f - 0.5) * 2
        charge.append((int(t), round(pct, 1), state))
        rate.append((int(t), round(max(2.0, w), 2), state))
        voltage.append((int(t), round(v, 3), state))
    if max_points is not None:
        charge, rate, voltage = (s[-max_points:] for s in (charge, rate, voltage))
    return {"charge": charge, "rate": rate, "voltage": voltage}


def _add_battery(transport: FakeUpowerTransport, path: str, native_path: str,
                 model: str, percent: float, history: dict[str, list] | None,
                 **props) -> None:
    transport.add(path, **upower_battery_props(native_path=native_path, model=model,
                                               percent=percent, **props))
    if history is not None:
        for series, rows in history.items():
            transport.set_history(path, series, rows)


def fake_upower_transport(scenario: str = "populated",
                          base_t: float = UPOWER_BASE_T) -> FakeUpowerTransport:
    """A configured fake UPower bus for the QA battery modes and the tests.

    Scenarios (each also carries the AC line power, which is never a battery row):
    populated (24 h charge/rate/voltage with charging + discharging states and one
    gap), short (a handful of points), empty (devices but no history rows),
    peripheral (laptop plus a mouse with a coarse percent), multi (two laptop
    batteries -> the aggregate case), duplicate (two UPower records sharing one
    native_path + model, which render as two rows) and ups (a UPS in its own group).
    """
    t = FakeUpowerTransport()
    t.add(UPOWER_AC, Type=1, Model="AC", NativePath="/sys/devices/AC")
    populated = upower_battery_history(base_t=base_t)
    if scenario == "populated":
        _add_battery(t, UPOWER_BAT0, UPOWER_SYSFS_BAT0, "GU60558", 87.5, populated)
    elif scenario == "short":
        _add_battery(t, UPOWER_BAT0, UPOWER_SYSFS_BAT0, "GU60558", 87.5,
                     upower_battery_history(base_t=base_t, max_points=5))
    elif scenario == "empty":
        _add_battery(t, UPOWER_BAT0, UPOWER_SYSFS_BAT0, "GU60558", 87.5, None)
    elif scenario == "peripheral":
        _add_battery(t, UPOWER_BAT0, UPOWER_SYSFS_BAT0, "GU60558", 87.5, populated)
        t.add(UPOWER_MOUSE, Type=5, Percentage=65, State=UPOWER_DISCHARGING,  # coarse
              Model="Wireless Mouse", Vendor="Logi", IsPresent=True, Voltage=None,
              NativePath="")
    elif scenario == "multi":
        _add_battery(t, UPOWER_BAT0, UPOWER_SYSFS_BAT0, "GU60558", 87.5, populated)
        _add_battery(t, UPOWER_BAT1, UPOWER_SYSFS_BAT1, "GU60558B", 41.0,
                     upower_battery_history(hours=24.0, step_s=600.0, base_t=base_t),
                     energy=21.0, energy_full=50.0, energy_design=60.0)
    elif scenario == "duplicate":
        # one physical battery reported twice: same native_path + model, two rows
        _add_battery(t, UPOWER_BAT0, UPOWER_SYSFS_BAT0, "GU60558", 87.5, populated)
        _add_battery(t, UPOWER_DUP, UPOWER_SYSFS_BAT0, "GU60558", 87.5, populated)
    elif scenario == "ups":
        _add_battery(t, UPOWER_BAT0, UPOWER_SYSFS_BAT0, "GU60558", 87.5, populated)
        t.add(UPOWER_UPS, Type=3, Percentage=89, State=UPOWER_DISCHARGING,
              Model="Back-UPS ES 700", Vendor="APC", IsPresent=True, EnergyRate=34.0,
              Voltage=12.1, NativePath="/sys/devices/ups")
    else:
        raise ValueError(f"unknown UPower scenario {scenario!r}")
    return t


def fake_upower_client(scenario: str = "populated",
                       base_t: float | None = None) -> UpowerClient:
    """A real UpowerClient over a fake transport: the object LiveHub(upower=...)
    and LiveHub.set_upower(...) take (tests, QA harness)."""
    return UpowerClient(fake_upower_transport(scenario, time.time() if base_t is None else base_t))
