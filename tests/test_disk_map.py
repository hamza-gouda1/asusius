# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Headless disk map geometry and context-menu behavior."""
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")  # the chart shares the Libadwaita theme
from asusius.ui.disk_map import (  # noqa: E402
    DiskUsageChart, MODE_RINGS, MODE_TREEMAP, tile_header_height,
)
from asusius.ui.charts import make_layout  # noqa: E402
from gi.repository import PangoCairo  # noqa: E402

from asusius.telemetry.disk_analyzer import scan_path  # noqa: E402


def mkfile(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(size))


class DiskMapDouble:
    """DiskUsageChart logic without GTK: borrowed unbound methods + Mock stubs."""

    set_model = DiskUsageChart.set_model
    selected = DiskUsageChart.selected
    drill = DiskUsageChart.drill
    up = DiskUsageChart.up
    _set_view = DiskUsageChart._set_view
    _emit_select = DiskUsageChart._emit_select
    _set_sel = DiskUsageChart._set_sel
    _geometry = DiskUsageChart._geometry
    _nest = DiskUsageChart._nest
    _rings_geom = DiskUsageChart._rings_geom
    _hit = DiskUsageChart._hit
    _node_size = DiskUsageChart._node_size
    _sibling_order = DiskUsageChart._sibling_order
    _menu_items = DiskUsageChart._menu_items
    _is_dir = DiskUsageChart._is_dir

    def __init__(self) -> None:
        self.on_drill = Mock()
        self.on_select = Mock()
        self._open_files = Mock()
        self._copy_path = Mock()
        self.trash_action = Mock()
        self.trash_allowed = Mock(return_value=True)
        self._model = None
        self._mode = MODE_TREEMAP
        self._apparent = False
        self._view = None
        self._sel = None
        self._sel_source = None
        self._last_size = None
        self._serial = 0
        self._geom_key = None
        self.tiles = []
        self.segments = []
        self.center = None
        self.queue_draw = Mock()
        self.get_allocated_width = Mock(return_value=0)
        self.get_allocated_height = Mock(return_value=0)


