# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Public compatibility facade for display-free UI viewmodels.

Implementations live in focused domain modules; existing imports remain valid.
"""
from .viewmodels_formatting import (
    DASH,
    OWN_APP_ID,
    OWN_APP_KEYS,
    _NB_RE,
    column_totals,
    desktop_index,
    fmt_num,
    fmt_rpm,
    fmt_temp,
    humanize_error,
    nb,
    norm_app_key,
    signal_refusal,
    temp_unit_label,
    temp_value,
    to_display_temp,
)
from .viewmodels_profiles_history import (
    History,
    PROFILE_INFO,
    PROFILE_ORDER,
    battery_icon,
    power_state_text,
    smooth_path,
    smooth_values,
)
from .viewmodels_power_limits import (
    LIMIT_INFO,
    LimitRow,
    PowerLimitsView,
    explain_unavailable,
    power_limits_view,
    snap_to_step,
)
from .viewmodels_fan_curves import (
    CurveGeometry,
    FanCurveModel,
    bubble_rect,
    pct_to_pwm,
    pwm_to_pct,
)
from .viewmodels_battery import (
    CHARGE_PRESETS,
    CHARGE_RANGE,
    charge_limit_hint,
    health_label,
    validate_charge_limit,
)
from .viewmodels_gpu import (
    DGPU_LOST_TEXT,
    DGPU_POWER_TEXT,
    GPU_MODE_INFO,
    GPU_UI_ORDER,
    gpu_action_text,
    gpu_switch_requirement,
)
from .viewmodels_keyboard import (
    AURA_MODES,
    ANIMATED_MODES,
    BREATHE_PERIODS,
    BRIGHTNESS_NAMES,
    DEFAULT_COLOUR2,
    DIRECTIONS,
    FrameGate,
    GRADIENT_DIP,
    KbdControls,
    KbdForm,
    KbdPlan,
    MODE_USES_COLOUR1,
    MODE_USES_COLOUR2,
    MODE_USES_DIRECTION,
    MODE_USES_SPEED,
    PALETTES,
    SPEEDS,
    TUF_DEVICE_TYPE,
    _RAW_SPEEDS,
    _SUMMARIES,
    _period_speed,
    effect_from_rgb_data,
    gradient_params,
    hardware_two_colour,
    hex_to_rgb,
    is_animated,
    kbd_controls,
    preview_colour,
    rgb_to_hex,
    speed_index,
)
from .viewmodels_display import (
    DisplayMode,
    Monitor,
    build_apply_config,
    parse_display_state,
)
from .viewmodels_hardware import (
    FAN_PRESETS,
    apply_fan_preset,
    cpu_vendor_info,
    dashboard_readiness,
    fan_preset_curve,
    gpu_vendor_title,
)
