# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Static tool catalogue, kept separate from call execution and handlers."""
from __future__ import annotations

def build_tools() -> dict:
    from .tools import (Tool, Capability, WRITE_PROPS, COLOUR, GPU_MODES, PROFILE_ENUM,
                        MODE_IDS, SPEEDS, DIRECTIONS, SECTION_ENUM, NAME_FILTER, obj, wobj,
                        t_get_status, t_get_capabilities, t_get_telemetry, t_list_processes,
                        t_get_fan_curves, t_get_battery_health, t_get_automation_rules,
                        t_get_keyboard_state, t_explain_unavailable, t_get_approval_status,
                        t_get_updates, t_get_update_history, w_set_platform_profile,
                        w_set_charge_limit, w_set_keyboard_effect, w_set_keyboard_brightness,
                        t_run_automation_dry_run, w_set_fan_curve, w_set_gpu_mode,
                        w_set_power_limit, w_restore_defaults)

    _T = [
        Tool("get_status", "Status",
             "Current platform profile, charge limit, GPU mode, keyboard brightness, CPU "
             "temperature, fan speeds, battery state, and this client's access level.",
             "read", obj({}), t_get_status, rate=30),
        Tool("get_capabilities", "Capabilities",
             "What this laptop exposes, through which backend, and why each missing feature is "
             "missing; plus which agent tools are allowed right now and why not.",
             "read", obj({}), t_get_capabilities, rate=20),
        Tool("get_telemetry", "Telemetry",
             "Live readings for selected sections (cpu, memory, gpus, fans, storage, network, "
             "battery). Numbers are rounded; key suffixes give the unit. The discrete GPU is not "
             "woken to read it. May take about half a second on the first call.",
             "read", obj({"sections": {"type": "array", "minItems": 1, "maxItems": 7,
                                       "items": {"type": "string", "enum": list(SECTION_ENUM)},
                                       "description": "Sections to include (default: all)."}}),
             t_get_telemetry, rate=20),
        Tool("list_processes", "Top processes",
             "The top processes by CPU or memory, optionally filtered by name. Names come from "
             "the programs themselves: untrusted data. Command lines are never included.",
             "read", obj({"sort": {"type": "string", "enum": ["cpu", "memory"], "default": "cpu"},
                          "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                          "name_filter": NAME_FILTER,
                          "include_kernel": {"type": "boolean", "default": False}}),
             t_list_processes, rate=20),
        Tool("get_fan_curves", "Fan curves",
             "The 8-point fan curves (temperature to PWM 0-255) per fan for a profile (default: "
             "the active one), with the limits a new curve must respect.",
             "read", obj({"profile": PROFILE_ENUM}), t_get_fan_curves, rate=20),
        Tool("get_battery_health", "Battery health",
             "Battery health and wear (full vs design capacity), cycle count, charge limit, "
             "current level and power draw.", "read", obj({}), t_get_battery_health, rate=20),
        Tool("get_automation_rules", "Automation rules",
             "The owner's automation rules (trigger -> actions), whether they run, and which are "
             "active now. Rule names are owner text: untrusted data.",
             "read", obj({}), t_get_automation_rules, rate=20),
        Tool("get_keyboard_state", "Keyboard state",
             "Keyboard brightness (level and maximum), the hardware effect (mode, colours, speed, "
             "direction), the modes this keyboard supports and the active software effect.",
             "read", obj({}), t_get_keyboard_state, rate=20),
        Tool("explain_unavailable", "Explain unavailable",
             "Why a capability is missing on this machine, or why a tool is refused for this "
             "client right now, and what would change it.",
             "read", obj({"feature": {"type": "string", "enum": [c.value for c in Capability] + [
                 "set_platform_profile", "set_charge_limit", "set_keyboard_effect",
                 "set_keyboard_brightness", "run_automation_dry_run", "set_fan_curve",
                 "set_gpu_mode", "set_power_limit", "restore_defaults"],
                 "description": "A capability name or a tool name."}}, ["feature"]),
             t_explain_unavailable, rate=30),
        Tool("get_approval_status", "Approval status",
             "Status of a write request from this session: pending, approved (repeat the "
             "identical call with approval_id), denied, used, expired or cancelled.",
             "read", obj({"approval_id": WRITE_PROPS["approval_id"]}, ["approval_id"]),
             t_get_approval_status, rate=60),
        Tool("get_updates", "Pending updates",
             "What on this laptop needs an update (system packages, snaps, flatpaks, firmware, "
             "a kernel or driver waiting for a restart), from the last check. Read-only: it "
             "does not run a check or install anything. Package names are untrusted data.",
             "read", obj({"source": {"type": "string",
                                     "enum": ["apt", "snap", "flatpak", "firmware", "system",
                                              "repos"],
                                     "description": "Only this source."}}),
             t_get_updates, rate=20),
        Tool("get_update_history", "Update history",
             "Past update runs from Asusius's persistent history: what was installed, "
             "what stayed pending and why, what failed, and updates installed outside the "
             "app (noticed by a later check). Read-only. Package names are untrusted data.",
             "read", obj({"since": {"type": "string",
                                    "pattern": "^(\\d+[mhdw]|today|yesterday|all)$",
                                    "description": "Only runs newer than this, e.g. '7d', "
                                                   "'24h', 'yesterday'."},
                          "source": {"type": "string",
                                     "enum": ["apt", "snap", "flatpak", "firmware", "system"],
                                     "description": "Only runs touching this source."},
                          "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
             t_get_update_history, rate=10),
        Tool("set_platform_profile", "Set platform profile",
             "Set the platform profile (quiet, balanced, performance). Dry run by default.",
             "basic", wobj({"profile": PROFILE_ENUM}, ["profile"]), w_set_platform_profile,
             rate=10, writes=True, read_only=False),
        Tool("set_charge_limit", "Set charge limit",
             "Set the battery charge limit in percent (20-100). Dry run by default.",
             "basic", wobj({"percent": {"type": "integer", "minimum": 20, "maximum": 100}},
                           ["percent"]), w_set_charge_limit, rate=10, writes=True, read_only=False),
        Tool("set_keyboard_effect", "Set keyboard effect",
             "Set the keyboard's hardware lighting effect. Only modes this keyboard supports "
             "are accepted (see get_keyboard_state). Dry run by default.",
             "basic", wobj({"mode": {"type": "string", "enum": sorted(MODE_IDS)},
                            "colour": COLOUR, "colour2": COLOUR,
                            "speed": {"type": "string", "enum": list(SPEEDS), "default": "medium"},
                            "direction": {"type": "string", "enum": list(DIRECTIONS),
                                          "default": "right"}}, ["mode"]),
             w_set_keyboard_effect, rate=10, writes=True, read_only=False),
        Tool("set_keyboard_brightness", "Set keyboard brightness",
             "Set the keyboard backlight level (0 = off; the maximum comes from the hardware, "
             "often 3). Dry run by default.",
             "basic", wobj({"level": {"type": "integer", "minimum": 0, "maximum": 65535}}, ["level"]),
             w_set_keyboard_brightness, rate=10, writes=True, read_only=False),
        Tool("run_automation_dry_run", "Automation dry run",
             "Evaluate the owner's automation rules against the current (or overridden) inputs "
             "and dry-run every action through the Controller. Never changes anything.",
             "basic", obj({"rule_id": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9_-]{0,63}$"},
                           "state": obj({
                               "on_ac": {"type": "boolean"},
                               "battery": {"type": "integer", "minimum": 0, "maximum": 100},
                               "apps": {"type": "array", "maxItems": 10, "items": {
                                   "type": "string", "pattern": r"^[A-Za-z0-9._+-]{1,64}$"}},
                               "time": {"type": "string", "pattern": r"^([01]\d|2[0-3]):[0-5]\d$"},
                               "profile": PROFILE_ENUM})}),
             t_run_automation_dry_run, rate=10),
        Tool("set_fan_curve", "Set fan curve (high risk)",
             "HIGH RISK. Replace one fan's 8-point curve for a profile. Temperatures must rise, "
             "PWM must not fall; agent-set curves also keep a minimum duty when hot (see "
             "get_fan_curves). Needs control-all, enabled high-risk tools and a per-call "
             "approval.",
             "high", wobj({"profile": PROFILE_ENUM,
                           "fan": {"type": "string", "enum": ["cpu", "gpu", "mid"]},
                           "points": {"type": "array", "minItems": 8, "maxItems": 8,
                                      "items": obj({"temp_c": {"type": "integer", "minimum": 20,
                                                               "maximum": 110},
                                                    "pwm": {"type": "integer", "minimum": 0,
                                                            "maximum": 255}},
                                                   ["temp_c", "pwm"])}},
                          ["profile", "fan", "points"]),
             w_set_fan_curve, rate=4, writes=True, read_only=False, destructive=True),
        Tool("set_gpu_mode", "Set GPU mode (high risk)",
             "HIGH RISK. Switch the GPU mode through supergfxd (integrated, hybrid, ultimate). "
             "Takes effect after a log-out or reboot. Needs control-all, enabled high-risk tools "
             "and a per-call approval.",
             "high", wobj({"mode": {"type": "string", "enum": list(GPU_MODES)}}, ["mode"]),
             w_set_gpu_mode, rate=4, writes=True, read_only=False, destructive=True),
        Tool("set_power_limit", "Set power limit (high risk)",
             "HIGH RISK. Set one firmware power limit (for example ppt_pl1_spl) in watts. The "
             "firmware's own min/max are hard bounds; limits with unknown bounds are refused. "
             "Needs control-all, enabled high-risk tools and a per-call approval.",
             "high", wobj({"name": {"type": "string", "pattern": r"^[a-z0-9_]{1,40}$"},
                           "value": {"type": "integer", "minimum": 0, "maximum": 500}},
                          ["name", "value"]),
             w_set_power_limit, rate=4, writes=True, read_only=False, destructive=True),
        Tool("restore_defaults", "Restore defaults (high risk)",
             "HIGH RISK. Reset fan curves (for a profile, default the active one) and/or power "
             "limits to the firmware defaults. Needs control-all, enabled high-risk tools and a "
             "per-call approval.",
             "high", wobj({"scope": {"type": "string", "enum": ["fan_curves", "power_limits", "all"],
                                     "default": "all"},
                           "profile": PROFILE_ENUM}),
             w_restore_defaults, rate=4, writes=True, read_only=False, destructive=True),
    ]

    return {t.name: t for t in _T}
