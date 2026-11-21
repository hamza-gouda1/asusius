# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""The GUI's single telemetry timer, plus the power page's UPower services.

One GLib timer drives the shared Sampler: 1 Hz normally, 2 Hz while a monitor detail
page is visible, stopped while the window is hidden or minimised. Each tick reads the
sources on the worker pool (never two at once) and commits on the main loop, then
notifies listeners. The process walk joins the same tick, only while asked for.

UPower uses a separate worker path: while the battery page is
shown, a lazily created UpowerClient (telemetry.upower, D-Bus free until used) is
polled on the worker pool at a bounded cadence and its inventory is pushed to the
page on the main loop; history requests run on the same pool with a per-(device,
range) token guard so stale responses are dropped. A matching local battery
supplies voltage samples on discharge if UPower has no voltage history.

Memory-page seams: showing the Memory page arms the sampler's vmstat reads
(track_swap), and process walks are owner-tagged (request_processes) so the
Processes page and the Memory page's app panel can both hold the walk without
stepping on each other; every owner activation re-baselines the app-memory
tracker, which observes every qualifying delivery (timer and synchronous ticks).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from gi.repository import GLib

from ..backends.pool import run_async
from ..telemetry import Sampler
from ..telemetry.app_memory import APP_MEMORY_OWNER, AppMemoryTracker
from ..telemetry.power_history import LocalVoltageStore
from .tasks import run_bg
from .viewmodels import OWN_APP_ID

PROCS_EVERY_S = 2.0
POWER_POLL_S = 10.0        # inventory poll cadence while the battery page is shown
POWER_BACKOFF_S = 30.0     # next poll after an empty/failed inventory
POWER_MAX_ERRORS = 5
HISTORY_RESOLUTION = 300
VOLTAGE_LOG_INTERVAL_S = 60.0


