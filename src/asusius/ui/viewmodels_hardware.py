# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Hardware presets and device readiness presentation helpers."""

from __future__ import annotations


from .viewmodels_fan_curves import FanCurveModel
from ..core import (
    Capability,
    FanCurve,
    FanPoint,
)

# -- fan curve presets & hardware/readiness helpers --------------------------------------------

FAN_PRESETS: dict[str, list[tuple[int, int]]] = {
    "Silent": [(30, 0), (45, 0), (55, 25), (65, 51), (75, 89), (85, 133), (95, 178), (105, 217)],
    "Balanced": [
        (30, 15),
        (45, 31),
        (55, 64),
        (65, 102),
        (72, 140),
        (80, 178),
        (88, 217),
        (96, 255),
    ],
    "Cool": [(25, 26), (40, 51), (50, 90), (60, 133), (68, 178), (76, 217), (84, 242), (92, 255)],
    "Turbo": [(25, 51), (38, 89), (48, 128), (58, 178), (66, 217), (74, 242), (82, 255), (90, 255)],
}


def fan_preset_curve(name: str, fan: str = "CPU", enabled: bool = True) -> FanCurve:
    pts = FAN_PRESETS[name]
    return FanCurve(fan, [FanPoint(t, p) for t, p in pts], enabled=enabled).validate()


def apply_fan_preset(model: FanCurveModel, name: str) -> None:
    """Populate a FanCurveModel from a named preset while retaining clean revert history."""
    curve = fan_preset_curve(name, fan=model.fan, enabled=True)
    model.points = [(p.temp, p.pwm) for p in curve.points]
    model.enabled = True


def cpu_vendor_info(static: dict | None, policy: dict | None) -> dict[str, str]:
    """Vendor-neutral CPU presentation labels (handles Intel, AMD Ryzen and generic CPUs)."""
    model = ((static or {}).get("model") or "").lower()
    driver = ((policy or {}).get("driver") or "").lower()
    is_amd = "amd" in driver or "amd" in model or "ryzen" in model or "epyc" in model
    if is_amd:
        return {
            "vendor": "amd",
            "pstate_title": "amd-pstate" if "amd" in driver else "P-state status",
            "temp_tip": "k10temp / ACPI package sensor",
            "rapl_note": (
                "Package power is unavailable: the kernel restricts energy counters "
                "to root or this AMD CPU does not expose powercap / hwmon energy."
            ),
        }
    return {
        "vendor": "intel" if ("intel" in driver or "intel" in model) else "generic",
        "pstate_title": "intel_pstate" if "intel" in driver else "P-state status",
        "temp_tip": "coretemp Package thermal sensor",
        "rapl_note": (
            "Package power is not measured: the kernel restricts energy counters "
            "to root or this CPU does not expose /sys/class/powercap."
        ),
    }


def gpu_vendor_title(info: dict | None, kind: str) -> str:
    """Clean GPU sidebar/heading name adapting across Intel, AMD and NVIDIA."""
    data = info or {}
    name = (data.get("name") or "").lower()
    driver = (data.get("driver") or "").lower()
    if kind == "igpu":
        if "amd" in driver or "radeon" in name or "amd" in name:
            return "AMD graphics"
        return "Intel graphics"
    if ("amd" in driver or "amdgpu" in driver or "radeon" in name) and "nvidia" not in name:
        return "AMD graphics"
    if "intel" in name and "nvidia" not in name:
        return "Intel Arc graphics"
    return "NVIDIA graphics"


def dashboard_readiness(
    caps: dict | None, caps_error: str | None, svc_status: dict | None, fake: bool = False
) -> dict | None:
    """Return a gentle first-run/diagnostic banner spec when setup needs attention."""
    if caps_error:
        return {
            "id": "caps-error",
            "icon": "dialog-warning-symbolic",
            "title": "Could not probe all hardware capabilities",
            "body": f"{caps_error}. Live telemetry continues; retry detection when ready.",
            "action": "retry_caps",
            "action_label": "Retry detection",
        }
    if fake or caps is None:
        return None
    hw_keys = (
        Capability.PROFILE.value,
        Capability.FAN_CURVES.value,
        Capability.KBD_RGB.value,
        Capability.KBD_BRIGHTNESS.value,
    )
    if caps and not any((caps.get(k) or {}).get("available") for k in hw_keys):
        return {
            "id": "no-asusd",
            "icon": "dialog-information-symbolic",
            "title": "ASUS hardware controls unavailable",
            "body": (
                "No ASUS firmware features responded (check that asusd is running if "
                "you are on a supported ASUS laptop). Monitoring, display, and updates "
                "still work normally."
            ),
            "action": "open_about",
            "action_label": "View support matrix",
        }
    if svc_status is not None and not svc_status.get("running", True):
        return {
            "id": "daemon-stopped",
            "icon": "system-run-symbolic",
            "title": "Background service is not running",
            "body": (
                "Start asusiusd for automation rules, software lighting gradients, "
                "and background update notifications."
            ),
            "action": "start_daemon",
            "action_label": "Start service",
        }
    return None
