# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Memory-only history ring: 360 slots at ~1 Hz for the Memory page's five-minute views.

The shared Store holds 300 samples at the sampler cadence (2 Hz), which is only
150 s. MemHistory wraps a private 360-slot Store with a fixed 13-field allowlist
and commits at most one sample per second bucket, so the effective cadence is 1/s and
360 slots always hold >= 5 minutes. Commits continue while no Memory page is
visible; only vmstat reads and process walks are visibility-gated. A value the
source could not read is stored as NaN and drawn as a gap, never as zero.
"""
from __future__ import annotations

import os

from .ring import Store

ALLOWED = frozenset((
    "anon", "file", "shmem", "slab", "in_use", "available", "total",
    "psi_some", "psi_full", "swap_total", "swap_used", "pswpin_bps", "pswpout_bps",
))
MAX_RATE_DT = 2.0  # a wider interval is a suspend/stop discontinuity, not a rate
PAGE = os.sysconf("SC_PAGE_SIZE")


class MemHistory:
    """Thin wrapper over Store(360); expose .store and delegate everything else.

    Charts read store.last_t / index_range / slot directly and cursor expiry
    needs count, so every other attribute access falls through to the Store.
    """

    def __init__(self) -> None:
        self.store = Store(360)
        # Last actually-read raw vmstat counters (pages) and the read's timestamp.
        self._pswpin: int | None = None
        self._pswpout: int | None = None
        self._vmstat_t: float | None = None
        self._vmstat_epoch: int | None = None

    def clear(self) -> None:
        """Reset the samples and the baseline for a new collection session."""
        self.store.clear()
        self._pswpin = self._pswpout = self._vmstat_t = self._vmstat_epoch = None

    def __getattr__(self, name: str):
        if name == "store":  # never delegate the attribute itself (no recursion)
            raise AttributeError(name)
        return getattr(self.store, name)

    def commit(self, now: float, m: dict) -> None:
        """Push one info["mem"]-shaped dict into the ring, throttled to ~1 Hz.

        Fields missing from m — PSI absent entirely, or a "some"/"full" line
        missing inside it — become None -> NaN gaps in the ring. Swap rates come
        from the raw pswpin/pswpout page counters, which the reader publishes
        only on an actual vmstat read (d["pswpin"], d["pswpout"], d["vmstat_t"]).
        """
        last = self.store.last_t
        if last is not None and int(now) <= int(last):
            return
        psi = m.get("psi") or {}
        fields = {
            "anon": m.get("anonymous_bytes", m.get("anon")),
            "file": m.get("file"),
            "shmem": m.get("shared_bytes", m.get("shmem")),
            "slab": m.get("slab"),
            "in_use": m.get("in_use"),
            "available": m.get("available") if m.get("in_use") is not None else None,
            "total": m.get("total"),
            "psi_some": (psi.get("some") or {}).get("avg10"),
            "psi_full": (psi.get("full") or {}).get("avg10"),
            "swap_total": m.get("swap_capacity_bytes", m.get("swap_total")),
            "swap_used": m.get("swap_used_bytes", m.get("swap_used")),
            "pswpin_bps": None,
            "pswpout_bps": None,
        }
        self._swap_rates(now, m, fields)
        self.store.push(now, fields)  # Store.push drops t <= last_t itself

    def _swap_rates(self, now: float, m: dict, fields: dict) -> None:
        """pswpin/pswpout page deltas -> B/s over the interval between actual reads.

        No counters on this committed tick (the reader skipped vmstat): rates are
        gaps and the baseline is retained, so the next read measures across the
        whole span between actual reads. A decrease or an invalid interval
        (> MAX_RATE_DT, e.g. suspend) is a gap that re-baselines; older ring data
        is kept until it ages out.
        """
        cur_in, cur_out = m.get("pswpin"), m.get("pswpout")
        if cur_in is None and cur_out is None and "vmstat_t" not in m:
            return
        stamp = m.get("vmstat_t", now)
        epoch = m.get("vmstat_epoch")
        dt = stamp - self._vmstat_t if self._vmstat_t is not None else None
        ok_dt = dt is not None and 0 < dt <= MAX_RATE_DT and epoch == self._vmstat_epoch
        for key, cur, prev in (("pswpin_bps", cur_in, self._pswpin),
                               ("pswpout_bps", cur_out, self._pswpout)):
            if cur is None:  # malformed/missing line: gap and no baseline to keep
                fields[key] = None
            elif prev is not None and ok_dt and cur >= prev:
                fields[key] = (cur - prev) * PAGE / dt
            else:
                fields[key] = None  # first sample, reset or suspend: gap
        self._pswpin, self._pswpout = cur_in, cur_out
        self._vmstat_t = stamp
        self._vmstat_epoch = epoch
