# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Network: per-interface receive/send rates, totals and link details."""
from __future__ import annotations

from gi.repository import Gtk

from ...telemetry import fmt
from ..charts import Line, legend
from .monitor_base import MonitorPage, Panel, StatGrid, note, set_text


class NetworkPage(MonitorPage):
    key, title = "network", "Network"

    def __init__(self, win) -> None:
        super().__init__(win)
        self.name, self.sub, pills = self.header("Network", "")
        self.virtual = Gtk.CheckButton(label="Show virtual interfaces", valign=Gtk.Align.CENTER)
        self.virtual.add_css_class("tall-check")
        self.virtual.connect("notify::active", lambda *_: self.update())
        pills.append(self.virtual)
        tp = self.panel("All physical interfaces", suffix=legend([("Receive", "primary"),
                                                                  ("Send", "gold")]))
        tp.append(self.chart([Line("net.rx", "Receive"), Line("net.tx", "Send", "gold",
                                                              fill=False)],
                             floor=0, min_span=1e6, fmt=fmt.axis_rate, height=170))
        self.ifaces: dict[str, tuple] = {}
        self._rate_rx = fmt.sticky_rate()
        self._rate_tx = fmt.sticky_rate()
        self._iface_rates: dict[str, tuple[fmt.StickyUnit, fmt.StickyUnit]] = {}
        self.box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                               min_children_per_line=1, max_children_per_line=2,
                               column_spacing=14, row_spacing=14)
        self.body.append(self.box)
        self.body.append(note("Rates from /proc/net/dev byte counters. Virtual interfaces "
                              "(VPN tunnels, bridges, containers) carry traffic that also "
                              "crosses a physical link, so they are left out of the total."))

    def _panel(self, name: str, meta: dict) -> tuple:
        p = Panel(f"{name} · {meta['kind']}")
        chart = self.chart([Line(f"net.{name}.rx", "Receive"),
                            Line(f"net.{name}.tx", "Send", "gold", fill=False)],
                           floor=0, min_span=1e5, fmt=fmt.axis_rate, height=110)
        p.append(chart)
        g = StatGrid(2, 3)
        for key, cap in (("rx", "Receive"), ("tx", "Send"), ("state", "State"),
                         ("rxt", "Received since boot"), ("txt", "Sent since boot"),
                         ("link", "Link")):
            g.add(key, cap)
        p.append(g)
        self.box.append(p)
        p.get_parent().set_focusable(False)
        return p, g, chart

    def save_state(self) -> dict:
        return {"show_virtual": self.virtual.get_active()}

    def restore_state(self, state: dict) -> None:
        if state and "show_virtual" in state:
            self.virtual.set_active(bool(state["show_virtual"]))

    def update(self) -> None:
        st = self.hub.store
        net = self.hub.info.get("net") or {}
        ifs = net.get("ifaces") or {}
        for gone in [k for k in self.ifaces if k not in ifs]:
            p, _g, chart = self.ifaces.pop(gone)
            parent = p.get_parent()
            if parent is not None:
                self.box.remove(parent)
            if chart in self.charts:
                self.charts.remove(chart)
            self._iface_rates.pop(gone, None)
        show_virtual = self.virtual.get_active()
        n_phys = 0
        for name, m in ifs.items():
            if not m["virtual"]:
                n_phys += 1
            if name not in self.ifaces:
                self.ifaces[name] = self._panel(name, m)
            p, g, _chart = self.ifaces[name]
            p.get_parent().set_visible(show_virtual or not m["virtual"])
            rx_fmt, tx_fmt = self._iface_rates.setdefault(
                name, (fmt.sticky_rate(), fmt.sticky_rate()))
            g.set("rx", rx_fmt(st.last(f"net.{name}.rx")))
            g.set("tx", tx_fmt(st.last(f"net.{name}.tx")))
            g.set("state", m.get("state") or "–")
            g.set("rxt", fmt.bytes_(m.get("rx_total")))
            g.set("txt", fmt.bytes_(m.get("tx_total")))
            link = "–"
            if m.get("wifi"):
                q, sig = m["wifi"]
                link = f"{sig:.0f} dBm · quality {q:.0f}/70"
            elif m.get("speed_mbps"):
                link = f"{m['speed_mbps']} Mbit/s"
            g.set("link", link)
        set_text(self.sub, f"{n_phys} physical · {len(ifs) - n_phys} virtual · receive "
                 f"{self._rate_rx(st.last('net.rx'))} · send {self._rate_tx(st.last('net.tx'))}")
        self.redraw()
