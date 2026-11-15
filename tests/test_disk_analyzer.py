# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Scanner semantics and fake filesystem fixtures."""
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from asusius.backends.fake import build_fake_tree
from asusius.telemetry import fake
from asusius.telemetry import disk_analyzer as da
from asusius.telemetry.disk_analyzer import (
    BUCKETS,
    HOG_MIN_BYTES,
    TOP_FILES_PER_DIR,
    bucket,
    reclaimable_tag,
    scan_path,
)

def _join(root: Path, rel: str) -> Path:
    """Join a fixture-relative path with a strict containment check (no escaping)."""
    root = Path(root).resolve()
    target = Path(os.path.normpath(str(root / rel)))
    if target != root and not str(target).startswith(str(root) + os.sep):
        raise ValueError(f"fixture path escapes {root}: {rel}")
    return target


def mkfile(path: Path, size: int, sparse: bool = False) -> None:
    """Deterministic file of apparent size `size` (sparse = seek+write, ~4 KiB disk)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if sparse:
        with open(path, "wb") as f:
            f.seek(size - 1)
            f.write(b"\0")
    else:
        path.write_bytes(bytes(size))


def metric_of(path) -> int:
    """The scan metric is the number of 512-byte blocks allocated."""
    return os.stat(path).st_blocks * 512


class _ForeignDevStat:
    """os.stat_result view with st_dev replaced (stand-in for a second filesystem)."""

    __slots__ = ("_st", "_dev")

    def __init__(self, st, dev: int) -> None:
        self._st = st
        self._dev = dev

    @property
    def st_dev(self) -> int:  # the one overridden field
        return self._dev

    def __getattr__(self, name):
        return getattr(self._st, name)


class _ForeignDevEntry:
    """DirEntry view that reports a foreign st_dev in stat(follow_symlinks=False)."""

    def __init__(self, entry, dev: int) -> None:
        self._e = entry
        self._dev = dev

    def stat(self, *, follow_symlinks=False):
        st = self._e.stat(follow_symlinks=follow_symlinks)
        return st if follow_symlinks else _ForeignDevStat(st, self._dev)

    def is_dir(self, follow_symlinks=True):
        return self._e.is_dir(follow_symlinks=follow_symlinks)

    def is_symlink(self):
        return self._e.is_symlink()

    @property
    def name(self):
        return self._e.name

    @property
    def path(self):
        return self._e.path


class _ScandirShim:
    """Materialised scandir result (context manager + iterator over fixed entries)."""

    def __init__(self, entries) -> None:
        self._entries = entries

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def __iter__(self):
        return iter(self._entries)


_REAL_SCANDIR = os.scandir


def _with_foreign_dev(wrap_dir: str, mount_name: str, dev: int):
    """os.scandir replacement that wraps `mount_name` entries of `wrap_dir` only."""

    def scandir(path):
        if os.path.abspath(os.fspath(path)) == wrap_dir:
            with _REAL_SCANDIR(path) as it:
                entries = [_ForeignDevEntry(e, dev) if e.name == mount_name else e
                           for e in it]
            return _ScandirShim(entries)
        return _REAL_SCANDIR(path)

    return scandir


class ScanBasicsTests(unittest.TestCase):
    """Scan metric modes, dedup, aggregation and pinned constants."""

    def test_fully_sparse_file_has_zero_allocated_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            sparse = Path(d) / "sparse.img"
            with sparse.open("wb") as f:
                f.truncate(1 << 20)
            if sparse.stat().st_blocks != 0:
                self.skipTest("fixture filesystem allocated the sparse file")
            result = scan_path(d)
            self.assertEqual(result.totals["bytes"], 0)
            self.assertEqual(result.totals["apparent"], 1 << 20)

    def test_pinned_constants(self):
        self.assertEqual(da.BUCKETS, ("code", "docs", "media", "archives",
                                      "binaries", "other"))
        self.assertEqual(da.TOP_FILES_PER_DIR, 8)
        self.assertEqual(da.HOG_MIN_BYTES, 64 * 1024 * 1024)
        self.assertEqual(da.PROGRESS_EVERY_DIRS, 256)
        self.assertEqual(da.PROGRESS_MIN_INTERVAL_S, 0.1)

    def test_allocated_vs_apparent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "scan"
            mkfile(root / "real.bin", 9000)            # real bytes: allocated > apparent
            mkfile(root / "sparse.img", 1 << 20, sparse=True)
            mkfile(root / "empty.bin", 0)              # st_blocks == 0 -> fallback to size
            apparent_total = 9000 + (1 << 20)
            alloc = metric_of(root / "real.bin")
            alloc_sparse = metric_of(root / "sparse.img")

            res = scan_path(str(root))                 # allocated (default) mode
            self.assertFalse(res.stats["use_apparent"])
            self.assertEqual(res.node(0).size, alloc + alloc_sparse)
            self.assertEqual(res.node(0).apparent_size, apparent_total)
            self.assertEqual(res.totals["bytes"], alloc + alloc_sparse)
            self.assertEqual(res.totals["apparent"], apparent_total)
            self.assertEqual(res.totals["files"], 3)
            self.assertEqual(res.totals["dirs"], 0)
            # top_files ranked by the scan metric (allocated here)
            self.assertEqual(res.node(0).top_files,
                             (("real.bin", alloc, 9000),
                              ("sparse.img", alloc_sparse, 1 << 20),
                              ("empty.bin", 0, 0)))
            # the sparse file dominates apparent but barely registers allocated
            self.assertLess(alloc + alloc_sparse, apparent_total)

            res_a = scan_path(str(root), use_apparent=True)
            self.assertTrue(res_a.stats["use_apparent"])
            self.assertEqual(res_a.node(0).size, apparent_total)
            self.assertEqual(res_a.node(0).apparent_size, apparent_total)
            for _name, size, apparent in res_a.node(0).top_files:
                self.assertEqual(size, apparent)       # st_size everywhere in this mode
            self.assertEqual(res_a.node(0).top_files,
                             (("sparse.img", 1 << 20, 1 << 20), ("real.bin", 9000, 9000),
                              ("empty.bin", 0, 0)))    # ranking flips with the metric

    def test_hardlink_pair_counted_once(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "scan"
            mkfile(root / "a.bin", 7000)
            os.link(root / "a.bin", root / "b.bin")
            sa = os.stat(root / "a.bin")
            self.assertEqual((sa.st_dev, sa.st_ino),
                             (os.stat(root / "b.bin").st_dev,
                              os.stat(root / "b.bin").st_ino))
            alloc = metric_of(root / "a.bin")

            res = scan_path(str(root))
            self.assertEqual(res.node(0).file_count, 1)     # dedup: one inode, not two
            self.assertEqual(res.node(0).size, alloc)       # counted exactly once
            self.assertEqual(res.node(0).apparent_size, 7000)
            self.assertEqual(res.node(0).top_files,
                             (("a.bin", alloc, 7000),))     # first (alphabetical) wins
            self.assertEqual(res.totals["files"], 1)
            self.assertEqual(res.stats["walked_files"], 2)  # both names were stat'd
            self.assertAlmostEqual(res.node(0).mtime, sa.st_mtime, places=3)

            res_a = scan_path(str(root), use_apparent=True)
            self.assertEqual(res_a.node(0).size, 7000)      # dedup holds in apparent mode
            self.assertEqual(res_a.node(0).apparent_size, 7000)
            self.assertEqual(res_a.node(0).file_count, 1)

    def test_nested_aggregation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "scan"
            mkfile(root / "m1.bin", 3000)
            mkfile(root / "r0.bin", 1000)
            mkfile(root / "mid/l1.bin", 2000)
            (root / "mid/leaf").mkdir(parents=True)         # empty dir: zero bytes
            alloc = {n: metric_of(root / n) for n in ("m1.bin", "r0.bin", "mid/l1.bin")}

            res = scan_path(str(root))
            self.assertEqual([n.name for n in res.nodes], ["scan", "mid", "leaf"])
            self.assertEqual([n.parent_idx for n in res.nodes], [None, 0, 1])
            self.assertEqual(res.children(0), (1,))
            self.assertEqual(res.children(1), (2,))
            self.assertEqual(res.children(2), ())
            self.assertEqual(res.node(0).size,
                             alloc["m1.bin"] + alloc["r0.bin"] + alloc["mid/l1.bin"])
            self.assertEqual(res.node(1).size, alloc["mid/l1.bin"])
            self.assertEqual(res.node(2).size, 0)           # dir inodes count zero bytes
            self.assertEqual(res.node(0).file_count, 3)     # recursive counts
            self.assertEqual(res.node(1).file_count, 1)
            self.assertEqual(res.node(0).dir_count, 2)      # mid + leaf, strictly below
            self.assertEqual(res.node(1).dir_count, 1)
            self.assertEqual(res.node(2).dir_count, 0)
            self.assertEqual(res.node(0).apparent_size, 6000)
            # root's direct files ranked by the scan metric (ties break by name desc)
            direct = [("m1.bin", alloc["m1.bin"], 3000), ("r0.bin", alloc["r0.bin"], 1000)]
            self.assertEqual(res.node(0).top_files,
                             tuple(sorted(direct, key=lambda t: (t[1], t[0]),
                                          reverse=True)))
            self.assertEqual(res.node(1).top_files,
                             (("l1.bin", alloc["mid/l1.bin"], 2000),))
            # pinned identity: dir.size == sum(child.size) + direct top-file bytes
            self.assertEqual(res.node(0).size - res.node(1).size
                             - sum(s for _, s, _ in res.node(0).top_files), 0)
            self.assertEqual(res.node(1).size - res.node(2).size
                             - sum(s for _, s, _ in res.node(1).top_files), 0)
            self.assertEqual(res.path(0), str(root))
            self.assertEqual(res.path(2), os.path.join(str(root), "mid", "leaf"))
            self.assertIsNotNone(res.node(0).mtime)

    def test_symlinks_never_followed_or_counted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "scan"
            target = Path(d) / "outside"
            mkfile(target / "t1.bin", 500)
            mkfile(root / "real.bin", 1000)
            os.symlink(target, root / "dirlink")
            os.symlink(root / "real.bin", root / "filelink")
            os.symlink(Path(d) / "nope", root / "dangling")

            res = scan_path(str(root))
            alloc = metric_of(root / "real.bin")
            self.assertEqual(res.node(0).size, alloc)       # links contribute nothing
            self.assertEqual(res.node(0).file_count, 1)
            self.assertEqual(res.totals["dirs"], 0)         # dirlink not descended
            self.assertEqual([n.name for n in res.nodes], ["scan"])
            self.assertEqual(res.node(0).top_files, (("real.bin", alloc, 1000),))

    def test_unreadable_root_sets_error_and_no_nodes(self):
        """scan_path error path: unreadable/nonexistent root -> error, no nodes."""
        res = scan_path(str(Path("/nonexistent/asusius-probe-missing-dir")))
        self.assertTrue(res.error)                       # error surfaced, not raised
        self.assertEqual(res.nodes, [])                  # empty nodes
        for key in ("bytes", "apparent", "files", "dirs"):
            self.assertEqual(res.totals[key], 0, key)    # totals all zero
        self.assertTrue(res.stats.get("duration_s") is not None)

    def test_not_a_directory_root_sets_error(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "plain.txt"
            f.write_text("x")
            res = scan_path(str(f))
            self.assertIn("not a directory", res.error)
            self.assertEqual(res.nodes, [])


class MountBoundaryTests(unittest.TestCase):
    def test_skips_same_device_mount_but_allows_selecting_it_directly(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            mount = root / "mounted folder"
            mkfile(mount / "inside", 1024)
            escaped = mount.as_posix().replace(" ", r"\040")
            mountinfo = f"27 26 8:1 /source {escaped} rw - ext4 /dev/sda rw"
            with patch("builtins.open", unittest.mock.mock_open(read_data=mountinfo)):
                result = scan_path(d)
                self.assertEqual(result.stats["mounts_skipped"], 1)
                self.assertEqual(result.totals["files"], 0)
                self.assertEqual(scan_path(str(mount)).totals["files"], 1)

    """Subdirectories on a different st_dev are neither descended into nor nodes."""

    FOREIGN_DEV = 987654321

    def test_foreign_st_dev_subdir_is_never_descended(self):
        with tempfile.TemporaryDirectory() as d:
            scan_root = Path(d) / "scan"
            mkfile(scan_root / "real/f.bin", 5000)
            mkfile(scan_root / "other_fs/inner/g.bin", 9999)
            f_alloc = metric_of(scan_root / "real/f.bin")

            patched = _with_foreign_dev(str(scan_root), "other_fs", self.FOREIGN_DEV)
            with patch("os.scandir", patched):
                res = scan_path(str(scan_root))
            self.assertEqual([n.name for n in res.nodes], ["scan", "real"])
            self.assertEqual(res.children(0), (1,))         # other_fs is not a node
            self.assertEqual(res.stats["walked_dirs"], 2)   # root + real only
            self.assertEqual(res.node(0).size, f_alloc)     # g.bin never counted
            self.assertEqual(res.node(0).file_count, 1)
            self.assertEqual(res.totals["files"], 1)
            self.assertEqual(res.totals["dirs"], 1)
            self.assertIsNone(res.error)

    def test_scan_root_on_the_foreign_device_itself_works(self):
        with tempfile.TemporaryDirectory() as d:
            scan_root = Path(d) / "scan"
            mkfile(scan_root / "other_fs/inner/g.bin", 9999)
            g_alloc = metric_of(scan_root / "other_fs/inner/g.bin")
            # the pinned rule: scanning that mount point directly works (root always
            # matches its own dev) — no patching needed here
            res = scan_path(str(_join(scan_root, "other_fs")))
            self.assertEqual([n.name for n in res.nodes], ["other_fs", "inner"])
            self.assertEqual(res.node(0).size, g_alloc)
            self.assertEqual(res.totals["files"], 1)
            self.assertIsNone(res.error)


class CancelTests(unittest.TestCase):
    """Cancellation stops the walk promptly and returns a deterministic partial tree."""

    def test_cancel_mid_scan_via_progress_callback(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "scan"
            mkfile(root / "a/x.bin", 5000)
            mkfile(root / "a/y.bin", 6000)
            mkfile(root / "b/z.bin", 7000)
            mkfile(root / "f0.bin", 4000)
            f0_alloc = metric_of(root / "f0.bin")
            calls = []
            event = threading.Event()

            def on_progress(dirs_done, bytes_done):
                calls.append((dirs_done, bytes_done))
                if dirs_done >= 1:
                    event.set()  # cancel as soon as the first directory is walked

            with patch.object(da, "PROGRESS_EVERY_DIRS", 1), \
                    patch.object(da, "PROGRESS_MIN_INTERVAL_S", 0.0):
                res = scan_path(str(root), cancel_event=event, progress_cb=on_progress)

            self.assertTrue(res.stats["cancelled"])
            self.assertEqual(res.stats["walked_dirs"], 1)   # stopped after the root
            self.assertEqual(len(res.nodes), 3)             # root + discovered a, b
            self.assertEqual(res.node(0).child_dir_indices, (1, 2))
            self.assertEqual(res.node(0).size, f0_alloc)    # direct file only
            self.assertEqual(res.node(0).file_count, 1)
            self.assertEqual(res.node(0).dir_count, 2)      # both dirs were discovered
            self.assertEqual((res.node(1).size, res.node(2).size), (0, 0))  # leaves
            self.assertEqual(calls, [(1, f0_alloc)])        # one throttled fire
            self.assertIsNone(res.error)

    def test_cancel_before_scan(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "scan"
            mkfile(root / "a/x.bin", 5000)
            event = threading.Event()
            event.set()
            res = scan_path(str(root), cancel_event=event)
            self.assertTrue(res.stats["cancelled"])
            self.assertEqual(res.stats["walked_dirs"], 0)
            self.assertEqual([n.name for n in res.nodes], ["scan"])  # root only
            self.assertEqual(res.node(0).size, 0)
            self.assertEqual(res.totals, {"bytes": 0, "apparent": 0, "files": 0,
                                          "dirs": 0})


class HogsTests(unittest.TestCase):
    """Space-hog grouping: top-25 ranking, bucket tags, reclaimable hints."""

    def test_top_hogs_top25_buckets_and_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "scan"
            layout = []  # (relpath, apparent bytes), largest first
            for i in range(8):                            # 100..93 MiB, all hogs
                layout.append((f"big/f{i + 1}.mp4", (100 - i) << 20))
            for i in range(8):                            # 60..53 MiB, below threshold
                layout.append((f"mid/g{i + 1}.zip", (60 - i) << 20))
            for i in range(8):                            # 12..5 MiB
                layout.append((f"small/h{i + 1}.md", (12 - i) << 20))
            for i in range(8):                            # 8..1 KiB real bytes
                layout.append((f"tiny/i{i + 1}.py", (8 - i) << 10))
            for rel, size in layout:
                mkfile(_join(root, rel), size, sparse=size >= (1 << 20))

            res = scan_path(str(root), use_apparent=True)
            self.assertEqual(res.totals["files"], 32)
            hogs = res.top_hogs(limit=25)
            self.assertEqual(len(hogs), 25)
            self.assertEqual(len(res.top_hogs()), 10)     # pinned default limit
            expected = [str(_join(root, rel)) for rel, _size in layout]
            self.assertEqual([p for p, _s, _t in hogs], expected[:25])
            self.assertEqual([s for _p, s, _t in hogs],
                             [sz for _rel, sz in layout][:25])
            # UI-side hog threshold keeps exactly the eight .mp4 files
            over = [(p, t) for p, s, t in hogs if s >= HOG_MIN_BYTES]
            self.assertEqual(len(over), 8)
            self.assertTrue(all(p.endswith(".mp4") for p, _t in over))
            # every tag is one of the pinned buckets; mp4/zip/md/py cover four
            self.assertTrue(all(t in BUCKETS for _p, _s, t in hogs))
            self.assertEqual({t for _p, _s, t in hogs},
                             {"media", "archives", "docs", "code"})

    def test_top_files_capped_at_eight_per_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "scan"
            for i in range(1, 10):                        # 9 files, apparent 1..9 KiB
                mkfile(root / f"f{i}.bin", i << 10)
            res = scan_path(str(root), use_apparent=True)
            self.assertEqual(res.totals["files"], 9)
            top = res.node(0).top_files
            self.assertEqual(len(top), TOP_FILES_PER_DIR)
            self.assertEqual([n for n, _s, _a in top],
                             [f"f{i}.bin" for i in range(9, 1, -1)])  # desc by size
            self.assertEqual(res.top_hogs(limit=25),
                             [(str(_join(root, f"f{i}.bin")), i << 10, "binaries")
                              for i in range(9, 0, -1)])

    def test_reclaimable_tag_matrix(self):
        base = "/tmp/asusius-hogs"
        cases = {
            f"{base}/var/lib/docker/overlay2/x/layer": "docker",
            f"{base}/.local/share/containers/storage/overlay/x": "podman",
            f"{base}/var/lib/snapd/snaps/core22.snap": "snap",
            f"{base}/var/lib/flatpak/app/org.x/x86_64/current": "flatpak",
            f"{base}/.local/share/flatpak/app/org.x": "flatpak",
            f"{base}/.var/app/org.x/cache": "flatpak",
            f"{base}/Downloads/setup.exe": "downloads",
            f"{base}/VMs/win10.qcow2": "vm-image",
            f"{base}/boxes/win.vdi": "vm-image",
            f"{base}/boxes/box.vhd": "vm-image",
            f"{base}/swapfile": "vm-image",
            f"{base}/swap.img": "vm-image",
            f"{base}/.cache/mozilla/cache.bin": "cache",
            f"{base}/home/user/foo/cache": "cache",
            f"{base}/proj/node_modules/leftpad": "node_modules",
            f"{base}/proj/__pycache__/mod.cpython-312.pyc": "build",
            f"{base}/proj/.mypy_cache/x": "build",
            f"{base}/proj/.pytest_cache/v/cache": "build",
            f"{base}/proj/.tox/py312": "build",
            f"{base}/Documents/report.pdf": None,
            f"{base}/README": None,
            # case-insensitive on every component
            f"{base}/VAR/LIB/DOCKER/layer": "docker",
            f"{base}/proj/NODE_MODULES/leftpad": "node_modules",
            f"{base}/Downloads/WIN10.QCOW2": "vm-image",
        }
        for path, want in cases.items():
            with self.subTest(path=path):
                self.assertEqual(reclaimable_tag(path), want)

    def test_bucket_matrix(self):
        cases = {"photo.JPG": "media", "archive.tar.gz": "archives",
                 "lib.so": "binaries", "script.sh": "code", "notes.txt": "docs",
                 "pkg.deb": "binaries", "disk.iso": "archives", "song.flac": "media",
                 "unknown.xyz": "other", "Makefile": "other", ".hidden": "other",
                 "dot.dir.name": "other"}
        for name, want in cases.items():
            with self.subTest(name=name):
                self.assertEqual(bucket(name), want)

    def test_fake_home_fixture_deterministic(self):
        """The FAKE_HOME demo tree scans to the same numbers every time."""
        with tempfile.TemporaryDirectory() as d:
            root = build_fake_tree(Path(d) / "root")
            fake.ensure(root)
            home = Path(root) / fake.FAKE_HOME

            res = scan_path(str(home))
            self.assertIsNone(res.error)
            self.assertEqual(res.totals["apparent"],
                             sum(sz for _rel, sz, _sp in fake.FAKE_HOME_FILES))
            names = [rel.rsplit("/", 1)[-1] for rel, _sz, _sp in fake.FAKE_HOME_FILES]
            dirs = set()
            for rel, _sz, _sp in fake.FAKE_HOME_FILES + (("dup2.bin", 0, False),):
                parts = rel.split("/")
                dirs.update("/".join(parts[:k]) for k in range(1, len(parts)))
            self.assertEqual(res.totals["files"], len(set(names)))  # dup2 deduped
            self.assertEqual(res.totals["dirs"], len(dirs))
            # sparse files dominate: allocated bytes stay far below apparent
            self.assertLess(res.totals["bytes"], res.totals["apparent"])

            res_a = scan_path(str(home), use_apparent=True)
            hogs = {p.rsplit("/", 1)[-1] for p, s, _t in res_a.top_hogs(limit=25)
                    if s >= HOG_MIN_BYTES}
            expected = {rel.rsplit("/", 1)[-1] for rel, sz, _sp in fake.FAKE_HOME_FILES
                        if sz >= HOG_MIN_BYTES}
            self.assertEqual(hogs, expected)
            # every dir has <= TOP_FILES_PER_DIR direct files, so all files compete
            self.assertEqual(len(res_a.top_hogs(limit=25)), len(set(names)))
            # the hardlink pair is counted once: dup2 appears nowhere
            all_names = {n for node in res_a.nodes for n, _s, _a in node.top_files}
            self.assertNotIn("dup2.bin", all_names)
            self.assertIn("dup1.bin", all_names)
            by_name = {rel.rsplit("/", 1)[-1]: rel for rel, _sz, _sp
                       in fake.FAKE_HOME_FILES}
            for name, want in (("ubuntu-24.04.iso", "downloads"),
                               ("leftpad.node", "node_modules"),
                               ("main.cpython-311.pyc", "build"),
                               ("cache.bin", "cache"),
                               ("win10.qcow2", "vm-image"),
                               ("swap.img", "vm-image"),
                               ("report.pdf", None)):
                with self.subTest(fixture=name):
                    self.assertEqual(reclaimable_tag(str(_join(home, by_name[name]))),
                                     want)

            again = scan_path(str(home))
            self.assertEqual(again.totals, res.totals)      # deterministic re-scan
