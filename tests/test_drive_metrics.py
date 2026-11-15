# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Drive counters, sysfs metadata, and the fake sampler integration."""
import tempfile
import unittest
from pathlib import Path

from asusius.backends.fake import build_fake_tree
from asusius.telemetry import Sampler, fake
from asusius.telemetry.storage import Storage, disk_rates, parse_diskstats

FIX = Path(__file__).parent / "fixtures" / "telemetry"


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


class DiskstatsMetadataTests(unittest.TestCase):
    """parse_diskstats 7-tuples, await_ms math, and the new sysfs metadata keys."""

    def test_parse_diskstats_seven_tuple(self):
        line = (b" 8 0 sda 10 0 100 140 20 0 50 35 0 500 750 0 0 0 0\n"
                b" 8 1 sdb 1 2 3 4 5 6 7 8 9\n")        # 12 fields: dropped
        stats = parse_diskstats(line)
        self.assertEqual(stats["sda"], (10, 100, 20, 50, 500, 140, 35))
        self.assertEqual(len(stats["sda"]), 7)
        self.assertNotIn("sdb", stats)                  # short lines are skipped
        fixture = parse_diskstats(fx("diskstats_1"))
        self.assertEqual(fixture["nvme0n1"],
                         (4364949, 692723871, 1401217, 116563506, 460550, 2657455, 6894754))
        self.assertEqual(fixture["loop0"], (3, 24, 0, 0, 1, 0, 0))

    def test_disk_rates_await_ms_math_and_safety(self):
        prev = (10, 100, 20, 50, 0, 0, 0)
        cur = (30, 210, 40, 150, 1000, 130, 70)
        r = disk_rates(cur, prev, 1.0)
        self.assertEqual(r["read"], 110 * 512)
        self.assertEqual(r["write"], 100 * 512)
        self.assertEqual(r["busy"], 100.0)
        self.assertEqual(r["riops"], 20.0)
        self.assertEqual(r["wiops"], 20.0)
        self.assertAlmostEqual(r["await_ms"], 5.0)
        zero = disk_rates(cur, cur, 1.0)                   # zero completed-I/O delta
        self.assertIsNone(zero["await_ms"])
        self.assertIsNone(disk_rates(cur[:5], prev[:5], 1.0)["await_ms"])
        self.assertEqual(zero["read"], 0.0)
        self.assertIsNone(disk_rates((9, 100, 20, 50, 0, 0, 0), prev, 1.0))  # reset
        self.assertIsNone(disk_rates(cur, prev, 0.0))      # zero interval

    def test_disk_rates_on_repo_fixtures(self):
        a = parse_diskstats(fx("diskstats_1"))
        b = parse_diskstats(fx("diskstats_2"))
        r = disk_rates(b["nvme0n1"], a["nvme0n1"], 2.0)
        self.assertEqual(r["read"], 4096 * 512 / 2.0)
        self.assertEqual(r["write"], 2048 * 512 / 2.0)
        self.assertEqual(r["busy"], 12.5)
        self.assertEqual(r["riops"], 50.0)
        self.assertEqual(r["wiops"], 50.0)
        self.assertAlmostEqual(r["await_ms"], 1.0)  # 200 read/write ms / 200 completed reads/writes

    def test_storage_disk_metadata_keys(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            base = root / "sys/block"
            (base / "sda/device").mkdir(parents=True)
            for sub, val in (("device/model", "Model A"),
                             ("device/serial", "SER-SDA-001"),
                             ("device/wwid", "eui.sda-dev"),
                             ("queue/scheduler", "none [mq-deadline] kyber bfq")):
                p = base / "sda" / sub
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(val + "\n")
            (base / "sdb/device").mkdir(parents=True)  # empty device: only fallbacks
            (base / "sdb/wwid").write_text("eui.sdb-top\n")
            q = base / "sdb/queue"
            q.mkdir(parents=True, exist_ok=True)
            (q / "rotational").write_text("1\n")
            (base / "sdd/device").mkdir(parents=True)
            (base / "sdd/device/name").write_text("Model via name\n")

            disks = Storage(str(root)).disks
            self.assertEqual(set(disks), {"sda", "sdb", "sdd"})
            sda = disks["sda"]
            self.assertEqual(sda["model"], "Model A")
            self.assertEqual(sda["serial"], "SER-SDA-001")
            self.assertEqual(sda["wwn"], "eui.sda-dev")      # device/wwid wins
            self.assertEqual(sda["scheduler"], "none [mq-deadline] kyber bfq")
            self.assertEqual(sda["kind"], "Disk")
            sdb = disks["sdb"]
            self.assertIsNone(sdb["serial"])                 # missing file -> None
            self.assertIsNone(sdb["scheduler"])
            self.assertIsNone(sdb["model"])
            self.assertEqual(sdb["wwn"], "eui.sdb-top")      # top-level wwid fallback
            self.assertEqual(sdb["kind"], "HDD")
            sdd = disks["sdd"]
            self.assertEqual(sdd["model"], "Model via name")  # device/name fallback
            self.assertIsNone(sdd["serial"])
            self.assertIsNone(sdd["wwn"])                    # neither file present

    def test_sampler_emits_new_disk_series_and_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            root = build_fake_tree(Path(d) / "root")
            w = fake.FakeWorld(root)
            w.advance(100.0)
            s = Sampler(str(root), fake=True)
            for t in (101.0, 102.0, 103.0):
                w.advance(t)
                s.sample(t)
            st = s.store
            self.assertEqual(s.info["errors"], {})
            self.assertGreaterEqual(st.last("disk.nvme0n1.riops"), 0.0)
            self.assertGreaterEqual(st.last("disk.nvme0n1.wiops"), 0.0)
            self.assertGreaterEqual(st.last("disk.nvme0n1.await_ms"), 0.0)
            disks = s.info["disk"]["disks"]
            self.assertEqual(disks["nvme0n1"]["serial"], fake.NVME_SERIAL)
            self.assertEqual(disks["nvme0n1"]["wwn"], fake.NVME_WWN)
            self.assertEqual(disks["nvme0n1"]["scheduler"], fake.NVME_SCHEDULER)
            self.assertEqual(disks["nvme0n1"]["model"], fake.NVME_MODEL)
