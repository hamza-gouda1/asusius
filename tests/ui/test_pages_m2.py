# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Milestone 2 page wiring: pure helpers plus the real pages over fake telemetry trees.

PureHelperTests run without GTK state. WiringTests constructs the actual pages on a
stub window over a LiveHub / Sampler driven from fake trees. The tests do not use
desktop settings or invoke hardware controls.
"""
import os

import tempfile  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gdk  # noqa: E402

from asusius.backends.fake import build_fake_tree  # noqa: E402
from asusius.telemetry import fmt  # noqa: E402
from asusius.telemetry.memory import derive, parse_meminfo  # noqa: E402
from asusius.telemetry.ring import Store  # noqa: E402
from asusius.telemetry.gpu_amd import AmdDiscreteGpu  # noqa: E402
from asusius.telemetry.sampler import Sampler  # noqa: E402
from asusius.ui.live import LiveHub  # noqa: E402
from asusius.ui.pages.cpu import CpuPage, cache_cells, format_policy_groups  # noqa: E402
from asusius.ui.pages.gpu_monitor import (  # noqa: E402
    IntelGpuPage,
    NvidiaGpuPage,
    link_text,
    video_entries,
)
from asusius.ui.pages.memory import MemoryPage, ddr_text, zram_line  # noqa: E402
from asusius.ui.viewmodels import nb  # the StatGrid cell text goes through nb()  # noqa: E402

FIXTURE_MEMINFO = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry" / "meminfo"


def _w(path: Path, text: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{text}\n")


# -- pure helpers --------------------------------------------------------------------------

class PolicyGroupTests(unittest.TestCase):
    def test_single_or_no_group_keeps_cpu0_display(self):
        self.assertIsNone(format_policy_groups(None))
        self.assertIsNone(format_policy_groups([]))
        one = [{"driver": "intel_pstate", "governor": "powersave",
                "epp": "balance_performance", "cpus": [0, 1]}]
        self.assertIsNone(format_policy_groups(one))  # identical to today's cell

    def test_several_groups_join_with_ranges(self):
        groups = [{"governor": "powersave", "epp": "balance_performance",
                   "cpus": list(range(16))},
                  {"governor": "powersave", "epp": "power", "cpus": list(range(16, 24))}]
        self.assertEqual(format_policy_groups(groups),
                         "powersave · balance_performance (0-15) · powersave · power (16-23)")

    def test_absent_epp_stays_dash_and_ranges_compress(self):
        groups = [{"governor": "schedutil", "epp": None, "cpus": [0, 2, 3]},
                  {"governor": "performance", "epp": None, "cpus": [1]}]
        self.assertEqual(format_policy_groups(groups),
                         "schedutil · – (0, 2-3) · performance · – (1)")


class ZramLineTests(unittest.TestCase):
    def test_single_device(self):
        row = zram_line([{"device": "zram0", "orig": 1_288_490_188,
                          "compr": 314_572_800, "total": 335_544_320}])
        self.assertEqual(row, f"zram0: {fmt.bytes_(1_288_490_188)} stored "
                              f"(compressed to {fmt.bytes_(314_572_800)}, "
                              f"{fmt.bytes_(335_544_320)} of RAM)")

    def test_devices_join_and_zero_stays_zero(self):
        row = zram_line([{"device": "zram0", "orig": 1024, "compr": 512, "total": 700},
                         {"device": "zram1", "orig": 0, "compr": 0, "total": 0}])
        self.assertEqual(row, f"zram0: {fmt.bytes_(1024)} stored (compressed to "
                              f"{fmt.bytes_(512)}, {fmt.bytes_(700)} of RAM) · "
                              f"zram1: {fmt.bytes_(0)} stored (compressed to "
                              f"{fmt.bytes_(0)}, {fmt.bytes_(0)} of RAM)")
        self.assertIn("0 B", row)  # a measured zero is a real zero

    def test_unparsed_fields_are_omitted(self):
        self.assertEqual(zram_line([{"device": "zram0", "orig": 1024, "compr": None,
                                     "total": 300}]),
                         f"zram0: {fmt.bytes_(1024)} stored ({fmt.bytes_(300)} of RAM)")
        self.assertEqual(zram_line([{"device": "zram0", "orig": None, "compr": 5,
                                     "total": None}]), "zram0: –")

    def test_nothing_parsed_is_none(self):
        self.assertIsNone(zram_line(None))
        self.assertIsNone(zram_line([]))


class CompositionTests(unittest.TestCase):
    def _derive(self) -> dict:
        return derive(parse_meminfo(FIXTURE_MEMINFO.read_bytes()))

    def test_partition_sums_to_total_without_overlap(self):
        m = self._derive()
        total = m["total"]
        parts = (m["in_use"], m["reclaimable"], m["free"])
        self.assertEqual(sum(parts), total)  # derive() guarantees the exact partition
        self.assertTrue(all(p >= 0 for p in parts))

    def test_partition_in_use_is_total_minus_available(self):
        # M5 contract §1.2: in_use = total - available directly; the old
        # composition bar is gone — the new page charts in_use against available.
        m = self._derive()
        self.assertEqual(m["in_use"], m["total"] - m["available"])
        self.assertEqual(m["reclaimable"], max(0, m["available"] - m["free"]))


class VideoEntriesTests(unittest.TestCase):
    def test_enc_and_dec_are_separate_rows(self):
        self.assertEqual(video_entries(["render", "enc", "dec"]),
                         [("render", "Render/3D"), ("enc", "Video encode"),
                          ("dec", "Video decode")])

    def test_combined_engine_gets_the_combined_label(self):
        self.assertEqual(video_entries(["copy", "render", "video", "video-enhance"]),
                         [("copy", "Blitter"), ("render", "Render/3D"),
                          ("video", "Video (combined)"), ("video-enhance",
                                                          "Video enhance")])

    def test_one_of_enc_or_dec_shows_what_exists(self):
        self.assertEqual(video_entries(["enc"]), [("enc", "Video encode")])
        self.assertEqual(video_entries(["dec", "video"]),
                         [("dec", "Video decode"), ("video", "Video (combined)")])

    def test_unknown_engine_never_appears_and_none_is_empty(self):
        self.assertEqual(video_entries(["jpeg"]), [("jpeg", "JPEG")])
        self.assertEqual(video_entries(None), [])
        self.assertEqual(video_entries([]), [])


class LinkTextTests(unittest.TestCase):
    def test_full_link(self):
        link = {"current_speed": "8.0 GT/s", "max_speed": "16.0 GT/s",
                "current_width": "8", "max_width": "16"}
        self.assertEqual(link_text(link), "8.0 GT/s ×8 (max 16.0 GT/s ×16)")

    def test_absent_or_suspended_is_none(self):
        self.assertIsNone(link_text(None))  # suspended GPUs publish no link key at all
        self.assertIsNone(link_text({}))

    def test_partial_fields_show_what_exists(self):
        self.assertEqual(link_text({"current_speed": "2.5 GT/s", "current_width": "2",
                                    "max_speed": None, "max_width": None}), "2.5 GT/s ×2")
        self.assertEqual(link_text({"current_speed": None, "current_width": None,
                                    "max_speed": "16.0 GT/s", "max_width": "16"}),
                         "(max 16.0 GT/s ×16)")


class CacheCellTests(unittest.TestCase):
    """cache_cells over the contract §2.1 pinned dedup output (cf. test_telemetry_m2)."""

    CACHES = {
        1: {"Data": [{"instances": 24, "size_bytes": 48 << 10, "scope": "per-core"}],
            "Instruction": [{"instances": 24, "size_bytes": 32 << 10, "scope": "per-core"}]},
        2: {"Data": [{"instances": 8, "size_bytes": 4096 << 10,
                      "scope": "0-1, 2-3, 4-5, 6-7, 8-9, 10-11, 12-13, 14-15"},
                     {"instances": 8, "size_bytes": 2048 << 10, "scope": "per-core"}]},
        3: {"Unified": [{"instances": 1, "size_bytes": 24576 << 10, "scope": "0-23"}]},
    }

    def test_one_cell_per_level_and_type(self):
        cells = cache_cells(self.CACHES)
        self.assertEqual([c[0] for c in cells],
                         ["L1-Data", "L1-Instruction", "L2-Data", "L3-Unified"])
        self.assertEqual([c[1] for c in cells],
                         ["L1 · Data", "L1 · Instruction", "L2 · Data", "L3 · Unified"])

    def test_value_joins_size_groups(self):
        cells = dict((c[0], c) for c in cache_cells(self.CACHES))
        self.assertEqual(cells["L2-Data"][2],
                         f"8 × {fmt.bytes_(4096 << 10)} · 8 × {fmt.bytes_(2048 << 10)}")
        self.assertEqual(cells["L3-Unified"][2], f"1 × {fmt.bytes_(24576 << 10)}")

    def test_tooltip_is_total_plus_scopes(self):
        cells = dict((c[0], c) for c in cache_cells(self.CACHES))
        tip = cells["L2-Data"][3]
        self.assertIn(f"{fmt.bytes_(48 << 20)} total", tip)  # 8×4 MiB + 8×2 MiB
        self.assertIn("shared by CPUs 0-1, 2-3", tip)
        self.assertIn("per core", tip)
        self.assertIn("shared by CPUs 0-23", cells["L3-Unified"][3])

    def test_no_parsed_cache_makes_no_cell(self):
        self.assertEqual(cache_cells({}), [])


class DdrTextTests(unittest.TestCase):
    def test_module_summary(self):
        mods = [{"slot": "Channel0-DIMM0", "type": "DDR4", "speed_mts": 3200,
                 "size_bytes": 8 << 30},
                {"slot": "Channel1-DIMM0", "type": "DDR4", "speed_mts": 3200,
                 "size_bytes": 8 << 30}]
        self.assertEqual(ddr_text(mods),
                         "2 modules · DDR4 · 3200 MT/s — Channel0-DIMM0, Channel1-DIMM0")
        self.assertEqual(ddr_text([{"slot": None, "type": None, "speed_mts": None,
                                    "size_bytes": None}]), "1 module")

    def test_absent_modules_are_none(self):
        self.assertIsNone(ddr_text(None))
        self.assertIsNone(ddr_text([]))


# -- wiring (real pages, fake trees) -------------------------------------------------------

def _win(hub) -> SimpleNamespace:
    return SimpleNamespace(hub=hub, ctl=None, caps=None, caps_error=None, fake=True)


def _chart_with(page, key: str):
    """The page chart whose lines include `key` (None when absent)."""
    for c in page.charts:
        lines = getattr(c, "lines", None)
        if lines and any(line.key == key for line in lines):
            return c
    return None


class WiringTests(unittest.TestCase):
    """Pages over LiveHub/Sampler fake trees: new cells exist and absent data shows '–'."""

    @classmethod
    def setUpClass(cls) -> None:
        if Gdk.Display.get_default() is None:
            raise unittest.SkipTest("GTK widget tests require a display")

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._tmp = Path(tmp.name)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self._tmp / "cfg")})
        env.start()
        self.addCleanup(env.stop)

    def _hub(self, ticks=(1.0, 2.0, 3.5), **build_kw) -> LiveHub:
        tag = "-".join(f"{k}={v}" for k, v in sorted(build_kw.items())) or "default"
        root = build_fake_tree(self._tmp / f"root-{tag}", **build_kw)
        hub = LiveHub(str(root))
        self.addCleanup(hub.close)
        for t in ticks:
            hub.collect_now(t)
        return hub

    # -- populated fake tree ----------------------------------------------------------------

    def test_cpu_page_wiring(self):
        hub = self._hub()
        page = CpuPage(_win(hub))
        page.on_shown()
        hub.collect_now(4.0)
        page.update()
        # one cache cell per (level, type) that parsed; the fake L2 spans two size groups
        self.assertEqual(set(page.cache_grid.cells),
                         {"L1-Data", "L1-Instruction", "L2-Data", "L3-Unified"})
        self.assertEqual(page.cache_grid.cells["L2-Data"].get_label(),
                         nb(f"8 × {fmt.bytes_(4096 << 10)} · 8 × {fmt.bytes_(2048 << 10)}"))
        self.assertIn("per core", page.cache_grid.boxes["L2-Data"].get_tooltip_text())
        # two EPP policy groups on the fake tree: P cores 0-15, E cores 16-23
        self.assertEqual(page.stats.cells["governor"].get_label(),
                         "powersave · balance_performance (0-15) · powersave · power (16-23)")
        # overall view lives, the per-core view (CoreGrid) stays on the shared store
        self.assertNotEqual(page.stats.cells["util"].get_label(), "–")
        self.assertIn(page.grid, page.charts)
        self.assertIsNotNone(hub.store.last("cpu.0"))
        for key in ("cpu.user", "cpu.freq.avg", "temp.cpu"):
            c = _chart_with(page, key)
            self.assertIsNotNone(c, key)
            self.assertTrue(c.dense_grid and c.monitor, key)  # dense_grid + monitor=True

    def test_memory_page_wiring(self):
        hub = self._hub()
        page = MemoryPage(_win(hub))
        page.update()
        # compressed-memory disclosure rows from info["mem"] (zram0 animated,
        # zram1 a measured zero; zswap off on the fake tree)
        row = page.zram_val.get_label()
        self.assertTrue(row.startswith("zram0: "))
        self.assertIn("stored", row)
        self.assertIn(nb("zram1: 0 B stored"), row)  # real zero, not absent
        # PSI avg300 cells from the static fake pressure file (details grid)
        self.assertEqual(page.details.cells["psi_some300"].get_label(), nb("0.12 %"))
        self.assertEqual(page.details.cells["psi_full300"].get_label(), nb("0.03 %"))
        # DMI absent on the fake tree -> needs-root tooltip on Total
        self.assertIn("Speed, type and slot layout need root (DMI)",
                      page.details.boxes["total"].get_tooltip_text())
        # the three memory charts draw from the Memory ring, monitor layout
        for c in page._mem_charts:
            self.assertIsNotNone(c)
            self.assertTrue(c.monitor)

    def test_dgpu_page_wiring(self):
        hub = self._hub()
        page = NvidiaGpuPage(_win(hub))
        page.update()
        self.assertEqual(page.stats.cells["link"].get_label(),
                         nb("16.0 GT/s ×16 (max 16.0 GT/s ×16)"))
        # the fake NVIDIA query carries enc/dec -> one Video chart with both lines
        self.assertTrue(page.video_panel.get_visible())
        self.assertEqual(page._video_mode, "chart")
        self.assertIsNotNone(_chart_with(page, "dgpu.enc"))
        self.assertIsNotNone(_chart_with(page, "dgpu.dec"))
        for key in ("dgpu.util", "dgpu.vram"):
            c = _chart_with(page, key)
            self.assertIsNotNone(c, key)
            self.assertTrue(c.dense_grid and c.monitor, key)

    def test_video_chart_expands_when_the_second_counter_appears(self):
        hub = self._hub()
        page = NvidiaGpuPage(_win(hub))
        dg = hub.info["dgpu"]
        dg["q"] = {"dec": 0}
        page.update()
        self.assertIsNotNone(_chart_with(page, "dgpu.dec"))
        self.assertIsNone(_chart_with(page, "dgpu.enc"))
        dg["q"] = {"enc": 1}
        page.update()
        self.assertIs(_chart_with(page, "dgpu.enc"), _chart_with(page, "dgpu.dec"))
        dg.update(backend="nvidia-smi", q={})
        page.update()
        self.assertFalse(page.video_panel.get_visible())

    def test_igpu_page_wiring(self):
        hub = self._hub()
        page = IntelGpuPage(_win(hub))
        page.update()
        self.assertEqual(page.stats.cells["link"].get_label(),
                         nb("8.0 GT/s ×8 (max 16.0 GT/s ×16)"))
        # the fake fdinfo reports the combined 'video' engine: combined fallback label
        self.assertEqual(page.engines.captions["video"].get_label(), "Video (combined)")
        self.assertEqual(page.engines.captions["render"].get_label(), "Render/3D")
        self.assertNotEqual(page.engines.cells["video"].get_label(), "–")

    def test_suspended_dgpu_page_shows_dash_and_no_video(self):
        hub = self._hub(dgpu_runtime="suspended")
        dg = hub.info.get("dgpu") or {}
        self.assertEqual(dg.get("state"), "sleeping")
        self.assertNotIn("link", dg)  # suspended: no probe, no cached republish
        page = NvidiaGpuPage(_win(hub))
        page.update()
        self.assertEqual(page.stats.cells["link"].get_label(), "–")
        self.assertFalse(page.video_panel.get_visible())  # no q published -> no panel

    def test_dgpu_sleeping_hides_the_video_panel(self):
        hub = self._hub()
        page = NvidiaGpuPage(_win(hub))
        page.update()
        self.assertTrue(page.video_panel.get_visible())  # awake: the video chart is built
        # The dGPU suspends: telemetry stops querying it, point-in-time readings go stale.
        root = next(self._tmp.glob("root-*"))
        _w(root / "sys/bus/pci/devices/0000:01:00.0/power/runtime_status", "suspended")
        hub.collect_now(4.0)
        self.assertEqual((hub.info.get("dgpu") or {}).get("state"), "sleeping")
        page.update()
        self.assertFalse(page.video_panel.get_visible())
        self.assertEqual(page.stats.cells["link"].get_label(), "–")

    def test_amd_dgpu_engine_rows_and_link(self):
        root = self._tmp / "amd"
        dev = root / "sys/class/drm/card2/device"
        _w(dev / "driver_name", "amdgpu")
        _w(dev / "address", "0000:03:00.0")
        _w(dev / "device", "0x747e")
        _w(dev / "power/runtime_status", "active")
        _w(dev / "current_link_speed", "8.0 GT/s")
        _w(dev / "max_link_speed", "16.0 GT/s")
        _w(dev / "current_link_width", 8)
        _w(dev / "max_link_width", 16)
        proc = root / "proc/100"
        (proc / "fd").mkdir(parents=True)
        os.symlink("/dev/dri/renderD128", proc / "fd/9")
        (proc / "fdinfo").mkdir()
        fdinfo = ("pos:\t0\nflags:\t02104002\ndrm-driver:\tamdgpu\n"
                  "drm-client-id:\t1\ndrm-pdev:\t0000:03:00.0\n"
                  "drm-engine-render:\t0 ns\ndrm-engine-capacity-render:\t1\n")
        (proc / "fdinfo/9").write_text(fdinfo)
        g = AmdDiscreteGpu(str(root))
        store = Store(300)
        vals, info = {}, {}
        g.sample(1.0, vals, info)
        store.push(1.0, dict(vals))
        (proc / "fdinfo/9").write_text(fdinfo.replace("\t0 ns", "\t250000000 ns"))
        vals, info = {}, {}
        g.sample(2.0, vals, info)
        store.push(2.0, dict(vals))
        hub = SimpleNamespace(store=store, info={"dgpu": info["dgpu"]},
                              sampler=SimpleNamespace(dgpu=g), root=str(root),
                              interval_s=1.0)
        page = NvidiaGpuPage(_win(hub))
        page.update()
        self.assertEqual(page.stats.cells["link"].get_label(),
                         nb("8.0 GT/s ×8 (max 16.0 GT/s ×16)"))
        # AMD dGPU: engine rows instead of the enc/dec chart, straight from dgpu.engine.*
        self.assertTrue(page.video_panel.get_visible())
        self.assertEqual(page._video_mode, "engines")
        self.assertIn("render", page.video_stats.cells)
        self.assertEqual(page.video_stats.captions["render"].get_label(), "Render/3D")
        self.assertEqual(page.video_stats.cells["render"].get_label(), nb("25.0 %"))

    # -- absent data: "–", never a fabricated zero -------------------------------------------

    def test_absent_data_shows_dash_and_no_cells(self):
        root = build_fake_tree(self._tmp / "bare")  # no telemetry ensure() files at all
        _w(root / "sys/devices/system/cpu/online", "0")  # one CPU: CoreGrid keeps 1 tile
        sampler = Sampler(str(root), fake=True)
        hub = SimpleNamespace(store=sampler.store, info={}, sampler=sampler, root=str(root),
                              interval_s=1.0)
        cpu = CpuPage(_win(hub))
        cpu.update()
        self.assertEqual(cpu.cache_grid.cells, {})  # no caches parsed: no cells at all
        self.assertEqual(cpu.stats.cells["governor"].get_label(), "–")
        mem = MemoryPage(_win(hub))
        mem.update()
        # M5: no zram devices -> the On zram disclosure row reads "–" (honest absence;
        # the old lazy-row StatGrid is gone)
        self.assertEqual(nb(mem.zram_val.get_label()), nb("–"))
        dg = NvidiaGpuPage(_win(hub))
        dg.update()
        self.assertEqual(dg.stats.cells["link"].get_label(), "–")
        self.assertFalse(dg.video_panel.get_visible())
        ig = IntelGpuPage(_win(hub))
        ig.update()
        self.assertEqual(ig.stats.cells["link"].get_label(), "–")
        self.assertEqual(ig.engines.cells, {})

    def test_meminfo_without_zram_or_psi_keeps_rows_absent(self):
        root = build_fake_tree(self._tmp / "memonly")
        _w(root / "proc/meminfo", FIXTURE_MEMINFO.read_text())
        sampler = Sampler(str(root), fake=True)
        sampler.sample(1.0)
        hub = SimpleNamespace(store=sampler.store, info=sampler.info, sampler=sampler,
                              root=str(root), interval_s=1.0)
        page = MemoryPage(_win(hub))
        page.update()
        # M5: meminfo-only tree -> zram row "–", PSI details "–", DMI tooltip kept
        self.assertEqual(nb(page.zram_val.get_label()), nb("–"))
        for k in ("psi_some60", "psi_some300", "psi_full60", "psi_full300"):
            self.assertEqual(page.details.cells[k].get_label(), nb("–"), k)
        self.assertIn("Speed, type and slot layout need root (DMI)",
                      page.details.boxes["total"].get_tooltip_text())


if __name__ == "__main__":
    unittest.main()
