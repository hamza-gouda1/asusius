# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Milestone 2 telemetry: CPU caches, policy groups, zram, DDR and gated PCIe links.

Every fixture is a hand-built tree under a tempdir (inline data, the existing test
style). Nothing here touches live dconf, hardware or the network, and every absent
source stays absent (None / missing key) — never a fabricated zero, never a probe of a
suspended device.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from asusius.backends.fake import build_fake_tree
from asusius.telemetry import gpu_amd as amd_mod
from asusius.telemetry import gpu_intel as intel_mod
from asusius.telemetry import pci as pci_mod
from asusius.telemetry.cpu import CpuReader, build_caches, parse_cache_size
from asusius.telemetry.gpu_amd import AmdDiscreteGpu, AmdGpu
from asusius.telemetry.gpu_intel import IntelGpu, pcie_link, read_pcie_link
from asusius.telemetry.gpu_nvidia import NvidiaGpu
from asusius.telemetry.memory import MemoryReader, derive, parse_dmi_memory, parse_mm_stat

MEMINFO = "MemTotal: 100 kB\nMemFree: 50 kB\nMemAvailable: 60 kB\n"


def _w(path: Path, text: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{text}\n")


def _link_files(dev: Path, cur_speed="8.0 GT/s", max_speed="16.0 GT/s", cur_width="8",
                max_width="16", with_max=True) -> None:
    _w(dev / "current_link_speed", cur_speed)
    _w(dev / "current_link_width", cur_width)
    if with_max:
        _w(dev / "max_link_speed", max_speed)
        _w(dev / "max_link_width", max_width)


# -- §2.1 CPU caches --------------------------------------------------------------------

def _cache_fixture(root: Path) -> None:
    """Contract §2.1 pinned layout: 16 P threads (8 pairs) + 8 E cores + shared L3."""
    for i in range(24):
        d = root / f"sys/devices/system/cpu/cpu{i}/cache"
        _w(d / "index0/level", 1)
        _w(d / "index0/type", "Data")
        _w(d / "index0/size", "48K")
        _w(d / "index1/level", 1)
        _w(d / "index1/type", "Instruction")
        _w(d / "index1/size", "32K")
        _w(d / "index2/level", 2)
        _w(d / "index2/type", "Data")
        _w(d / "index3/level", 3)
        _w(d / "index3/type", "Unified")
        _w(d / "index3/size", "24576 K")
        _w(d / "index3/shared_cpu_list", "0-23")
        if i < 16:  # P threads share one 4 MiB L2 per physical core pair
            lo = i - i % 2
            _w(d / "index2/size", "4096 K")
            _w(d / "index2/shared_cpu_list", f"{lo}-{lo + 1}")
        else:  # E cores have a private 2 MiB L2
            _w(d / "index2/size", "2048 K")
            _w(d / "index2/shared_cpu_list", "")


class CacheTests(unittest.TestCase):
    def test_size_strings(self):
        cases = {"4096 K": 4096 << 10, "256K": 256 << 10, "32M": 32 << 20, "1G": 1 << 30,
                 "131072": 131072, "2048 KiB": 2048 << 10, " 64 k ": 64 << 10}
        for text, want in cases.items():
            self.assertEqual(parse_cache_size(text), want, text)

    def test_size_malformed_is_none(self):
        for text in ("", None, "broken", "12x", "-4 K", "K", "4 T", "0x10", "2.5 M"):
            self.assertIsNone(parse_cache_size(text), text)

    def test_zero_size_is_unknown(self):
        # the kernel writes 0 when the size is unknown: absence, never a "N x 0 B" group
        for text in ("0 K", "0", "0M", "0k", "0 KiB"):
            self.assertIsNone(parse_cache_size(text), text)

    def test_fixture_dedup_and_groups(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _cache_fixture(root)
            caches = build_caches(str(root), list(range(24)))
            self.assertEqual(set(caches), {1, 2, 3})
            self.assertEqual(caches[1]["Data"],
                             [{"instances": 24, "size_bytes": 48 << 10,
                               "scope": "per-core"}])
            self.assertEqual(caches[1]["Instruction"],
                             [{"instances": 24, "size_bytes": 32 << 10,
                               "scope": "per-core"}])
            # cpu1 reads the same L2 dir content as cpu0: identical identity collapses
            l2 = caches[2]["Data"]
            self.assertEqual([g["instances"] for g in l2], [8, 8])
            self.assertEqual([g["size_bytes"] for g in l2], [4096 << 10, 2048 << 10])
            self.assertEqual(l2[0]["scope"], "0-1, 2-3, 4-5, 6-7, 8-9, 10-11, 12-13, 14-15")
            self.assertEqual(l2[1]["scope"], "per-core")
            self.assertEqual(caches[3]["Unified"],
                             [{"instances": 1, "size_bytes": 24576 << 10, "scope": "0-23"}])
            # totals per (level, type) = sum over groups of instances x size
            self.assertEqual(sum(g["instances"] * g["size_bytes"] for g in l2), 48 << 20)

    def test_malformed_instances_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "t"
            _w(root / "sys/devices/system/cpu/cpu0/cache/index2/level", 2)
            _w(root / "sys/devices/system/cpu/cpu0/cache/index2/type", "Data")
            _w(root / "sys/devices/system/cpu/cpu0/cache/index2/size", "broken")
            _w(root / "sys/devices/system/cpu/cpu1/cache/index2/level", 2)
            _w(root / "sys/devices/system/cpu/cpu1/cache/index2/type", "Data")
            _w(root / "sys/devices/system/cpu/cpu1/cache/index2/size", "2048K")
            _w(root / "sys/devices/system/cpu/cpu1/cache/index2/shared_cpu_list", "0-1")
            # bad type and unreadable level never create instances either
            _w(root / "sys/devices/system/cpu/cpu0/cache/index3/level", 3)
            _w(root / "sys/devices/system/cpu/cpu0/cache/index3/type", "Unified2")
            _w(root / "sys/devices/system/cpu/cpu0/cache/index3/size", "1M")
            _w(root / "sys/devices/system/cpu/cpu1/cache/index3/level", "many")
            _w(root / "sys/devices/system/cpu/cpu1/cache/index3/type", "Unified")
            _w(root / "sys/devices/system/cpu/cpu1/cache/index3/size", "1M")
            # size the kernel reports as unknown is not a real zero cache
            _w(root / "sys/devices/system/cpu/cpu0/cache/index4/level", 2)
            _w(root / "sys/devices/system/cpu/cpu0/cache/index4/type", "Data")
            _w(root / "sys/devices/system/cpu/cpu0/cache/index4/size", "0 K")
            _w(root / "sys/devices/system/cpu/cpu0/cache/index4/shared_cpu_list", "0-1")
            caches = build_caches(str(root), [0, 1])
            self.assertEqual(caches[2]["Data"],
                             [{"instances": 1, "size_bytes": 2048 << 10, "scope": "0-1"}])
            self.assertNotIn(3, caches)  # nothing valid: no (level, type) cell at all

    def test_reversed_shared_list_is_skipped(self):
        # a non-empty shared_cpu_list that parses to nothing ("5-3") is bad sysfs: the
        # instance is skipped, never silently reclassified as private per-core (which
        # would double-count a shared cache)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "t"
            for cpu in (0, 1):
                d = root / f"sys/devices/system/cpu/cpu{cpu}/cache/index2"
                _w(d / "level", 2)
                _w(d / "type", "Data")
                _w(d / "size", "1024K")
                _w(d / "shared_cpu_list", "5-3")
            self.assertEqual(build_caches(str(root), [0, 1]), {})

    def test_no_cache_dirs_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(build_caches(d, [0, 1]), {})

    def test_sample_publishes_caches_every_tick(self):
        # the per-tick info dict is rebuilt from scratch: a static-only field would
        # never reach the page, so sample() must republish the topology every tick
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _cache_fixture(root)
            _w(root / "sys/devices/system/cpu/online", "0-23")
            lines = ["cpu  10 20 30 40 50 60 70 80"]
            lines += [f"cpu{i} 1 2 3 4 5 6 7 8" for i in range(24)]
            (root / "proc").mkdir(parents=True)
            (root / "proc/stat").write_text("\n".join(lines) + "\n")
            r = CpuReader(str(root))
            self.assertEqual(set(r.static["caches"]), {1, 2, 3})
            vals, info = {}, {}
            r.sample(1.0, vals, info)
            self.assertEqual(info["cpu"]["caches"], r.static["caches"])
            r.sample(2.0, vals, info)
            self.assertEqual(info["cpu"]["caches"], r.static["caches"])


# -- §2.2 policy groups -------------------------------------------------------------------

def _policy_fixture(root: Path, epp_of) -> None:
    """Per-cpu cpufreq files; epp_of(i) returns the EPP text or None to omit the file."""
    _w(root / "sys/devices/system/cpu/online", "0-23")
    _w(root / "sys/devices/system/cpu/intel_pstate/status", "active")
    _w(root / "sys/devices/system/cpu/intel_pstate/no_turbo", 0)
    for i in range(24):
        d = root / f"sys/devices/system/cpu/cpu{i}/cpufreq"
        _w(d / "scaling_driver", "intel_pstate")
        _w(d / "scaling_governor", "powersave")
        epp = epp_of(i)
        if epp is not None:
            _w(d / "energy_performance_preference", epp)


class PolicyTests(unittest.TestCase):
    def test_sample_only_reads_policy_when_requested_and_due(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _policy_fixture(root, lambda i: "power")
            _w(root / "proc/stat", "cpu 1 0 0 1 0 0 0 0")
            reader = CpuReader(str(root))
            with patch.object(reader, "policy", wraps=reader.policy) as policy:
                info = {}
                reader.sample(1.0, {}, info)
                self.assertNotIn("policy", info["cpu"])
                reader.want_policy = True
                reader.sample(2.0, {}, info)
                self.assertEqual(info["cpu"]["policy"]["epp"], "power")
                reader.sample(3.0, {}, info)
                policy.assert_called_once()
                reader.sample(7.0, {}, info)
                self.assertEqual(policy.call_count, 2)
                reader.want_policy = False
                reader.sample(8.0, {}, info)
                self.assertNotIn("policy", info["cpu"])

    def test_uniform_single_group(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _policy_fixture(root, lambda i: "balance_performance")
            p = CpuReader(str(root)).policy()
            self.assertEqual(p["driver"], "intel_pstate")
            self.assertEqual(p["governor"], "powersave")
            self.assertEqual(p["epp"], "balance_performance")
            self.assertEqual(p["pstate_status"], "active")
            self.assertEqual(p["no_turbo"], "0")
            self.assertEqual(p["groups"], [{"driver": "intel_pstate",
                                            "governor": "powersave",
                                            "epp": "balance_performance",
                                            "pstate_status": "active", "no_turbo": "0",
                                            "cpus": list(range(24))}])

    def test_two_groups_sorted_by_first_cpu(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _policy_fixture(root, lambda i: "power" if i >= 16 else "balance_performance")
            groups = CpuReader(str(root)).policy()["groups"]
            self.assertEqual([g["epp"] for g in groups], ["balance_performance", "power"])
            self.assertEqual([g["cpus"] for g in groups],
                             [list(range(16)), list(range(16, 24))])

    def test_absent_epp_stays_none(self):
        # e.g. acpi-cpufreq: EPP absent -> None -> "–", never inferred from the governor
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            _policy_fixture(root, lambda i: "balance_performance" if i < 16 else None)
            p = CpuReader(str(root)).policy()
            self.assertEqual(p["epp"], "balance_performance")  # cpu0-based top-level key
            second = p["groups"][1]
            self.assertIsNone(second["epp"])
            self.assertEqual(second["governor"], "powersave")
            self.assertEqual(second["cpus"], list(range(16, 24)))


# -- §2.3 zram ----------------------------------------------------------------------------

class ZramTests(unittest.TestCase):
    def test_memory_partition_when_available_is_smaller_than_free(self):
        mem = derive({"MemTotal": 100, "MemFree": 20, "MemAvailable": 10})
        self.assertEqual(mem["available"], 10)
        # derive v2 (M5 contract §1.2): in_use = total - available holds directly
        # even when available < free; reclaimable clamps to 0 and the old
        # three-way partition identity is accepted to break here.
        self.assertEqual(mem["in_use"], 90)
        self.assertEqual(mem["free"], 20)
        self.assertEqual(mem["reclaimable"], 0)

    def _tree(self, root: Path, stats: dict[str, str | None]) -> None:
        _w(root / "proc/meminfo", MEMINFO)
        for name, text in stats.items():
            if text is None:  # unreadable: a directory where the file should be
                (root / f"sys/block/{name}/mm_stat").mkdir(parents=True)
            else:
                _w(root / f"sys/block/{name}/mm_stat", text)

    def _sample(self, root: Path) -> tuple[dict, dict]:
        vals, info = {}, {}
        MemoryReader(str(root)).sample(1.0, vals, info)
        return vals, info["mem"]

    def test_mm_stat_parse(self):
        self.assertEqual(parse_mm_stat("15728640 3869106 4198400 0 0 64 0 0"),
                         (15728640, 3869106, 4198400))
        self.assertEqual(parse_mm_stat("1024 512"), (1024, 512, None))
        self.assertEqual(parse_mm_stat("1024"), (1024, None, None))
        self.assertEqual(parse_mm_stat("1024 xyz 300"), (1024, None, 300))
        self.assertEqual(parse_mm_stat("100 -5 300"), (100, None, 300))
        self.assertEqual(parse_mm_stat("0 0 0 0 0 0 0 0"), (0, 0, 0))  # real zero
        for text in ("", None, "abc def"):
            self.assertIsNone(parse_mm_stat(text))

    def test_normal_device(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, {"zram0": "15728640 3869106 4198400 0 0 64 0 0"})
            vals, mem = self._sample(root)
            self.assertEqual(vals["mem.zram_orig"], 15728640)
            self.assertEqual(vals["mem.zram_compr"], 3869106)
            self.assertEqual(vals["mem.zram_total"], 4198400)
            self.assertEqual(mem["zram"], [{"device": "zram0", "orig": 15728640,
                                            "compr": 3869106, "total": 4198400}])
            self.assertTrue(mem["zram_any"])

    def test_zero_device_publishes_measured_zero(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, {"zram0": "0 0 0 0 0 0 0 0"})
            vals, mem = self._sample(root)
            self.assertEqual(vals["mem.zram_orig"], 0)  # a real zero, not absent
            self.assertEqual(vals["mem.zram_compr"], 0)
            self.assertEqual(vals["mem.zram_total"], 0)
            self.assertEqual(mem["zram"], [{"device": "zram0", "orig": 0, "compr": 0,
                                            "total": 0}])

    def test_two_devices_sum(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, {"zram0": "1024 512 700 0 0 0 0 0",
                              "zram1": "0 0 0 0 0 0 0 0"})  # a measured zero device
            vals, mem = self._sample(root)
            self.assertEqual(vals["mem.zram_orig"], 1024)
            self.assertEqual(vals["mem.zram_total"], 700)
            self.assertEqual([r["device"] for r in mem["zram"]], ["zram0", "zram1"])
            self.assertEqual(mem["zram"][1], {"device": "zram1", "orig": 0, "compr": 0,
                                              "total": 0})

    def test_short_fields(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, {"zram0": "1024 512"})
            vals, mem = self._sample(root)
            self.assertEqual(vals["mem.zram_orig"], 1024)
            self.assertEqual(vals["mem.zram_compr"], 512)
            self.assertNotIn("mem.zram_total", vals)  # nobody parsed it: absent, not 0
            self.assertIsNone(mem["zram"][0]["total"])

    def test_malformed_fields_become_none(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, {"zram0": "1024 xyz 300"})
            vals, mem = self._sample(root)
            self.assertEqual(vals["mem.zram_orig"], 1024)
            self.assertEqual(vals["mem.zram_total"], 300)
            self.assertNotIn("mem.zram_compr", vals)
            self.assertEqual(mem["zram"][0], {"device": "zram0", "orig": 1024,
                                              "compr": None, "total": 300})

    def test_unreadable_or_garbage_devices_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, {"zram0": None, "zram1": "abc def"})
            vals, mem = self._sample(root)
            self.assertFalse(any(k.startswith("mem.zram") for k in vals))
            self.assertNotIn("zram", mem)
            self.assertTrue(mem["zram_any"])  # the block devices do exist

    def test_absent_devices_publish_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, {})
            vals, mem = self._sample(root)
            self.assertFalse(any(k.startswith("mem.zram") for k in vals))
            self.assertNotIn("zram", mem)
            self.assertFalse(mem["zram_any"])  # no fabricated zero over an absent device

    def test_empty_sigma_per_field(self):
        # zram1 parses only orig -> total never published even though a device exists
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, {"zram0": "100 200", "zram1": "300"})
            vals, mem = self._sample(root)
            self.assertEqual(vals["mem.zram_orig"], 400)
            self.assertEqual(vals["mem.zram_compr"], 200)
            self.assertNotIn("mem.zram_total", vals)
            self.assertEqual([r["device"] for r in mem["zram"]], ["zram0", "zram1"])


# -- §2.4 DDR (DMI) -----------------------------------------------------------------------

def _dmi_raw(size_mib: int | None, mtype: int, speed: int, slot: str | None) -> bytes:
    """A minimal SMBIOS type-17 record: formatted area + string table."""
    raw = bytearray(0x1C)
    raw[0] = 0x11  # Memory Device
    raw[1] = 0x1C  # formatted length (SMBIOS 2.6+ layout)
    if size_mib is not None:
        raw[0x0C:0x0E] = size_mib.to_bytes(2, "little")
    raw[0x10] = 1 if slot else 0
    raw[0x11] = 2 if slot else 0  # independent bank locator index
    raw[0x14] = mtype
    raw[0x17:0x19] = speed.to_bytes(2, "little")
    return bytes(raw) + (slot.encode() + b"\x00Bank 0\x00\x00" if slot else b"\x00\x00")


class DdrTests(unittest.TestCase):
    def test_declared_length_bounds_the_memory_type_field(self):
        raw = bytearray(_dmi_raw(8192, 0x22, 5600, "DIMM0"))
        raw[1] = 0x14
        self.assertIsNone(parse_dmi_memory(bytes(raw)))

    def test_extended_size_and_decoder_type(self):
        raw = bytearray(_dmi_raw(0x7FFF, 0x22, 5600, "DIMM0"))
        raw[0x1C:0x1C] = (32768).to_bytes(4, "little")
        raw[1] = 0x20
        self.assertEqual(parse_dmi_memory(bytes(raw)), {
            "slot": "DIMM0", "type": "DDR5", "speed_mts": 5600, "size_bytes": 32 << 30,
        })
        raw[0x0C:0x0E] = (0x8008).to_bytes(2, "little")
        self.assertEqual(parse_dmi_memory(bytes(raw))["size_bytes"], 8 << 10)

    def test_inventory_read_once_even_when_absent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, None)
            reader = MemoryReader(str(root))
            with patch.object(reader, "ddr", return_value=None) as ddr:
                for now in (1.0, 2.0, 3.0):
                    reader.sample(now, {}, {})
                ddr.assert_called_once()

    def _tree(self, root: Path, raws: list[bytes] | None, unreadable: bool = False) -> None:
        _w(root / "proc/meminfo", MEMINFO)
        if raws is None:
            return
        for n, raw in enumerate(raws):
            d = root / f"sys/firmware/dmi/entries/17-{n}"
            d.mkdir(parents=True)
            if unreadable:
                (d / "raw").mkdir()  # opening a directory raises OSError
            else:
                (d / "raw").write_bytes(raw)

    def _sample(self, root: Path) -> dict:
        vals, info = {}, {}
        MemoryReader(str(root)).sample(1.0, vals, info)
        return info["mem"]

    def test_modules_parse(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, [_dmi_raw(8192, 0x1A, 3200, "Channel0-DIMM0"),
                              _dmi_raw(8192, 0x1A, 3200, "Channel1-DIMM0")])
            mem = self._sample(root)
            self.assertEqual(mem["ddr"], {"modules": [
                {"slot": "Channel0-DIMM0", "type": "DDR4", "speed_mts": 3200,
                 "size_bytes": 8 << 30},
                {"slot": "Channel1-DIMM0", "type": "DDR4", "speed_mts": 3200,
                 "size_bytes": 8 << 30}]})

    def test_empty_slot_skipped_and_unknown_size_stays_none(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, [_dmi_raw(0, 0x1A, 3200, "Spare"),  # size 0: empty slot
                              _dmi_raw(0xFFFF, 0x1A, 3200, "DIMM0")])
            mem = self._sample(root)
            self.assertEqual(mem["ddr"]["modules"], [{"slot": "DIMM0", "type": "DDR4",
                                                      "speed_mts": 3200,
                                                      "size_bytes": None}])

    def test_truncated_speed_field_stays_none(self):
        # the record claims length 0x1C but the dump stops at 0x18: slicing the speed
        # word would fabricate a value from leftover bytes, so it must stay None
        raw = bytearray(0x18)
        raw[0] = 0x11
        raw[1] = 0x1C
        raw[0x0C:0x0E] = (8192).to_bytes(2, "little")
        raw[0x14] = 0x1A
        raw[0x17] = 0x64  # first half of the speed word, the second half is missing
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, [bytes(raw)])
            self.assertEqual(MemoryReader(str(root)).ddr(), {"modules": [
                {"slot": None, "type": "DDR4", "speed_mts": None, "size_bytes": 8 << 30}]})

    def test_unknown_type_code_stays_none(self):
        # SMBIOS "Unknown" (0x02) must not be published as a literal string
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, [_dmi_raw(8192, 0x02, 3200, "DIMM0")])
            self.assertEqual(MemoryReader(str(root)).ddr(), {"modules": [
                {"slot": "DIMM0", "type": None, "speed_mts": 3200, "size_bytes": 8 << 30}]})

    def test_absent_or_unreadable_dmi_is_none(self):
        self.assertIsNone(MemoryReader("/nonexistent").ddr())
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, None)
            self.assertIsNone(MemoryReader(str(root)).ddr())  # no dmi entries at all
            self.assertNotIn("ddr", self._sample(root))
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._tree(root, [_dmi_raw(8192, 0x18, 3200, "DIMM0")], unreadable=True)
            self.assertIsNone(MemoryReader(str(root)).ddr())  # OSError -> None
            self.assertNotIn("ddr", self._sample(root))  # never a guess


