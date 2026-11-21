# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Detail pages for integrated (Intel / AMD) and discrete GPUs."""
from __future__ import annotations

from gi.repository import Gtk, Pango

from ...telemetry import fmt
from ...telemetry.gpu_intel import ENGINE_NAMES
from ...telemetry.util import join, read_str
from ..charts import Line, legend
from ..viewmodels import DGPU_LOST_TEXT, fmt_temp, gpu_vendor_title
from ..widgets import label
from .monitor_base import MonitorPage, StatGrid, note, set_text

def pct(v):
    return f"{v:.0f}%"

LINK_TIP = "Negotiated PCIe link, current vs maximum (read from sysfs only while awake)"


def link_text(link: dict | None) -> str | None:
    """'8.0 GT/s ×8 (max 16.0 GT/s ×16)' from info[...]['link']; None when absent.

    Whatever fields the reader published are shown (a suspended GPU publishes no link
    at all, so the cell falls back to "–" — a cached maximum is never republished).
    """
    if not link:
        return None

    def part(speed, width):
        bits = []
        if speed is not None:
            bits.append(str(speed))
        if width is not None:
            bits.append(f"×{width}")
        return " ".join(bits)

    cur = part(link.get("current_speed"), link.get("current_width"))
    mx = part(link.get("max_speed"), link.get("max_width"))
    if cur and mx:
        return f"{cur} (max {mx})"
    if mx:
        return f"(max {mx})"  # only the maximum is known: never pass it off as current
    return cur or None


def video_entries(engines: list[str] | None) -> list[tuple[str, str]]:
    """Display rows for the engines a provider actually reports: (key, label) pairs.

    enc + dec both present -> separate 'Video encode' / 'Video decode' rows; only the
    combined 'video' engine -> one 'Video (combined)' row; a single enc or dec shows
    what exists. Engines the provider does not publish never appear, and non-video
    engines keep their ENGINE_NAMES labels.
    """
    superseded = {"enc", "dec"}.issubset(engines or ())
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for e in engines or []:
        if e in seen or (e == "video" and superseded):
            continue
        seen.add(e)
        out.append((e, "Video (combined)" if e == "video" else ENGINE_NAMES.get(e, e.title())))
    return out


