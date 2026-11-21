# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Headless checks for UI preferences, vendor/preset viewmodels, CSS polish, and callbacks."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gtk  # noqa: E402

from asusius.backends.fake import DEFAULT_CURVES  # noqa: E402
from asusius.core import Capability, FanCurve, GpuMode, Telemetry  # noqa: E402
from asusius.telemetry.ring import Store  # noqa: E402
from asusius.ui import app as ui_app  # noqa: E402
from asusius.ui import chartmath as ui_chartmath  # noqa: E402
from asusius.ui import charts as ui_charts  # noqa: E402
from asusius.ui import disk_map as ui_disk_map  # noqa: E402
from asusius.ui import history_chart as ui_history_chart  # noqa: E402
from asusius.ui import live as ui_live  # noqa: E402
from asusius.ui import prefs  # noqa: E402
from asusius.ui import viewmodels as vm  # noqa: E402
from asusius.ui.pages.memory import MemoryPage  # noqa: E402
from asusius.ui.window import MainWindow  # noqa: E402
from asusius.ui.pages.agent_access import AgentAccessPage  # noqa: E402
from asusius.ui.pages.automation import AutomationPage  # noqa: E402
from asusius.ui.pages.battery import BatteryPage  # noqa: E402
from asusius.ui.pages.cpu import CpuPage  # noqa: E402
from asusius.ui.pages.display import DisplayPage  # noqa: E402
from asusius.ui.pages.gpu import GpuPage  # noqa: E402
from asusius.ui.pages.keyboard import HW_NOTE, KeyboardPage  # noqa: E402
from asusius.ui.pages.keyboard_effects import EffectsSection  # noqa: E402
from asusius.ui.pages.monitor_base import StatGrid  # noqa: E402
from asusius.ui.pages.services import ServicesPage  # noqa: E402
from asusius.ui.pages.updates import UpdatesPage  # noqa: E402

UI_DIR = Path(ui_app.__file__).resolve().parent


class PrefsTests(unittest.TestCase):
    def test_prefs_roundtrip_validation_and_xdg_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                d0 = prefs.load_prefs()
                self.assertEqual(d0, {"scheme": "system", "temp_unit": "C",
                                      "dismissed_setup": False})
                saved = prefs.save_prefs(scheme="dark", temp_unit="F", dismissed_setup=True)
                self.assertEqual(saved, {"scheme": "dark", "temp_unit": "F",
                                         "dismissed_setup": True})
                # Invalid values leave saved values intact
                after = prefs.save_prefs(scheme="neon", temp_unit="K")
                self.assertEqual(after["scheme"], "dark")
                self.assertEqual(after["temp_unit"], "F")
            # Switching XDG_CONFIG_HOME reloads clean defaults instead of stale cache
            with tempfile.TemporaryDirectory() as tmp2:
                with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp2}):
                    self.assertEqual(prefs.get_scheme(), "system")
                    self.assertEqual(prefs.get_temp_unit(), "C")


