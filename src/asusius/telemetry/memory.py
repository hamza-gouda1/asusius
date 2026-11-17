# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Memory: /proc/meminfo, swap, zram, zswap, DDR (DMI) and pressure stall information."""
from __future__ import annotations

import math

from functools import cached_property

from .util import join, listdir, read_bytes, read_str

PSI_MISSING = ("not available: this kernel has no /proc/pressure (built without "
               "CONFIG_PSI or booted with psi=0)")


def parse_meminfo(data: bytes) -> dict[str, int]:
    """{field: bytes}; values in /proc/meminfo are kB (KiB) unless unitless (HugePages_*).

    Per-line independence: one malformed line (a value int() cannot parse) leaves
    that field absent and never fails the rest of the file.
    """
    out: dict[str, int] = {}
    for line in data.split(b"\n"):
        if not line:
            continue
        key, _, rest = line.partition(b":")
        parts = rest.split()
        if not parts:
            continue
        try:
            v = int(parts[0])
        except ValueError:
            continue  # that field is absent; the others still parse
        if v >= 0:
            out[key.decode(errors="replace")] = (
                v * 1024 if len(parts) > 1 and parts[1] == b"kB" else v)
    return out


def derive(mi: dict[str, int]) -> dict[str, float | None]:
    """Validity-aware memory figures, plus the figures `free` prints.

    The estimate keys (total/available/free/in_use/reclaimable and the new file,
    slab, page_tables, kernel_stack) are None when their source is missing or
    invalid — an unavailable estimate is never fabricated from a zero:
      in_use      = MemTotal - MemAvailable  (what procps >= 4 calls "used")
      reclaimable = MemAvailable - MemFree   (cache the kernel can drop)
    in_use + reclaimable + free == total holds only while MemAvailable >= MemFree;
    when Available dips below Free, reclaimable clamps to 0 and the identity may
    break (the two-part availability estimate stays exact). Legacy keys keep their
    0-default semantics exactly. KReclaimable (which includes SReclaimable) is
    never added to the slab figures; AnonPages, Shmem and Active(file)+Inactive(file)
    are independent series — the file line is file-backed LRU only, never Shmem.
    """
    total = mi.get("MemTotal")
    free = mi.get("MemFree")
    if free is not None:
        free = min(total, max(0, free)) if total is not None else max(0, free)
    avail = mi.get("MemAvailable")
    if avail is not None:  # no free-fallback: absent kernel line -> absent estimate
        avail = min(total, max(0, avail)) if total is not None else max(0, avail)
    afile, ifile = mi.get("Active(file)"), mi.get("Inactive(file)")
    swap_total, swap_free = mi.get("SwapTotal"), mi.get("SwapFree")
    swap_used = (swap_total - swap_free
                 if swap_total is not None and swap_free is not None
                 and 0 <= swap_free <= swap_total else None)
    slab = mi.get("Slab")
    if slab is None:
        sr, su = mi.get("SReclaimable"), mi.get("SUnreclaim")
        slab = sr + su if sr is not None and su is not None else None
    out = {
        "total": total, "available": avail, "free": free,
        "in_use": total - avail if total is not None and avail is not None else None,
        "reclaimable": (max(0, avail - free)
                        if avail is not None and free is not None else None),
        "file": afile + ifile if afile is not None and ifile is not None else None,
        "slab": slab,
        "page_tables": mi.get("PageTables"), "kernel_stack": mi.get("KernelStack"),
        "buffers": mi.get("Buffers", 0), "cached": mi.get("Cached", 0),
        "buff_cache": mi.get("Buffers", 0) + mi.get("Cached", 0) + mi.get("SReclaimable", 0),
        "shmem": mi.get("Shmem", 0), "slab_reclaimable": mi.get("SReclaimable", 0),
        "slab_unreclaimable": mi.get("SUnreclaim", 0), "dirty": mi.get("Dirty", 0),
        "writeback": mi.get("Writeback", 0), "committed": mi.get("Committed_AS", 0),
        "commit_limit": mi.get("CommitLimit", 0),
        "swap_total": mi.get("SwapTotal", 0),
        "swap_used": max(0, mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)),
        "swap_cached": mi.get("SwapCached", 0),
        "swap_capacity_bytes": swap_total,
        "swap_used_bytes": swap_used,
        "anonymous_bytes": mi.get("AnonPages"),
        "shared_bytes": mi.get("Shmem"),
        "zswap_pool": mi.get("Zswap"), "zswap_stored": mi.get("Zswapped"),
        "anon": mi.get("AnonPages", 0), "mapped": mi.get("Mapped", 0),
    }
    return out


