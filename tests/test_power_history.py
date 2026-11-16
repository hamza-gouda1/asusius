# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""History normalisation and the local voltage store: tmp dirs, injected clock,
no D-Bus, no sleeps."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from asusius.telemetry.power_history import (
    HistoryPoint,
    LocalVoltageStore,
    device_key,
    normalize_history,
)
from asusius.telemetry.upower import UpowerHistoryPoint

BAT = "/sys/devices/LNXSYSTM:00/power_supply/BAT0"


class NormalizeTests(unittest.TestCase):
    def test_triples_are_sorted_and_mapped_to_percent(self):
        rows = [(100, 50.0, 2), (60, 60.0, 1)]
        pts = normalize_history(rows, series="charge")
        self.assertEqual([(p.t, p.percent, p.state) for p in pts],
                         [(60.0, 60.0, "charging"), (100.0, 50.0, "discharging")])
        self.assertEqual([p.watts for p in pts], [None, None])

    def test_series_maps_the_value_field(self):
        self.assertEqual(normalize_history([(1, 9.5, 2)], series="rate")[0].watts, 9.5)
        self.assertEqual(
            normalize_history([(1, 11.1, 2)], series="voltage")[0].voltage, 11.1)
        self.assertIsNone(normalize_history([(1, 9.5, 2)], series="rate")[0].percent)

    def test_pairs_read_as_unknown_state(self):
        pts = normalize_history([(10, 87.5), (11, 80.0)])
        self.assertEqual([(p.t, p.percent, p.state) for p in pts],
                         [(10.0, 87.5, None), (11.0, 80.0, None)])

    def test_upower_history_points_map_by_series(self):
        for series, field in (("charge", "percent"), ("rate", "watts")):
            pts = normalize_history([UpowerHistoryPoint(t=1, value=42.0, state=1)],
                                    series=series)
            self.assertEqual(getattr(pts[0], field), -42.0 if series == "rate" else 42.0)
            self.assertEqual(pts[0].state, "charging")

    def test_history_point_objects_pass_through(self):
        pts = normalize_history([HistoryPoint(t=5.0, percent=10.0, watts=1.0,
                                              voltage=2.0, state="fully-charged")])
        self.assertEqual(pts[0], HistoryPoint(t=5.0, percent=10.0, watts=None,
                                              voltage=2.0, state="fully-charged"))

    def test_dicts_with_named_fields_or_value(self):
        pts = normalize_history([{"t": 1, "voltage": 3.7, "percent": 55.0,
                                  "state": "discharging"}])
        self.assertEqual((pts[0].voltage, pts[0].percent, pts[0].state),
                         (3.7, 55.0, "discharging"))
        pts = normalize_history([{"t": 2, "value": 9.9}], series="rate")
        self.assertEqual((pts[0].watts, pts[0].percent), (9.9, None))

    def test_non_finite_values_become_none(self):
        rows = [(1, float("nan"), 2), (2, float("inf"), 2), (3, -5.0, 2),
                (4, True, 2), (5, 60.0, 2)]
        pts = normalize_history(rows, series="charge")
        self.assertEqual([p.percent for p in pts], [None, None, None, None, 60.0])

    def test_rows_without_a_finite_timestamp_are_dropped(self):
        pts = normalize_history([(float("nan"), 1.0, 2), (float("inf"), 2.0, 2),
                                 ("x", 3.0, 2), (10, 3.0, 2)])
        self.assertEqual([(p.t, p.percent) for p in pts], [(10.0, 3.0)])

    def test_duplicate_timestamps_keep_the_first(self):
        pts = normalize_history([(10, 1.0, 2), (10, 2.0, 2), (11, 3.0, 2)])
        self.assertEqual([(p.t, p.percent) for p in pts], [(10.0, 1.0), (11.0, 3.0)])

    def test_non_monotonic_clock_jump_sorts_and_dedupes(self):
        # a clock stepped back produces out-of-order input: sort handles it and the
        # row first seen for a timestamp wins
        pts = normalize_history([(20, 1.0, 2), (10, 2.0, 2), (20, 3.0, 2)])
        self.assertEqual([(p.t, p.percent) for p in pts], [(10.0, 2.0), (20.0, 1.0)])

    def test_gaps_stay_gaps(self):
        pts = normalize_history([HistoryPoint(t=1, percent=50.0), HistoryPoint(t=2),
                                 HistoryPoint(t=3, percent=40.0)])
        self.assertEqual([p.percent for p in pts], [50.0, None, 40.0])  # no interpolation

    def test_empty_and_junk_input(self):
        self.assertEqual(normalize_history(None), [])
        self.assertEqual(normalize_history([]), [])
        self.assertEqual(normalize_history(["junk", 42, None]), [])

    def test_unknown_series_is_a_caller_bug(self):
        with self.assertRaises(ValueError):
            normalize_history([(1, 2.0, 2)], series="power")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name) / "voltage"
        self.addCleanup(self._tmp.cleanup)
        self.now = 1_700_000_000.0
        self.store = LocalVoltageStore(base_dir=self.base,
                                       clock=lambda: self.now)

    def rec(self, t, voltage=7.4, percent=88.0, state="discharging", model="M",
            store=None):
        return (store or self.store).record(BAT, model, voltage, percent, state, t=t)

    def test_record_and_load_roundtrip(self):
        self.assertTrue(self.rec(100.0, voltage=7.41, percent=88.5))
        pts = self.store.load(BAT, "M")
        self.assertEqual(pts, [HistoryPoint(t=100.0, voltage=7.41, percent=88.5,
                                            state="discharging")])
        self.assertEqual(self.store.path_for(BAT, "M"),
                         self.base / f"{device_key(BAT, 'M')}.json")

    def test_sampling_guard_rejects_without_file_churn(self):
        for kwargs in ({"voltage": None}, {"percent": None},
                       {"state": "charging"}, {"state": None},
                       {"voltage": float("nan")}, {"percent": float("inf")}):
            self.assertFalse(self.rec(100.0, **kwargs))
            self.assertEqual(os.listdir(self.base) if self.base.exists() else [], [])

    def test_genuine_zeros_are_kept(self):
        self.assertTrue(self.rec(100.0, voltage=0.0, percent=0.0))
        pts = self.store.load(BAT, "M")
        self.assertEqual((pts[0].voltage, pts[0].percent), (None, 0.0))

    def test_injected_clock_used_when_t_omitted(self):
        self.now = 1234.5
        self.assertTrue(self.store.record(BAT, "M", 7.0, 50.0, "discharging"))
        self.assertEqual(self.store.load(BAT, "M")[0].t, 1234.5)

    def test_retention_keeps_the_newest_cap(self):
        small = LocalVoltageStore(base_dir=self.base, max_points=5)
        for t in range(1, 8):
            self.assertTrue(self.rec(float(t), voltage=float(t), store=small))
        pts = small.load(BAT, "M")
        self.assertEqual([p.t for p in pts], [3.0, 4.0, 5.0, 6.0, 7.0])
        self.assertEqual([p.voltage for p in pts], [3.0, 4.0, 5.0, 6.0, 7.0])

    def test_duplicate_timestamp_keeps_the_first_voltage(self):
        self.assertTrue(self.rec(10.0, voltage=1.0))
        self.assertTrue(self.rec(10.0, voltage=2.0))
        pts = self.store.load(BAT, "M")
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0].voltage, 1.0)

    def test_corrupt_file_is_treated_as_empty_and_rebuilt(self):
        self.rec(100.0)
        self.store.path_for(BAT, "M").write_text("{not json at all", encoding="utf-8")
        self.assertEqual(self.store.load(BAT, "M"), [])
        self.assertTrue(self.rec(200.0, voltage=7.0))
        pts = self.store.load(BAT, "M")  # rebuilt: older rows are lost, not wrong
        self.assertEqual([(p.t, p.voltage) for p in pts], [(200.0, 7.0)])

    def test_non_list_json_is_treated_as_empty(self):
        self.rec(100.0)
        self.store.path_for(BAT, "M").write_text("42", encoding="utf-8")
        self.assertEqual(self.store.load(BAT, "M"), [])

    def test_load_returns_sorted_points(self):
        for t in (300.0, 100.0, 200.0):
            self.rec(t, voltage=t / 100)
        self.assertEqual([p.t for p in self.store.load(BAT, "M")],
                         [100.0, 200.0, 300.0])

    def test_absent_device_stops_cleanly(self):
        self.rec(100.0)
        path = self.store.path_for(BAT, "M")
        before = (path.stat().st_mtime_ns, sorted(os.listdir(self.base)))
        # a device that reports nothing useful (e.g. unplugged, or gone) writes nothing
        self.assertFalse(self.rec(200.0, state="charging"))
        self.assertFalse(self.store.load(BAT + "-gone", "M"))
        after = (path.stat().st_mtime_ns, sorted(os.listdir(self.base)))
        self.assertEqual(before, after)

    def test_atomic_write_leaves_no_tmp_files(self):
        self.rec(100.0)
        self.assertEqual(sorted(os.listdir(self.base)),
                         sorted([f"{device_key(BAT, 'M')}.json",
                                 f"{device_key(BAT, 'M')}.json.lock"]))

    def test_content_never_carries_serials(self):
        self.rec(100.0, model="ModelX")
        raw = self.store.path_for(BAT, "ModelX").read_text(encoding="utf-8")
        rows = json.loads(raw)
        self.assertEqual(rows, [{"t": 100.0, "voltage": 7.4, "percent": 88.0,
                                 "state": "discharging"}])
        self.assertEqual(set(rows[0]), {"t", "voltage", "percent", "state"})

    def test_key_is_a_hash_of_native_path_and_model_only(self):
        self.assertEqual(device_key(BAT, "M1"), device_key(BAT, "M1"))
        self.assertNotEqual(device_key(BAT, "M1"), device_key(BAT, "M2"))
        self.assertNotEqual(device_key(BAT, "M1"), device_key(BAT + "1", "M1"))
        name = f"{device_key(BAT, 'M1')}.json"
        self.assertRegex(name, r"^[0-9a-f]{16}\.json$")  # hashed, not raw paths

    def test_same_path_different_model_separate_files(self):
        self.assertTrue(self.store.record(BAT, "A", 7.4, 88.0, "discharging", t=1.0))
        self.assertTrue(self.store.record(BAT, "B", 7.5, 88.0, "discharging", t=1.0))
        self.assertNotEqual(self.store.path_for(BAT, "A"), self.store.path_for(BAT, "B"))
        self.assertEqual(self.store.load(BAT, "A")[0].voltage, 7.4)
        self.assertEqual(self.store.load(BAT, "B")[0].voltage, 7.5)

    def test_default_base_honours_xdg_state_home(self):
        saved = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(self.base.parent)
        try:
            store = LocalVoltageStore()
        finally:
            if saved is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = saved
        self.assertEqual(store.path_for(BAT, "M"),
                         self.base.parent / "asusius" / "voltage" /
                         f"{device_key(BAT, 'M')}.json")


if __name__ == "__main__":
    unittest.main()