class ViewModelPolishTests(unittest.TestCase):
    def test_temperature_formatting_celsius_and_fahrenheit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                prefs.save_prefs(temp_unit="C")
                self.assertEqual(vm.temp_unit_label(), "°C")
                self.assertEqual(vm.temp_value(0.0), "0")
                self.assertEqual(vm.fmt_temp(65.0), "65\u00a0°C")
                self.assertEqual(vm.fmt_temp(None), vm.DASH)
                self.assertEqual(vm.temp_value(float("nan")), vm.DASH)

                prefs.save_prefs(temp_unit="F")
                self.assertEqual(vm.temp_unit_label(), "°F")
                self.assertEqual(vm.temp_value(100.0), "212")
                self.assertEqual(vm.fmt_temp(0.0), "32\u00a0°F")
                self.assertEqual(vm.fmt_temp(100.0, digits=1), "212.0\u00a0°F")

    def test_fan_presets_are_valid_and_revertible(self):
        temps, pwms = DEFAULT_CURVES[1]
        base = FanCurve.from_lists("CPU", list(temps), list(pwms))
        for name in ("Silent", "Balanced", "Cool", "Turbo"):
            curve = vm.fan_preset_curve(name, fan="GPU", enabled=True)
            self.assertEqual(len(curve.points), 8)
            model = vm.FanCurveModel(base)
            vm.apply_fan_preset(model, name)
            self.assertTrue(model.dirty)
            self.assertTrue(model.enabled)
            model.revert()
            self.assertFalse(model.dirty)

    def test_cpu_and_gpu_vendor_detection(self):
        intel = vm.cpu_vendor_info({"model": "Intel Core i7"}, {"driver": "intel_pstate"})
        self.assertEqual(intel["vendor"], "intel")
        self.assertEqual(intel["pstate_title"], "intel_pstate")

        amd = vm.cpu_vendor_info({"model": "AMD Ryzen 9 7945HX"}, {"driver": "amd-pstate-epp"})
        self.assertEqual(amd["vendor"], "amd")
        self.assertEqual(amd["pstate_title"], "amd-pstate")
        self.assertIn("k10temp", amd["temp_tip"])

        generic = vm.cpu_vendor_info({"model": "ARM Neoverse"}, {"driver": "cppc_cpufreq"})
        self.assertEqual(generic["vendor"], "generic")
        self.assertEqual(generic["pstate_title"], "P-state status")

        self.assertEqual(vm.gpu_vendor_title({"driver": "i915"}, "igpu"), "Intel graphics")
        self.assertEqual(vm.gpu_vendor_title({"driver": "amdgpu", "name": "Radeon 780M"}, "igpu"),
                         "AMD graphics")
        self.assertEqual(vm.gpu_vendor_title({"driver": "amdgpu"}, "dgpu"), "AMD graphics")
        self.assertEqual(vm.gpu_vendor_title({"name": "Intel Arc A770M"}, "dgpu"),
                         "Intel Arc graphics")
        self.assertEqual(vm.gpu_vendor_title({"driver": "nvml"}, "dgpu"), "NVIDIA graphics")

    def test_dashboard_readiness_states(self):
        err = vm.dashboard_readiness({}, "dbus timed out", {"running": False})
        self.assertEqual(err["id"], "caps-error")
        self.assertEqual(err["action"], "retry_caps")

        self.assertIsNone(vm.dashboard_readiness({}, None, {"running": False}, fake=True))
        self.assertIsNone(vm.dashboard_readiness(None, None, {"running": False}))

        no_asus = vm.dashboard_readiness(
            {c.value: {"available": False} for c in Capability}, None, {"running": True})
        self.assertEqual(no_asus["id"], "no-asusd")

        has_asus = {Capability.PROFILE.value: {"available": True}}
        stopped = vm.dashboard_readiness(has_asus, None, {"running": False})
        self.assertEqual(stopped["id"], "daemon-stopped")
        self.assertIsNone(vm.dashboard_readiness(has_asus, None, {"running": True}))

    def test_aura_modes_exported(self):
        self.assertIn(0, vm.AURA_MODES)
        self.assertIn(1, vm.AURA_MODES)


