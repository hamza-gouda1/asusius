# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Memory detail: in use / reclaimable cache / free over time, composition, swap, pressure."""
from __future__ import annotations

from gi.repository import Gtk

from ...telemetry import fmt
from ..charts import CompositionBar, Line, legend
from .monitor_base import MonitorPage, StatGrid, note, set_text

GIB = 1 << 30

DDR_ROOT_TIP = "Speed, type and slot layout need root (DMI)"

ZRAM_TIP = ("Compressed RAM block devices and their memory allocation. Swap used and "
            "Swap size include zram devices enabled as swap. zswap is a separate cache.")


def zram_line(zram_list: list[dict] | None) -> str | None:
    """'On zram' row text: 'zram0: 1.2 GiB stored (compressed to 300 MiB, 320 MiB of RAM)'.

    Devices are ' · '-joined into one row. A field a device did not parse (short or
    malformed mm_stat) is simply omitted; None when nothing parsed at all, so the
    caller never shows a fabricated zero over an absent pool.
    """
    if not zram_list:
        return None
    parts = []
    for d in zram_list:
        name = d.get("device") or "zram"
        if d.get("orig") is None:
            parts.append(f"{name}: –")
            continue
        text = f"{name}: {fmt.bytes_(d['orig'])} stored"
        detail = []
        if d.get("compr") is not None:
            detail.append(f"compressed to {fmt.bytes_(d['compr'])}")
        if d.get("total") is not None:
            detail.append(f"{fmt.bytes_(d['total'])} of RAM")
        if detail:
            text += f" ({', '.join(detail)})"
        parts.append(text)
    return " · ".join(parts)


def composition_parts(m: dict) -> list[tuple[float, str]]:
    """(fraction, colour) parts for the CompositionBar: in use / cache / free.

    derive() guarantees in_use + reclaimable + free == total exactly, so the fractions
    sum to 1.0 (±1e-6). An empty list when there is no total yet (nothing to draw).
    """
    total = m.get("total") or 0
    if total <= 0:
        return []
    return [(m.get("in_use", 0) / total, "primary"),
            (m.get("reclaimable", 0) / total, "gold"),
            (m.get("free", 0) / total, "muted")]


def ddr_text(modules: list[dict] | None) -> str | None:
    """DDR module summary for the Total tooltip: '2 modules · DDR4 · 3200 MT/s — slots'.

    Unknown fields are left out (never guessed); None when there is nothing to show.
    """
    if not modules:
        return None
    kinds = sorted({m.get("type") for m in modules if m.get("type")})
    speeds = sorted({m.get("speed_mts") for m in modules if m.get("speed_mts")})
    text = f"{len(modules)} module{'s' if len(modules) != 1 else ''}"
    if kinds:
        text += " · " + "/".join(kinds)
    if speeds:
        text += (f" · {speeds[0]} MT/s" if len(speeds) == 1
                 else f" · {speeds[0]}–{speeds[-1]} MT/s")
    slots = [m.get("slot") for m in modules if m.get("slot")]
    if slots:
        text += " — " + ", ".join(slots)
    return text


