import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.chronos.scanner import ChronosScanner, DueVault
from app.core.errors import TimeConsensusError, TimeRegressionError
from tests.fakes import FakeChronosRepo, FixedClock

UTC = timezone.utc
NOW = datetime(2060, 1, 1, tzinfo=UTC)


def due(trigger, cfg, **kw):
    return DueVault(uuid4(), uuid4(), trigger, cfg, kw.get("last"), kw.get("att", 0), kw.get("bps", 0))


class ScannerTests(unittest.TestCase):
    def test_matures_only_due_fixed_date(self):
        a = due("fixed_date", {"unlock_at": "2059-12-31T00:00:00+00:00"})
        b = due("fixed_date", {"unlock_at": "2070-01-01T00:00:00+00:00"})
        repo = FakeChronosRepo([a, b])
        r = ChronosScanner(repo, FixedClock(NOW)).run()
        self.assertEqual(r.matured_ids, [a.id])
        self.assertEqual(repo.state[a.id]["state"], "matured")
        self.assertEqual(repo.state[b.id]["state"], "sealed")
        self.assertEqual(repo.state[b.id]["next"], datetime(2070, 1, 1, tzinfo=UTC))
        self.assertIn("vault.matured", [e for e, _ in repo.audits])

    def test_progressive_advances_tranche(self):
        v = due("progressive", {"start_at": "2055-01-01T00:00:00+00:00", "step_bps": 500})
        repo = FakeChronosRepo([v])
        r = ChronosScanner(repo, FixedClock(NOW)).run()   # 5 anniversaries elapsed
        self.assertEqual((r.advanced, r.matured), (1, 0))
        self.assertEqual(repo.state[v.id]["bps"], 2500)

    def test_liveness_needs_attestation(self):
        cfg = {"interval_days": 30, "grace_days": 0, "required_attestations": 1,
               "created_at": "2059-01-01T00:00:00+00:00"}
        v = due("liveness", cfg, att=0)
        repo = FakeChronosRepo([v])
        r = ChronosScanner(repo, FixedClock(NOW)).run()
        self.assertEqual(r.matured, 0)
        self.assertEqual(repo.state[v.id]["next"], NOW + timedelta(days=1))

    def test_bad_trigger_is_isolated_and_deferred(self):
        bad = due("progressive", {"start_at": "garbage", "step_bps": 500})
        good = due("fixed_date", {"unlock_at": "2000-01-01T00:00:00+00:00"})
        repo = FakeChronosRepo([bad, good])
        r = ChronosScanner(repo, FixedClock(NOW)).run()
        self.assertEqual((r.errors, r.matured), (1, 1))
        self.assertEqual(repo.state[bad.id]["state"], "sealed")
        self.assertIn("vault.trigger_error", [e for e, _ in repo.audits])

    def test_fails_closed_without_trusted_time(self):
        class Broken:
            def now(self):
                raise TimeConsensusError("no quorum")
        v = due("fixed_date", {"unlock_at": "2000-01-01T00:00:00+00:00"})
        repo = FakeChronosRepo([v])
        with self.assertRaises(TimeConsensusError):
            ChronosScanner(repo, Broken()).run()
        self.assertEqual(repo.state[v.id]["state"], "sealed")

    def test_time_regression_blocks_maturity(self):
        v = due("fixed_date", {"unlock_at": "2000-01-01T00:00:00+00:00"})
        repo = FakeChronosRepo([v], last_time=NOW + timedelta(days=30))
        with self.assertRaises(TimeRegressionError):
            ChronosScanner(repo, FixedClock(NOW)).run()
        self.assertEqual(repo.state[v.id]["state"], "sealed")

    def test_hook_runs_after_maturity_and_failure_is_contained(self):
        v = due("fixed_date", {"unlock_at": "2000-01-01T00:00:00+00:00"})
        calls = []

        def hook(vault, now):
            calls.append(vault.id)
            raise RuntimeError("s3 down")
        r = ChronosScanner(FakeChronosRepo([v]), FixedClock(NOW), on_matured=hook).run()
        self.assertEqual((calls, r.matured), ([v.id], 1))

    def test_batching(self):
        vs = [due("fixed_date", {"unlock_at": "2000-01-01T00:00:00+00:00"}) for _ in range(25)]
        r = ChronosScanner(FakeChronosRepo(vs), FixedClock(NOW), batch_size=10).run()
        self.assertEqual(r.matured, 25)


if __name__ == "__main__":
    unittest.main()
