# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Processor detail: utilisation, per-core grid, frequency, temperatures, caches, policy."""
from __future__ import annotations

from ...telemetry import fmt
from ..charts import CoreGrid, Line, legend
from ..viewmodels import cpu_vendor_info, fmt_temp, temp_unit_label, temp_value
from .monitor_base import WINDOW_S, MonitorPage, StatGrid, note, set_text


def _ranges(cpus: list[int]) -> str:
    """[0, 1, 2, 5] -> '0-2, 5' (', '-joined, like the cache scope text)."""
    out: list[str] = []
    start = prev = None
    for c in sorted(cpus):
        if start is None:
            start = prev = c
        elif c == prev + 1:
            prev = c
        else:
            out.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = c
    if start is not None:
        out.append(str(start) if start == prev else f"{start}-{prev}")
    return ", ".join(out)


def format_policy_groups(groups: list[dict] | None) -> str | None:
    """Governor·EPP text when policy() reports several distinct CPU groups.

    One (or zero) group returns None: the caller keeps the cpu0-based display, which is
    identical to the single group. Several groups -> "governor · epp (cpulist)" joined
    with " · "; an absent EPP stays "–" (never inferred from the governor).
    """
    if not groups or len(groups) < 2:
        return None
    parts = []
    for g in groups:
        text = f"{g.get('governor') or '–'} · {g.get('epp') or '–'}"
        cpus = g.get("cpus") or []
        if cpus:
            text += f" ({_ranges(cpus)})"
        parts.append(text)
    return " · ".join(parts)


def _cache_scope(scope: str) -> str:
    """'0-1, 2-3' -> 'shared by CPUs 0-1, 2-3'; a mixed group renders each part.

    A size group may mix shared lists with private per-core instances of the same
    size; joining those verbatim would read 'shared by CPUs 0-1, per-core'.
    """
    parts = scope.split(", ")
    if "per-core" not in parts:
        return f"shared by CPUs {scope}"
    return "; ".join("per core" if p == "per-core" else f"shared by CPUs {p}"
                     for p in parts)


def cache_cells(caches: dict) -> list[tuple[str, str, str, str]]:
    """One cell per (level, type) that parsed: (key, caption, value, tooltip).

    The value joins the size groups ("8 × 4.0 MiB · 8 × 2.0 MiB"); the tooltip gives
    the (level, type) total plus each group's scope ("shared by CPUs 0-15" / "per
    core"). Levels/types that never parsed produce no cell at all.
    """
    out: list[tuple[str, str, str, str]] = []
    for level in sorted(caches):
        for ctype in sorted(caches[level]):
            groups = caches[level][ctype]
            key = f"L{level}-{ctype}"
            caption = f"L{level} · {ctype}"
            value = " · ".join(f"{g['instances']} × {fmt.bytes_(g['size_bytes'])}"
                               for g in groups)
            total = sum(g["instances"] * g["size_bytes"] for g in groups)
            tip = " · ".join(f"{g['instances']} × {fmt.bytes_(g['size_bytes'])} "
                             f"({_cache_scope(g['scope'])})" for g in groups)
            out.append((key, caption, value, f"{fmt.bytes_(total)} total: {tip}"))
    return out