def parse_psi(text: str | None) -> dict[str, dict[str, float]] | None:
    """{'some': {'avg10': .., 'avg60': .., 'avg300': .., 'total': us}, 'full': {...}}.

    Per-line tolerant like parse_meminfo: one malformed avg= line is dropped and
    the other lines still parse; an entry whose value fails float() is dropped,
    not the whole line. A missing "some" line simply leaves "some" out.
    """
    if not text:
        return None
    out: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        vals: dict[str, float] = {}
        for p in parts[1:]:
            k, _, v = p.partition("=")
            try:
                parsed = float(v)
            except ValueError:
                continue  # malformed entry dropped; the line's other fields stay
            if math.isfinite(parsed) and parsed >= 0:
                vals[k] = parsed
        if vals:
            out[parts[0]] = vals
    return out


def parse_mm_stat(text: str | None) -> tuple[int | None, int | None, int | None] | None:
    """(orig, compr, mem_used_total) bytes from a zram mm_stat file.

    Short files publish the fields that parsed (missing -> None); a file with no
    parsable field (empty, unreadable, all malformed) -> None so the device is skipped.
    Negative or non-integer fields -> None; a measured zero stays a real zero.
    """
    if not text:
        return None
    fields: list[int | None] = []
    for tok in text.split()[:3]:
        try:
            v = int(tok)
        except ValueError:
            v = None
        fields.append(None if v is None or v < 0 else v)
    if all(f is None for f in fields):
        return None
    return tuple(fields) + (None,) * (3 - len(fields))


def zram_devices(root: str) -> list[str]:
    """zram block device names from /sys/block, sorted ('zram0', 'zram1', …)."""
    return [d for d in listdir(join(root, "sys/block"))
            if d.startswith("zram") and d[4:].isdigit()]


# SMBIOS "Memory Type" codes (byte at offset 0x14 of a type-17 Memory Device record).
# 0x02 ("Unknown") is deliberately absent: unknown stays None, never a published string.
_DDR_TYPES = {
    0x01: "Other", 0x03: "DRAM", 0x04: "EDRAM", 0x05: "VRAM",
    0x06: "SRAM", 0x07: "RAM", 0x08: "ROM", 0x09: "Flash", 0x0A: "EEPROM",
    0x0B: "FEPROM", 0x0C: "EPROM", 0x0D: "CDRAM", 0x0E: "3DRAM", 0x0F: "SDRAM",
    0x10: "SGRAM", 0x11: "RDRAM", 0x12: "DDR", 0x13: "DDR2", 0x14: "DDR2 FB-DIMM",
    0x18: "DDR3", 0x19: "FBD2", 0x1A: "DDR4", 0x1B: "LPDDR", 0x1C: "LPDDR2",
    0x1D: "LPDDR3", 0x1E: "LPDDR4", 0x1F: "Logical non-volatile", 0x20: "HBM",
    0x21: "HBM2", 0x22: "DDR5", 0x23: "LPDDR5", 0x24: "HBM3",
}


def _dmi_string(raw: bytes, length: int, index: int) -> str | None:
    """NUL-terminated string `index` from the table after a DMI record; None when unset."""
    if index == 0 or length >= len(raw):
        return None
    off = length
    for _ in range(index - 1):
        end = raw.find(b"\x00", off)
        if end <= off:
            return None
        off = end + 1
    end = raw.find(b"\x00", off)
    if end < 0:
        return None
    return raw[off:end].decode(errors="replace") or None


def parse_dmi_memory(raw: bytes) -> dict | None:
    """One populated SMBIOS type-17 (Memory Device) record; None when absent or empty.

    Use the type 17 fields defined in DSP0134 §7.18. The formatted length and
    the bytes actually read both bound each field.
    """
    if len(raw) < 0x15 or raw[0] != 0x11:
        return None
    length = raw[1]
    formatted = min(length, len(raw))
    if formatted < 0x15:
        return None
    size_raw = int.from_bytes(raw[0x0C:0x0E], "little")
    if size_raw == 0x0000:  # no memory device installed in this slot
        return None
    if size_raw in (0x8000, 0xFFFF):
        size_bytes = None
    elif size_raw == 0x7FFF:
        extended = int.from_bytes(raw[0x1C:0x20], "little") if formatted >= 0x20 else 0
        size_bytes = extended << 20 if 0 < extended < (1 << 31) else None
    elif size_raw & 0x8000:
        size_bytes = (size_raw & 0x7FFF) << 10
    else:
        size_bytes = size_raw << 20
    mtype = _DDR_TYPES.get(raw[0x14])
    speed = int.from_bytes(raw[0x17:0x19], "little") if formatted >= 0x19 else 0
    if speed == 0xFFFF:
        extended_speed = int.from_bytes(raw[0x54:0x58], "little") if formatted >= 0x58 else 0
        speed = extended_speed if (1 << 16) - 1 <= extended_speed < (1 << 31) else 0
    speed = speed or None
    slot = _dmi_string(raw, length, raw[0x10])
    if size_bytes is None and mtype is None and speed is None:
        return None
    return {"slot": slot, "type": mtype, "speed_mts": speed, "size_bytes": size_bytes}


