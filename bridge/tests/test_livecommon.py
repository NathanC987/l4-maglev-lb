import os
import statistics
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import livecommon as lc  # noqa: E402


class TestServiceTime(unittest.TestCase):
    def test_gamma_matches_configured_mean_and_cv(self):
        rng = lc.make_rng(7)
        for mean, cv in ((25.0, 0.6), (100.0, 1.5), (5.0, 0.2)):
            xs = [lc.service_time_ms(rng, mean, cv) for _ in range(20000)]
            m = statistics.fmean(xs)
            got_cv = statistics.pstdev(xs) / m
            self.assertAlmostEqual(m, mean, delta=mean * 0.05)
            self.assertAlmostEqual(got_cv, cv, delta=cv * 0.05)

    def test_low_cv_is_deterministic(self):
        rng = lc.make_rng(1)
        self.assertEqual(lc.service_time_ms(rng, 40.0, 0.0), 40.0)
        self.assertEqual(lc.service_time_ms(rng, 40.0, 0.01), 40.0)


class TestHistogram(unittest.TestCase):
    def test_percentiles_within_bucket_width(self):
        h = lc.LogHistogram()
        for v in range(1, 1001):
            h.record(float(v))
        for q, want in ((0.5, 500), (0.95, 950), (0.99, 990)):
            got = lc.percentile(h.counts, q)
            self.assertAlmostEqual(got, want, delta=want * 0.08)

    def test_delta_is_sparse_and_resets(self):
        h = lc.LogHistogram()
        h.record(1.0)
        h.record(1.0)
        h.record(100.0)
        d = h.take_delta()
        self.assertEqual(sum(d.values()), 3)
        self.assertEqual(len(d), 2)
        self.assertEqual(h.take_delta(), {})
        merged = [0] * lc.HIST_BUCKETS
        lc.merge_into(merged, d)
        self.assertEqual(merged, h.counts)

    def test_empty_and_clamping(self):
        self.assertIsNone(lc.percentile([0] * lc.HIST_BUCKETS, 0.5))
        self.assertEqual(lc.bucket_of(0.0), 0)
        self.assertEqual(lc.bucket_of(1e12), lc.HIST_BUCKETS - 1)


class TestParsing(unittest.TestCase):
    def test_parse_command_rejects_bad_input(self):
        self.assertIsNone(lc.parse_command('{"op": "config", "rps": NaN}'))
        self.assertIsNone(lc.parse_command('[1, 2]'))
        self.assertIsNone(lc.parse_command('{"rps": 1}'))
        self.assertIsNone(lc.parse_command('not json'))
        self.assertEqual(lc.parse_command('{"op": "clear"}'), {"op": "clear"})

    def test_num_bounds_and_bools(self):
        self.assertEqual(lc.num({"x": 5}, "x", 0, 10, -1), 5.0)
        self.assertEqual(lc.num({"x": 11}, "x", 0, 10, -1), -1)
        self.assertEqual(lc.num({"x": True}, "x", 0, 10, -1), -1)
        self.assertEqual(lc.num({"x": "5"}, "x", 0, 10, -1), -1)
        self.assertEqual(lc.num({}, "x", 0, 10, -1), -1)
        self.assertEqual(lc.num({"x": 3.7}, "x", 0, 10, -1, integer=True), 3)


if __name__ == "__main__":
    unittest.main()
