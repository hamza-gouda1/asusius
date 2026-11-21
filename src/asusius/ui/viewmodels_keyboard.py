# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Keyboard lighting controls, forms, and animation helpers."""

from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass

from ..core import (
    KbdEffect,
    ValidationError,
)

# -- keyboard -----------------------------------------------------------------------------------

try:
    from ..core.models import AURA_MODES  # canonical table when available in core
except ImportError:
    AURA_MODES: dict[int, str] = {
        0: "Static",
        1: "Breathe",
        2: "Colour cycle",
        3: "Rainbow wave",
        4: "Star",
        5: "Rain",
        6: "Highlight",
        7: "Laser",
        8: "Ripple",
        10: "Pulse",
        11: "Comet",
        12: "Flash",
    }
# which inputs each mode takes, as asusctl 6.3 builds them (asusctl/src/aura_cli.rs:
# SingleColour, TwoColourSpeed, SingleSpeed, SingleSpeedDirection, StarsTwoColour, ...)
MODE_USES_COLOUR2 = {1, 4}
MODE_USES_COLOUR1 = {0, 1, 4, 6, 7, 8, 10, 11, 12}
MODE_USES_SPEED = {1, 2, 3, 4, 5, 6, 7, 8}
MODE_USES_DIRECTION = {3}
# rog-aura AuraDeviceType::LaptopKeyboardTuf (asusd `DeviceType` 2). asusd drives it through
# the kernel's kbd_rgb_mode ("cmd mode red green blue speed") and writes
# [1, mode, colour1.r, colour1.g, colour1.b, speed]: colour2 is stored and read back over
# D-Bus but never reaches the keys, so its firmware Breathe is Colour 1 <-> off.
TUF_DEVICE_TYPE = 2
SPEEDS = [("Slow", 0), ("Medium", 1), ("Fast", 2)]
DIRECTIONS = [("Right", 0), ("Left", 1), ("Up", 2), ("Down", 3)]
BRIGHTNESS_NAMES = ["Off", "Low", "Med", "High"]
_RAW_SPEEDS = {0xE1: 0, 0xEB: 1, 0xF5: 2}


def speed_index(raw: int) -> int:
    """asusd reports speed as 0-2 or as the raw HID byte; map both to 0-2."""
    return _RAW_SPEEDS.get(raw, raw if raw in (0, 1, 2) else 1)


def rgb_to_hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(int(c) for c in rgb))


