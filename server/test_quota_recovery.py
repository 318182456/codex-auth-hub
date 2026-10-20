import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from quota_service import build_profile_quota, estimate_quota_recoveries, load_quota_recovery_estimates


class RecoveryEstimateTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 18, 0, 0, tzinfo=timezone.utc)

    def event(self, hours, profile=7, window="weekly", gap=90):
        end = self.start + timedelta(hours=hours)
        return {"profile_id": profile, "window_key": window,
                "previous_at": end - timedelta(seconds=gap), "observed_at": end}

    def profile(self):
        return {"quota_last_updated": self.start, "quota_raw": {"rate_limit": {"primary_window": {
            "limit_window_seconds": 604800, "used_percent": 70,
            "reset_at": (self.start + timedelta(days=7)).timestamp()}}}}

    def test_actual_recoveries_override_wrong_official_schedule(self):
        now = self.start + timedelta(hours=60)
        history = estimate_quota_recoveries([self.event(0), self.event(58)], now)
        quota = build_profile_quota(self.profile(), now, history[7])["weekly"]
        expected = self.start + timedelta(hours=116, seconds=-45)
        self.assertEqual(quota["reset_at"], expected.isoformat())
        self.assertEqual(quota["reset_source"], "history")
        self.assertEqual(quota["recovery_sample_count"], 2)
        self.assertEqual(quota["estimated_cycle_seconds"], 58 * 3600)
        self.assertEqual(quota["remaining_percent"], 30)
        self.assertNotEqual(quota["reset_at"], quota["official_reset_at"])
        self.assertEqual(quota["reset_after_seconds"], int((expected - now).total_seconds()))

    def test_recent_intervals_use_median_and_keep_accounts_windows_separate(self):
        events = [self.event(h) for h in (0, 58, 118, 176)]
        events += [self.event(0, profile=8), self.event(100, profile=8)]
        events += [self.event(0, window="five_hour"), self.event(5, window="five_hour")]
        estimates = estimate_quota_recoveries(events, self.start + timedelta(hours=177))
        self.assertEqual(estimates[7]["weekly"]["estimated_cycle_seconds"], 58 * 3600)
        self.assertEqual(estimates[8]["weekly"]["estimated_cycle_seconds"], 100 * 3600)
        self.assertEqual(estimates[7]["five_hour"]["estimated_cycle_seconds"], 5 * 3600)

    def test_insufficient_samples_long_gap_and_future_events_do_not_fabricate_reset(self):
        now = self.start + timedelta(hours=60)
        for events in ([], [self.event(0)], [self.event(0), self.event(58, gap=1800)],
                       [self.event(0), self.event(100)]):
            estimates = estimate_quota_recoveries(events, now)
            quota = build_profile_quota(self.profile(), now, estimates.get(7))["weekly"]
            self.assertIsNone(quota["reset_at"])
            self.assertIsNone(quota["reset_after_seconds"])
            self.assertEqual(quota["used_percent"], 70)

    def test_overdue_prediction_does_not_roll_forward_or_zero_usage(self):
        now = self.start + timedelta(hours=200)
        estimates = estimate_quota_recoveries([self.event(0), self.event(58)], now)
        quota = build_profile_quota(self.profile(), now, estimates[7])["weekly"]
        self.assertEqual(quota["reset_after_seconds"], 0)
        self.assertEqual(quota["used_percent"], 70)
        self.assertLess(datetime.fromisoformat(quota["reset_at"]), now)

    def test_query_only_uses_real_recovery_transitions_in_retained_history(self):
        cur = Mock()
        cur.fetchall.return_value = [self.event(0), self.event(58)]
        now = self.start + timedelta(hours=60)
        self.assertIn(7, load_quota_recovery_estimates(cur, [7], now))
        sql, args = cur.execute.call_args.args
        self.assertIn("previous_used > 0", sql)
        self.assertIn("used_percent = 0", sql)
        self.assertEqual(args[1], now - timedelta(days=14))
        self.assertEqual(args[2], now)


if __name__ == "__main__":
    unittest.main()
