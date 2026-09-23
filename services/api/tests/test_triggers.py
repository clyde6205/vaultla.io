import unittest
from datetime import datetime, timedelta, timezone

from app.chronos.triggers import (BPS_FULL, FixedDate, Liveness, Progressive, add_years,
                                  build_trigger, releasable_item_count)
from app.core.errors import ValidationFailed

UTC = timezone.utc
D = lambda y, m=1, d=1, h=0: datetime(y, m, d, h, tzinfo=UTC)  # noqa: E731


class FixedDateTests(unittest.TestCase):
    def test_before_and_at_and_after(self):
        t = FixedDate(D(2050))
        self.assertFalse(t.evaluate(D(2049, 12, 31, 23)).matured)
        self.assertTrue(t.evaluate(D(2050)).matured)
        self.assertTrue(t.evaluate(D(2051)).matured)

    def test_next_eval_is_unlock(self):
        self.assertEqual(FixedDate(D(2050)).evaluate(D(2030)).next_eval_at, D(2050))

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValidationFailed):
            build_trigger("fixed_date", {"unlock_at": "2050-01-01T00:00:00"})


class ProgressiveTests(unittest.TestCase):
    def setUp(self):
        self.p = Progressive(start_at=D(2030), step_bps=500, every_years=1)   # 5% per year

    def test_nothing_before_first_anniversary(self):
        e = self.p.evaluate(D(2030, 12, 31))
        self.assertEqual((e.matured, e.releasable_bps, e.next_eval_at), (False, 0, D(2031)))

    def test_five_percent_per_year(self):
        self.assertEqual(self.p.evaluate(D(2031)).releasable_bps, 500)
        self.assertEqual(self.p.evaluate(D(2035, 6)).releasable_bps, 2500)

    def test_completes_after_twenty_years(self):
        e = self.p.evaluate(D(2050))
        self.assertTrue(e.matured)
        self.assertEqual(e.releasable_bps, BPS_FULL)
        self.assertIsNone(e.next_eval_at)

    def test_first_step_at_start(self):
        p = Progressive(D(2030), 500, 1, first_step_at_start=True)
        self.assertEqual(p.evaluate(D(2030)).releasable_bps, 500)
        self.assertEqual(p.evaluate(D(2049)).releasable_bps, BPS_FULL)

    def test_leap_day_anchor(self):
        self.assertEqual(add_years(D(2028, 2, 29), 1), D(2029, 2, 28))
        self.assertEqual(add_years(D(2028, 2, 29), 4), D(2032, 2, 29))

    def test_horizon_and_bounds_enforced(self):
        with self.assertRaises(ValidationFailed):
            Progressive(D(2030), 0)
        with self.assertRaises(ValidationFailed):
            Progressive(D(2030), 100, every_years=2)      # 100 steps * 2y = 200y > cap

    def test_item_count_never_releases_early(self):
        self.assertEqual(releasable_item_count(10, 999), 0)
        self.assertEqual(releasable_item_count(10, 1000), 1)
        self.assertEqual(releasable_item_count(10, BPS_FULL), 10)


class LivenessTests(unittest.TestCase):
    def test_deadline_and_reset(self):
        t = Liveness(interval_days=30, grace_days=10, created_at=D(2030))
        self.assertFalse(t.evaluate(D(2030) + timedelta(days=39)).matured)
        self.assertTrue(t.evaluate(D(2030) + timedelta(days=40)).matured)
        # a check-in on day 35 pushes the deadline out
        e = t.evaluate(D(2030) + timedelta(days=60), last_checkin_at=D(2030) + timedelta(days=35))
        self.assertFalse(e.matured)

    def test_requires_attestations(self):
        t = Liveness(30, 0, required_attestations=2, created_at=D(2030))
        late = D(2030) + timedelta(days=31)
        self.assertFalse(t.evaluate(late, attestations=1).matured)
        self.assertTrue(t.evaluate(late, attestations=2).matured)

    def test_bad_config(self):
        with self.assertRaises(ValidationFailed):
            build_trigger("liveness", {"interval_days": 0})
        with self.assertRaises(ValidationFailed):
            build_trigger("nonsense", {})


if __name__ == "__main__":
    unittest.main()
