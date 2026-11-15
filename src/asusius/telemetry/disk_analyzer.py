# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Disk tree scanner (stdlib only): mount-boundary aware, hardlink dedup, low RAM.

Walks one filesystem subtree iteratively (directories in alphabetical depth-first order) and
aggregates every file into its parent :class:`DirNode`; only the directory tree stays
in memory, never millions of individual file objects. Pinned invariants (shared with
the disk-map and storage pages):

- files are never nodes; they aggregate into the parent DirNode ("size",
  "apparent_size", "file_count", "top_files"); directory inodes themselves count zero
  bytes, so a parent's size is exactly sum(child.size) + direct file bytes, and a
  dir's "other files" overflow is derivable as
  dir.size - sum(child.size) - sum(s for _, s, _ in dir.top_files)
- node index == append order, parent_idx < idx, root == 0
- "top_files" holds the top-8 largest *direct* files per dir, ranked by scan metric
- on cancel, directories discovered but not yet walked remain as zero-size leaves.
"""
from __future__ import annotations

import heapq
import os
import re
import stat as stat_mod
import threading
import time
from collections.abc import Callable

BUCKETS = ("code", "docs", "media", "archives", "binaries", "other")
TOP_FILES_PER_DIR = 8            # per-dir top_files cap (pinned schema)
PROGRESS_EVERY_DIRS = 256        # progress_cb trigger: dirs walked since the last fire
PROGRESS_MIN_INTERVAL_S = 0.1    # progress_cb throttle: min seconds between fires
MAX_HOGS = 25
HOG_MIN_BYTES = 64 * 1024 * 1024  # 64 MiB "space hog" threshold the UI may apply

# bucket(file_name) -> one BUCKETS tag, by extension table (worker 1A owns the table).
_EXT_BUCKET: dict[str, str] = {
    ext: tag
    for tag, exts in (
        ("code", (
            "py", "pyw", "pyi", "ipynb", "js", "mjs", "cjs", "ts", "tsx", "jsx", "c", "h",
            "cpp", "cxx", "cc", "hh", "hpp", "hxx", "rs", "go", "java", "kt", "kts",
            "scala", "swift", "m", "mm", "rb", "php", "pl", "pm", "lua", "r", "jl", "sql",
            "ps1", "psm1", "sh", "bash", "zsh", "fish", "ksh", "bat", "cmd", "vbs", "asm",
            "s", "vue", "svelte", "astro", "dart", "groovy", "gradle", "cmake", "mk",
            "html", "htm", "css", "scss", "sass", "less", "styl", "json", "jsonc", "yaml",
            "yml", "toml", "ini", "cfg", "conf", "xml", "xsl", "xslt", "dtd", "proto",
            "graphql", "gql", "vim", "el", "clj", "cljs", "ex", "exs", "erl", "hs", "ml",
            "fs", "fsx", "vb", "pas", "dpr", "ada", "f", "for", "f90", "f95", "f03", "cob",
            "cbl", "patch", "diff",
        )),
        ("docs", (
            "txt", "text", "md", "markdown", "rst", "adoc", "asciidoc", "org", "tex",
            "latex", "pdf", "doc", "docx", "docm", "dot", "dotx", "odt", "rtf", "epub",
            "mobi", "azw", "azw3", "fb2", "djvu", "chm", "pages", "ppt", "pptx", "pptm",
            "odp", "key", "xls", "xlsx", "xlsm", "ods", "csv", "tsv", "psv", "numbers",
            "log", "srt", "vtt", "sub", "nfo", "man", "info",
        )),
        ("media", (
            "jpg", "jpeg", "jpe", "jfif", "png", "gif", "webp", "bmp", "dib", "tif",
            "tiff", "heic", "heif", "avif", "svg", "svgz", "ico", "icns", "psd", "ai",
            "eps", "raw", "cr2", "cr3", "nef", "arw", "dng", "orf", "rw2", "mp3", "flac",
            "ogg", "oga", "opus", "wav", "wave", "wma", "aac", "m4a", "m4b", "m4r",
            "aif", "aiff", "ape", "mid", "midi", "mod", "xm", "it", "s3m", "mp4", "m4v",
            "mkv", "webm", "avi", "mov", "wmv", "flv", "f4v", "mpg", "mpeg", "mpe",
            "m2v", "3gp", "3g2", "ogv", "mxf", "vob", "rm", "rmvb", "asf", "divx",
            "m2ts", "mts", "m3u", "m3u8",
        )),
        ("archives", (
            "zip", "zipx", "rar", "7z", "tar", "tgz", "tbz", "tbz2", "txz", "tzst", "gz",
            "bz2", "bz", "xz", "lz", "lzma", "lz4", "zst", "br", "arj", "cab", "lzh",
            "lha", "zoo", "cpio", "iso", "dmg", "img", "vhd", "vhdx", "vmdk", "vdi",
            "qcow", "qcow2", "wim", "esd", "squashfs", "snap", "xar", "zpaq", "crx",
            "xpi",
        )),
        ("binaries", (
            "exe", "dll", "com", "sys", "ocx", "cpl", "msi", "msp", "mst", "app",
            "appimage", "appx", "appxbundle", "deb", "rpm", "pkg", "apk", "ipa", "jar",
            "war", "ear", "class", "pyc", "pyo", "pyd", "so", "o", "obj", "a", "lib",
            "dylib", "bin", "elf", "ko", "node", "wasm", "swf", "dex", "odex", "ttf",
            "otf", "woff", "woff2", "eot", "rom", "nes", "smc",
        )),
    ) for ext in exts
}

# Well-known reclaimable directory names -> short tag (see reclaimable_tag()).
_RECLAIM_DIRS: dict[str, str] = {
    "node_modules": "node_modules",
    "__pycache__": "build",
    ".gradle": "build",
    ".mypy_cache": "build",
    ".pytest_cache": "build",
    ".tox": "build",
    ".next": "build",
    ".nuxt": "build",
    ".turbo": "build",
    ".parcel-cache": "build",
    ".nox": "build",
    ".hypothesis": "build",
    ".cache": "cache",
    "cache": "cache",
}

_VM_IMAGE_EXTS = frozenset({"vmdk", "vdi", "qcow", "qcow2", "vhd", "vhdx"})
_SWAP_NAMES = frozenset({"swapfile", "swap.img"})


def bucket(file_name: str) -> str:
    """One of BUCKETS for a file name, by its last extension; "other" is the fallback."""
    _, dot, ext = file_name.rpartition(".")
    if not dot or not ext:
        return "other"
    return _EXT_BUCKET.get(ext.lower(), "other")


def reclaimable_tag(file_path: str) -> str | None:
    """Heuristic tag for well-known reclaimable locations, or None.

    Recognises (case-insensitively): /var/lib/docker, Podman container storage,
    /var/lib/snapd (Snap revisions), Flatpak installs, Downloads, VM/swap image files
    (.vmdk/.vdi/.qcow2/... and swapfile/swap.img) and ~/.cache, node_modules and
    common build/pytest/mypy cache directories. Heuristic only: the UI must treat the
    tag as a hint, never auto-delete from it.
    """
    parts = file_path.lower().split(os.sep)

    def has(*seq: str) -> bool:
        n = len(seq)
        return any(parts[i:i + n] == list(seq) for i in range(len(parts) - n + 1))

    if has("var", "lib", "docker"):
        return "docker"
    if has("var", "lib", "containers") or has(".local", "share", "containers"):
        return "podman"
    if has("var", "lib", "snapd"):
        return "snap"
    if (has("var", "lib", "flatpak") or has(".local", "share", "flatpak")
            or has(".var", "app")):
        return "flatpak"
    ext = parts[-1].rpartition(".")[2]
    if ext in _VM_IMAGE_EXTS or parts[-1] in _SWAP_NAMES:
        return "vm-image"
    if "downloads" in parts:
        return "downloads"
    for comp in parts:
        tag = _RECLAIM_DIRS.get(comp)
        if tag is not None:
            return tag
    return None


def _nested_mounts(root: str) -> set[str]:
    """Mount points below root, including bind mounts on the same device."""
    points = set()
    prefix = root.rstrip(os.sep) + os.sep
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as stream:
            for line in stream:
                fields = line.split()
                if len(fields) < 6:
                    continue
                mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
                if mount != root and mount.startswith(prefix):
                    points.add(mount)
    except OSError:
        pass  # st_dev still detects foreign filesystems
    return points


def _size_of(st: os.stat_result, use_apparent: bool) -> int:
    """Allocated bytes, including zero-block sparse files, or apparent bytes."""
    return st.st_size if use_apparent else st.st_blocks * 512


def _top_push(top: list, name: str, metric: int, apparent: int) -> None:
    """Insert (name, metric, apparent) into `top`, kept desc by (metric, name), capped."""
    pos = len(top)
    while pos and (metric, name) > (top[pos - 1][1], top[pos - 1][0]):
        pos -= 1
    if pos >= TOP_FILES_PER_DIR:
        return
    top.insert(pos, (name, metric, apparent))
    if len(top) > TOP_FILES_PER_DIR:
        top.pop()


class DirNode:
    """One directory in the scanned tree (files are never nodes; they aggregate up)."""
    __slots__ = ("name", "parent_idx", "child_dir_indices", "size", "apparent_size",
                 "file_count", "dir_count", "mtime", "top_files", "identity")

    def __init__(self, name: str, parent_idx: int | None, mtime: float | None,
                 identity: tuple[int, int] | None = None) -> None:
        self.name = name
        self.parent_idx = parent_idx
        self.child_dir_indices: tuple[int, ...] = ()
        self.size = 0
        self.apparent_size = 0
        self.file_count = 0
        self.dir_count = 0
        self.mtime = mtime
        self.identity = identity
        self.top_files: tuple[tuple[str, int, int], ...] = ()


class ScanResult:
    """Immutable scan output: flat list of DirNodes plus totals, stats and error."""

    __slots__ = ("root_path", "nodes", "root", "totals", "stats", "error", "_hogs", "_apparent_hogs")

    def __init__(self, root_path: str, nodes: list[DirNode], root: int,
                 totals: dict, stats: dict, error: str | None = None, *,
                 hogs=(), apparent_hogs=()) -> None:
        self.root_path = root_path
        self.nodes = nodes
        self.root = root
        self.totals = totals
        self.stats = stats
        self.error = error
        self._hogs = hogs
        self._apparent_hogs = apparent_hogs

    def node(self, idx: int) -> DirNode:
        """DirNode for a node index (O(1) list index; IndexError on a bad index)."""
        return self.nodes[idx]

    def children(self, idx: int) -> tuple[int, ...]:
        """Child DIRECTORY indices (files are never nodes)."""
        return self.nodes[idx].child_dir_indices

    def path(self, idx: int) -> str:
        """Absolute path of a node, rebuilt via parent_idx (root -> root_path)."""
        parts = []
        i = idx
        while i is not None and i != self.root:
            n = self.nodes[i]
            parts.append(n.name)
            i = n.parent_idx
        if not parts:
            return self.root_path
        return os.path.join(self.root_path, *reversed(parts))

    def top_hogs(self, limit: int = 10, *, use_apparent: bool = False) -> list[tuple[str, int, str]]:
        """Return up to MAX_HOGS largest files in the requested size metric."""
        heap = self._apparent_hogs if use_apparent else self._hogs
        best = heapq.nlargest(limit, heap)
        return [(os.path.join(self.path(-idx), name), size, bucket(name))
                for size, idx, name in best]


def scan_path(root_path: str, cancel_event: threading.Event | None = None,
              progress_cb: Callable[[int, float], None] | None = None,
              use_apparent: bool = False) -> ScanResult:
    """Scan a single filesystem subtree, with streaming entries and bounded file lists.

    The root may resolve a symlink, while links inside it are skipped. Child mounts
    are omitted. For hard links, the first visited directory and the alphabetical
    name within that directory own the bytes. Cancellation returns a partial result.
    Progress callbacks run on the calling thread and must hand UI work to GTK.
    """
    root_path = os.path.realpath(root_path)
    t0 = time.perf_counter()
    stats = {"duration_s": 0.0, "cancelled": False, "use_apparent": bool(use_apparent),
             "walked_dirs": 0, "walked_files": 0, "skipped": 0, "mounts_skipped": 0}
    totals = {"bytes": 0, "apparent": 0, "files": 0, "dirs": 0}
    nodes: list[DirNode] = []
    hogs: list[tuple[int, int, str]] = []
    apparent_hogs: list[tuple[int, int, str]] = []

    def _finish(error: str | None = None) -> ScanResult:
        if nodes:
            r = nodes[0]
            totals.update(bytes=r.size, apparent=r.apparent_size,
                          files=r.file_count, dirs=r.dir_count)
        stats["duration_s"] = time.perf_counter() - t0
        return ScanResult(root_path, nodes, 0, totals, stats, error,
                          hogs=tuple(hogs), apparent_hogs=tuple(apparent_hogs))

    try:
        st = os.stat(root_path)
    except OSError as exc:
        return _finish(error=f"{root_path!r}: {type(exc).__name__}: {exc}")
    if not stat_mod.S_ISDIR(st.st_mode):
        return _finish(error=f"{root_path!r}: not a directory")
    nodes.append(DirNode(os.path.basename(root_path), None, st.st_mtime, (st.st_dev, st.st_ino)))
    root_dev = st.st_dev
    mounts = _nested_mounts(root_path)
    hardlinks: set[tuple[int, int]] = set()
    stack: list[tuple[int, str]] = [(0, root_path)]
    dirs_since = 0
    last_fire = t0
    bytes_seen = 0
    def cancelled() -> bool:
        if cancel_event is not None and cancel_event.is_set():
            stats["cancelled"] = True
        return stats["cancelled"]

    def publish_progress(force: bool = False) -> None:
        nonlocal dirs_since, last_fire
        now = time.perf_counter()
        if (progress_cb is not None and (force or dirs_since >= PROGRESS_EVERY_DIRS)
                and now - last_fire >= PROGRESS_MIN_INTERVAL_S):
            dirs_since = 0
            last_fire = now
            progress_cb(stats["walked_dirs"], bytes_seen)

    while stack:
        if cancelled():
            break
        idx, path = stack.pop()
        node = nodes[idx]
        try:
            st = os.stat(path, follow_symlinks=False)
            if (st.st_dev, st.st_ino) != node.identity or not stat_mod.S_ISDIR(st.st_mode):
                raise OSError("directory changed during scan")
            entries = os.scandir(path)
        except OSError as exc:
            stats["skipped"] += 1
            if idx == 0:
                return _finish(f"Cannot read {root_path!r}: {exc}")
            continue
        stats["walked_dirs"] += 1
        children: dict[str, tuple[str, os.stat_result]] = {}
        dir_links: dict[tuple[int, int], tuple[str, os.stat_result]] = {}
        top: list[tuple[str, int, int]] = []

        def add_file(name: str, st: os.stat_result) -> None:
            nonlocal bytes_seen
            metric = _size_of(st, use_apparent)
            node.size += metric
            node.apparent_size += st.st_size
            node.file_count += 1
            bytes_seen += metric
            if node.mtime is None or st.st_mtime > node.mtime:
                node.mtime = st.st_mtime
            _top_push(top, name, metric, st.st_size)
            for heap, size in ((hogs, metric), (apparent_hogs, st.st_size)):
                item = (size, -idx, name)
                if len(heap) < MAX_HOGS:
                    heapq.heappush(heap, item)
                elif item > heap[0]:
                    heapq.heapreplace(heap, item)

        with entries as stream:
            entries_iter = iter(stream)
            while not cancelled():
                try:
                    entry = next(entries_iter)
                except StopIteration:
                    break
                except OSError:
                    stats["skipped"] += 1
                    break
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    stats["skipped"] += 1
                    continue
                if st.st_dev != root_dev or entry.path in mounts:
                    stats["mounts_skipped"] += 1
                    continue
                if stat_mod.S_ISDIR(st.st_mode):
                    children[entry.name] = (entry.path, st)
                    continue
                if not stat_mod.S_ISREG(st.st_mode):
                    continue
                stats["walked_files"] += 1
                if st.st_nlink <= 1:
                    add_file(entry.name, st)
                else:
                    key = (st.st_dev, st.st_ino)
                    old = dir_links.get(key)
                    if key not in hardlinks and (old is None or entry.name < old[0]):
                        dir_links[key] = (entry.name, st)
                if stats["walked_files"] % 256 == 0:
                    publish_progress(force=True)
        for key, (name, st) in dir_links.items():
            hardlinks.add(key)
            add_file(name, st)
        node.top_files = tuple(top)
        to_push = []
        for name in sorted(children):
            child_path, child_stat = children[name]
            cidx = len(nodes)
            nodes.append(DirNode(name, idx, child_stat.st_mtime, (child_stat.st_dev, child_stat.st_ino)))
            to_push.append((cidx, child_path))
        node.child_dir_indices = tuple(cidx for cidx, _ in to_push)
        stack.extend(reversed(to_push))
        dirs_since += 1
        publish_progress()
    # Phase 2: subtree aggregation, children before parents (a child's index is
    # always higher than its parent's, so one reverse pass finalises every node).
    for i in range(len(nodes) - 1, 0, -1):
        n = nodes[i]
        p = n.parent_idx
        pn = nodes[p]  # p is never None here: root (index 0) is excluded above
        pn.size += n.size
        pn.apparent_size += n.apparent_size
        pn.file_count += n.file_count
        pn.dir_count += n.dir_count + 1
        if n.mtime is not None and (pn.mtime is None or n.mtime > pn.mtime):
            pn.mtime = n.mtime
    return _finish()