class ProcTable(Gtk.Box):
    """Small table of processes using a GPU (rebuilt only when the set changes)."""

    def __init__(self, root: str, columns: list[str]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.root, self.columns = root, columns
        self.add_css_class("gpu-procs")
        self._key = None
        self._names: dict[int, str] = {}
        self.empty = note("No processes reported.")
        self.show_pid = True
        self.pid_labels: list[Gtk.Label] = []

    def name(self, pid: int) -> str:
        n = self._names.get(pid)
        if n is None:
            n = read_str(join(self.root, f"proc/{pid}/comm")) or f"pid {pid}"
            self._names[pid] = n
        return n

    def set_rows(self, rows: list[tuple[int, list[str]]]) -> None:
        key = tuple((pid, tuple(cells)) for pid, cells in rows)
        if key == self._key:
            return
        self._key = key
        self._names.clear()  # PIDs may be reused; retain only this small visible set.
        child = self.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.remove(child)
            child = nxt
        if not rows:
            self.append(self.empty)
            return
        self.pid_labels = []
        head = Gtk.Box(spacing=12)
        head.add_css_class("gpu-procs-head")
        head.append(label("Process", ("stat-caption",), hexpand=True))
        self.pid_labels.append(label("PID", ("stat-caption",), xalign=1.0, width_chars=6))
        head.append(self.pid_labels[-1])
        for c in self.columns:
            head.append(label(c, ("stat-caption",), xalign=1.0, width_chars=7))
        self.append(head)
        for pid, cells in rows:
            r = Gtk.Box(spacing=12)
            r.add_css_class("gpu-procs-row")
            r.append(label(f"{self.name(pid)}", ("numeric",), hexpand=True, wrap=True,
                           wrap_mode=Pango.WrapMode.WORD_CHAR))
            self.pid_labels.append(label(str(pid), ("dim-label", "numeric"), xalign=1.0,
                                         width_chars=6))
            r.append(self.pid_labels[-1])
            for c in cells:
                r.append(label(c, ("numeric",), xalign=1.0, width_chars=7))
            self.append(r)
        self.set_show_pid(self.show_pid)

    def set_show_pid(self, show: bool) -> None:
        """The PID column goes first when the window is phone-sized."""
        self.show_pid = show
        for lab in self.pid_labels:
            lab.set_visible(show)


class IntelGpuPage(MonitorPage):
    key, title = "igpu", "Integrated graphics"

    def __init__(self, win) -> None:
        super().__init__(win)
        ig = self.hub.sampler.igpu
        self.name, self.sub, self.pills = self.header(
            gpu_vendor_title({"name": getattr(ig, "name", "")}, "igpu"), "")
        p = self.panel("Utilisation", suffix=legend([("Render engine busy", "primary"),
                                                     ("GT awake (not in RC6)", "gold")]))
        p.append(self.chart([Line("igpu.util", "Render"),
                             Line("igpu.awake", "Awake", "gold", fill=False, dash=True)],
                            y_fixed=(0, 100), fmt=pct, height=180))
        self.stats = StatGrid(2, 4)
        for key, cap, tip in (("util", "Render busy", None), ("awake", "GT awake", None),
                              ("rc6", "RC6 (power-gated)", "Share of time the GT slept"),
                              ("freq", "Actual clock", "gt_act_freq_mhz"),
                              ("req", "Requested clock", "gt_cur_freq_mhz"),
                              ("range", "Clock range", "min – max (RP0)"),
                              ("clients", "DRM clients", None),
                              ("throttle", "Throttling", "throttle_reason_status"),
                              ("link", "PCIe link", LINK_TIP)):
            self.stats.add(key, cap, tip)
        p.append(self.stats)
        p.append(note("Utilisation is summed from the per-client engine time the DRM driver "
                      "(i915 / xe / amdgpu) publishes in /proc/<pid>/fdinfo, for processes this "
                      "app may read (your session). The iGPU shares system memory."))
        fp = self.panel("Clock", suffix=legend([("Actual MHz", "primary"),
                                                ("Requested MHz", "gold")]))
        fp.append(self.chart([Line("igpu.freq", "Actual"),
                              Line("igpu.freq_req", "Requested", "gold", fill=False)],
                             floor=0, min_span=1600, fmt=lambda v: f"{v:.0f}", height=130))
        ep = self.panel("Engines")
        self.engines = StatGrid(2, 5)
        ep.append(self.engines)
        self._engines_shown: set[str] = set()
        pp = self.panel("Processes using the iGPU")
        self.procs = ProcTable(self.hub.root, ["busy"])
        pp.append(self.procs)

    def fit_width(self, level: int) -> None:
        self.procs.set_show_pid(level < 3)

    def update(self) -> None:
        st = self.hub.store
        ig = self.hub.info.get("igpu") or {}
        set_text(self.name, ig.get("name") or gpu_vendor_title(ig, "igpu"))
        if not ig.get("present"):
            set_text(self.sub, "No integrated graphics device is reported by the kernel, so "
                               "this page has nothing to show.")
            return
        set_text(self.sub, f"{ig.get('driver')} · {ig.get('pdev')} · shares system memory")
        g = self.stats
        g.set("util", fmt.pct(st.last("igpu.util"), 1))
        g.set("awake", fmt.pct(st.last("igpu.awake"), 1))
        g.set("rc6", fmt.pct(st.last("igpu.rc6"), 1))
        act = st.last("igpu.freq")
        g.set("freq", ("0 MHz (asleep)" if act == 0 else f"{act:.0f} MHz") if act is not None
              else "–")
        g.set("req", fmt.num(st.last("igpu.freq_req")) + " MHz")
        g.set("range", f"{ig.get('min_mhz') or '–'} – {ig.get('rp0_mhz') or ig.get('max_mhz')}"
                       " MHz")
        g.set("clients", str(ig.get("clients", "–")))
        thr = ig.get("throttled")
        g.set("throttle", "–" if thr is None else ("yes" if thr else "no"))
        g.set("link", link_text(ig.get("link")) or "–")
        entries = video_entries(ig.get("engines"))
        visible_engines = {key for key, _ in entries}
        for e in self._engines_shown - visible_engines:
            self.engines.show(e, False)
        for e, lab in entries:
            if e not in self._engines_shown:
                self._engines_shown.add(e)
                self.engines.add(e, lab)
            self.engines.show(e, True)
            self.engines.set(e, fmt.pct(st.last(f"igpu.engine.{e}"), 1))
        per = ig.get("per_pid") or {}
        rows = sorted(((pid, v) for pid, v in per.items() if v >= 0.05), key=lambda x: -x[1])
        self.procs.set_rows([(pid, [f"{v:.1f} %"]) for pid, v in rows[:12]])
        self.redraw()


class NvidiaGpuPage(MonitorPage):
    key, title = "dgpu", "NVIDIA graphics"

    def __init__(self, win) -> None:
        super().__init__(win)
        dg = self.hub.sampler.dgpu
        self.name, self.sub, self.pills = self.header(dg.name(), "")
        self.state_label = label("", ("pill",))
        self.pills.append(self.state_label)
        self.sleep_note = note("", "monitor-callout")
        self.body.append(self.sleep_note)
        p = self.panel("Utilisation", suffix=legend([("GPU", "primary"),
                                                     ("Memory controller", "gold")]))
        p.append(self.chart([Line("dgpu.util", "GPU"),
                             Line("dgpu.mem_util", "Memory", "gold", fill=False)],
                            y_fixed=(0, 100), fmt=pct, height=170,
                            dense_grid=True, monitor=True))
        self.stats = StatGrid(2, 4)
        for key, cap, tip in (("util", "GPU busy", None), ("vram", "VRAM used", None),
                              ("gr", "Graphics clock", None), ("mem", "Memory clock", None),
                              ("temp", "Temperature", None), ("power", "Power · limit", None),
                              ("pstate", "Performance state", "P0 fastest … P8/P12 idle"),
                              ("link", "PCIe link", LINK_TIP),
                              ("fan", "Fan", None), ("enc", "Encoder · decoder", None),
                              ("driver", "Driver", None), ("runtime", "Runtime PM", None),
                              ("source", "Source", None)):
            self.stats.add(key, cap, tip)
        p.append(self.stats)
        vp0 = self.panel("Video")
        vp0.set_visible(False)  # built only from series the provider actually publishes
        self.video_panel = vp0
        self._video_mode: str | None = None
        self._video_provider = None
        self._video_keys: set[str] = set()
        self._video_shown: set[str] = set()
        self._video_widgets = []
        self._video_chart = None
        vp = self.panel("Video memory")
        self.vram_chart = self.chart([Line("dgpu.vram", "VRAM", "slate", scale=1 / (1 << 30))],
                                     floor=0, min_span=0.5, fmt=lambda v: f"{v:g} GiB",
                                     height=120, dense_grid=True, monitor=True)
        vp.append(self.vram_chart)
        cp = self.panel("Clock, temperature and power")
        cp.append(legend([("Graphics MHz", "primary")]))
        cp.append(self.chart([Line("dgpu.gr_mhz", "Graphics")], floor=0, min_span=3000,
                             fmt=lambda v: f"{v:.0f}", height=110))
        cp.append(legend([("°C", "rose"), ("W", "gold")]))
        cp.append(self.chart([Line("dgpu.temp", "°C", "rose", fill=False),
                              Line("dgpu.power", "W", "gold", fill=False)], y_fixed=(0, 100),
                             fmt=lambda v: f"{v:.0f}", height=110))
        pp = self.panel("Processes using the dGPU")
        self.procs = ProcTable(self.hub.root, ["type", "VRAM", "SM"])
        pp.append(self.procs)
        pp.append(note("A process listed here with a few MiB (often the compositor) holds a "
                       "context on the GPU; that alone can keep it from sleeping."))

    def fit_width(self, level: int) -> None:
        self.procs.set_show_pid(level < 3)

    def refresh_temperature_unit(self) -> None:
        self.update()

    def update(self) -> None:
        st = self.hub.store
        dg = self.hub.info.get("dgpu") or {}
        vendor = gpu_vendor_title(dg, "dgpu")
        g = self.stats
        # PCI power errors are meaningful even when NVML could not initialise.
        # Check them before the missing-backend state so recovery advice stays visible.
        if dg.get("state") == "lost" or dg.get("runtime") == "error":
            set_text(self.name, dg.get("name") or vendor)
            set_text(self.sub, f"{vendor} is unavailable until the laptop restarts.")
            set_text(self.state_label, "Unavailable")
            self.sleep_note.set_visible(True)
            set_text(self.sleep_note, DGPU_LOST_TEXT)
            self.sleep_note.set_tooltip_text(dg.get("reason") or None)
            g.set("runtime", "power error · driver lost the GPU")
            for k in ("util", "vram", "gr", "mem", "temp", "power", "pstate", "link", "fan",
                      "enc", "driver", "source"):
                g.set(k, "–")
            self.video_panel.set_visible(False)
            self.procs.set_rows([])
            self.redraw()
            return
        if not dg.get("present") or not dg.get("backend") or dg.get("state") == "unavailable":
            set_text(self.name, dg.get("name") or vendor)
            set_text(self.sub, f"No readings from {vendor}. Check that the driver is "
                               "installed and working, then reopen this page.")
            self.sleep_note.set_visible(False)
            self.sub.set_tooltip_text(dg.get("reason") or None)
            set_text(self.state_label, "Unavailable")
            self.video_panel.set_visible(False)
            g.set("link", "–")
            self.procs.set_rows([])
            return
        self.sub.set_tooltip_text(None)
        set_text(self.name, dg.get("name") or vendor)
        stt = dg.get("state")
        rs, ps = dg.get("runtime"), dg.get("power_state")
        set_text(self.sub, f"{dg.get('addr')} · read through "
                 f"{'NVML' if dg.get('backend') == 'nvml' else dg.get('backend')}")
        g.set("runtime", f"{rs or 'no runtime PM'} · {ps or '–'}")
        g.set("source", {"nvml": "NVML (libnvidia-ml)", "nvidia-smi": "nvidia-smi (every 5 s)",
                         "fake": "fake tree"}.get(dg.get("backend"), "–"))
        if stt in ("sleeping", "idle-released"):
            set_text(self.state_label, f"Sleeping ({ps or 'D3'})" if stt == "sleeping"
                     else "Idle, released")
            self.sleep_note.set_visible(True)
            self.video_panel.set_visible(False)
            if stt == "sleeping":
                set_text(self.sleep_note, f"The GPU is powered down ({ps or 'D3'}). Reading "
                         "its sensors would wake it and cost battery, so nothing is queried "
                         "until something else wakes it. Graphs resume then.")
            else:
                left = dg.get("next_check_s") or 0
                age = dg.get("last_age_s") or 0
                set_text(self.sleep_note, "The GPU is awake but idle. Polling is paused so "
                         f"the driver can put it to sleep; next check in {left:.0f} s. "
                         f"Figures below are from the last check, {age:.0f} s ago.")
            last = dg.get("last_q") if stt == "idle-released" else None
            if last:
                self._fill(last, dg.get("static") or {}, st)
            else:
                for k in ("util", "vram", "gr", "mem", "temp", "power", "pstate", "fan",
                          "enc"):
                    g.set(k, "–")
            g.set("link", link_text(dg.get("link")) or "–")
            self.procs.set_rows([])
            self.redraw()
            return
        set_text(self.state_label, "Awake")
        self.sleep_note.set_visible(False)
        self._fill(dg.get("q") or {}, dg.get("static") or {}, st)
        g.set("link", link_text(dg.get("link")) or "–")
        self._update_video(dg, st)
        rows = []
        for pid, pr in sorted((dg.get("procs") or {}).items(),
                              key=lambda kv: -(kv[1].get("vram") or 0)):
            rows.append((pid, [pr.get("type") or "", fmt.bytes_(pr.get("vram")),
                               fmt.pct(pr.get("sm"))]))
        self.procs.set_rows(rows[:16])
        self.redraw()

    def _fill(self, q: dict, sta: dict, st) -> None:
        g = self.stats
        g.set("util", fmt.pct(q.get("util")))
        used, total = q.get("vram_used"), q.get("vram_total")
        g.set("vram", f"{fmt.bytes_(used)} of {fmt.bytes_(total, 0)}" if used is not None
              else "–")
        if total:
            self.vram_chart.y_fixed = (0, total / (1 << 30))
        g.set("gr", f"{fmt.num(q.get('gr_mhz'))} / {fmt.num(sta.get('max_gr_mhz'))} MHz")
        g.set("mem", f"{fmt.num(q.get('mem_mhz'))} MHz")
        g.set("temp", fmt_temp(q.get("temp")))
        g.set("power", f"{fmt.watts(q.get('power'))} · {fmt.watts(q.get('power_limit'), 0)}")
        g.set("pstate", f"P{q['pstate']}" if q.get("pstate") is not None else "–")
        fan = q.get("fan")
        gf = st.last("fan.gpu")
        g.set("fan", f"{fan} %" if fan is not None else
              (f"{fmt.rpm(gf)} (laptop fan)" if gf is not None else "not reported"),
              tooltip="" if fan is not None else
              "Laptop GPUs report no fan to NVML; this is the GPU fan from the ASUS hwmon")
        g.set("enc", f"{fmt.pct(q.get('enc'))} · {fmt.pct(q.get('dec'))}")
        g.set("driver", sta.get("driver_version") or "–")

    def _clear_video(self) -> None:
        for widget in self._video_widgets:
            self.video_panel.remove(widget)
        self._video_widgets.clear()
        if self._video_chart is not None:
            self.charts.remove(self._video_chart)
            self._video_chart = None
        self._video_shown.clear()
        self._video_mode = None

    def _update_video(self, dg: dict, st) -> None:
        """Display advertised engine rows or the NVML counters observed so far."""
        provider = dg.get("backend")
        if provider != self._video_provider:
            self._clear_video()
            self._video_keys.clear()
            self._video_provider = provider
        engines = dg.get("engines")
        q = dg.get("q") or {}
        if engines:
            if self._video_mode != "engines":
                self._clear_video()
                self.video_stats = StatGrid(2, 4)
                self.video_panel.append(self.video_stats)
                self._video_widgets.append(self.video_stats)
                self._video_mode = "engines"
            entries = video_entries(engines)
            visible_engines = {key for key, _ in entries}
            for key in self._video_shown - visible_engines:
                self.video_stats.show(key, False)
            for key, lab in entries:
                if key not in self._video_shown:
                    self._video_shown.add(key)
                    self.video_stats.add(key, lab)
                self.video_stats.show(key, True)
                self.video_stats.set(key, fmt.pct(st.last(f"dgpu.engine.{key}"), 1))
            self.video_panel.set_visible(True)
            return

        keys = self._video_keys | {key for key in ("enc", "dec") if q.get(key) is not None}
        if not keys:
            self.video_panel.set_visible(False)
            return
        if keys != self._video_keys or self._video_mode != "chart":
            self._clear_video()
            self._video_keys = keys
            series = [(f"dgpu.{key}", title, colour)
                      for key, title, colour in (("enc", "Encode", "primary"),
                                                 ("dec", "Decode", "gold"))
                      if key in keys]
            self._video_chart = self.chart(
                [Line(key, title, colour, fill=False) for key, title, colour in series],
                y_fixed=(0, 100), fmt=pct, height=110)
            self._video_widgets = [legend([(title, colour) for _, title, colour in series]),
                                   self._video_chart]
            for widget in self._video_widgets:
                self.video_panel.append(widget)
            self._video_mode = "chart"
        self.video_panel.set_visible(True)
