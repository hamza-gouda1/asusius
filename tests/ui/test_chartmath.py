# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Chart maths without a display: ticks, axis spans, windowing, gaps, no-overshoot curves."""
import math
import random
import unittest
from array import array

from asusius.ui.chartmath import (
    GAP_FACTOR,
    axis_span,
    bezier_controls,
    monotone_tangents,
    nice_range,
    nice_step,
    segments,
    window_points,
)


class ChartMathTests(unittest.TestCase):
    def test_nice_ticks(self):
        self.assertEqual(nice_step(100, 4), 25)
        self.assertEqual(nice_step(15.6, 3), 10)
        lo, hi, step = nice_range(43.0, 71.0, floor=20, min_span=20)
        self.assertLessEqual(lo, 43)
        self.assertGreaterEqual(hi, 71)
        self.assertGreaterEqual(lo, 20)
        self.assertAlmostEqual((hi - lo) / step, round((hi - lo) / step))
        self.assertEqual(nice_range(0, 0, floor=0, min_span=1)[0], 0)

    def _series(self, times, vals):
        t = array("d", times)
        v = array("f", vals)
        return t, v, range(len(times)), (lambda k: k)

    def test_window_and_gaps(self):
        t, v, idx, slot = self._series([0, 1, 2, 3, 10, 11], [1, 2, float("nan"), 4, 5, 6])
        xs, ys = [], []
        window_points(t, v, idx, slot, 0.0, 11.0, 1.0, xs, ys, bucket_px=0.001)
        runs = segments(xs, ys)
        # NaN sample and the 7 s pause both break the line
        self.assertEqual([(ys[a], ys[b - 1]) for a, b in runs], [(1, 2), (4, 4), (5, 6)])
        self.assertAlmostEqual(xs[-1], 1.0)

    def test_buckets_keep_peaks(self):
        n = 100
        vals = [0.0] * n
        vals[37] = 99.0
        t, v, idx, slot = self._series(list(range(n)), vals)
        xs, ys = [], []
        window_points(t, v, idx, slot, 0.0, n - 1.0, 10.0, xs, ys, bucket_px=1.0)
        self.assertLessEqual(len(xs), 12)
        self.assertEqual(max(ys), 99.0)  # a one-sample spike survives downsampling

    def test_monotone_curve_never_overshoots(self):
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [0.0, 0.0, 100.0, 100.0, 100.0]
        m = monotone_tangents(xs, ys, 0, len(xs))
        self.assertEqual(m[0], 0.0)
        segs = bezier_controls(xs, ys, 0, len(xs), lambda v: v)
        for x1, y1, x2, y2, x3, y3 in segs:  # control points stay inside [0, 100]
            for y in (y1, y2, y3):
                self.assertTrue(-1e-9 <= y <= 100 + 1e-9)
        self.assertEqual(segs[-1][4:], (4.0, 100.0))  # passes through the last sample
        self.assertTrue(math.isfinite(sum(m)))


class AxisSpanTests(unittest.TestCase):
    """axis_span: the pure counterpart of Chart._draw's axis block."""

    def test_fixed_axis_stays_exact_below_ceiling(self):
        self.assertEqual(axis_span((0, 100), 0.0, 1.0, 4, 0.0, 87.0, None), (0, 100, 25.0))
        self.assertEqual(axis_span((0, 100), 0.0, 1.0, 4, 0.0, 100.0, None), (0, 100, 25.0))

    def test_fixed_axis_widens_when_a_value_would_clip(self):
        ymin, ymax, step = axis_span((0, 100), 0.0, 1.0, 4, 0.0, 137.0, None)
        self.assertEqual(ymin, 0)
        self.assertGreater(ymax, 100)
        self.assertGreaterEqual(ymax, 137)  # the spike is never clipped
        self.assertAlmostEqual((ymax - ymin) / step, round((ymax - ymin) / step))

    def test_fixed_axis_ignores_seen(self):
        # a fixed axis has no _seen state: history must not widen it
        self.assertEqual(axis_span((0, 100), 0.0, 1.0, 4, 0.0, 50.0, (0.0, 500.0)),
                         (0, 100, 25.0))

    def test_degenerate_empty_y_fixed_takes_the_free_axis(self):
        # parity with the old inline truthiness check: () is falsy, so no unpack
        self.assertEqual(axis_span((), 0.0, 1.0, 4, 0.0, 87.0, None),
                         axis_span(None, 0.0, 1.0, 4, 0.0, 87.0, None))
        self.assertEqual(axis_span((), 0.0, 1.0, 4, 0.0, 12.0, (0.0, 30.0)),
                         axis_span(None, 0.0, 1.0, 4, 0.0, 12.0, (0.0, 30.0)))

    def test_free_axis_widens_monotonically_and_never_shrinks(self):
        seen = None
        prev = None
        for lo, hi in [(0.0, 30.0), (0.0, 12.0), (4.0, 80.0), (0.0, 40.0), (0.0, 220.0),
                       (10.0, 60.0)]:
            ymin, ymax, step = axis_span(None, 0.0, 1.0, 4, lo, hi, seen)
            self.assertLess(ymin, ymax)
            self.assertGreater(step, 0.0)
            if prev is not None:
                self.assertLessEqual(ymin, prev[0])  # never moves down...
                self.assertGreaterEqual(ymax, prev[1])  # ...never shrinks
            self.assertAlmostEqual(step, nice_step(ymax - ymin, 4))  # live re-label
            prev = seen = (ymin, ymax)
        self.assertEqual(seen, (0.0, 300.0))  # the widest frame wins and stays

    def test_free_axis_relabels_against_the_widened_span(self):
        # a small frame keeps the widened axis and re-derives its ticks from it
        ymin, ymax, step = axis_span(None, 0.0, 1.0, 4, 0.0, 12.0, (0.0, 30.0))
        self.assertEqual((ymin, ymax), (0.0, 30.0))
        self.assertEqual(step, 10.0)  # for the 30-unit span, not the 12-unit data

    def test_free_axis_empty_data_falls_back_to_floor_and_min_span(self):
        ymin, ymax, step = axis_span(None, 0.0, 5.0, 4, math.inf, -math.inf, None)
        self.assertEqual(ymin, 0.0)
        self.assertGreaterEqual(ymax - ymin, 5.0)
        self.assertGreater(step, 0.0)

    def test_free_axis_honours_min_span_without_a_floor(self):
        ymin, ymax, step = axis_span(None, None, 1.0, 4, -3.0, -1.0, None)
        self.assertLessEqual(ymin, -3.0)
        self.assertGreaterEqual(ymax, -1.0)
        self.assertGreater(step, 0.0)

    def test_axis_span_is_pure_in_seen(self):
        seen = (0.0, 30.0)
        first = axis_span(None, 0.0, 1.0, 4, 0.0, 12.0, seen)
        self.assertEqual(axis_span(None, 0.0, 1.0, 4, 0.0, 12.0, seen), first)
        self.assertEqual(seen, (0.0, 30.0))  # the caller owns widening: it stores the result


