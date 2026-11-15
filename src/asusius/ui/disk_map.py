# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Interactive disk-usage canvas: a squarified treemap and a sunburst rings view.

The canvas is a pure view over a finished scan result. It never touches the
filesystem and never constructs nodes; it only reads the scan's public surface
(``result.root / node / children / path / totals / stats`` and each node's
``name / size / apparent_size / mtime / parent_idx``). Layout and painting happen at
snapshot time only: there are no timers and no unthrottled redraws, so nothing repaints
while the page is unmapped. Colours come from the brand palette via
``charts.colour()`` cycled over the seven pinned SERIES names — no new colours.

Worker 1D codes against exactly this surface:

    MODE_TREEMAP = "treemap"; MODE_RINGS = "rings"
    DiskUsageChart(on_drill=None, on_select=None, height=240)
        set_model(result, mode=MODE_TREEMAP, use_apparent=False)
        selected() -> int | None
        drill(idx) -> None          # sets view root, fires on_drill(result, idx)
        up() -> bool                # drill to the view node's parent_idx; False at root
"""
from __future__ import annotations

import collections
import heapq
import math
from dataclasses import dataclass

import cairo
from gi.repository import Gdk, Gio, Gtk, Pango, PangoCairo

from ..telemetry import fmt
from . import charts, theme
from .charts import _round_rect, make_layout

MODE_TREEMAP = "treemap"
MODE_RINGS = "rings"

# label a tile only when it is at least 52x26 px (name line + size line fit)
LABEL_MIN_W, LABEL_MIN_H = 52.0, 26.0
_HEADER_H = 16.0            # label strip reserved inside a labelled dir tile
_GAP = 2.0                  # crisp background-coloured gap between tiles
_MAX_DEPTH = 8              # treemap nesting cap (view root = depth 0)
_MAX_RINGS = 6              # sunburst ring cap around the centre readout
_CENTER_FRAC = 0.30         # centre readout radius, fraction of the outer radius
_MIN_WEDGE = 0.0012         # radians; slivers below this are not painted
_WEDGE_LABEL_SWEEP = 0.35   # radians; a wedge is labelled only above this sweep
_WEDGE_LABEL_THICK = 16.0   # px; a wedge is labelled only above this thickness
_TILE_ALPHAS = (0.92, 0.84, 0.78, 0.72, 0.66, 0.62, 0.58, 0.55)  # dimmer with depth

# the seven pinned series colours; tiles/segments cycle over these names
SERIES_NAMES = ("primary", "gold", "slate", "sage", "mauve", "rose", "muted")

# adaptive ink ends used over tile colours (both clear AA on nearly every blend)
_INK_DARK = (0.07, 0.05, 0.06)
_INK_LIGHT = (0.99, 0.97, 0.96)
_AA = 4.5                   # WCAG AA threshold for normal text
_AA_PLATE = 4.6             # plate target keeps a margin above AA

# paths whose deletion (Trash) is never offered
_MAX_TILES_PER_LEVEL = 128


# ---------------------------------------------------------------- contrast helpers
def _lin(c: float) -> float:
    """sRGB channel -> linear light (WCAG 2.x relative-luminance definition)."""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _lum(rgb) -> float:
    return 0.2126 * _lin(rgb[0]) + 0.7152 * _lin(rgb[1]) + 0.0722 * _lin(rgb[2])


def _contrast(a, b) -> float:
    hi, lo = max(_lum(a), _lum(b)), min(_lum(a), _lum(b))
    return (hi + 0.05) / (lo + 0.05)


def _blend(fg, bg, alpha: float):
    return tuple(f * alpha + b * (1.0 - alpha) for f, b in zip(fg, bg))


def _mix(a, b, t: float):
    return tuple(x * (1.0 - t) + y * t for x, y in zip(a, b))


def _pick_ink(rgb) -> tuple[tuple, float]:
    """(ink, contrast): the higher-contrast ink over the blended tile colour."""
    cd, cw = _contrast(rgb, _INK_DARK), _contrast(rgb, _INK_LIGHT)
    return (_INK_DARK, cd) if cd >= cw else (_INK_LIGHT, cw)


def _plate_for(rgb, ink) -> tuple:
    """Tile colour nudged AWAY from `ink` until the ink clears AA over it.

    Moving the plate toward the ink end can only lower its contrast against the
    ink, so the bisection walks toward the opposite ink extreme instead.
    """
    far = _INK_LIGHT if ink == _INK_DARK else _INK_DARK
    lo, hi = 0.0, 1.0
    for _ in range(18):
        mid = (lo + hi) / 2.0
        if _contrast(_mix(rgb, far, mid), ink) >= _AA_PLATE:
            hi = mid
        else:
            lo = mid
    return _mix(rgb, far, hi)


def _wedge_path(cr, cx, cy, r_in, r_out, a0, a1) -> bool:
    """Trace a sunburst wedge (annulus sector). True when a full-circle ring was
    traced, which switches the fill rule to EVEN_ODD (caller restores WINDING)."""
    if a1 - a0 >= 2.0 * math.pi - 1e-6:
        cr.set_fill_rule(cairo.FillRule.EVEN_ODD)
        cr.new_sub_path()
        cr.arc(cx, cy, r_out, 0.0, 2.0 * math.pi)
        cr.new_sub_path()
        cr.arc(cx, cy, r_in, 0.0, 2.0 * math.pi)
        return True
    cr.new_sub_path()
    cr.arc(cx, cy, r_out, a0, a1)
    cr.arc_negative(cx, cy, r_in, a1, a0)
    cr.close_path()
    return False


def _bg_rgb() -> tuple:
    """Card background of the active scheme, read from the generated palette."""
    from ..brand_palette import HEX_PALETTES, rgb

    return rgb(HEX_PALETTES["dark" if theme.is_dark() else "light"]["bg-2"])


def _tile_alpha(depth: int) -> float:
    return _TILE_ALPHAS[min(depth, len(_TILE_ALPHAS) - 1)]


# ---------------------------------------------------------------- squarified layout
def _squarify(sizes, x, y, dx, dy):
    """Squarified row layout (Bruls, Huizing & van Wijk 2000) of `sizes` into a rect.

    `sizes` are scaled to the rect area internally; returns one (x, y, w, h) per size,
    same order (callers sort descending first). Non-positive sizes must be filtered by
    the caller. Rows are laid along the shorter side, so sibling areas are exactly
    proportional to sibling sizes by construction.
    """
    n = len(sizes)
    if n == 0 or dx <= 0.0 or dy <= 0.0 or sum(sizes) <= 0.0:
        return [(x, y, 0.0, 0.0)] * n
    scale = dx * dy / sum(sizes)
    queue = collections.deque(s * scale for s in sizes)
    cx, cy, cw, ch = float(x), float(y), float(dx), float(dy)
    out: list[tuple[float, float, float, float]] = []
    while queue:
        if cw <= 1e-6 or ch <= 1e-6:
            out.extend([(cx, cy, 0.0, 0.0)] * len(queue))
            break
        short = min(cw, ch)
        strip, area, best = [], 0.0, math.inf
        while queue:
            a = queue[0]
            thick = (area + a) / short
            if thick <= 0.0:
                break
            lengths = [s / thick for s in strip + [a]]
            worst = max(max(thick, t) / min(thick, t) for t in lengths)
            if strip and worst > best:
                break
            strip.append(queue.popleft())
            area += a
            best = worst
        if not strip:
            a = queue.popleft()
            strip, area = [a], a
        if cw >= ch:
            thick = area / ch
            yy = cy
            for a in strip:
                out.append((cx, yy, thick, a / thick))
                yy += a / thick
            cx += thick
            cw -= thick
        else:
            thick = area / cw
            xx = cx
            for a in strip:
                out.append((xx, cy, a / thick, thick))
                xx += a / thick
            cy += thick
            ch -= thick
    return out


# ---------------------------------------------------------------- geometry records
@dataclass
class Tile:
    """One rectangle of the treemap (both dirs and leaves; view root = depth 0)."""

    idx: int
    x: float
    y: float
    w: float
    h: float
    depth: int
    hue: int


@dataclass
class Seg:
    """One wedge of the rings view (view root is the centre, not a segment)."""

    idx: int
    depth: int          # 1 = first ring around the centre readout
    a0: float           # start angle, radians in [-pi/2, 3*pi/2)
    a1: float           # end angle; children lie fully inside [a0, a1]
    r_in: float
    r_out: float
    hue: int


class DiskUsageChart(Gtk.DrawingArea):
    """Interactive treemap/rings canvas over a finished scan result.

    Rebuilds geometry only when the model, mode, view root, sizes flag or the widget
    size changes; repaints only on data change, pointer events or mode change. The
    last built geometry stays readable for tests as ``tiles``, ``segments`` and
    ``center``. Keyboard: arrows move the selection, Enter drills in, Backspace goes
    up, Escape clears. Selection changes are exposed only through ``on_select``.
    """

    def __init__(self, on_drill=None, on_select=None, height=240, *,
                 trash_action=None, trash_allowed=None) -> None:
        super().__init__()
        self.on_drill = on_drill
        self.on_select = on_select
        self.trash_action = trash_action
        self.trash_allowed = trash_allowed
        self.set_content_height(height)
        self.set_hexpand(True)
        self.add_css_class("disk-map")
        self.set_draw_func(self._draw)
        self.set_focusable(True)
        from .pages.base import a11y_name  # lazy: pages pulls the whole page chain

        a11y_name(self, "Disk usage map")
        self._model = None
        self._mode = MODE_TREEMAP
        self._apparent = False
        self._view = None
        self._sel = None          # hover / keyboard selection (one slot)
        self._sel_source = None   # "hover" or "kb": pointer leave clears hover only
        self._last_size = None    # (w, h) of the last built geometry (hit fallback)
        self._serial = 0          # bumped on every model/view/mode change
        self._geom_key = None
        self.tiles: list[Tile] = []
        self.segments: list[Seg] = []
        self.center = None        # (cx, cy, r_center) while rings geometry exists
        self._pop = None          # context-menu popover, built lazily
        self.connect("unrealize", self._unrealize_menu)
        mo = Gtk.EventControllerMotion()
        mo.connect("motion", self._motion)
        mo.connect("leave", self._leave)
        self.add_controller(mo)
        gc = Gtk.GestureClick()
        gc.set_button(0)  # route left, middle and right buttons to one handler
        gc.connect("pressed", self._click)
        self.add_controller(gc)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

    # ------------------------------------------------------------ pinned public API
    def set_model(self, result, mode=MODE_TREEMAP, use_apparent=False) -> None:
        """Install a ScanResult (or None for the empty state) and reset the view.

        ``use_apparent=True`` forces apparent sizes; the default follows
        ``result.stats.get("use_apparent")`` when the scan reports one.
        """
        had = self._sel is not None
        self._model = result
        self._mode = mode if mode in (MODE_TREEMAP, MODE_RINGS) else MODE_TREEMAP
        stats = getattr(result, "stats", None) or {}
        self._apparent = bool(use_apparent) or bool(stats.get("use_apparent"))
        self._view = getattr(result, "root", None) if result is not None else None
        self._sel = None
        self._sel_source = None
        self._serial += 1
        self._geom_key = None
        self.tiles, self.segments, self.center = [], [], None
        self.queue_draw()
        if had:
            self._emit_select()

    def selected(self) -> int | None:
        return self._sel

    def drill(self, idx: int) -> None:
        """Set the view root to any directory idx (fires on_drill)."""
        if self._model is None or idx is None:
            return
        try:
            if self._model.node(idx) is None:
                return
        except IndexError:
            return  # ScanResult.node() raises on a bad index, not None (analyzer)
        self._set_view(idx)

    def up(self) -> bool:
        """Drill to the view node's parent; False when already at result.root."""
        if self._model is None or self._view is None:
            return False
        node = self._model.node(self._view)
        parent = getattr(node, "parent_idx", None)
        if parent is None or parent < 0:
            return False
        self._set_view(parent)
        return True

    # ------------------------------------------------------------ scan-surface access
    def _node_size(self, node) -> float:
        """node.apparent_size when the scan/caller asked for it, else node.size."""
        if node is None:
            return 0.0
        if self._apparent:
            v = getattr(node, "apparent_size", None)
            if v is not None:
                return float(v)
        return float(getattr(node, "size", 0) or 0)

    def _is_dir(self, result, idx) -> bool:
        """Whether idx can be drilled into.

        Explicit ``node.is_dir`` wins when a model provides it. Otherwise nodes are
        treated as directories (the real ScanResult stores directories only — files
        are aggregated into ``file_count`` / ``top_files``), so drilling is always
        offered; drilling a childless dir just zooms, and ``up()`` returns.
        """
        node = result.node(idx)
        if node is None:
            return False
        flag = getattr(node, "is_dir", None)
        return True if flag is None else bool(flag)

    def _count_of(self, result, idx) -> int | None:
        """Item count for the tooltip: node.file_count when present, else children."""
        node = result.node(idx)
        n = getattr(node, "file_count", None)
        if n is not None:
            return int(n)
        n = getattr(node, "items", None)
        if n is not None:
            return int(n)
        if self._is_dir(result, idx):
            try:
                return len(result.children(idx))
            except Exception:
                return None
        return None

    def _sibling_order(self, result, idx) -> list[int]:
        """Children of `idx` with a positive size, largest first (layout order)."""
        keys = ((self._node_size(result.node(c)), -c) for c in result.children(idx))
        return [-idx for size, idx in heapq.nlargest(_MAX_TILES_PER_LEVEL, keys) if size > 0]

    # ------------------------------------------------------------ state transitions
    def _set_view(self, view) -> None:
        had = self._sel is not None
        self._view = view
        self._sel = None
        self._sel_source = None
        # a new view invalidates the geometry cache and the stored geometry lists
        self._serial += 1
        self._geom_key = None
        self.tiles, self.segments, self.center = [], [], None
        self.queue_draw()
        if had:  # the old selection died with the view; report the clear
            self._emit_select()
        if self.on_drill:
            self.on_drill(self._model, view)

    def _emit_select(self) -> None:
        if self.on_select:
            self.on_select(self._model, self._sel)

    def _set_sel(self, idx, source: str) -> None:
        if idx != self._sel or self._sel_source != source:
            self._sel = idx
            self._sel_source = source
            self._emit_select()
        self.queue_draw()

    # ------------------------------------------------------------ tooltip and safety
    def _summary(self, idx) -> str:
        """Plain-text tooltip: path, size, % of parent, item count."""
        result = self._model
        node = result.node(idx)
        path = result.path(idx) or ""
        raw = self._node_size(node)
        lines = [path, fmt.bytes_(raw)]
        parent_idx = getattr(node, "parent_idx", None)
        if parent_idx is not None and parent_idx >= 0:
            pnode = result.node(parent_idx)
            psize = self._node_size(pnode)
            if pnode is not None and psize > 0:
                share = 100.0 * raw / psize
                lines[1] = f"{lines[1]} · {share:.0f} % of {pnode.name}"
        count = self._count_of(result, idx)
        if count is not None:
            lines.append(f"{fmt.count(count)} items")
        elif not self._is_dir(result, idx):
            lines.append("file")
        return "\n".join(lines)

    # ------------------------------------------------------------ geometry
    def _geometry(self, w, h):
        """(tiles, segments, center) for the current state at size (w, h), cached.

        Only reads the model and plain numbers — safe to call headless or from a
        CanvasDouble. Keyed on size, mode, view root, sizes flag and model serial.
        """
        key = (int(w), int(h), self._mode, self._view, self._apparent, self._serial)
        if self._geom_key == key:
            return self.tiles, self.segments, self.center
        tiles: list[Tile] = []
        segments: list[Seg] = []
        center = None
        if self._model is not None and self._view is not None:
            if self._mode == MODE_RINGS:
                segments, center = self._rings_geom(w, h)
            else:
                self._nest(self._model, self._view, 0.0, 0.0, w, h, 0, 0, tiles)
        self._geom_key = key
        self.tiles, self.segments, self.center = tiles, segments, center
        self._last_size = (int(w), int(h))
        return tiles, segments, center

    def _nest(self, result, idx, x, y, w, h, depth, hue, tiles) -> None:
        """Squarify the children of `idx` into its rect; recurse into each child."""
        node = result.node(idx)
        if node is None:
            return
        tiles.append(Tile(idx, x, y, w, h, depth, hue))
        if depth >= _MAX_DEPTH or min(w, h) < 5 or len(tiles) >= 2048:
            return
        pairs = [(c, self._node_size(result.node(c)))
                 for c in self._sibling_order(result, idx)]
        if not pairs:
            return
        labeled = w >= LABEL_MIN_W and h >= LABEL_MIN_H
        header = _HEADER_H if (labeled and h >= 42.0) else 0.0
        top = y + (header if header else _GAP)
        ix = x + _GAP
        iy = top
        iw = max(0.0, w - 2.0 * _GAP)
        ih = max(0.0, h - header - _GAP if header else h - 2.0 * _GAP)
        if iw <= 0.0 or ih <= 0.0:
            return
        sizes = [size for _, size in pairs]
        unshown = self._node_size(result.node(idx)) - sum(sizes)
        indices = [child for child, _ in pairs]
        if unshown > 0:
            insert_at = next((i for i, size in enumerate(sizes) if size < unshown), len(sizes))
            sizes.insert(insert_at, unshown)
            indices.insert(insert_at, None)
        rects = _squarify(sizes, ix, iy, iw, ih)
        nhue = len(SERIES_NAMES)
        for i, (child, rect) in enumerate(zip(indices, rects)):
            if child is None:
                continue
            child_hue = hue if depth > 0 else i % nhue
            self._nest(result, child, rect[0], rect[1], rect[2], rect[3],
                       depth + 1, child_hue, tiles)

    # ------------------------------------------------------------ rings geometry
    def _rings_geom(self, w, h):
        """(segments, (cx, cy, r_center)): sunburst wedges for the current view.

        Angles run in [-pi/2, 3*pi/2): -pi/2 is 12 o'clock, sweep grows clockwise.
        Sibling wedges are exactly proportional to sibling sizes; a parent wedge
        covers its own size share and children fill a sub-range of the parent span.
        """
        segments: list[Seg] = []
        result = self._model
        cx, cy = w / 2.0, h / 2.0
        outer = max(12.0, min(w, h) / 2.0 - 4.0)
        center_r = outer * _CENTER_FRAC

        def deep(idx, depth) -> int:
            """Deepest ring level actually used below `idx`, capped at _MAX_RINGS."""
            if depth >= _MAX_RINGS or not self._is_dir(result, idx):
                return depth
            best = depth
            for c in self._sibling_order(result, idx):
                best = max(best, deep(c, depth + 1))
                if best == _MAX_RINGS:
                    break
            return best

        used = max(1, deep(self._view, 0))
        ring_t = (outer - center_r) / used

        def wedge(idx, a0, a1, depth, hue) -> None:
            """Append one ring: children of `idx` proportionally within (a0, a1)."""
            pos = self._sibling_order(result, idx)
            if depth > used or not pos:
                return
            span = a1 - a0
            total = self._node_size(result.node(idx))
            if total <= 0.0:
                return
            nhue = len(SERIES_NAMES)
            r_in = center_r + (depth - 1) * ring_t
            r_out = r_in + ring_t
            angle = a0
            for i, c in enumerate(pos):
                sweep = span * self._node_size(result.node(c)) / total
                if len(segments) >= 2048:
                    return
                segments.append(Seg(c, depth, angle, angle + sweep, r_in, r_out,
                                    i % nhue))
                wedge(c, angle, angle + sweep, depth + 1, i % nhue)
                angle += sweep

        wedge(self._view, -math.pi / 2.0, 3.0 * math.pi / 2.0, 1, 0)
        return segments, (cx, cy, center_r)

    # ------------------------------------------------------------ hit testing
    def _hit(self, x, y, w=None, h=None):
        """("tile"|"seg"|"center", idx) under the pointer, or (None, None).

        `w`/`h` override the widget allocation — unrealized widgets (headless
        tests) have no allocation, so the last built geometry's size is used.
        """
        w = w if w is not None else (
            max(0, self.get_allocated_width()) or (self._last_size or (0, 0))[0])
        h = h if h is not None else (
            max(0, self.get_allocated_height()) or (self._last_size or (0, 0))[1])
        if w <= 0 or h <= 0:
            return None, None
        _tiles, segments, center = self._geometry(w, h)
        if center is not None:
            dx, dy = x - center[0], y - center[1]
            r = math.hypot(dx, dy)
            if r <= center[2]:
                return "center", None
            a = math.atan2(dy, dx)
            if a < -math.pi / 2.0:
                a += 2.0 * math.pi
            for seg in reversed(segments):
                if seg.r_in <= r <= seg.r_out and seg.a0 <= a <= seg.a1:
                    return "seg", seg.idx
            return None, None
        tiles = _tiles
        for t in reversed(tiles):
            if t.x <= x <= t.x + t.w and t.y <= y <= t.y + t.h:
                return "tile", t.idx
        return None, None

    # ------------------------------------------------------------ pointer input
    def _motion(self, _c, x, y) -> None:
        kind, idx = self._hit(x, y)
        idx = idx if idx is not None else None
        self._set_sel(idx, "hover")
        if idx is None:
            self.set_tooltip_text(None)
        else:
            try:
                self.set_tooltip_text(self._summary(idx))
            except Exception:
                self.set_tooltip_text(None)

    def _leave(self, _c) -> None:
        if self._sel_source == "hover":
            self._set_sel(None, "hover")
            self.set_tooltip_text(None)

    def _click(self, gc, _n, x, y) -> None:
        btn = gc.get_current_button()
        kind, idx = self._hit(x, y)
        if btn == 3:
            self._menu(x, y, idx)
            return
        if btn == 2:
            self.up()
            return
        if kind == "center":
            self.up()
            return
        if idx is None or kind is None:
            return
        if kind in ("tile", "seg") and idx != self._view \
                and self._is_dir(self._model, idx):
            self.drill(idx)

    # ------------------------------------------------------------ keyboard
    def _key(self, _c, keyval, _kc, _st) -> bool:
        """Arrows move the selection, Enter drills in, Backspace up, Escape clears."""
        result = self._model
        if result is None or self._view is None:
            return False
        key = Gdk.keyval_name(keyval) or ""
        pos = self._sibling_order(result, self._view)
        if not pos:
            return False
        try:
            at = pos.index(self._sel)
        except ValueError:
            at = -1
        if key in ("Right", "Down"):
            self._set_sel(pos[min(len(pos) - 1, at + 1 if at >= 0 else 0)], "kb")
            return True
        if key in ("Left", "Up"):
            self._set_sel(pos[max(0, at - 1) if at >= 0 else 0], "kb")
            return True
        if key == "Home":
            self._set_sel(pos[0], "kb")
            return True
        if key == "End":
            self._set_sel(pos[-1], "kb")
            return True
        if key in ("Return", "KP_Enter", "ISO_Enter") and at >= 0:
            self.drill(pos[at])
            return True
        if key == "BackSpace":
            return self.up()
        if key == "Escape":
            if self._sel is not None:
                self._set_sel(None, "kb")
            return True
        return False

    # ------------------------------------------------------------ context menu
    def _menu_items(self, path, idx):
        items = [
            ("Open in Files", self._open_files, path, True, None),
            ("Copy Path", self._copy_path, path, True, None),
        ]
        if self.trash_action is not None and self.trash_allowed is not None:
            model = self._model
            allowed = self.trash_allowed(model, idx)
            items.append(("Move to Trash", lambda target: self.trash_action(*target),
                          (model, idx), allowed,
                          None if allowed else "Trash is unavailable for this folder."))
        return items

    def _unrealize_menu(self, _widget) -> None:
        if self._pop is not None:
            self._pop.unparent()
            self._pop = None

    def _menu(self, x, y, idx) -> None:
        """Right-click popover: Open in Files / Copy Path / Move to Trash."""
        if self._model is None or idx is None:
            return
        path = self._model.path(idx) or ""
        pop = self._popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                      margin_top=4, margin_bottom=4)
        for label, cb, arg, sensitive, tip in self._menu_items(path, idx):
            mb = Gtk.Button(label=label)  # Gtk.ModelButton is gone in GTK 4.14
            mb.add_css_class("flat")
            mb.set_sensitive(sensitive)
            if tip:
                mb.set_tooltip_text(tip)
            mb.connect("clicked", lambda _b, cb=cb, arg=arg: cb(arg))
            box.append(mb)
        pop.set_child(box)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.popup()

    def _popover(self) -> Gtk.Popover:
        """One popover, reused; contents are replaced on every invocation."""
        if self._pop is None:
            self._pop = Gtk.Popover()
            self._pop.set_has_arrow(False)
            self._pop.set_autohide(True)
            self._pop.set_parent(self)
        child = self._pop.get_child()
        if child is not None:
            self._pop.set_child(None)
        return self._pop

    def _open_files(self, path) -> None:
        """Hand the directory to the desktop's default file manager."""
        try:
            uri = Gio.File.new_for_path(path).get_uri()
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception:
            pass  # no window handle here; nothing else to do

    def _copy_path(self, path) -> None:
        disp = Gdk.Display.get_default()
        if disp is None:
            return
        disp.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(str(path)))

    # ------------------------------------------------------------ painting
    def _draw(self, _area, cr, w, h) -> None:
        """Draw func: empty state, or treemap/rings from the cached geometry."""
        if self._model is None:
            self._draw_empty(cr, w, h, "No disk-usage data yet",
                             "Open the Storage page to start a scan.")
            return
        error = getattr(self._model, "error", None)
        if error:
            self._draw_empty(cr, w, h, "Scan failed", str(error))
            return
        if self._view is None:
            self._draw_empty(cr, w, h, "Scan has no root node", None)
            return
        if self._mode == MODE_RINGS:
            self._draw_rings(cr, w, h)
        else:
            self._draw_treemap(cr, w, h)

    def _draw_empty(self, cr, w, h, message, sub=None) -> None:
        pal = theme.colours()
        layout = make_layout(cr)
        layout.set_font_description(theme.mono_font(10.5))
        layout.set_width(max(10, int((w - 32) * Pango.SCALE)))
        layout.set_ellipsize(Pango.EllipsizeMode.NONE)
        layout.set_text(message, -1)
        _mw, mh = layout.get_pixel_size()
        cr.set_source_rgba(*pal["line"], 1.0)
        cr.move_to((w - _mw) / 2.0, (h - mh) / 2.0 - (6 if sub else 0))
        PangoCairo.show_layout(cr, layout)
        if sub:
            layout2 = make_layout(cr)
            layout2.set_font_description(theme.mono_font(8.5))
            layout2.set_width(max(10, int((w - 32) * Pango.SCALE)))
            layout2.set_wrap(Pango.WrapMode.WORD_CHAR)
            layout2.set_text(sub, -1)
            sw, sh = layout2.get_pixel_size()
            cr.set_source_rgba(*pal["dim"][:3], pal["dim"][3])
            cr.move_to((w - sw) / 2.0, (h - mh) / 2.0 + mh + 8.0)
            PangoCairo.show_layout(cr, layout2)

    # ------------------------------------------------------------ treemap painting
    def _draw_treemap(self, cr, w, h) -> None:
        pal = theme.colours()
        bg = _bg_rgb()
        tiles, _segments, _center = self._geometry(w, h)
        if not tiles:
            return
        for t in tiles:
            rgb = charts.colour(SERIES_NAMES[t.hue % len(SERIES_NAMES)])
            alpha = _tile_alpha(t.depth)
            cr.set_source_rgba(*rgb, alpha)
            r = min(2.0, t.w / 4.0, t.h / 4.0)
            _round_rect(cr, t.x + 1.0, t.y + 1.0, max(1.0, t.w - 2.0),
                        max(1.0, t.h - 2.0), max(0.0, r))
            cr.fill()
        name_l = make_layout(cr)
        name_l.set_font_description(theme.mono_font(9.0))
        size_l = make_layout(cr)
        size_l.set_font_description(theme.mono_font(8.0))
        for t in tiles:
            if t.w < LABEL_MIN_W or t.h < LABEL_MIN_H:
                continue
            node = self._model.node(t.idx)
            if node is None:
                continue
            rgb = charts.colour(SERIES_NAMES[t.hue % len(SERIES_NAMES)])
            blended = _blend(rgb, bg, _tile_alpha(t.depth))
            ink, best = _pick_ink(blended)
            lx, ly = t.x + 6.0, t.y + 3.0
            avail = max(8.0, t.w - 12.0)
            name_l.set_ellipsize(Pango.EllipsizeMode.END)
            name_l.set_width(int(avail * Pango.SCALE))
            name_l.set_text(str(getattr(node, "name", "")) or "…", -1)
            size_l.set_ellipsize(Pango.EllipsizeMode.END)
            size_l.set_width(int(avail * Pango.SCALE))
            size_l.set_text(fmt.bytes_(self._node_size(node)), -1)
            nw, nh = name_l.get_pixel_size()
            sw, sh = size_l.get_pixel_size()
            plate_w, plate_h = max(nw, sw) + 8.0, nh + sh + 2.0
            if best < _AA:
                plate = _plate_for(blended, ink)
                cr.set_source_rgb(*plate)
                _round_rect(cr, lx - 4.0, ly - 2.0, plate_w, plate_h, 3.0)
                cr.fill()
            cr.set_source_rgb(*ink)
            cr.move_to(lx, ly)
            PangoCairo.show_layout(cr, name_l)
            cr.move_to(lx, ly + nh)
            PangoCairo.show_layout(cr, size_l)
        if self._sel is None:
            return
        for t in tiles:
            if t.idx == self._sel:
                cr.set_source_rgb(*pal["gold"])
                cr.set_line_width(2.0)
                _round_rect(cr, t.x + 1.5, t.y + 1.5, max(1.0, t.w - 3.0),
                            max(1.0, t.h - 3.0), 2.0)
                cr.stroke()

    # ------------------------------------------------------------ rings painting
    def _draw_rings(self, cr, w, h) -> None:
        pal = theme.colours()
        bg = _bg_rgb()
        _tiles, segments, center = self._geometry(w, h)
        if center is None:
            return
        cx, cy, center_r = center
        for seg in segments:
            if seg.a1 - seg.a0 < _MIN_WEDGE:
                continue
            rgb = charts.colour(SERIES_NAMES[seg.hue % len(SERIES_NAMES)])
            alpha = _tile_alpha(max(0, seg.depth - 1))
            full = _wedge_path(cr, cx, cy, seg.r_in + 0.5, seg.r_out - 0.5,
                               seg.a0, seg.a1)
            cr.set_source_rgba(*rgb, alpha)
            cr.fill()
            if full:
                cr.set_fill_rule(cairo.FillRule.WINDING)
            cr.set_source_rgba(*bg, 1.0)
            cr.set_line_width(1.2)
            cr.stroke()
        wl = make_layout(cr)
        wl.set_font_description(theme.mono_font(8.0))
        for seg in segments:
            sweep = seg.a1 - seg.a0
            thick = seg.r_out - seg.r_in
            if sweep < _WEDGE_LABEL_SWEEP or thick < _WEDGE_LABEL_THICK:
                continue
            wnode = self._model.node(seg.idx)
            if wnode is None:
                continue
            mid = (seg.a0 + seg.a1) / 2.0
            mr = (seg.r_in + seg.r_out) / 2.0
            wl.set_ellipsize(Pango.EllipsizeMode.END)
            wl.set_width(int(max(24.0, sweep * mr * 0.7) * Pango.SCALE))
            wl.set_text(str(getattr(wnode, "name", "")) or "", -1)
            ww, wh = wl.get_pixel_size()
            rgb = charts.colour(SERIES_NAMES[seg.hue % len(SERIES_NAMES)])
            blended = _blend(rgb, bg, _tile_alpha(max(0, seg.depth - 1)))
            ink, best = _pick_ink(blended)
            tx = cx + math.cos(mid) * mr - ww / 2.0
            ty = cy + math.sin(mid) * mr - wh / 2.0
            if best < _AA:
                cr.set_source_rgb(*_plate_for(blended, ink))
                _round_rect(cr, tx - 3.0, ty - 1.0, ww + 6.0, wh + 2.0, 3.0)
                cr.fill()
            cr.set_source_rgb(*ink)
            cr.move_to(tx, ty)
            PangoCairo.show_layout(cr, wl)
        node = self._model.node(self._view)
        if node is None:
            return
        avail = max(20.0, 2.0 * center_r - 16.0)
        total_l = make_layout(cr)
        total_l.set_font_description(theme.mono_font(12.0, weight="Bold"))
        total_l.set_ellipsize(Pango.EllipsizeMode.END)
        total_l.set_width(int(avail * Pango.SCALE))
        total_l.set_text(fmt.bytes_(self._node_size(node)), -1)
        tw, th = total_l.get_pixel_size()
        cr.set_source_rgb(*pal["line"])
        cr.move_to(cx - tw / 2.0, cy - th / 2.0 - (7.0 if center_r >= 44.0 else 0.0))
        PangoCairo.show_layout(cr, total_l)
        if center_r >= 44.0:
            name_l = make_layout(cr)
            name_l.set_font_description(theme.mono_font(8.5))
            name_l.set_ellipsize(Pango.EllipsizeMode.END)
            name_l.set_width(int(avail * Pango.SCALE))
            name_l.set_text(str(getattr(node, "name", "")) or "", -1)
            nw, nh = name_l.get_pixel_size()
            cr.set_source_rgba(*pal["dim"][:3], pal["dim"][3])
            cr.move_to(cx - nw / 2.0, cy + th / 2.0 + 2.0)
            PangoCairo.show_layout(cr, name_l)
        if self._sel is None:
            return
        for seg in segments:
            if seg.idx != self._sel:
                continue
            full = _wedge_path(cr, cx, cy, seg.r_in, seg.r_out, seg.a0, seg.a1)
            cr.set_source_rgb(*pal["gold"])
            cr.set_line_width(2.0)
            cr.stroke()
            if full:
                cr.set_fill_rule(cairo.FillRule.WINDING)
