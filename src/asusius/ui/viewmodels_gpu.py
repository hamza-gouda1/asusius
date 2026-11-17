# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""GPU mode descriptions and transition guidance."""

from __future__ import annotations

from ..core import (
    GpuAction,
    GpuMode,
)

# -- gpu ------------------------------------------------------------------------------------

GPU_MODE_INFO: dict[GpuMode, tuple[str, str]] = {
    GpuMode.INTEGRATED: ("Integrated", "dGPU powered off. Longest battery life"),
    GpuMode.HYBRID: ("Hybrid", "dGPU wakes on demand. Recommended"),
    GpuMode.ASUS_MUX_DGPU: ("Ultimate", "dGPU drives the panel through the MUX. Lowest latency"),
    GpuMode.NVIDIA_NO_MODESET: ("NVIDIA no modeset", "Legacy mode"),
    GpuMode.VFIO: ("VFIO", "dGPU passed through to a VM"),
    GpuMode.ASUS_EGPU: ("eGPU", "External GPU"),
    GpuMode.NONE: ("Unknown", ""),
}
GPU_UI_ORDER = [GpuMode.INTEGRATED, GpuMode.HYBRID, GpuMode.ASUS_MUX_DGPU]


def gpu_switch_requirement(current: GpuMode, target: GpuMode) -> str | None:
    """What the user should expect before confirming: 'reboot', 'logout' or None."""
    if current is target:
        return None
    if GpuMode.ASUS_MUX_DGPU in (current, target):
        return "reboot"
    return "logout"


def gpu_action_text(action: GpuAction | None, pending: GpuMode | None = None) -> str | None:
    target = f" to switch to {GPU_MODE_INFO[pending][0]}" if pending is not None else ""
    return (
        {
            GpuAction.REBOOT: f"Restart{target}",
            GpuAction.LOGOUT: f"Log out{target}",
            GpuAction.SWITCH_TO_INTEGRATED: "Switch to Integrated first",
            GpuAction.ASUS_EGPU_DISABLE: "Disconnect the eGPU first",
        }.get(action)
        if action is not None
        else None
    )


DGPU_LOST_TEXT = (
    "GPU unavailable: the NVIDIA driver lost the GPU (runtime power error). "
    "A restart usually recovers it."
)
DGPU_POWER_TEXT = {
    "error": "Unavailable (lost by the driver)",
    "active": "Active",
    "suspended": "Sleeping",
    "off": "Powered off",
    "disabled": "Disabled in firmware",
    "mux-discreet": "Driving the display",
    "suspending": "Going to sleep",
    "resuming": "Waking up",
}