class WindowGapTests(unittest.TestCase):
    """Gaps: missing samples are holes, never zeros; output stays O(pixels)."""

    def _run(self, times, vals, width=100.0, bucket_px=1.0, interval=1.0):
        t, v = array("d", times), array("f", vals)
        xs, ys = [], []
        n = window_points(t, v, range(len(times)), lambda k: k, 0.0, times[-1], width,
                          xs, ys, bucket_px=bucket_px, interval=interval)
        self.assertEqual(n, len(xs))
        self.assertEqual(len(xs), len(ys))
        return xs, ys

    @staticmethod
    def _has_nan(seq):
        return any(y != y for y in seq)  # NaN never equals itself

    def test_nan_sample_is_a_gap_marker_not_a_zero(self):
        xs, ys = self._run([0, 1, 2, 3, 4], [1, 2, float("nan"), 4, 5])
        self.assertTrue(self._has_nan(ys))
        runs = segments(xs, ys)
        self.assertEqual(len(runs), 2)
        self.assertEqual(ys[runs[0][0]:runs[0][1]], [1.0, 2.0])
        self.assertEqual(ys[runs[1][0]:runs[1][1]], [4.0, 5.0])

    def test_pause_at_gap_factor_boundary(self):
        # interval 1 s, GAP_FACTOR 2.6: a 2.5 s pause stays joined, 2.7 s breaks
        self.assertEqual(GAP_FACTOR, 2.6)
        xs, ys = self._run([0, 1, 2, 4.5, 5.5], [1, 2, 3, 4, 5])
        self.assertFalse(self._has_nan(ys))
        self.assertEqual(len(segments(xs, ys)), 1)
        xs, ys = self._run([0, 1, 2, 4.7, 5.7], [1, 2, 3, 4, 5])
        self.assertTrue(self._has_nan(ys))
        self.assertEqual(len(segments(xs, ys)), 2)

    def test_bound_32core_300samples(self):
        # 32 cores x 300 samples (missing readings as NaN, one > GAP_FACTOR pause):
        # per-core redraw cost stays O(pixels) — at most nb + 2*gaps + 2 points, for the
        # chart-style and CoreGrid-cell-style bucketing alike, however narrow the plot.
        rng = random.Random(7)
        gaps = 3
        nan_at = {90, 91, 92, 93, 180, 181}
        for _core in range(32):
            times = [float(i) for i in range(300)]
            for i in range(150, 300):
                times[i] += 5.0  # one suspended stretch: a 6 s pause entering at i == 150
            vals = [float("nan") if i in nan_at else rng.random() * 100 for i in range(300)]
            for width, bucket_px in ((300.0, 1.0), (160.0, 2.0), (60.0, 1.5)):
                nb = max(2, int(width / bucket_px))
                xs, ys = self._run(times, vals, width=width, bucket_px=bucket_px)
                self.assertEqual(sum(1 for y in ys if y != y), gaps)
                self.assertLessEqual(len(xs), nb + 2 * gaps + 2)


class NiceRangeEdgeTests(unittest.TestCase):
    def test_non_finite_bounds_fall_back_to_floor(self):
        lo, hi, step = nice_range(math.inf, -math.inf, floor=0.0, min_span=1.0)
        self.assertEqual((lo, hi), (0.0, 1.0))
        self.assertEqual(step, 0.25)

    def test_degenerate_span_expands_to_min_span(self):
        lo, hi, step = nice_range(5.0, 5.2, floor=None, min_span=1.0)
        self.assertLessEqual(lo, 5.0)
        self.assertGreaterEqual(hi - lo, 1.0)
        self.assertAlmostEqual(step, 0.25)

    def test_floor_lifts_a_negative_span(self):
        lo, hi, step = nice_range(-5.0, 5.0, floor=0.0, min_span=1.0)
        self.assertEqual(lo, 0.0)
        self.assertEqual(hi, 10.0)
        self.assertEqual(step, 2.5)


if __name__ == "__main__":
    unittest.main()