class CpuPage(MonitorPage):
    key, title = "cpu", "Processor"

    def __init__(self, win) -> None:
        super().__init__(win)
        s = self.hub.sampler
        st = s.cpu.static
        self.static = st
        vinfo = cpu_vendor_info(st, {})
        sub = (f"{st['count']} thread{'s' if st['count'] != 1 else ''} · "
               f"{st['cores']} core{'s' if st['cores'] != 1 else ''}")
        if st["hybrid"]:
            sub += f" ({st['p_cores']} performance + {st['e_cores']} efficient)"
        self.name, self.sub, self.pills = self.header(st.get("model") or "Processor", sub)

        def pct(v):
            return f"{v:.0f}%"

        p = self.panel("Utilisation", suffix=legend([("User", "primary"), ("System", "gold"),
                                                     ("I/O wait", "slate")]))
        p.append(self.chart([Line("cpu.user", "User"), Line("cpu.system", "System", "gold"),
                             Line("cpu.iowait", "I/O wait", "slate")], stacked=True,
                            y_fixed=(0, 100), fmt=pct, height=190, window_s=WINDOW_S,
                            dense_grid=True, monitor=True))
        self.stats = StatGrid(2, 5)
        for key, cap, tip in (
                ("util", "Utilisation", "Busy time across all logical CPUs (/proc/stat)"),
                ("freq", "Average clock", "Mean of scaling_cur_freq over all CPUs"),
                ("temp", "Package", vinfo["temp_tip"]),
                ("power", "Package power", None),
                ("load", "Load average", "1, 5 and 15 minutes (runnable + uninterruptible)"),
                ("uptime", "Uptime", None),
                ("threads", "Runnable · threads", "Runnable now / all threads (/proc/loadavg)"),
                ("ctxt", "Context switches", "per second"),
                ("intr", "Interrupts", "per second"),
                ("governor", "Governor · EPP", None)):
            self.stats.add(key, cap, tip)
        p.append(self.stats)
        self.rapl_note = note("")
        p.append(self.rapl_note)
        cp = self.panel("Caches")
        self.cache_grid = StatGrid(2, 4)
        cells = cache_cells(st.get("caches") or {})
        for key, cap, value, tip in cells:
            self.cache_grid.add(key, cap, tip)
            self.cache_grid.set(key, value, tip)
        cp.append(self.cache_grid)
        cp.set_visible(bool(cells))
        cores = self.panel("Logical CPUs", suffix=legend(
            [("Performance core", "primary"), ("Efficient core", "gold")]
            if st["hybrid"] else []))
        cores.append(note("Each tile: usage over the last minute, current clock and the "
                          "temperature of its physical core."))
        self.grid = CoreGrid(self.hub, st["cpus"], st["topology"])
        self.charts.append(self.grid)
        cores.append(self.grid)
        fp = self.panel("Clock and temperature")
        fp.append(legend([("Average clock (GHz)", "primary")]))
        fp.append(self.chart([Line("cpu.freq.avg", "Avg", scale=1 / 1000)], floor=0,
                             min_span=5, fmt=lambda v: f"{v:g} GHz", height=120,
                             dense_grid=True, monitor=True))
        self.temp_legend = legend([(f"Package {temp_unit_label()}", "rose")])
        fp.append(self.temp_legend)
        self.temp_chart = self.chart([Line("temp.cpu", "Pkg", "rose")], y_fixed=(20, 100),
                                     fmt=lambda v: f"{temp_value(v)}°", height=120,
                                     dense_grid=True, monitor=True)
        fp.append(self.temp_chart)
        self.policy_grid = StatGrid(2, 4)
        for key, cap in (("driver", "Scaling driver"), ("pstate", vinfo["pstate_title"]),
                         ("turbo", "Turbo"), ("range", "Clock range")):
            self.policy_grid.add(key, cap)
        fp.append(self.policy_grid)
        self._pol: dict | None = None

    def on_shown(self) -> None:
        self.hub.sampler.cpu.want_policy = True
        super().on_shown()

    def on_hidden(self) -> None:
        self.hub.sampler.cpu.want_policy = False

    def refresh_temperature_unit(self) -> None:
        from gi.repository import Gtk
        stack = [self.temp_legend]
        unit = temp_unit_label()
        while stack:
            w = stack.pop()
            if isinstance(w, Gtk.Label):
                w.set_label(f"Package {unit}")
            c = w.get_first_child()
            while c is not None:
                stack.append(c)
                c = c.get_next_sibling()
        self.temp_chart.queue_draw()
        self.grid.queue_draw()
        self.stats.set("temp", fmt_temp(self.hub.store.last("temp.cpu")))

    def update(self) -> None:
        st, info = self.hub.store, self.hub.info
        cpu = info.get("cpu") or {}
        g = self.stats
        g.set("util", fmt.pct(st.last("cpu.total"), 1))
        f = st.last("cpu.freq.avg")
        g.set("freq", fmt.mhz(f))
        g.set("temp", fmt_temp(st.last("temp.cpu")))
        w = st.last("power.cpu")
        g.set("power", fmt.watts(w) if w is not None else "needs root")
        load = cpu.get("load")
        g.set("load", f"{load[0]:.2f} · {load[1]:.2f} · {load[2]:.2f}" if load else "–")
        g.set("uptime", fmt.duration(cpu.get("uptime")))
        g.set("threads", f"{load[3]} · {load[4]}" if load else "–")
        g.set("ctxt", fmt.count(st.last("cpu.ctxt")) + " /s")
        g.set("intr", fmt.count(st.last("cpu.intr")) + " /s")
        pol = cpu.get("policy")
        if pol is not None and pol is not self._pol:
            self._pol = pol
            groups_text = format_policy_groups(pol.get("groups"))
            if groups_text is not None:
                g.set("governor", groups_text)
            else:
                g.set("governor", f"{pol.get('governor') or '–'} · {pol.get('epp') or '–'}")
            pg = self.policy_grid
            pg.set("driver", pol.get("driver") or "–")
            vinfo = cpu_vendor_info(self.static, pol)
            if "pstate" in getattr(pg, "captions", ()):
                pg.captions["pstate"].set_label(vinfo["pstate_title"])
            if "temp" in getattr(g, "boxes", ()):
                g.boxes["temp"].set_tooltip_text(vinfo["temp_tip"])
            pg.set("pstate", pol.get("pstate_status") or "not in use")
            nt = pol.get("no_turbo")
            pg.set("turbo", {"0": "enabled", "1": "disabled"}.get(nt or "", "–"))
            topo = self.static["topology"]
            mins = [t["min_mhz"] for t in topo.values() if t["min_mhz"]]
            maxs = [t["max_mhz"] for t in topo.values() if t["max_mhz"]]
            pg.set("range", f"{fmt.mhz(min(mins))} – {fmt.mhz(max(maxs))}" if mins and maxs
                   else "–")
        vinfo = cpu_vendor_info(self.static, self._pol)
        set_text(self.rapl_note, "" if w is not None else vinfo["rapl_note"])
        self.rapl_note.set_visible(w is None)
        self.redraw()