class AssetAndCssHygieneTests(unittest.TestCase):
    def test_wheel_icon_dir_is_discovered(self):
        with patch("pathlib.Path.is_dir", return_value=True):
            dirs = ui_app._icon_dirs()
        self.assertEqual(dirs[0], UI_DIR / "icons")
        self.assertIn(UI_DIR / "icons", dirs)

    def test_css_rules_and_tokens_present(self):
        sys_css = (UI_DIR / "system.css").read_text()
        self.assertIn("@MONO@", sys_css)
        self.assertNotIn("font-family: monospace;", sys_css)

        light = (UI_DIR / "style.css").read_text()
        dark = (UI_DIR / "style-dark.css").read_text()
        for token in (".bri-toggle > button", ".quick-pill", ".setup-card", ".preset-chip",
                      ".card-icon-badge", ".linked > button", "banner button"):
            self.assertIn(token, light)
        for token in (".linked > button", "button.suggested-action:disabled",
                      ".quick-pill.accent", ".setup-card"):
            self.assertIn(token, dark)

    def test_all_css_files_parse_cleanly_in_gtk(self):
        base = (UI_DIR / "style.css").read_text().replace("@MONO@", "monospace")
        css_files = (
            "style.css",
            "style-dark.css",
            "style-popover.css",
            "style-popover-dark.css",
            "monitor.css",
            "system.css",
        )
        for name in css_files:
            raw = (UI_DIR / name).read_text().replace("@MONO@", "monospace")
            self.assertNotIn("margin-end:", raw, f"GTK4 CSS rejects margin-end in {name}")
            self.assertNotIn("margin-start:", raw, f"GTK4 CSS rejects margin-start in {name}")
            for label, css in ((f"{name} (standalone)", raw),
                               (f"{name} (with style.css)",
                                raw if name == "style.css" else f"{base}\n{raw}")):
                provider = Gtk.CssProvider()
                errors = []
                provider.connect(
                    "parsing-error",
                    lambda _p, sec, err, lbl=label, out=errors: out.append(
                        f"{lbl}:{sec.get_start_location().lines + 1}: {err.message}"
                    ),
                )
                provider.load_from_string(css)
                self.assertEqual(errors, [], f"GTK CSS parsing errors in {label}: {errors}")