# -- §2.5/§2.6 PCIe link and suspended no-probe ------------------------------------------

class LinkTests(unittest.TestCase):
    def test_read_pcie_link(self):
        with tempfile.TemporaryDirectory() as d:
            dev = Path(d) / "dev"
            _link_files(dev)
            self.assertEqual(read_pcie_link(str(dev)),
                             {"current_speed": "8.0 GT/s", "max_speed": "16.0 GT/s",
                              "current_width": "8", "max_width": "16"})
            self.assertIsNone(read_pcie_link(None))
            self.assertIsNone(read_pcie_link(str(Path(d) / "empty")))  # nothing readable

    def test_max_link_cache(self):
        # max link is cached (a dedicated attribute, never `static`) and reused when a
        # later awake sample cannot read the max files; current is re-read every time
        with tempfile.TemporaryDirectory() as d:
            awake = Path(d) / "awake"
            _link_files(awake)
            link, cache = pcie_link(str(awake), None)
            self.assertEqual(cache, {"max_speed": "16.0 GT/s", "max_width": "16"})
            reduced = Path(d) / "reduced"
            _link_files(reduced, cur_speed="2.5 GT/s", cur_width="2", with_max=False)
            link, cache = pcie_link(str(reduced), cache)
            self.assertEqual(link, {"current_speed": "2.5 GT/s", "max_speed": "16.0 GT/s",
                                    "current_width": "2", "max_width": "16"})
            self.assertEqual(cache, {"max_speed": "16.0 GT/s", "max_width": "16"})

    def test_sleeping_nvidia_dgpu_is_never_probed(self):
        # contract §2.6: link files present but the GPU sleeps -> nothing is read and
        # no link key is published (Boom-style counters, cf. test_sleeping_dgpu_is_
        # never_queried in test_telemetry.py)
        with tempfile.TemporaryDirectory() as d:
            root = build_fake_tree(Path(d) / "root", dgpu_runtime="suspended")
            dev = root / "sys/bus/pci/devices/0000:01:00.0"
            (dev / "power_state").write_text("D3cold\n")
            _link_files(dev)
            g = NvidiaGpu(str(root))

            class Boom:
                def __getattr__(self, name):
                    raise AssertionError(f"NVML {name} called on a sleeping GPU")

            real_pci_read = pci_mod.read_str

            def pci_guard(path):  # a Boom that still allows the safe PM files
                if "link" in os.path.basename(path):
                    raise AssertionError(f"device attr read on a sleeping GPU: {path}")
                return real_pci_read(path)

            def link_boom(path):
                raise AssertionError(f"link attribute read on a sleeping GPU: {path}")

            def _noop():
                return None

            g.nvml, g.backend = Boom(), "nvml"
            g.release = _noop
            with patch.object(pci_mod, "read_str", side_effect=pci_guard) as pci_reads, \
                    patch.object(intel_mod, "read_str", side_effect=link_boom) as link_reads:
                vals, info = {}, {}
                g.sample(1.0, vals, info)
            self.assertEqual(link_reads.call_args_list, [])  # the link reader never ran
            self.assertEqual([c.args[0] for c in pci_reads.call_args_list],
                             [str(dev / "power/runtime_status"),
                              str(dev / "power_state")])  # only the safe PM files
            self.assertEqual(info["dgpu"]["state"], "sleeping")
            self.assertNotIn("link", info["dgpu"])  # cached max is not republished
            self.assertFalse(any(k.startswith("dgpu.") for k in vals))

    def test_nvidia_link_polling_skips_idle_backoff_and_reports_decode_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = build_fake_tree(Path(d) / "root")
            g = NvidiaGpu(str(root), fake=True)
            g._hold_until = 5.0
            with patch.object(g, "_query_fake", return_value={"dec": 0, "util": 5}) as query, \
                    patch("asusius.telemetry.gpu_nvidia.pcie_link",
                          return_value=({"current_width": "16"}, None)) as link:
                vals, info = {}, {}
                g.sample(1.0, vals, info)
                self.assertEqual(info["dgpu"]["state"], "idle-released")
                self.assertNotIn("link", info["dgpu"])
                link.assert_not_called()
                query.assert_not_called()
                g.sample(6.0, vals, info)
                link.assert_called_once()
                self.assertEqual(info["dgpu"]["link"]["current_width"], "16")
                self.assertEqual(vals["dgpu.dec"], 0)
                self.assertNotIn("dgpu.enc", vals)

    def _amd_card(self, root: Path, runtime: str, with_link: bool = True) -> Path:
        """One amdgpu card whose device dir stands in for its PCI device dir."""
        dev = root / "sys/class/drm/card2/device"
        _w(dev / "driver_name", "amdgpu")
        _w(dev / "address", "0000:03:00.0")
        _w(dev / "device", "0x747e")
        _w(dev / "power/runtime_status", runtime)
        if with_link:
            _link_files(dev)
        return dev

    def test_sleeping_amd_dgpu_is_never_probed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            dev = self._amd_card(root, "suspended")
            real_pci_read = pci_mod.read_str

            def pci_guard(path):
                if "link" in os.path.basename(path):
                    raise AssertionError(f"device attr read on a sleeping GPU: {path}")
                return real_pci_read(path)

            def amd_boom(path):
                raise AssertionError(f"sysfs attr read on a sleeping GPU: {path}")

            g = AmdDiscreteGpu(str(root))
            with patch.object(pci_mod, "read_str", side_effect=pci_guard) as pci_reads, \
                    patch.object(amd_mod, "read_str", side_effect=amd_boom) as amd_reads:
                vals, info = {}, {}
                g.sample(1.0, vals, info)
            self.assertEqual(amd_reads.call_args_list, [])  # no other sysfs attr read
            self.assertEqual([c.args[0] for c in pci_reads.call_args_list],
                             [str(dev / "power/runtime_status"),
                              str(dev / "power_state")])
            self.assertEqual(info["dgpu"]["state"], "sleeping")
            self.assertNotIn("link", info["dgpu"])
            self.assertFalse(any(k.startswith("dgpu.") for k in vals))

    def test_awake_amd_dgpu_publishes_link_and_engines(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._amd_card(root, "active")
            proc = root / "proc/100"
            (proc / "fd").mkdir(parents=True)
            os.symlink("/dev/dri/renderD128", proc / "fd/9")
            fdinfo = ("pos:\t0\nflags:\t02104002\ndrm-driver:\tamdgpu\n"
                      "drm-client-id:\t1\ndrm-pdev:\t0000:03:00.0\n"
                      "drm-engine-render:\t0 ns\ndrm-engine-capacity-render:\t1\n")
            (proc / "fdinfo").mkdir()
            (proc / "fdinfo/9").write_text(fdinfo)
            g = AmdDiscreteGpu(str(root))
            vals, info = {}, {}
            g.sample(10.0, vals, info)  # baseline: no delta yet
            self.assertEqual(info["dgpu"]["engines"], ["render"])
            self.assertFalse(any(k.startswith("dgpu.engine.") for k in vals))
            (proc / "fdinfo/9").write_text(fdinfo.replace("\t0 ns", "\t250000000 ns"))
            g.sample(11.0, vals, info)
            self.assertEqual(info["dgpu"]["state"], "awake")
            self.assertEqual(info["dgpu"]["link"],
                             {"current_speed": "8.0 GT/s", "max_speed": "16.0 GT/s",
                              "current_width": "8", "max_width": "16"})
            self.assertAlmostEqual(vals["dgpu.engine.render"], 25.0)
            self.assertEqual(info["dgpu"]["engines"], ["render"])

    def test_igpu_link_gate_intel(self):
        # read when runtime PM is off (no file) or active/unsupported; otherwise absent
        cases = {"active": True, "no-pm": True, "unsupported": True, "suspended": False,
                 "suspending": False}
        for state, want_link in cases.items():
            with self.subTest(state=state), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                dev = root / "sys/class/drm/card0/device"
                _w(dev / "driver_name", "i915")
                _link_files(dev)
                if state != "no-pm":
                    _w(dev / "power/runtime_status", state)
                vals, info = {}, {}
                IntelGpu(str(root)).sample(1.0, vals, info)
                self.assertTrue(info["igpu"]["present"])
                if want_link:
                    self.assertEqual(info["igpu"]["link"],
                                     {"current_speed": "8.0 GT/s", "max_speed": "16.0 GT/s",
                                      "current_width": "8", "max_width": "16"})
                else:
                    self.assertNotIn("link", info["igpu"])

    def test_igpu_link_gate_amd(self):
        for runtime, want_link in (("active", True), ("unsupported", True),
                                   ("suspended", False)):
            with self.subTest(runtime=runtime), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                self._amd_card(root, runtime)
                vals, info = {}, {}
                AmdGpu(str(root)).sample(1.0, vals, info)
                self.assertTrue(info["igpu"]["present"])
                if want_link:
                    self.assertEqual(info["igpu"]["link"]["current_speed"], "8.0 GT/s")
                    self.assertEqual(info["igpu"]["link"]["max_speed"], "16.0 GT/s")
                else:
                    self.assertNotIn("link", info["igpu"])

    def test_amd_dgpu_link_read_while_unsupported(self):
        # "unsupported" is on the NVIDIA whitelist (§2.5): the link must be read, not
        # mistaken for a suspended device
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._amd_card(root, "unsupported")
            vals, info = {}, {}
            AmdDiscreteGpu(str(root)).sample(1.0, vals, info)
            self.assertEqual(info["dgpu"]["state"], "awake")
            self.assertEqual(info["dgpu"]["link"],
                             {"current_speed": "8.0 GT/s", "max_speed": "16.0 GT/s",
                              "current_width": "8", "max_width": "16"})
            self.assertFalse(any(k.startswith("dgpu.engine.") for k in vals))


if __name__ == "__main__":
    unittest.main()