class ContextMenuTests(unittest.TestCase):
    """The right-click menu's item logic (the GTK build is a thin loop over these)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mkfile(Path(tmp.name) / "scan/normal.bin", 100)
        self.result = scan_path(tmp.name)

    def canvas(self) -> DiskMapDouble:
        return DiskMapDouble()

    def test_normal_folder_offers_all_three_actions(self):
        c = self.canvas()
        c._model = self.result
        path = self.result.root_path
        items = c._menu_items(path, self.result.root)
        self.assertEqual([i[0] for i in items],
                         ["Open in Files", "Copy Path", "Move to Trash"])
        self.assertTrue(all(i[3] for i in items))
        self.assertIsNone(items[2][4])  # no tooltip on Move to Trash

    def test_menu_uses_owner_safety_check_and_captures_model(self):
        canvas = self.canvas()
        canvas._model = self.result
        canvas.trash_allowed.return_value = False
        path = "/usr/bin"
        trash = canvas._menu_items(path, 0)[-1]
        canvas.trash_allowed.assert_called_once_with(self.result, 0)
        self.assertFalse(trash[3])
        self.assertIsNotNone(trash[4])

        canvas.trash_allowed.return_value = True
        _label, action, target, allowed, _tip = canvas._menu_items(path, 0)[-1]
        self.assertTrue(allowed)
        canvas._model = None
        action(target)
        canvas.trash_action.assert_called_once_with(self.result, 0)


class GeometryTests(unittest.TestCase):
    """Treemap and sunburst geometry invariants on a borrowed-method chart double."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "scan"
        mkfile(root / "a/big.bin", 1_000_000)
        mkfile(root / "a/small.bin", 500_000)
        mkfile(root / "b/big.bin", 400_000)
        mkfile(root / "b/small.bin", 200_000)
        mkfile(root / "c/direct.bin", 300_000)
        mkfile(root / "c/inner/deep.bin", 150_000)
        self.result = scan_path(str(root))
        self.assertIsNone(self.result.error)
        self.canvas = DiskMapDouble()
        self.canvas.set_model(self.result)
        self.W, self.H = 600, 400

    def test_treemap_geometry_invariants(self):
        self.canvas.set_model(self.result, mode=MODE_TREEMAP)
        tiles, segments, center = self.canvas._geometry(self.W, self.H)
        self.assertIsNone(center)
        self.assertEqual(segments, [])
        dir_idxs = set(range(len(self.result.nodes)))
        self.assertEqual({t.idx for t in tiles}, dir_idxs)   # every dir exactly once
        root_tile = tiles[0]
        self.assertEqual(root_tile.idx, self.result.root)
        for t in tiles:
            for v in (t.x, t.y, t.w, t.h):
                self.assertTrue(math.isfinite(v), t)         # no NaN/inf anywhere
            self.assertGreaterEqual(t.x, -1e-6)
            self.assertGreaterEqual(t.y, -1e-6)
            self.assertLessEqual(t.x + t.w, self.W + 1e-6)   # inside the viewport
            self.assertLessEqual(t.y + t.h, self.H + 1e-6)
        self.assertAlmostEqual(root_tile.x, 0.0, places=6)
        self.assertAlmostEqual(root_tile.y, 0.0, places=6)
        self.assertAlmostEqual(root_tile.w, float(self.W), places=6)
        self.assertAlmostEqual(root_tile.h, float(self.H), places=6)
        # sibling tile areas are proportional to sibling sizes, grouped by parent
        by_parent = {}
        for i, n in enumerate(self.result.nodes):
            for c in n.child_dir_indices:
                by_parent.setdefault(i, []).append(c)
        tile = {t.idx: t for t in tiles}
        size = self.canvas._node_size
        for parent, kids in by_parent.items():
            kids = [c for c in kids if size(self.result.node(c)) > 0]
            if len(kids) < 2:
                continue
            areas = [tile[c].w * tile[c].h for c in kids]
            for c, a in zip(kids, areas):
                self.assertAlmostEqual(
                    a / areas[0],
                    size(self.result.node(c)) / size(self.result.node(kids[0])),
                    delta=abs(a / areas[0]) * 1e-6, msg=f"parent {parent} child {c}")
            self.assertLessEqual(sum(areas), self.W * self.H + 1e-6)
        # children sit inside their parent's rectangle
        for parent, kids in by_parent.items():
            for c in kids:
                self.assertGreaterEqual(tile[c].x, tile[parent].x - 1e-6)
                self.assertGreaterEqual(tile[c].y, tile[parent].y - 1e-6)
                self.assertLessEqual(tile[c].x + tile[c].w,
                                     tile[parent].x + tile[parent].w + 1e-6)
                self.assertLessEqual(tile[c].y + tile[c].h,
                                     tile[parent].y + tile[parent].h + 1e-6)

    def test_nested_tiles_stay_below_their_folders_label_band(self):
        tiles, _, _ = self.canvas._geometry(580, 340)
        by_idx = {tile.idx: tile for tile in tiles}
        preview, = [tile for tile in tiles if tile.depth == 2]
        parent = by_idx[self.result.node(preview.idx).parent_idx]
        band = tile_header_height(parent.depth, parent.w, parent.h)
        self.assertGreater(band, 30)
        self.assertGreaterEqual(preview.y, parent.y + band)
        self.assertEqual(tile_header_height(0, 580, 340), 0)
        self.assertEqual(tile_header_height(preview.depth, preview.w, preview.h), 0)

    def test_treemap_keeps_folder_names_when_larger_fonts_crowd_out_sizes(self):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.W, self.H)
        context = cairo.Context(surface)
        drawn = []

        def large_layout(cr):
            layout = make_layout(cr)
            PangoCairo.context_set_resolution(layout.get_context(), 160)
            return layout

        with (patch("asusius.ui.disk_map.theme.is_dark", return_value=True),
              patch("asusius.ui.disk_map.make_layout", side_effect=large_layout),
              patch.object(PangoCairo, "show_layout",
                           side_effect=lambda _cr, layout: drawn.append(layout.get_text()))):
            DiskUsageChart._draw_treemap(self.canvas, context, self.W, self.H)

        self.assertCountEqual(drawn, ["a", "b", "c"])

    def test_treemap_apparent_mode_changes_proportions(self):
        self.canvas.set_model(self.result, mode=MODE_TREEMAP, use_apparent=True)
        tiles, _segs, _center = self.canvas._geometry(self.W, self.H)
        for t in tiles:
            node = self.result.node(t.idx)
            self.assertEqual(self.canvas._node_size(node), node.apparent_size)
        # same tree, same tile set, rebuilt proportions in allocated mode
        self.canvas.set_model(self.result, mode=MODE_TREEMAP)
        alloc_tiles, _s, _c = self.canvas._geometry(self.W, self.H)
        self.assertEqual({t.idx for t in tiles}, {t.idx for t in alloc_tiles})

    def test_rings_geometry_invariants(self):
        self.canvas.set_model(self.result, mode=MODE_RINGS)
        tiles, segments, center = self.canvas._geometry(self.W, self.H)
        self.assertEqual(tiles, [])
        cx, cy, center_r = center
        self.assertAlmostEqual(cx, self.W / 2.0, places=6)
        self.assertAlmostEqual(cy, self.H / 2.0, places=6)
        outer = max(12.0, min(self.W, self.H) / 2.0 - 4.0)   # 196 px at 600x400
        self.assertAlmostEqual(center_r, outer * 0.30, places=6)
        dir_idxs = set(range(len(self.result.nodes))) - {self.result.root}
        self.assertEqual({s.idx for s in segments}, dir_idxs)
        seen = {}
        for s in segments:
            self.assertTrue(math.isfinite(s.a0) and math.isfinite(s.a1)
                            and math.isfinite(s.r_in) and math.isfinite(s.r_out))
            self.assertLess(s.a0, s.a1)
            self.assertGreaterEqual(s.a0, -math.pi / 2 - 1e-9)   # [-pi/2, 3pi/2)
            self.assertLessEqual(s.a1, 3.0 * math.pi / 2 + 1e-9)
            self.assertGreaterEqual(s.r_in, center_r - 1e-6)
            self.assertLessEqual(s.r_out, outer + 1e-6)
            self.assertLess(s.r_in, s.r_out)
            seen.setdefault(s.depth, []).append(s)
        self.assertEqual(min(seen), 1)                       # rings start at depth 1
        for _depth, segs in seen.items():                    # one radius pair per ring
            self.assertEqual({s.r_in for s in segs}, {segs[0].r_in})
            self.assertEqual({s.r_out for s in segs}, {segs[0].r_out})
        depths = sorted(seen)
        for lo, hi in zip(depths, depths[1:]):
            self.assertAlmostEqual(seen[hi][0].r_in, seen[lo][0].r_out, places=6)
        # depth-1 sweeps sum to a full turn and are proportional to sibling sizes
        first = seen[1]
        self.assertAlmostEqual(sum(s.a1 - s.a0 for s in first), 2.0 * math.pi, places=9)
        by_parent = {}
        for i, n in enumerate(self.result.nodes):
            for c in n.child_dir_indices:
                by_parent.setdefault(i, []).append(c)
        seg = {s.idx: s for s in segments}
        for parent, kids in by_parent.items():
            kids = [c for c in kids if c in seg]
            if len(kids) < 2:
                continue
            sweeps = [seg[c].a1 - seg[c].a0 for c in kids]
            for c, sweep in zip(kids, sweeps):
                self.assertAlmostEqual(
                    sweep / sweeps[0],
                    self.canvas._node_size(self.result.node(c))
                    / self.canvas._node_size(self.result.node(kids[0])),
                    delta=abs(sweep / sweeps[0]) * 1e-6, msg=f"parent {parent} child {c}")
        # a parent's wedge covers its children's wedges
        for s in segments:
            if s.depth == 1:
                continue
            p = seg[self.result.node(s.idx).parent_idx]
            self.assertGreaterEqual(s.a0, p.a0 - 1e-9)
            self.assertLessEqual(s.a1, p.a1 + 1e-9)

    def test_geometry_cache_is_reused_and_invalidated(self):
        tiles1, segs1, c1 = self.canvas._geometry(self.W, self.H)
        tiles2, segs2, c2 = self.canvas._geometry(self.W, self.H)
        self.assertIs(tiles1, tiles2)                    # cached, not rebuilt
        self.assertIs(segs1, segs2)
        self.assertIs(c1, c2)
        self.canvas.set_model(self.result, mode=MODE_RINGS)  # serial bump rebuilds
        r_tiles, r_segs, r_center = self.canvas._geometry(self.W, self.H)
        self.assertEqual(r_tiles, [])
        self.assertTrue(r_segs)
        self.assertIsNotNone(r_center)
        r_tiles2, _s, _c = self.canvas._geometry(self.W, self.H)
        self.assertIs(r_tiles, r_tiles2)                 # cached again

    def test_drill_rejects_bad_indices(self):
        """A bad index must not raise or change state (node() raises IndexError)."""
        c = self.canvas
        view0 = c._view
        c.drill(999999)
        self.assertEqual(c._view, view0)                 # state unchanged
        c.on_drill.assert_not_called()

    def test_set_model_drill_up_and_selection_callbacks(self):
        c = self.canvas
        c.on_select.assert_not_called()                  # nothing selected yet
        self.assertIsNone(c.selected())
        c._set_sel(1, "kb")
        self.assertEqual(c.selected(), 1)
        c.on_select.assert_called_once_with(self.result, 1)
        c.set_model(self.result)                         # re-set clears the selection
        self.assertIsNone(c.selected())
        c.on_select.assert_called_with(self.result, None)
        self.assertEqual(c._view, self.result.root)
        c.drill(1)
        self.assertEqual(c._view, 1)
        c.on_drill.assert_called_once_with(self.result, 1)
        self.assertIsNone(c.selected())                  # view change clears selection
        self.assertTrue(c.up())
        self.assertEqual(c._view, self.result.root)
        self.assertFalse(c.up())                         # already at the scan root
        c.drill(4)                                       # deepest dir: root>c>inner
        self.assertEqual(c._view, 4)
        self.assertTrue(c.up())
        self.assertEqual(c._view, 3)
        self.assertTrue(c.up())
        self.assertEqual(c._view, self.result.root)
        self.assertFalse(c.up())
        c.set_model(None)                                # empty state resets everything
        self.assertIsNone(c._view)
        self.assertEqual(c._geometry(self.W, self.H), ([], [], None))
        c.set_model(self.result, mode="bogus")           # unknown mode -> treemap
        self.assertEqual(c._mode, MODE_TREEMAP)

    def test_hit_testing_both_modes(self):
        c = self.canvas
        c.set_model(self.result)
        kind, idx = c._hit(self.W / 2, self.H / 2, self.W, self.H)
        self.assertEqual(kind, "tile")
        self.assertIn(idx, {1, 2, 3})                    # a child tile, not the root
        self.assertEqual(c._hit(self.W * 2, self.H * 2, self.W, self.H), (None, None))

        c.set_model(self.result, mode=MODE_RINGS)
        kind, idx = c._hit(self.W / 2, self.H / 2, self.W, self.H)
        self.assertEqual((kind, idx), ("center", None))
        _tiles, segments, center = c._geometry(self.W, self.H)
        cx, cy, _r = center
        seg = next(s for s in segments if s.depth == 1)
        mid, mr = (seg.a0 + seg.a1) / 2.0, (seg.r_in + seg.r_out) / 2.0
        x, y = cx + math.cos(mid) * mr, cy + math.sin(mid) * mr
        kind, idx = c._hit(x, y, self.W, self.H)
        self.assertEqual((kind, idx), ("seg", seg.idx))


if __name__ == "__main__":
    unittest.main()