class MemoryPage(MonitorPage):
    key, title = "memory", "Memory"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.name, self.sub, _ = self.header("Memory", "")
        gib = lambda v: f"{v:g} GiB"  # noqa: E731
        p = self.panel("Usage", suffix=legend([("in use", "primary"),
                                               ("cache (reclaimable)", "gold")]))
        self.main = self.chart([Line("mem.in_use", "in use", scale=1 / GIB),
                                Line("mem.reclaimable", "cache", "gold", scale=1 / GIB)],
                               stacked=True, floor=0, fmt=gib, height=200,
                               dense_grid=True, monitor=True)
        p.append(self.main)
        self.bar = CompositionBar(16)
        p.append(self.bar)
        self.bar_caption = Gtk.Box(spacing=18)
        self.bar_labels = {}
        for k in ("in_use", "reclaimable", "free"):
            lab = Gtk.Label(xalign=0, wrap=True)
            lab.add_css_class("numeric")
            lab.add_css_class("stat-sub")
            self.bar_caption.append(lab)
            self.bar_labels[k] = lab
        p.append(self.bar_caption)
        p.append(note("In use = total − available (what `free` calls used). Cache is memory "
                      "the kernel can reclaim on demand (available − free). The three parts "
                      "add up to the total; free is the space above the chart."))
        self.stats = StatGrid(2, 4)
        for key, cap, tip in (
                ("total", "Total", "MemTotal: RAM the kernel manages"),
                ("in_use", "In use", None), ("available", "Available", None),
                ("free", "Free", None),
                ("buff_cache", "Buffers + cache", "Buffers + Cached + SReclaimable, as free -b"),
                ("shmem", "Shared (tmpfs, shm)", None),
                ("slab", "Kernel slab", "reclaimable / unreclaimable"),
                ("dirty", "Dirty · writeback", None),
                ("committed", "Committed", "Committed_AS of CommitLimit"),
                ("dimm", "Module temperature", None)):
            self.stats.add(key, cap, tip)
        p.append(self.stats)
        sp = self.panel("Swap")
        self.swap_chart = self.chart([Line("mem.swap_used", "swap", "slate", scale=1 / GIB)],
                                     floor=0, min_span=0.5, fmt=gib, height=110,
                                     dense_grid=True, monitor=True)
        sp.append(self.swap_chart)
        self.swap = StatGrid(2, 4)
        for key, cap in (("used", "Swap used"), ("total", "Swap size"),
                         ("cached", "Swap cached"), ("zswap", "zswap")):
            self.swap.add(key, cap)
        sp.append(self.swap)
        self._zram_added = False  # the 'On zram' row appears only once a device parsed
        pp = self.panel("Pressure")
        self.psi_grid = StatGrid(2, 3)
        for key, cap in (("some", "Some stalled (10 s)"), ("full", "All stalled (10 s)"),
                         ("some60", "Some stalled (60 s)"), ("some300", "Some stalled (300 s)"),
                         ("full300", "All stalled (300 s)")):
            self.psi_grid.add(key, cap)
        pp.append(self.psi_grid)
        self.psi_note = note("")
        pp.append(self.psi_note)

    def update(self) -> None:
        info = self.hub.info
        st = self.hub.store
        m = info.get("mem") or {}
        if not m or "total" not in m:
            set_text(self.sub, (m or {}).get("error") or "No data yet")
            return
        total = m["total"]
        self.main.y_fixed = (0, total / GIB)
        set_text(self.sub, f"{fmt.bytes_(total)} usable · {fmt.bytes_(m['in_use'])} in use "
                           f"({100 * m['in_use'] / total:.0f} %)")
        self.bar.set_parts(composition_parts(m))
        for k, name in (("in_use", "In use"), ("reclaimable", "Cache"), ("free", "Free")):
            set_text(self.bar_labels[k], f"{name} {fmt.bytes_(m[k])}")
        g = self.stats
        ddr = m.get("ddr")
        if ddr is None:
            g.set("total", fmt.bytes_(total),
                  "MemTotal: RAM the kernel manages. " + DDR_ROOT_TIP)
        else:
            g.set("total", fmt.bytes_(total),
                  ddr_text(ddr.get("modules")) or "MemTotal: RAM the kernel manages")
        g.set("in_use", fmt.bytes_(m["in_use"]))
        g.set("available", fmt.bytes_(m["available"]))
        g.set("free", fmt.bytes_(m["free"]))
        g.set("buff_cache", fmt.bytes_(m["buff_cache"]))
        g.set("shmem", fmt.bytes_(m["shmem"]))
        g.set("slab", f"{fmt.bytes_(m['slab_reclaimable'])} / "
                      f"{fmt.bytes_(m['slab_unreclaimable'])}")
        g.set("dirty", f"{fmt.bytes_(m['dirty'])} · {fmt.bytes_(m['writeback'])}")
        g.set("committed", f"{fmt.bytes_(m['committed'])} of {fmt.bytes_(m['commit_limit'])}")
        d = st.last("temp.dimm0")
        g.set("dimm", fmt.temp(d) if d is not None else "no sensor")
        s = self.swap
        s.set("used", fmt.bytes_(m["swap_used"]))
        s.set("total", fmt.bytes_(m["swap_total"]) if m["swap_total"] else "no swap")
        s.set("cached", fmt.bytes_(m["swap_cached"]))
        zs = self.hub.sampler.mem.zswap_enabled()
        if m.get("zswap_pool") is None:
            s.set("zswap", "not reported")
        elif not zs and not m.get("zswap_stored"):
            s.set("zswap", "off")
        else:
            s.set("zswap", f"{fmt.bytes_(m['zswap_stored'])} in {fmt.bytes_(m['zswap_pool'])}")
        zram_rows = m.get("zram")
        if (m.get("zram_any") or zram_rows) and not self._zram_added:
            self.swap.add("zram", "On zram", ZRAM_TIP)
            self._zram_added = True
        if self._zram_added:  # once shown, degrade to "–" if every zram device disappears
            s.set("zram", zram_line(zram_rows) or "–", ZRAM_TIP)
        psi = m.get("psi")
        if psi:
            some, full = psi.get("some", {}), psi.get("full", {})
            self.psi_grid.set("some", fmt.pct(some.get("avg10"), 2))
            self.psi_grid.set("full", fmt.pct(full.get("avg10"), 2))
            self.psi_grid.set("some60", fmt.pct(some.get("avg60"), 2))
            self.psi_grid.set("some300", fmt.pct(some.get("avg300"), 2))
            self.psi_grid.set("full300", fmt.pct(full.get("avg300"), 2))
            set_text(self.psi_note, "Share of time tasks waited for memory (PSI).")
        else:
            for k in ("some", "full", "some60", "some300", "full300"):
                self.psi_grid.set(k, "–")
            set_text(self.psi_note, "Memory pressure is not measured: this kernel has no "
                                    "/proc/pressure (built without CONFIG_PSI or booted "
                                    "with psi=0).")
        self.redraw()