def hex_to_rgb(text: str) -> tuple[int, int, int]:
    t = text.strip().lstrip("#")
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    if len(t) != 6:
        raise ValidationError(f"bad colour {text!r}")
    try:
        return tuple(int(t[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        raise ValidationError(f"bad colour {text!r}") from None


def effect_from_rgb_data(data: dict | None) -> KbdEffect:
    if not data:
        return KbdEffect(0)
    return KbdEffect(
        int(data.get("mode", 0)),
        int(data.get("zone", 0)),
        tuple(data.get("colour1", (142, 42, 68))),
        tuple(data.get("colour2", (0, 0, 0))),
        int(data.get("speed", 1)),
        int(data.get("direction", 0)),
    )


def hardware_two_colour(device_type) -> bool:
    """Whether the keyboard's own firmware shows colour2 (False on TUF; unknown: True)."""
    return device_type is None or int(device_type) != TUF_DEVICE_TYPE


# The software Gradient breathing cycle (A -> B -> A) for each hardware speed.
BREATHE_PERIODS = (6.0, 4.0, 2.5)
GRADIENT_DIP = 0.3  # default darkening at the crossing (matches the effect's default)
DEFAULT_COLOUR2 = (201, 174, 134)  # gold: Breathe never starts as "Colour 1 <-> black"

# quick two-colour palettes: (name, colour 1, colour 2)
PALETTES: list[tuple[str, tuple[int, int, int], tuple[int, int, int]]] = [
    ("Wine & Gold", (142, 42, 68), (201, 174, 134)),
    ("Wine & Ivory", (142, 42, 68), (242, 232, 218)),
    ("Ice", (47, 111, 223), (191, 230, 255)),
    ("Sunset", (255, 94, 58), (122, 28, 52)),
    ("Ocean", (11, 79, 138), (34, 193, 195)),
    ("Neon", (255, 43, 214), (0, 229, 255)),
]


@dataclass(frozen=True)
class KbdControls:
    """Which rows the hardware-effect form shows for a mode, and what the mode does."""

    colour1: bool
    colour2: bool
    speed: bool
    direction: bool
    blend: bool  # offer the "Two colours" switch (single-colour firmware Breathe)
    palettes: bool
    colour1_title: str
    summary: str
    breathe: str  # preview style: alternate | single | blend


_SUMMARIES = {
    0: "Static: one steady colour.",
    2: "Colour cycle: walks round the colour wheel. Speed only.",
    3: "Rainbow wave: a rainbow moving across the keys.",
    4: "Stars: Colour 1 and Colour 2 twinkle.",
    5: "Rain: colours fall down the keys.",
    6: "Highlight: the colour follows the keys.",
    7: "Laser: the colour sweeps across.",
    8: "Ripple: the colour ripples out.",
    10: "Pulse: the colour pulses.",
    11: "Comet: the colour streaks across.",
    12: "Flash: the colour blinks.",
}


def kbd_controls(mode: int, device_type=None, blend: bool = False) -> KbdControls:
    hw2 = hardware_two_colour(device_type)
    offer_blend = mode == 1 and not hw2
    two = mode in MODE_USES_COLOUR2 and (hw2 or (offer_blend and blend))
    if mode == 1 and hw2:
        summary, style = "Breathe: Colour 1 \u2194 Colour 2.", "alternate"
    elif mode == 1 and two:
        summary, style = "Breathe: Colour 1 \u2194 Colour 2, a smooth fade.", "blend"
    elif mode == 1:
        summary, style = (
            ("Breathe: colour \u2194 off. This keyboard's firmware has one colour slot."),
            "single",
        )
    else:
        summary, style = _SUMMARIES.get(mode, AURA_MODES.get(mode, "Effect")), "alternate"
        if mode == 4 and not hw2:
            summary = "Stars: the colour twinkles (this keyboard's firmware has one colour slot)."
    return KbdControls(
        colour1=mode in MODE_USES_COLOUR1,
        colour2=two,
        speed=mode in MODE_USES_SPEED,
        direction=mode in MODE_USES_DIRECTION,
        blend=offer_blend,
        palettes=two,
        colour1_title="Colour 1" if two else "Colour",
        summary=summary,
        breathe=style,
    )


def gradient_params(effect: KbdEffect, dip: float = GRADIENT_DIP) -> dict:
    """Gradient breathing parameters for a two-colour Breathe."""
    return {
        "colour1": rgb_to_hex(effect.colour1).lower(),
        "colour2": rgb_to_hex(effect.colour2).lower(),
        "period": BREATHE_PERIODS[speed_index(effect.speed)],
        "dip": dip,
    }


def _period_speed(period) -> int:
    try:
        p = float(period)
    except (TypeError, ValueError):
        return 1
    return min(range(3), key=lambda i: abs(BREATHE_PERIODS[i] - p))


@dataclass(frozen=True)
class KbdPlan:
    hardware: KbdEffect
    software: dict | None  # {"active": "gradient", "params": {...}} or None (effects off)


class KbdForm:
    """The hardware-effect form without GTK: per-mode drafts so switching modes keeps the
    colours you picked, mode-dependent controls, the dirty flag and what Apply writes."""

    def __init__(self) -> None:
        self.saved: KbdEffect | None = None
        self.current = KbdEffect(0)
        self.builtins: dict[int, KbdEffect] = {}
        self.drafts: dict[int, KbdEffect] = {}
        self.modes: list[int] = [0]
        self.device_type = None
        self.blend = self.saved_blend = False
        self.raw_speed = False

    def load(self, info: dict, soft: dict | None = None) -> None:
        """info: Controller.get_kbd_rgb(); soft: the config's effects section."""
        saved = effect_from_rgb_data(info.get("mode_data"))
        self.device_type = info.get("device_type")
        # asusd keeps colours/speed per mode: switching the picker shows that mode's own
        self.builtins = {
            int(m): effect_from_rgb_data(d) for m, d in (info.get("builtins") or {}).items()
        }
        self.builtins[saved.mode] = saved
        modes = [int(m) for m in (info.get("supported_modes") or [saved.mode])]
        self.modes = [m for m in modes if m in AURA_MODES] or [saved.mode]
        self.blend = False
        fx = soft or {}
        g = (fx.get("params") or {}).get("gradient") or {}
        if (
            fx.get("active") == "gradient"
            and 1 in self.modes
            and not hardware_two_colour(self.device_type)
        ):
            # the keyboard shows Breathe's two-colour fade (frames are Static writes)
            base = self.builtins.get(1, KbdEffect(1))
            try:
                c1 = hex_to_rgb(g.get("colour1", "#8e2a44"))
                c2 = hex_to_rgb(g.get("colour2", "#c9ae86"))
            except ValidationError:
                c1, c2 = base.colour1, base.colour2
            speed = _period_speed(g.get("period", 4.0))
            if base.speed not in (0, 1, 2):
                speed = (0xE1, 0xEB, 0xF5)[speed]
            saved = KbdEffect(1, base.zone, c1, c2, speed, base.direction)
            self.builtins[1] = saved
            self.blend = True
        self.saved_blend = self.blend
        self.saved = saved
        self.current = saved
        self.drafts = {}
        self.raw_speed = saved.speed not in (0, 1, 2)

    @property
    def controls(self) -> KbdControls:
        return kbd_controls(self.current.mode, self.device_type, self.blend)

    @property
    def dirty(self) -> bool:
        return self.saved is not None and (
            self.current != self.saved or self.blend != self.saved_blend
        )

    def _fill_colour2(self, eff: KbdEffect) -> KbdEffect:
        if eff.mode in MODE_USES_COLOUR2 and tuple(eff.colour2) == (0, 0, 0):
            c2 = DEFAULT_COLOUR2 if tuple(eff.colour1) != DEFAULT_COLOUR2 else (242, 232, 218)
            return KbdEffect(eff.mode, eff.zone, eff.colour1, c2, eff.speed, eff.direction)
        return eff

    def select(self, mode: int) -> KbdEffect:
        if mode == self.current.mode:
            return self.current
        self.drafts[self.current.mode] = self.current
        new = self.drafts.get(mode) or self.builtins.get(mode)
        if new is None:  # never set: carry the colours over
            c = self.current
            new = KbdEffect(mode, c.zone, c.colour1, c.colour2, c.speed, c.direction)
        self.current = self._fill_colour2(new) if mode != (self.saved and self.saved.mode) else new
        return self.current

    def edit(
        self, colour1=None, colour2=None, speed_idx: int | None = None, direction: int | None = None
    ) -> KbdEffect:
        c = self.current
        speed = c.speed
        if speed_idx is not None:
            speed = (0xE1, 0xEB, 0xF5)[speed_idx] if self.raw_speed else speed_idx
        self.current = KbdEffect(
            c.mode,
            c.zone,
            tuple(colour1) if colour1 is not None else c.colour1,
            tuple(colour2) if colour2 is not None else c.colour2,
            speed,
            c.direction if direction is None else direction,
        )
        return self.current

    def palette(self, index: int) -> KbdEffect:
        _name, a, b = PALETTES[index]
        return self.edit(colour1=a, colour2=b)

    def set_blend(self, on: bool) -> KbdEffect:
        self.blend = bool(on)
        if on:
            self.current = self._fill_colour2(self.current)
        return self.current

    def plan(self, dip: float = GRADIENT_DIP) -> KbdPlan:
        eff = self.current
        if self.controls.blend and self.blend:
            return KbdPlan(eff, {"active": "gradient", "params": gradient_params(eff, dip)})
        return KbdPlan(eff, None)

    def applied(self, plan: KbdPlan) -> None:
        self.saved = self.current = plan.hardware
        self.saved_blend = self.blend
        self.builtins[plan.hardware.mode] = plan.hardware
        self.drafts.pop(plan.hardware.mode, None)


def preview_colour(
    effect: KbdEffect, t: float, x: float = 0.0, brightness: int = 3, breathe: str = "alternate"
) -> tuple[float, float, float]:
    """Approximate colour (0..1 floats) of the keyboard at time t and position x in [0,1].

    breathe: "alternate" fades Colour 1 -> off -> Colour 2 -> off (two-colour firmware),
    "single" Colour 1 <-> off (TUF firmware), "blend" the software Gradient breathing."""
    period = (4.0, 2.5, 1.4)[speed_index(effect.speed)]
    c1 = tuple(c / 255 for c in effect.colour1)
    c2 = tuple(c / 255 for c in effect.colour2)
    m = effect.mode
    if m in (2, 3):  # colour cycle / rainbow wave
        sign = -1 if effect.direction == 1 else 1
        hue = (t / period * 0.25 + (sign * x * 0.6 if m == 3 else 0.0)) % 1.0
        col = colorsys.hsv_to_rgb(hue, 0.85, 1.0)
    elif m == 1 and breathe == "blend":
        from ..effects.plugins import two_colour_breathe

        rgb = two_colour_breathe(
            effect.colour1,
            effect.colour2,
            t,
            BREATHE_PERIODS[speed_index(effect.speed)],
            GRADIENT_DIP,
        )
        col = tuple(c / 255 for c in rgb)
    elif m == 1:  # breathe: fade c1 -> off -> c2 -> off (or c1 <-> off)
        phase = (t / period) % 2.0
        k = 0.5 - 0.5 * math.cos(math.pi * 2 * (phase % 1.0))
        base = c1 if phase < 1.0 or breathe == "single" else (c2 if any(c2) else c1)
        col = tuple(c * k for c in base)
    elif m == 10:  # pulse
        k = 0.35 + 0.65 * (0.5 - 0.5 * math.cos(math.pi * 2 * t / period))
        col = tuple(c * k for c in c1)
    elif m == 12:  # flash
        col = c1 if (t / period * 2) % 1.0 < 0.5 else (0.0, 0.0, 0.0)
    else:
        col = c1
    level = (0.0, 0.45, 0.72, 1.0)[max(0, min(3, brightness))]
    return tuple(c * level for c in col)  # type: ignore[return-value]


ANIMATED_MODES = frozenset({1, 2, 3, 10, 12})  # breathe, cycle, rainbow, pulse, flash


def is_animated(effect: KbdEffect) -> bool:
    """False when preview_colour does not depend on time (it only needs drawing once)."""
    return effect.mode in ANIMATED_MODES


class FrameGate:
    """Frame limiter and dirty flag for a live preview (no GTK, so it can be tested).

    Runs at `fast_fps` (15), drops to `slow_fps` (10) when a frame costs more than
    `slow_cost_s` on average, and backs off to `idle_hz` once `settle_frames` frames in a row
    produced nothing new. `observe(key, cost)` is the dirty flag: it returns True only when
    `key` differs from the last one, and any change wakes the gate back to full speed.
    """

    def __init__(
        self,
        fast_fps: float = 15.0,
        slow_fps: float = 10.0,
        idle_hz: float = 4.0,
        settle_frames: int = 30,
        slow_cost_s: float = 0.020,
    ) -> None:
        self.fast, self.slow, self.idle = 1 / fast_fps, 1 / slow_fps, 1 / idle_hz
        self.settle_frames, self.slow_cost = settle_frames, slow_cost_s
        self.key: object = None
        self.unchanged = 0
        self.cost = 0.0
        self.last = -1e9

    @property
    def interval(self) -> float:
        if self.unchanged >= self.settle_frames:
            return self.idle
        return self.slow if self.cost > self.slow_cost else self.fast

    def due(self, now: float) -> bool:
        """True at most once per `interval`; the caller then does the frame."""
        if now - self.last >= self.interval - 0.002:  # 2 ms of timer jitter
            self.last = now
            return True
        return False

    def add_cost(self, cost: float) -> None:
        self.cost = cost if self.cost == 0.0 else self.cost * 0.8 + cost * 0.2

    def observe(self, key, cost: float = 0.0) -> bool:
        if cost:
            self.add_cost(cost)
        changed = key != self.key
        self.key = key
        self.unchanged = 0 if changed else self.unchanged + 1
        return changed

    def wake(self) -> None:
        self.unchanged = 0
        self.key = object()  # nothing equals it: the next observe() is a change