class PageCallbackSafetyTests(unittest.TestCase):
    def test_keyboard_brightness_recovery_and_note(self):
        page = SimpleNamespace(
            bri_row=Mock(),
            bri_buttons=[Mock(), Mock(), Mock(), Mock()],
            _quiet=False,
            brightness=0,
            _show=Mock(),
            hw_note=HW_NOTE,
            soft=SimpleNamespace(active=True),
            form=SimpleNamespace(controls=SimpleNamespace(breathe="none")),
            fx=Mock(),
        )
        KeyboardPage._bri_failed(page, RuntimeError("asusd down"))
        page.bri_row.set_sensitive.assert_called_once_with(False)
        KeyboardPage._got_bri(page, (2, 3))
        page.bri_row.set_sensitive.assert_called_with(True)
        page.bri_row.set_subtitle.assert_called_with("")
        KeyboardPage.describe(page)
        self.assertIn("Paused while a software effect is selected",
                      page.fx.set_description.call_args.args[0])

    def test_battery_error_recovery_and_health_reset(self):
        page = SimpleNamespace(
            limit_err=Mock(),
            scale=Mock(),
            presets=Mock(),
            oneshot=Mock(),
            adj=Mock(),
            _quiet=False,
            current=None,
            _label=Mock(),
            h_val=Mock(),
            level=Mock(),
            cap_val=Mock(),
            cyc_val=Mock(),
        )
        BatteryPage._limit_failed(page, RuntimeError("no battery"))
        page.oneshot.set_sensitive.assert_called_once_with(False)
        page.limit_err.set_visible.assert_called_with(True)

        BatteryPage._got_limit(page, 80)
        page.limit_err.set_visible.assert_called_with(False)
        page.oneshot.set_sensitive.assert_called_with(True)
        page.scale.set_sensitive.assert_called_with(True)
        page.presets.set_sensitive.assert_called_with(True)

        BatteryPage._health_failed(page, RuntimeError("read failed"))
        page.level.set_value.assert_called_once_with(0)
        page.cap_val.set_label.assert_called_once_with("–")
        page.cyc_val.set_label.assert_called_once_with("–")

    def test_display_recovery_and_markup_escape(self):
        page = SimpleNamespace(
            overdrive=Mock(),
            _quiet=False,
            rate_row=Mock(),
            msg=Mock(),
        )
        DisplayPage._state_failed(page, RuntimeError("no display"))
        page.msg.set_visible.assert_called_once_with(True)
        DisplayPage._attrs(page, None, RuntimeError("<bad&xml>"))
        self.assertIn("&lt;bad&amp;xml&gt;", page.overdrive.set_subtitle.call_args.args[0])

    def test_gpu_switch_disables_button_while_switching(self):
        st = SimpleNamespace(mode=GpuMode.HYBRID, pending_mode=None)
        page = SimpleNamespace(
            status=st,
            _target=lambda: GpuMode.INTEGRATED,
            _switching=True,
            switch_btn=Mock(),
        )
        GpuPage._sync(page)
        page.switch_btn.set_sensitive.assert_called_once_with(False)
        page._switching = False
        GpuPage._sync(page)
        page.switch_btn.set_sensitive.assert_called_with(True)

    def test_effects_timer_does_not_run_when_off(self):
        sec = SimpleNamespace(
            active=False,
            get_mapped=lambda: True,
            _root=SimpleNamespace(get_visible=lambda: True, get_property=lambda _n: False),
            _timer=None,
            _after_timer=0,
            _arm=Mock(),
        )
        EffectsSection._sync(sec)
        sec._arm.assert_not_called()
        sec._timer = 123
        self.assertFalse(EffectsSection._on_timer(sec))
        self.assertIsNone(sec._timer)

    def test_updates_caches_settings_and_copies_with_toast(self):
        page = SimpleNamespace(
            _loaded=Mock(),
            _auto_settings=None,
            auto_switch=Mock(),
            auto_row=Mock(),
            _syncing_auto=False,
            win=Mock(),
            get_clipboard=Mock(return_value=SimpleNamespace(set=Mock())),
        )
        UpdatesPage._loaded_all(page, (None, {"enabled": False}))
        self.assertEqual(page._auto_settings, {"enabled": False})
        page._loaded.assert_called_once_with(None)
        with patch("asusius.updates.settings.load_settings",
                   side_effect=AssertionError("must use cached settings")):
            UpdatesPage._render_auto(page)
        page.auto_switch.set_active.assert_called_once_with(False)
        UpdatesPage._copy_command(page, "sudo apt upgrade")
        page.win.toast.assert_called_once_with("Command copied")

    def test_services_after_act_respects_visibility_and_cleans_timer(self):
        page = SimpleNamespace(_shown=False, _after_id=99, refresh=Mock())
        self.assertFalse(ServicesPage._after_act(page))
        self.assertEqual(page._after_id, 0)
        page.refresh.assert_not_called()
        page._shown = True
        ServicesPage._after_act(page)
        page.refresh.assert_called_once_with(force_files=True)

    def test_automation_status_hides_resolved_config_error_and_escapes(self):
        page = SimpleNamespace(
            status={},
            svc_pill=Mock(),
            svc_btn=Mock(),
            svc_row=Mock(),
            cfg_err=Mock(),
            _mark_active=Mock(),
            _build_activity=Mock(),
        )
        AutomationPage._got_status(page, {"running": False, "config_error": "<bad&line>"})
        self.assertIn("&lt;bad&amp;line&gt;", page.cfg_err.set_subtitle.call_args.args[0])
        page.cfg_err.set_visible.assert_called_with(True)
        AutomationPage._got_status(page, {"running": True, "active_rules": []})
        page.cfg_err.set_visible.assert_called_with(False)

    def test_agent_access_telemetry_stats_off_thread(self):
        page = SimpleNamespace(
            loaded=True,
            win=SimpleNamespace(current="agent"),
            key="agent",
            _ticks=1,
            _sigs=(),
            _stat_busy=False,
            _paths=lambda: (),
            refresh=Mock(),
        )
        with patch("asusius.ui.pages.agent_access.run_bg") as run:
            AgentAccessPage.on_telemetry(page, None)
        run.assert_called_once()
        self.assertTrue(page._stat_busy)
        run.call_args.kwargs["done"](("changed",))
        self.assertFalse(page._stat_busy)
        page.refresh.assert_called_once_with()

    def test_gpu_page_on_telemetry_handles_active_sleeping_and_fake(self):
        page = SimpleNamespace(
            temp_val=Mock(),
            draw_val=Mock(),
            power_row=Mock(),
            power_val=Mock(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                prefs.save_prefs(temp_unit="C")

                active = Telemetry(gpu_runtime="active", gpu_temp_c=62.4, gpu_power_w=45.67)
                GpuPage.on_telemetry(page, active)
                page.temp_val.set_label.assert_called_with("62\u00a0°C")
                page.draw_val.set_label.assert_called_with("45.7 W")
                page.power_row.set_subtitle.assert_called_with("")
                page.power_val.set_label.assert_called_with(vm.DGPU_POWER_TEXT["active"])

                sleeping = Telemetry(gpu_runtime="suspended", gpu_temp_c=49.0, gpu_power_w=0.0)
                GpuPage.on_telemetry(page, sleeping)
                page.temp_val.set_label.assert_called_with("–")
                page.draw_val.set_label.assert_called_with("–")
                page.power_val.set_label.assert_called_with(vm.DGPU_POWER_TEXT["suspended"])

                lost = Telemetry(gpu_runtime="error", gpu_temp_c=None, gpu_power_w=None)
                GpuPage.on_telemetry(page, lost)
                page.power_row.set_subtitle.assert_called_with(vm.DGPU_LOST_TEXT)
                page.power_val.set_label.assert_called_with(vm.DGPU_POWER_TEXT["error"])

                fake_t = SimpleNamespace(gpu_runtime=None, gpu_temp_c=55.0, gpu_power_w=None)
                GpuPage.on_telemetry(page, fake_t)
                page.temp_val.set_label.assert_called_with("55\u00a0°C")
                page.draw_val.set_label.assert_called_with("– W")

    def test_cpu_page_update_handles_intel_and_amd_telemetry(self):
        def build_grid(*keys):
            g = SimpleNamespace(
                cells={k: Mock(get_label=Mock(return_value="")) for k in keys},
                captions={k: Mock() for k in keys},
                boxes={k: Mock() for k in keys},
            )
            def _grid_set(key, text, tip=None):
                StatGrid.set(g, key, text, tip)
            g.set = _grid_set
            return g

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                prefs.save_prefs(temp_unit="C")
                for vendor, static, policy, telemetry, exp_pstate, exp_tip, exp_power in (
                    (
                        "intel",
                        {
                            "model": "Intel(R) Core(TM) i7-14700HX",
                            "topology": {0: {"min_mhz": 800, "max_mhz": 5500}},
                        },
                        {
                            "governor": "powersave",
                            "epp": "balance_performance",
                            "driver": "intel_pstate",
                            "pstate_status": "active",
                            "no_turbo": "0",
                        },
                        Telemetry(cpu_temp_c=64.0, cpu_power_w=None),
                        "intel_pstate",
                        "coretemp Package thermal sensor",
                        "needs root",
                    ),
                    (
                        "amd",
                        {
                            "model": "AMD Ryzen 9 7945HX",
                            "topology": {0: {"min_mhz": 400, "max_mhz": 5400}},
                        },
                        {
                            "governor": "powersave",
                            "epp": "power",
                            "driver": "amd-pstate-epp",
                            "pstate_status": "active",
                            "no_turbo": "1",
                        },
                        Telemetry(cpu_temp_c=71.2, cpu_power_w=24.5),
                        "amd-pstate",
                        "k10temp / ACPI package sensor",
                        "24.5\u00a0W",
                    ),
                ):
                    values = {
                        "cpu.total": 32.5,
                        "cpu.freq.avg": 3200.0,
                        "temp.cpu": telemetry.cpu_temp_c,
                        "power.cpu": telemetry.cpu_power_w,
                        "cpu.ctxt": 12000.0,
                        "cpu.intr": 8500.0,
                    }
                    store = SimpleNamespace(last=values.get, last_t=10.0)
                    hub = SimpleNamespace(
                        store=store,
                        info={"cpu": {"load": (1.1, 0.8, 0.4, 2, 400), "uptime": 7200,
                                      "policy": policy}},
                    )
                    stats = build_grid("util", "freq", "temp", "power", "load", "uptime",
                                       "threads", "ctxt", "intr", "governor")
                    policy_grid = build_grid("driver", "pstate", "turbo", "range")
                    page = SimpleNamespace(
                        hub=hub,
                        static=static,
                        stats=stats,
                        policy_grid=policy_grid,
                        _pol=None,
                        rapl_note=Mock(get_label=Mock(return_value="prev")),
                        redraw=Mock(),
                    )
                    CpuPage.update(page)
                    policy_grid.captions["pstate"].set_label.assert_called_with(exp_pstate)
                    stats.boxes["temp"].set_tooltip_text.assert_called_with(exp_tip)
                    stats.cells["power"].set_label.assert_called_with(exp_power)
                    page.redraw.assert_called_once()
                    if vendor == "intel":
                        page.rapl_note.set_visible.assert_called_with(True)
                    else:
                        page.rapl_note.set_visible.assert_called_with(False)

    def test_core_grid_renders_fahrenheit_when_selected(self):
        import cairo
        store = SimpleNamespace(
            last_t=10.0,
            series={},
            last=lambda k: {"cpu.0": 42.0, "cpu.freq.0": 3500.0, "temp.core.0": 100.0}.get(k),
            index_range=lambda _t: range(0),
        )
        grid = SimpleNamespace(
            hub=SimpleNamespace(store=store, interval_s=1.0),
            cpus=[0],
            topo={0: {"kind": "P", "thread": 0, "core_id": 0}},
            window_s=60.0,
            _cols=1,
            CELL_H=74,
            GAP=8,
            _xs=[],
            _ys=[],
        )
        drawn = []
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 100)
        cr = cairo.Context(surf)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                prefs.save_prefs(temp_unit="F")
                with patch.object(ui_charts, "_text",
                                  side_effect=lambda _c, _l, txt, *_a: drawn.append(txt)):
                    ui_charts.CoreGrid._draw(grid, None, cr, 200, 100)
        self.assertIn("3.50 GHz · 212°", drawn)

    def test_window_refresh_temperature_unit_calls_page_hook_and_cpu_redraws_grid(self):
        hook_page = SimpleNamespace(is_monitor_page=True, refresh_temperature_unit=Mock(),
                                    update=Mock())
        win = SimpleNamespace(
            current="cpu",
            pages=SimpleNamespace(get_built=lambda _k: hook_page),
            telemetry=Mock(return_value="t"),
            on_telemetry=Mock(),
        )
        MainWindow.refresh_temperature_unit(win)
        hook_page.refresh_temperature_unit.assert_called_once_with()
        hook_page.update.assert_not_called()

        cpu_page = SimpleNamespace(
            temp_legend=SimpleNamespace(get_first_child=lambda: None),
            temp_chart=Mock(),
            grid=Mock(),
            stats=Mock(),
            hub=SimpleNamespace(store=SimpleNamespace(last=lambda _k: 50.0)),
        )
        CpuPage.refresh_temperature_unit(cpu_page)
        cpu_page.temp_chart.queue_draw.assert_called_once_with()
        cpu_page.grid.queue_draw.assert_called_once_with()


class Batch3AuditRegressionTests(unittest.TestCase):
    def test_window_points_clears_stale_outputs_on_empty_span(self):
        xs, ys = [1.0, 2.0], [3.0, 4.0]
        n = ui_chartmath.window_points([1.0], [1.0], range(1), lambda k: k,
                                       10.0, 10.0, 100.0, xs, ys)
        self.assertEqual((n, xs, ys), (0, [], []))

    def test_chart_set_window_resets_seen_and_hover_clamps_left_edge(self):
        import cairo
        chart = SimpleNamespace(window_s=120.0, _seen=(0.0, 50.0), queue_draw=Mock())
        ui_charts.Chart.set_window(chart, 300.0)
        self.assertEqual(chart.window_s, 300.0)
        self.assertIsNone(chart._seen)
        chart.queue_draw.assert_called_once_with()

        store = Store(10)
        store.push(0.0, {"k": 1.0})
        drawn_rects = []
        hover = SimpleNamespace(hover_x=10.0, window_s=60.0, lines=[ui_charts.Line("k", "Val")],
                                fmt=lambda v: f"{v:.0f}")
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 40, 80)
        cr = cairo.Context(surf)
        layout = ui_charts.make_layout(cr)
        with patch.object(ui_charts, "_round_rect",
                          side_effect=lambda _cr, bx, by, bw, bh, _r:
                              drawn_rects.append((bx, bw))):
            ui_charts.Chart._draw_hover(hover, cr, layout, store, 10.0, 10.0, 20.0, 60.0,
                                        40.0, 0.0, lambda _v: 40.0, ui_charts.theme.colours(), 1.0)
        self.assertTrue(drawn_rects)
        self.assertEqual(drawn_rects[0][0], 2.0)

    def test_memory_escape_returns_false_when_idle_and_true_when_clearing_pin(self):
        page = SimpleNamespace(pin_ts=None, hover_ts=None, _refresh_readout=Mock())
        self.assertFalse(MemoryPage._ram_key(page, None, Gdk.KEY_Escape, 0, 0))
        page.pin_ts = 5.0
        self.assertTrue(MemoryPage._ram_key(page, None, Gdk.KEY_Escape, 0, 0))
        self.assertIsNone(page.pin_ts)
        page._refresh_readout.assert_called_once_with()

    def test_disk_map_hover_preserves_keyboard_selection(self):
        d = SimpleNamespace(_hover=None, _kb_sel=None, _sel=None, _sel_source=None,
                            queue_draw=Mock(), _emit_select=Mock(),
                            _model=SimpleNamespace(), _view=0,
                            _sibling_order=lambda _m, _v: [1, 2, 3],
                            drill=Mock(), up=Mock(), set_tooltip_text=Mock())
        def _set_sel(idx, src):
            ui_disk_map.DiskUsageChart._set_sel(d, idx, src)

        d._set_sel = _set_sel
        d._set_sel(2, "kb")
        self.assertEqual((d._kb_sel, d._sel), (2, 2))
        d._set_sel(1, "hover")
        self.assertEqual((d._kb_sel, d._sel), (2, 1))
        ui_disk_map.DiskUsageChart._leave(d, None)
        self.assertEqual((d._kb_sel, d._sel), (2, 2))
        self.assertTrue(ui_disk_map.DiskUsageChart._key(d, None, Gdk.KEY_Return, 0, 0))
        d.drill.assert_called_once_with(2)

    def test_live_hub_collect_now_busy_guard_and_error_dedup(self):
        hub = SimpleNamespace(_closed=False, _busy=True)
        with self.assertRaises(RuntimeError):
            ui_live.LiveHub.collect_now(hub, 1.0)
        err_hub = SimpleNamespace(_busy=True, _closed=False, last_error=None, _error_count=0)
        fut = Mock()
        fut.exception.return_value = RuntimeError("boom")
        import io
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            ui_live.LiveHub._deliver(err_hub, fut)
            ui_live.LiveHub._deliver(err_hub, fut)
        self.assertEqual(buf.getvalue().count("boom"), 1)
        self.assertEqual(err_hub._error_count, 2)

    def test_window_park_state_dynamic_gpu_title_and_singular_updates_badge(self):
        protected = SimpleNamespace(can_park=lambda: False, save_state=Mock())
        restorable = SimpleNamespace(can_park=lambda: True,
                                     save_state=lambda: {"view": "usage"})
        win = SimpleNamespace(
            current="dashboard",
            _park_ids={},
            _parked_state={},
            responders=[],
            stack=Mock(),
            _schedule_trim=Mock(),
            pages=SimpleNamespace(built={"processes": protected, "memory": restorable},
                                  get_built=lambda k: {"processes": protected,
                                                       "memory": restorable}.get(k)),
        )
        MainWindow._park(win, "processes")
        self.assertNotIn("processes", win._parked_state)
        win.stack.remove.assert_not_called()

        win.pages.built = {"processes": protected, "memory": restorable}
        MainWindow._park(win, "memory")
        self.assertEqual(win._parked_state["memory"], {"view": "usage"})
        win.stack.remove.assert_called_once_with(restorable)

        badge = Mock()
        uwin = SimpleNamespace(updates_badge=badge)
        MainWindow.set_updates_count(uwin, 1)
        badge.set_tooltip_text.assert_called_with("1 update available")
        MainWindow.set_updates_count(uwin, 3)
        badge.set_tooltip_text.assert_called_with("3 updates available")

        gwin = SimpleNamespace(
            current="dgpu",
            _hw_sig=None,
            _row_titles={"igpu": Mock(get_label=lambda: "Intel"),
                         "dgpu": Mock(get_label=lambda: "AMD graphics")},
            rows={"igpu": Mock(), "dgpu": Mock()},
            title=Mock(),
            content_page=Mock(),
        )
        MainWindow._sync_sidebar_hardware(gwin, {"igpu": {"driver": "i915"},
                                                 "dgpu": {"driver": "amdgpu"}})
        gwin.title.set_title.assert_called_with("AMD graphics")
        gwin.content_page.set_title.assert_called_with("AMD graphics")