class LiveHub:
    def __init__(self, fake_root: str | None = None, upower=None,
                 world_mode: str | None = None) -> None:
        self.fake = fake_root is not None
        root = fake_root or "/"
        self.world = None
        if self.fake:
            from ..telemetry.fake import FakeWorld
            self.world = FakeWorld(Path(root), mode=world_mode or "default")
            self.world.advance(time.monotonic() - 1.0)
        self.sampler = Sampler(root, fake=self.fake)
        self.store = self.sampler.store
        self.mem_store = self.sampler.mem_history  # the Memory page's 5-minute ring
        self.root = root
        self.interval_s = 1.0
        self.listeners: list = []
        self._timer: int | None = None
        self._busy = False
        self._closed = False
        self._procs_epoch = 0
        self.running = False
        self.hold = False  # screenshot harness: drive ticks by hand with a fake clock
        self.ticks = 0
        self.last_error: str | None = None
        self._error_count = 0
        self.procs_wanted = False
        self.procs: list = []
        self.procs_t = -1e9
        self.procs_gen = 0
        self._walker = None
        self._proc_owners: set[str] = set()
        self.app_memory = AppMemoryTracker(own_pid=os.getpid(), own_app_id=OWN_APP_ID)
        # -- power page (UPower) state; `upower` injects a client (tests, QA) ---------------
        self._upower = upower
        self._power_page = None             # the battery page while it is shown
        self._mem_page = None               # the memory page while it is shown
        self._power_timer: int | None = None
        self._power_errors = 0
        self._power_delay = POWER_POLL_S
        self._power_epoch = 0
        self._power_pending = False
        self._power_devices = {}
        self._history_sequence = 0
        self._history_tokens: dict[tuple[str, float], int] = {}
        self._voltage: LocalVoltageStore | None = None
        self._voltage_target = None
        self._voltage_next = 0.0
        self._voltage_pending = False

    # -- control ----------------------------------------------------------------------
    def start(self) -> None:
        if self.running or self.hold or self._closed:
            return
        self.running = True
        self.sampler.mem.track_swap = self._mem_page is not None
        self._schedule()
        self.tick()
        if self._power_page is not None:
            self._power_refresh()

    def stop(self) -> None:
        self.running = False
        self.sampler.mem.track_swap = False
        self._power_stop_poll()
        self._procs_epoch += 1
        self.procs_t = -1e9
        self.app_memory.reset()  # a sampling discontinuity: growth starts fresh
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop()
        if self._power_page is not None:
            self.power_page_hidden(self._power_page)
        if self._mem_page is not None:
            self.mem_page_hidden(self._mem_page)
        self._power_stop_poll()
        self._upower = None
        if not self._busy:
            self.sampler.close()

    def set_interval(self, seconds: float) -> None:
        if abs(seconds - self.interval_s) < 1e-6:
            return
        self.interval_s = seconds
        if self.running:
            self._schedule()

    def _schedule(self) -> None:
        if self._timer is not None:
            GLib.source_remove(self._timer)
        if self.interval_s >= 1.0 and float(self.interval_s).is_integer():
            # second-granular timers are batched by GLib with other wakeups
            self._timer = GLib.timeout_add_seconds(int(self.interval_s), self._on_timer)
        else:
            self._timer = GLib.timeout_add(int(self.interval_s * 1000), self._on_timer)

    def want_processes(self, on: bool) -> None:
        """Compat shim for the Processes page's walk request (owner "processes")."""
        self.request_processes(on, "processes")

    def request_processes(self, on: bool, owner: str = "processes") -> None:
        """Owner-tagged walk request (the Processes page, the Memory app panel).

        Every owner activation — each off->on transition for that owner — resets
        the app-memory tracker, so re-opening the panel re-baselines. The epoch
        bump invalidates walks in flight; when the last owner releases, the walk
        stops and the delivered rows are dropped."""
        self._procs_epoch += 1
        if on:
            if owner == APP_MEMORY_OWNER and owner not in self._proc_owners:
                self.app_memory.reset()  # the next valid delivery sets the baseline
            self._proc_owners.add(owner)
            self.procs_t = -1e9
        else:
            self._proc_owners.discard(owner)
            if not self._proc_owners:
                self.procs = []
                self._walker = None
        self.procs_wanted = bool(self._proc_owners)

    def _on_timer(self) -> bool:
        self.tick()
        return GLib.SOURCE_CONTINUE

    # -- one tick ---------------------------------------------------------------------
    def tick(self, now: float | None = None) -> None:
        if self._busy or self._closed:
            return
        self._busy = True
        fut = run_async(self._collect, now)
        fut.add_done_callback(lambda f: GLib.idle_add(self._deliver, f))

    def _collect(self, now):
        epoch = self._procs_epoch
        now = time.monotonic() if now is None else now
        if self.world is not None:
            self.world.advance(now)
        now, vals, info = self.sampler.collect(now)
        procs = None
        if epoch == self._procs_epoch and self.procs_wanted \
                and now - self.procs_t >= PROCS_EVERY_S - 0.05:
            walker = self._walker
            if walker is None:
                from ..telemetry.procs import ProcessWalker
                walker = ProcessWalker(self.root, self.sampler.cpu.static["count"])
                self._walker = walker
            procs = walker.walk(now, info.get("igpu", {}).get("per_pid"),
                                info.get("dgpu", {}).get("procs"))
        return now, vals, info, procs, epoch

    def _deliver_procs(self, now: float, procs: list) -> None:
        """Publish one qualifying walker snapshot and feed the app-memory tracker.

        Both the timer delivery and collect_now funnel through here, so the QA
        harness's synchronous ticks observe exactly what the live path observes."""
        self.procs = procs
        self.procs_t = now
        self.procs_gen += 1
        if APP_MEMORY_OWNER in self._proc_owners:
            self.app_memory.observe(now, procs)

    def _deliver(self, fut) -> bool:
        self._busy = False
        if self._closed:
            self.sampler.close()
            return GLib.SOURCE_REMOVE
        exc = fut.exception()
        if exc is not None:
            msg = f"telemetry tick failed: {exc!r}"
            self._error_count += 1
            if msg != self.last_error:
                import sys
                self.last_error = msg
                print(msg, file=sys.stderr)
            return GLib.SOURCE_REMOVE
        self.last_error = None
        self._error_count = 0
        now, vals, info, procs, epoch = fut.result()
        self.sampler.commit(now, vals, info)
        self._record_voltage()
        if procs is not None and epoch == self._procs_epoch and self.procs_wanted:
            self._deliver_procs(now, procs)
        self.ticks += 1
        for fn in list(self.listeners):
            fn(self)
        return GLib.SOURCE_REMOVE

    def collect_now(self, now: float, notify: bool = False) -> None:
        """Synchronous tick (screenshot harness: build history with a fake clock).

        The epoch+wanted guards live in _collect, so a delivery here already
        passed them."""
        if self._closed:
            return
        if self._busy:
            raise RuntimeError("collect_now called while async tick is in flight")
        now, vals, info, procs, _epoch = self._collect(now)
        self.sampler.commit(now, vals, info)
        self._record_voltage()
        if procs is not None:
            self._deliver_procs(now, procs)
        if notify:
            self.ticks += 1
            for fn in list(self.listeners):
                fn(self)

    @property
    def info(self) -> dict:
        return self.sampler.info

    # -- battery page: UPower inventory -------------------------------------------------
    def set_upower(self, client) -> None:
        """Inject a UpowerClient-like object (tests, QA harness). Not owned: close()
        will not touch it."""
        self._upower = client

    def _power_client(self):
        if self._upower is None:
            from ..telemetry.upower import UpowerClient
            if self.fake:
                from ..telemetry.fake import FakeUpowerTransport
                self._upower = UpowerClient(FakeUpowerTransport(owner=False))
            else:
                self._upower = UpowerClient()
        return self._upower

    # -- memory page: vmstat read gating --------------------------------------------------
    def mem_page_shown(self, page) -> None:
        """The Memory page is visible: the sampler may read /proc/vmstat for the
        swap-rate series (the reader throttles itself to ~1 read per second)."""
        self._mem_page = page
        self.sampler.mem.track_swap = self.running or self.fake

    def mem_page_hidden(self, page) -> None:
        """Stop the vmstat reads for the page that hid (identity-guarded, like the
        power page's hidden path)."""
        if page is None or self._mem_page is not page:
            return
        self._mem_page = None
        self.sampler.mem.track_swap = False

    def power_page_shown(self, page) -> None:
        """The battery page is visible: poll UPower inventory and keep it fresh.

        The inventory refresh runs off the main loop and backs off after failures.
        """
        self._power_epoch += 1
        self._power_page = page
        self._power_errors = 0
        self._power_delay = POWER_POLL_S
        if self._voltage is None:
            self._voltage = LocalVoltageStore()
        self._power_refresh()

    def power_page_hidden(self, page) -> None:
        """Stop polling and invalidate pending history for the page that closed."""
        if page is None or self._power_page is not page:
            return
        self._power_page = None
        self._power_epoch += 1
        self.cancel_power_history()
        self._power_devices.clear()
        self._power_stop_poll()
        self._voltage = None

    def cancel_power_history(self) -> None:
        self._history_tokens.clear()
        self._voltage_target = None

    def _power_stop_poll(self) -> None:
        if self._power_timer is not None:
            GLib.source_remove(self._power_timer)
            self._power_timer = None

    def _power_refresh(self) -> None:
        """One inventory pass on the worker pool; the result lands on the main loop."""
        if self._power_pending or self._power_page is None or self._closed:
            return
        epoch = self._power_epoch
        self._power_pending = True

        def deliver(result):
            self._power_pending = False
            if epoch == self._power_epoch:
                self._power_inventory_done(result)
            else:
                self._power_refresh()

        run_bg(self._power_client().refresh_inventory, done=deliver,
               error=lambda _: deliver(([], "UPower query failed")))

    def _power_schedule(self) -> None:
        """Arm the next poll (no-op once the page is hidden or the retries ran out)."""
        self._power_stop_poll()
        if self._power_page is None or (not self.fake and not self.running):
            return
        delay = min(self._power_delay * max(self._power_errors, 1),
                    POWER_BACKOFF_S * POWER_MAX_ERRORS)
        self._power_timer = GLib.timeout_add_seconds(
            max(1, int(delay)), self._power_poll_tick)

    def _power_poll_tick(self) -> bool:
        self._power_timer = None
        self._power_refresh()
        return GLib.SOURCE_REMOVE

    def _power_inventory_done(self, result) -> None:
        devices, status = result
        if not devices and (status == "UPower not available"
                            or status.startswith("UPower query failed")):
            self._power_errors += 1
            self._power_delay = POWER_BACKOFF_S
        else:
            self._power_errors = 0
            self._power_delay = POWER_POLL_S
        page = self._power_page
        if page is not None:
            self._power_devices = {d.object_path: d for d in devices}
            page.on_power_inventory(self._sysfs_visible(devices), status)
            self._power_schedule()

    def _sysfs_visible(self, devices: list) -> list:
        """Deduplicate aliases while retaining each battery's UPower history path."""
        seen = set()
        visible = []
        local = {Path(p).name: Path(p).resolve()
                 for p in self.sampler.power.native_paths}
        for device in devices:
            native = device.native_path
            if native:
                canonical = local.get(native, native)
                if native.startswith("/sys/"):
                    canonical = Path(native).resolve()
                identity = (device.kind, canonical, device.serial)
                if identity in seen:
                    continue
                seen.add(identity)
            visible.append(device)
        return visible

    # -- battery page: UPower history ------------------------------------------------------
    def request_power_history(self, dev_key: str, range_s: float, done, error) -> None:
        """Fetch raw UPower history on the worker pool, delivered on the main loop.

        dev_key is a UPower object path, optionally suffixed with a series so the
        token guard stays per series (the battery page's history_key):
        - "<object path>|rate" / "|charge" / "|voltage": that one series is fetched
          and done(list[UpowerHistoryPoint]) receives its raw rows.
        - a bare object path: all three series are fetched and
          done({"rate": [...], "charge": [...], "voltage": [...]}) receives the raw
          UpowerHistoryPoint lists (no merging, no normalisation — the page does
          that through telemetry.power_history.normalize_history).

        Token guard: the latest token is kept per (dev_key, range_s); when a
        response arrives it is delivered through done/error only if that request's
        token is still current — stale ones are silently dropped.
        """
        key = (dev_key, float(range_s))
        self._history_sequence += 1
        token = self._history_sequence
        self._history_tokens[key] = token
        path, series = self._split_history_key(dev_key)
        client = self._power_client()
        store = self._voltage
        device = self._power_devices.get(path)

        def work():
            if self._history_tokens.get(key) != token:
                return None, None
            if series is not None:
                rows = client.get_history(path, series, range_s, HISTORY_RESOLUTION)
                if rows or store is None or series != "voltage":
                    return rows, None
                identity = self._voltage_identity(device)
                if identity is None:
                    return rows, None
                native, identity_key, _serial = identity
                now = time.time()
                try:
                    local = [p for p in store.load(native, identity_key)
                             if now - range_s <= p.t <= now]
                except OSError:
                    local = []
                return local, identity
            return ({s: client.get_history(path, s, range_s, HISTORY_RESOLUTION)
                     for s in ("rate", "charge", "voltage")}, None)

        def deliver(result) -> None:
            if self._history_tokens.get(key) == token:
                self._history_tokens.pop(key)
                rows, identity = result
                if identity is not None:
                    self._voltage_target = (path, *identity)
                    self._record_voltage()
                elif (series == "voltage" and self._voltage_target is not None
                      and self._voltage_target[0] == path):
                    self._voltage_target = None
                done(rows)

        def fail(exc) -> None:
            if self._history_tokens.get(key) == token:
                self._history_tokens.pop(key)
                if error is not None:
                    error(exc)

        run_bg(work, done=deliver, error=fail)

    @staticmethod
    def _split_history_key(dev_key: str) -> tuple[str, str | None]:
        for name in ("rate", "charge", "voltage"):
            suffix = f"|{name}"
            if dev_key.endswith(suffix):
                return dev_key[:-len(suffix)], name
        return dev_key, None

    # -- battery page: local voltage log ----------------------------------------------------
    def _voltage_identity(self, device):
        """Bind local history to the sampled battery with a verified physical serial."""
        paths = self.sampler.power.native_paths
        if not paths or device is None or device.kind != "laptop" or not device.serial:
            return None
        primary = Path(paths[0])
        native = device.native_path
        if native != primary.name and (not native.startswith("/sys/")
                                       or Path(native).resolve() != primary.resolve()):
            return None
        try:
            if (primary / "serial_number").read_text().strip() != device.serial:
                return None
        except OSError:
            return None
        return str(primary), f"{device.model or ''}\x1f{device.serial}", device.serial

    def _record_voltage(self) -> None:
        """Schedule a bounded write while using the local fallback on Battery."""
        store, identity = self._voltage, self._voltage_target
        if store is None or identity is None or self._voltage_pending:
            return
        now = time.monotonic()
        if now < self._voltage_next:
            return
        bat = (self.info.get("battery") or {})
        status = bat.get("status")
        if not isinstance(status, str) or status.lower() != "discharging":
            return
        v = self.store.last("bat.voltage")
        p = self.store.last("bat.percent")
        if v is None or p is None:
            return
        self._voltage_next = now + VOLTAGE_LOG_INTERVAL_S
        self._voltage_pending = True
        _path, native, key, serial = identity

        def work():
            if self._voltage_target == identity \
                    and (Path(native) / "serial_number").read_text().strip() == serial:
                store.record(native, key, v, p, "discharging")

        def finish(_):
            self._voltage_pending = False

        run_bg(work, done=finish, error=finish)