class MemoryReader:

    def __init__(self, root: str = "/") -> None:
        self.root = root
        # Swap-activity tracking is requested by the main thread while the Memory
        # page is visible; the worker only reads it (benign one-tick races, no lock).
        self._track_swap = False
        self._swap_epoch = 0
        self._vmstat_epoch = -1
        self._vmstat_t: float | None = None

    @property
    def track_swap(self) -> bool:
        return self._track_swap

    @track_swap.setter
    def track_swap(self, active: bool) -> None:
        if active != self._track_swap:
            self._swap_epoch += 1
            self._track_swap = active

    def _vmstat(self, now: float, d: dict) -> None:
        """pswpin/pswpout page counters from /proc/vmstat, throttled to ~1 read/s.

        Keys are absent from d when the read is skipped or a line is malformed —
        never a stale reuse and never a fabricated zero; zero traffic is a real 0.
        A counter decrease is not fixed up here (MemHistory rebaselines).
        """
        epoch = self._swap_epoch
        if not self._track_swap:
            return
        if (epoch == self._vmstat_epoch and self._vmstat_t is not None
                and int(now) <= int(self._vmstat_t)):
            return
        self._vmstat_t = now
        self._vmstat_epoch = epoch
        d["vmstat_t"] = now
        d["vmstat_epoch"] = epoch
        data = read_bytes(join(self.root, "proc/vmstat"))
        if data is None:
            return
        for line in data.split(b"\n"):
            key, _, rest = line.partition(b" ")
            if key not in (b"pswpin", b"pswpout"):
                continue
            try:
                value = int(rest)
            except ValueError:
                continue  # malformed line -> that counter absent, never 0
            if value >= 0:
                d[key.decode()] = value

    def zswap_enabled(self) -> bool | None:
        v = read_str(join(self.root, "sys/module/zswap/parameters/enabled"))
        return None if v is None else v in ("Y", "1")

    def pressure(self, name: str) -> tuple[dict | None, str | None]:
        """PSI for cpu|memory|io, or (None, reason)."""
        text = read_str(join(self.root, f"proc/pressure/{name}"))
        if text is None:
            return None, PSI_MISSING
        return parse_psi(text), None

    def ddr(self) -> dict | None:
        """DDR modules from SMBIOS type-17 DMI entries; None when DMI is unreadable.

        /sys/firmware/dmi is 0400 root-only on typical desktops, so without root this
        is None and the page shows "–" — never a guess and never a privilege prompt.
        """
        base = join(self.root, "sys/firmware/dmi/entries")
        modules = []
        for name in sorted(listdir(base)):
            if not name.startswith("17-"):
                continue
            raw = read_bytes(join(base, f"{name}/raw"), 1 << 14)
            mod = parse_dmi_memory(raw) if raw else None
            if mod:
                modules.append(mod)
        return {"modules": modules} if modules else None

    @cached_property
    def _ddr_inventory(self) -> dict | None:
        return self.ddr()

    def _zram(self, vals: dict, d: dict) -> None:
        """Per-device mm_stat readings; Σ keys publish only when a field was parsed.

        SwapTotal includes zram when enabled as swap, so neither the zram usage
        nor the zswap cache is added to the kernel's aggregate swap counters.
        """
        devs = zram_devices(self.root)
        rows: list[dict] = []
        sums: dict[str, int] = {}
        for name in devs:
            parsed = parse_mm_stat(read_str(join(self.root, f"sys/block/{name}/mm_stat")))
            if parsed is None:  # unreadable, empty or nothing parsable: skip the device
                continue
            row = {"device": name, "orig": parsed[0], "compr": parsed[1], "total": parsed[2]}
            rows.append(row)
            for k in ("orig", "compr", "total"):
                if row[k] is not None:
                    sums[k] = sums.get(k, 0) + row[k]
        for k, v in sums.items():
            vals[f"mem.zram_{k}"] = v  # absent when no device parsed that field
        if rows:
            d["zram"] = rows
        d["zram_any"] = bool(devs)

    def sample(self, now: float, vals: dict, info: dict) -> None:
        data = read_bytes(join(self.root, "proc/meminfo"))
        if data is None:
            info["mem"] = {"error": "/proc/meminfo unreadable"}
            return
        d = derive(parse_meminfo(data))
        vals["mem.in_use"] = d["in_use"]
        vals["mem.reclaimable"] = d["reclaimable"]
        vals["mem.free"] = d["free"]
        vals["mem.swap_used"] = d["swap_used"]
        if d["zswap_pool"] is not None:
            vals["mem.zswap_pool"] = d["zswap_pool"]
        psi, why = self.pressure("memory")
        if psi:
            vals["mem.psi_some"] = psi.get("some", {}).get("avg10")
        d["psi"], d["psi_reason"] = psi, why
        if self.track_swap:
            self._vmstat(now, d)
        self._zram(vals, d)
        ddr = self._ddr_inventory
        if ddr:
            d["ddr"] = ddr
        info["mem"] = d
