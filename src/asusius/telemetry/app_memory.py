# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""App-memory attribution: the process grouping rule and a bounded growth tracker.

group_apps() is THE grouping ProcessesPage uses (cgroup app id, walking up to eight
parent hops when a process carries no app id of its own; the monitor's own process
counts as its own app). ProcessesPage._rebuild calls it so the page and the Memory
page's app panel can never drift apart; the tracker reuses it per delivery.

AppMemoryTracker folds consistent walker snapshots into a bounded, immutable
aggregate: per app group, the summed approximate-private memory (the same p.mem
metric Processes shows), a partial flag (any member had to fall back to RSS), a
launched flag (any member ran under an app-*.scope/.service unit — the display
side filters unrecognized groups with it, exactly like the page), and member
identities as (pid, start) frozensets. Baselines, growth and the "New" state
follow the contract: a baseline is the first fully valid reading per app, growth
compares against it only when at least one member identity overlaps, a restart
or full member churn re-baselines and marks the group new again.

The module is gi-free: the own-app id arrives as a parameter, never an import.
Proc rows are mutable and reused between walks — only copied values (sizes and
identity tuples) are ever retained here, never row objects. The tracker adds no
reads of its own: it consumes walker output only.
"""
from __future__ import annotations

import os
from typing import NamedTuple

MAX_GROUPS = 128      # baseline entries retained before coverage turns partial
MAX_MEMBERS = 2048    # baseline member identities retained across all groups
MAX_HOPS = 8          # parent hops when a process carries no app id of its own
APP_MEMORY_OWNER = "memory-apps"


def group_apps(procs, own_pid: int, own_app_id: str | None) -> dict[str, list]:
    """app id -> member procs, exactly the grouping ProcessesPage._rebuild uses.

    A process joins the group of its own cgroup app id; without one, the nearest
    ancestor with an app id wins (up to MAX_HOPS hops through listed processes,
    never through init). The monitor's own process is always its own app. Processes
    that end up with no app id at all are left out (they are the page's "System
    processes" pseudo-group, not an app). Assignment only — nothing here changes
    what counts as a recognized app; name/icon resolution stays with the caller.
    """
    by_pid = {p.pid: p for p in procs}
    groups: dict[str, list] = {}
    for p in procs:
        app = p.app
        hops, cur = 0, p
        while (cur.pid != own_pid and app is None and hops < MAX_HOPS
               and cur.ppid in by_pid and cur.ppid > 1):
            cur = by_pid[cur.ppid]
            app = cur.app
            hops += 1
        if cur.pid == own_pid:
            app = own_app_id
        if app is None:
            continue
        groups.setdefault(app, []).append(p)
    return groups


class AppMemRow(NamedTuple):
    """One ranking row. name_key is the stable group key itself; growth_bytes is
    None without a comparable baseline, baseline_age_s the age of that baseline.
    launched follows the Processes rule (any member ran under an app-*.scope or
    app-*.service unit): rows without it and without a .desktop file resolve to
    no display info and the panel skips them, like the page's Apps section."""

    app_id: str
    name_key: str
    size_bytes: int
    growth_bytes: int | None
    partial: bool
    is_new: bool
    baseline_age_s: float | None
    launched: bool


class AppMemoryTracker:
    """Bounded per-delivery aggregate plus growth baselines for the app panel.

    coverage turns "partial" when a cap stops new baseline entries (the size and
    growth ranking of the CURRENT groups is never truncated); group_count and
    baseline_count back the panel's "Growth covers N of M apps" caption.
    """

    def __init__(self, own_pid: int | None = None,
                 own_app_id: str | None = None) -> None:
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self.own_app_id = own_app_id  # None: this process's own procs stay ungrouped
        self._opening: set[str] | None = None   # app ids of the first post-reset read
        self._opening_limited = False
        self._baseline: dict[str, tuple[int, frozenset, float]] = {}
        self._rebased: set[str] = set()         # re-baselined after an identity break
        self._members = 0
        self._aggregate: tuple = ()
        self._last_t: float | None = None
        self.coverage = "full"
        self.group_count = 0
        self.baseline_count = 0

    def reset(self) -> None:
        """Clear the baseline and the New state (owner activation, hub stop, panel
        close, page leave): the next valid delivery sets a fresh baseline."""
        self._opening = None
        self._opening_limited = False
        self._baseline.clear()
        self._rebased.clear()
        self._members = 0
        self._aggregate = ()
        self._last_t = None
        self.coverage = "full"
        self.group_count = 0
        self.baseline_count = 0

    def observe(self, now: float, procs) -> None:
        """Fold one walker snapshot in: rebuild the immutable aggregate, then take
        the missing (or identity-broken) baselines that this reading can anchor."""
        groups = group_apps(procs, self.own_pid, self.own_app_id)
        self._rebased.intersection_update(groups)
        if self._opening is None and groups:  # an empty first walk is not a snapshot
            self._opening = set(sorted(groups)[:MAX_GROUPS])
            self._opening_limited = len(groups) > MAX_GROUPS
        agg = []
        for app_id in sorted(groups):
            members = groups[app_id]
            identities = frozenset((m.pid, m.start) for m in members)
            total = sum(m.mem or 0 for m in members)
            partial = any(bool(getattr(m, "mem_partial", False)) for m in members)
            launched = any((m.unit or "").startswith("app-") for m in members)
            agg.append((app_id, total, partial, identities, launched))
        self._aggregate = tuple(agg)
        self._last_t = now
        self.group_count = len(agg)
        for app_id, total, partial, identities, _launched in agg:
            base = self._baseline.get(app_id)
            if base is not None and not (identities & base[1]):
                # restarted under a reused PID, or every member churned: the old
                # baseline can never compare again, so the app gets a fresh one
                del self._baseline[app_id]
                self._members -= len(base[1])
                if len(self._rebased) < MAX_GROUPS:
                    self._rebased.add(app_id)
                base = None
            if base is not None or partial:
                continue  # already anchored, or waiting for a fully valid reading
            if len(self._baseline) >= MAX_GROUPS \
                    or self._members + len(identities) > MAX_MEMBERS:
                self.coverage = "partial"  # caps limit retention, never the ranking
                continue
            self._baseline[app_id] = (total, identities, now)
            self._members += len(identities)
        self.baseline_count = len(self._baseline)

    def member_count(self, app_id: str) -> int:
        """Members of the group in the latest delivery (0 when it is absent)."""
        for app, _total, _partial, identities, _launched in self._aggregate:
            if app == app_id:
                return len(identities)
        return 0

    def ranking(self, mode: str = "size") -> list[AppMemRow]:
        """Rows for the panel: by size, or by growth with None-growth rows last.
        Ties break deterministically by app id, then size. Ages are relative to
        the last observe() (the delivery cadence the panel renders)."""
        now = self._last_t
        opening = self._opening or frozenset()
        rows = []
        for app_id, total, partial, identities, launched in self._aggregate:
            growth = age = None
            base = self._baseline.get(app_id)
            if base is not None:
                b_total, b_members, b_t = base
                # growth needs an overlapping member identity AND an earlier
                # baseline: the reading that sets a baseline has no change yet
                if not partial and identities & b_members and now is not None and b_t < now:
                    growth = total - b_total
                age = None if now is None else now - b_t
            rows.append(AppMemRow(app_id, app_id, total, growth, partial,
                                  app_id in self._rebased
                                  or (not self._opening_limited and app_id not in opening),
                                  age, launched))
        if mode == "growth":
            rows.sort(key=lambda r: (r.growth_bytes is None, -(r.growth_bytes or 0),
                                     r.app_id, r.size_bytes))
        else:
            rows.sort(key=lambda r: (-r.size_bytes, r.app_id))
        return rows
