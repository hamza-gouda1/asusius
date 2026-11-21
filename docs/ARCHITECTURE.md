# Architecture

## Layers

1. `asusius.core`: pure data models (Profile, PowerLimit, FanCurve, GpuMode,
   BatteryHealth, Telemetry) and errors. Validation lives here; the firmware's own
   min/max are hard bounds.
2. `asusius.backends`: one `Backend` interface, several implementations:
   `dbus` (asusd, supergfxd, power-profiles-daemon via Gio, lazy import),
   `sysfs` (asus-armoury firmware attributes, platform_profile, LEDs, battery, hwmon),
   `cli` (asusctl wrapper, allow-listed arguments, no shell), `fake` (sysfs backend
   over a generated tree for tests).
3. `asusius.controller.Controller`: picks a backend per capability and reports
   `capabilities()` including why something is unavailable.
4. `asusius.cli`: argparse front end, JSON output, `--dry-run` on writes.
5. `asusius.daemon` (`asusiusd`): per-user service running `asusius.automation`
   (rules), `asusius.effects` (keyboard software effects), and
   `asusius.updates.scheduler.UpdateScheduler` (periodic update checks and desktop
   notifications). Hardware writes go only through `Controller`; see
   [AUTOMATION.md](AUTOMATION.md), [EFFECTS.md](EFFECTS.md), and
   [UPDATES.md](UPDATES.md).
6. `asusius.telemetry`: GTK-free system monitor sources (/proc, /sys, NVML via ctypes),
   one `Sampler`, preallocated ring buffers (`Store`), the process walker and a fake tree for
   development. The GUI drives it from one timer (`ui/live.py`); see
   [PERFORMANCE.md](PERFORMANCE.md).
7. `asusius.services`: systemd system and user unit inspection, filtering, and
   polkit-mediated unit actions via D-Bus.
8. `asusius.updates`: multi-source update checking (`apt`, `snap`, `flatpak`, `fwupd`,
   kernel/NVIDIA restart hints, optional git repos), caching, verification, and
   PackageKit/fwupd/snapd/flatpak apply pipeline; see [UPDATES.md](UPDATES.md).
9. `asusius.agent`: local MCP/JSON-RPC agent interface, capability/policy enforcement,
   standing/one-time approvals, rate limiting, and audit log; see
   [AGENT_API.md](AGENT_API.md).
10. `asusius.ui`: GTK 4 + Libadwaita desktop app built over `viewmodels*.py`, `live.py`,
    and lazy-loaded sidebar pages; see [UI.md](UI.md).
11. Shared infrastructure: `asusius.dbusutil` (bounded-timeout Gio D-Bus connections),
    `asusius.helper` (polkit-guarded sysfs write helper), `asusius.touchpad` (GSettings
    wrappers for touchpad and extension gestures), and `asusius.brand_palette`
    (generated colour tokens; see [BRAND_PALETTE.md](BRAND_PALETTE.md)).

## Backend order

D-Bus, then sysfs, then CLI. A capability is served by the first backend whose probe
reports it available. Reads fall through to the next backend if one fails; writes only
fall through on "unsupported/unavailable", never after a validation error.
When `ASUSIUS_FAKE_ROOT` is set only the fake sysfs backend is constructed, so
real hardware, D-Bus and asusctl are never touched.

## Privilege model

Reads need no privileges. Writes go through a `Writer`; the default writes directly
where sysfs permits (for example keyboard brightness for the `input` group) and raises
`PrivilegeRequired` otherwise. The optional polkit-guarded helper (`packaging/polkit/`)
provides a `Writer` for root-only attributes from an interactive GUI or CLI. The D-Bus
backend writes via asusd, which holds the privileges itself.

The shipped `asusiusd` user unit keeps `NoNewPrivileges=yes`. It cannot invoke the
privileged sysfs fallback: those changes report `PrivilegeRequired` with an unavailable
reason. Background rules need asusd or power-profiles-daemon for protected changes;
attributes already writable by the user remain usable. Installing the helper does not
remove this daemon boundary.

## Time and threads

D-Bus calls use `call_sync` with an explicit timeout; `backends.pool.run_async` runs any
blocking call on a thread pool and returns a `concurrent.futures.Future`.