class Batch3WidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if Gdk.Display.get_default() is None:
            raise unittest.SkipTest("GTK widget tests require a display")

    def test_memory_page_save_and_restore_state(self):
        hub = SimpleNamespace(store=Store(10), interval_s=1.0, sampler=SimpleNamespace(), info={})
        w = SimpleNamespace(hub=hub, ctl=None, caps=None, caps_error=None, fake=True)
        p1 = MemoryPage(w)
        p1.view_btns["usage"].set_active(True)
        p1.range_btns[300].set_active(True)
        p1.comp_toggle.set_active(True)
        p1.det_toggle.set_active(True)
        saved = p1.save_state()

        p2 = MemoryPage(w)
        p2.restore_state(saved)
        self.assertEqual(p2.view, "usage")
        self.assertEqual(p2.range_s, 300)
        self.assertTrue(p2.comp_toggle.get_active())
        self.assertTrue(p2.det_toggle.get_active())

    def test_history_chart_keyboard_navigation_and_tooltip(self):
        hc = ui_history_chart.HistoryChart()
        self.assertFalse(hc.get_focusable())
        hc.set_data(1000.0, 600.0,
                    {"percent": [(800.0, 50.0), (900.0, 55.0), (1000.0, 60.0)]}, [])
        self.assertTrue(hc.get_focusable())
        self.assertTrue(hc._on_key(None, Gdk.KEY_Home, 0, 0))
        self.assertEqual(hc._kb_t, 800.0)
        self.assertTrue(hc._on_key(None, Gdk.KEY_Right, 0, 0))
        self.assertEqual(hc._kb_t, 900.0)
        self.assertTrue(hc._on_key(None, Gdk.KEY_End, 0, 0))
        self.assertEqual(hc._kb_t, 1000.0)
        tip = Mock()
        self.assertTrue(hc._query_tooltip(hc, 0, 0, True, tip))
        self.assertIn("Charge · 60%", tip.set_text.call_args.args[0])
        self.assertTrue(hc._on_key(None, Gdk.KEY_Escape, 0, 0))
        self.assertIsNone(hc._kb_t)
        self.assertFalse(hc._on_key(None, Gdk.KEY_Escape, 0, 0))
        hc.set_empty_text("None")
        self.assertFalse(hc.get_focusable())


if __name__ == "__main__":
    unittest.main()
