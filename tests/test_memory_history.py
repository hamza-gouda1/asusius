# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""MemHistory: the Memory page's 360-slot ring — allowlist, throttle, rates, bounds."""
import math
import unittest

from asusius.telemetry.memory_history import ALLOWED, PAGE, MemHistory


def mem(**over):
    """An info["mem"]-shaped dict with every field valid (float32-exact values)."""
    m = {
        "anon": 1.0e9, "file": 2.0e9, "shmem": 3.0e8, "slab": 4.0e8,
        "in_use": 6.0e9, "available": 7.0e9, "total": 16.0e9,
        "psi": {"some": {"avg10": 0.5, "avg60": 0.2}, "full": {"avg10": 0.1}},
        "swap_used": 5.0e8,
    }
    m.update(over)
    return m


class MemHistoryTests(unittest.TestCase):
    def test_allowlist_and_delegation(self):
        h = MemHistory()
        h.commit(0.0, {**mem(), "bogus": 123, "psi_some": 9.0})  # flat psi is ignored
        self.assertEqual(set(h.store.series), ALLOWED)
        self.assertNotIn("bogus", h.store.series)
        self.assertEqual(h.last("psi_some"), 0.5)  # taken from the nested dict
        self.assertEqual(h.count, 1)
        self.assertEqual(h.last_t, 0.0)
        self.assertEqual(h.last("anon"), 1.0e9)
        self.assertEqual(h.capacity, 360)
        self.assertEqual(h.nbytes(), h.store.nbytes())
        self.assertEqual(list(h.index_range(0)), [0])
        with self.assertRaises(AttributeError):
            h.no_such_attribute  # delegation passes unknown names through

    def test_throttle_and_cadence_parity(self):
        h = MemHistory()
        h.commit(0.0, mem())
        h.commit(0.5, mem())  # same second: dropped
        self.assertEqual(h.count, 1)
        h.commit(1.0, mem())  # next second: committed
        self.assertEqual(h.count, 2)
        # 2 Hz ticks for 300 s commit every other one: >= 300 samples in the window
        h2 = MemHistory()
        for i in range(600):
            h2.commit(i * 0.5, mem())
        self.assertGreaterEqual(h2.count, 300)
        # 1 Hz ticks all commit: 300 samples for 300 s
        h3 = MemHistory()
        for i in range(300):
            h3.commit(float(i), mem())
        self.assertEqual(h3.count, 300)
        for i in range(400, 800):
            h3.commit(float(i), mem())
        self.assertEqual(h3.count, 360)  # the ring itself never exceeds 360 slots

    def test_rate_math_with_page_size_and_elapsed(self):
        h = MemHistory()
        h.commit(10.0, {**mem(), "pswpin": 1000, "pswpout": 500, "vmstat_t": 10.0})
        self.assertIsNone(h.last("pswpin_bps"))  # first sample: gap
        self.assertIsNone(h.last("pswpout_bps"))
        h.commit(12.0, {**mem(), "pswpin": 2000, "pswpout": 1100, "vmstat_t": 12.0})
        self.assertEqual(h.last("pswpin_bps"), 1000 * PAGE / 2.0)
        self.assertEqual(h.last("pswpout_bps"), 600 * PAGE / 2.0)
        # a different real elapsed divides the same delta differently
        h2 = MemHistory()
        h2.commit(10.0, {**mem(), "pswpin": 0, "pswpout": 0, "vmstat_t": 10.0})
        h2.commit(11.5, {**mem(), "pswpin": 300, "pswpout": 150, "vmstat_t": 11.5})
        self.assertEqual(h2.last("pswpin_bps"), 300 * PAGE / 1.5)
        self.assertEqual(h2.last("pswpout_bps"), 150 * PAGE / 1.5)

    def test_zero_traffic_is_a_real_zero(self):
        h = MemHistory()
        h.commit(10.0, {**mem(), "pswpin": 100, "pswpout": 0, "vmstat_t": 10.0})
        h.commit(11.0, {**mem(), "pswpin": 100, "pswpout": 0, "vmstat_t": 11.0})
        self.assertEqual(h.last("pswpin_bps"), 0.0)
        self.assertEqual(h.last("pswpout_bps"), 0.0)

    def test_rebaseline_on_decrease(self):
        h = MemHistory()
        h.commit(10.0, {**mem(), "pswpin": 1000, "pswpout": 0, "vmstat_t": 10.0})
        h.commit(11.0, {**mem(), "pswpin": 500, "pswpout": 0, "vmstat_t": 11.0})
        self.assertIsNone(h.last("pswpin_bps"))  # counter reset: gap
        h.commit(12.0, {**mem(), "pswpin": 1500, "pswpout": 0, "vmstat_t": 12.0})
        # the baseline was re-set to 500 at t=11, so this rate spans 11 -> 12
        self.assertEqual(h.last("pswpin_bps"), 1000 * PAGE / 1.0)

    def test_rebaseline_after_suspend_gap(self):
        h = MemHistory()
        h.commit(10.0, {**mem(), "pswpin": 0, "pswpout": 0, "vmstat_t": 10.0})
        h.commit(20.0, {**mem(), "pswpin": 1000, "pswpout": 0, "vmstat_t": 20.0})
        self.assertIsNone(h.last("pswpin_bps"))  # dt 10 s > 2.0: gap + re-baseline
        h.commit(21.0, {**mem(), "pswpin": 2000, "pswpout": 0, "vmstat_t": 21.0})
        self.assertEqual(h.last("pswpin_bps"), 1000 * PAGE / 1.0)

    def test_rebaseline_on_reopen_even_within_the_normal_rate_interval(self):
        h = MemHistory()
        h.commit(10.0, mem(pswpin=100, vmstat_epoch=1))
        h.commit(11.0, mem(pswpin=200, vmstat_epoch=3))
        self.assertIsNone(h.last("pswpin_bps"))
        h.commit(12.0, mem(pswpin=300, vmstat_epoch=3))
        self.assertEqual(h.last("pswpin_bps"), 100 * PAGE)

    def test_clear_accepts_a_new_clock_and_discards_the_rate_baseline(self):
        h = MemHistory()
        h.commit(10.0, mem(pswpin=100))
        h.clear()
        h.commit(9.0, mem(pswpin=200))
        self.assertEqual(h.count, 1)
        self.assertEqual(h.last_t, 9.0)
        self.assertIsNone(h.last("pswpin_bps"))

    def test_absent_counters_retain_baseline(self):
        h = MemHistory()
        h.commit(10.0, {**mem(), "pswpin": 100, "pswpout": 50, "vmstat_t": 10.0})
        h.commit(11.0, mem())  # reader skipped vmstat: rates gap, baseline retained
        self.assertIsNone(h.last("pswpin_bps"))
        self.assertIsNone(h.last("pswpout_bps"))
        # the next actual read measures between the actual reads (10 -> 12, dt 2.0)
        h.commit(12.0, {**mem(), "pswpin": 400, "pswpout": 50, "vmstat_t": 12.0})
        self.assertEqual(h.last("pswpin_bps"), 300 * PAGE / 2.0)
        self.assertEqual(h.last("pswpout_bps"), 0.0)

    def test_unreadable_counter_snapshot_clears_rate_baseline(self):
        h = MemHistory()
        h.commit(10.0, mem(pswpin=100, vmstat_t=10.0))
        h.commit(11.0, mem(vmstat_t=11.0))
        h.commit(12.0, mem(pswpin=300, vmstat_t=12.0))
        self.assertIsNone(h.last("pswpin_bps"))

    def test_missing_fields_become_nan_gaps(self):
        h = MemHistory()
        h.commit(0.0, {"total": 16.0e9})
        for key in ALLOWED:
            if key != "total":
                self.assertIsNone(h.last(key), key)
        self.assertEqual(h.last("total"), 16.0e9)
        self.assertTrue(math.isnan(h.values("anon")[0][1]))
        # nested PSI: a missing "some" line leaves only psi_full
        h.commit(1.0, {"psi": {"full": {"avg10": 0.25}}})
        self.assertIsNone(h.last("psi_some"))
        self.assertEqual(h.last("psi_full"), 0.25)
        # a dict without psi at all (older callers/tests) is fine: both gap
        h.commit(2.0, {"swap_used": 0})
        self.assertIsNone(h.last("psi_full"))
        self.assertEqual(h.last("swap_used"), 0.0)
        self.assertEqual(h.count, 3)
        # a measured PSI zero lands as a real 0.0, never a NaN gap
        h.commit(3.0, {"psi": {"some": {"avg10": 0.0}, "full": {"avg10": 0.0}}})
        self.assertEqual(h.last("psi_some"), 0.0)
        self.assertEqual(h.last("psi_full"), 0.0)
        self.assertEqual(h.count, 4)

    def test_bounds_and_wrap(self):
        h = MemHistory()
        for i in range(400):
            h.commit(float(i), mem())
        self.assertEqual(h.count, 360)
        self.assertEqual(h.last_t, 399.0)
        self.assertEqual(h.times[0], 360.0)  # the first 40 samples aged out
        nbytes = h.nbytes()
        self.assertLessEqual(nbytes, 26 * 1024)
        self.assertEqual(nbytes, 360 * 8 + 13 * 360 * 4)  # float32 series + times

    def test_store_drops_non_monotonic_pushes(self):
        h = MemHistory()
        h.commit(5.0, mem())
        h.commit(5.0, mem())  # the throttle skips same-instant commits
        h.commit(4.0, mem())  # ...and earlier ones
        self.assertEqual(h.count, 1)
        h.store.push(4.0, {"x": 1.0})  # Store's own guard is the last line of defence
        self.assertEqual(h.count, 1)
        self.assertNotIn("x", h.store.series)


if __name__ == "__main__":
    unittest.main()
