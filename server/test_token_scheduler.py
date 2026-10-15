import asyncio
import base64
import json
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from quota_service import _refresh_profile_auth
from token_scheduler import TokenRefreshScheduler


class TokenSchedulerTests(unittest.TestCase):
    def profile(self, pid, remaining=120, refresh="refresh"):
        payload = base64.urlsafe_b64encode(json.dumps({
            "exp": datetime.now(timezone.utc).timestamp() + remaining
        }).encode()).decode().rstrip("=")
        return {"id": pid, "auth_json": {"tokens": {
            "access_token": f"header.{payload}.signature", "refresh_token": refresh
        }}}

    def setUp(self):
        self.scheduler = TokenRefreshScheduler()
        db = patch("token_scheduler.get_connection")
        self.cursor = db.start().return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        self.addCleanup(db.stop)
        http = patch("token_scheduler.httpx.Client")
        http.start()
        self.addCleanup(http.stop)
        refresh = patch("token_scheduler._refresh_profile_auth")
        self.refresh = refresh.start()
        self.addCleanup(refresh.stop)
        quota = patch("token_scheduler.fetch_codex_quota", return_value={"success": True})
        self.quota = quota.start()
        self.addCleanup(quota.stop)
        update = patch("token_scheduler.update_profile_quota_in_db")
        self.update = update.start()
        self.addCleanup(update.stop)
        status = patch("token_scheduler.update_profile_auth_status")
        self.status = status.start()
        self.addCleanup(status.stop)

    def test_only_due_refreshable_profiles_are_refreshed(self):
        profiles = [self.profile(1), self.profile(2, -10), self.profile(3, 3600), self.profile(4, refresh=None),
                    {"id": 5, "auth_json": {"OPENAI_API_KEY": "key"}},
                    {"id": 6, "auth_json": {"tokens": {"access_token": "opaque", "refresh_token": "refresh"}}}]
        self.cursor.fetchall.return_value = profiles
        self.scheduler.check_once()
        self.assertEqual([c.args[1] for c in self.refresh.call_args_list], [1, 2])
        self.assertTrue(all(c.kwargs["require_active"] for c in self.refresh.call_args_list))
        self.assertIn("is_active = TRUE", self.cursor.execute.call_args.args[0])

    def test_failure_does_not_block_other_accounts_and_has_retry_delay(self):
        self.cursor.fetchall.return_value = [self.profile(1), self.profile(2)]
        self.refresh.side_effect = [ValueError("secret"), None]
        with patch("token_scheduler.time.monotonic", return_value=100), self.assertLogs("token_scheduler", level="WARNING") as logs:
            self.scheduler.check_once()
        self.assertNotIn("secret", "".join(logs.output))
        self.assertEqual(self.refresh.call_count, 2)
        self.refresh.reset_mock()
        self.refresh.side_effect = None
        with patch("token_scheduler.time.monotonic", return_value=200):
            self.scheduler.check_once()
        self.assertEqual([c.args[1] for c in self.refresh.call_args_list], [2])
        self.refresh.reset_mock()
        with patch("token_scheduler.time.monotonic", return_value=401):
            self.scheduler.check_once()
        self.assertEqual([c.args[1] for c in self.refresh.call_args_list], [1, 2])

    def test_replaced_credentials_bypass_failure_delay(self):
        profile = self.profile(1)
        self.scheduler.failed_profiles[1] = (dict(profile["auth_json"]["tokens"]), float("inf"))
        profile["auth_json"]["tokens"]["refresh_token"] = "replacement"
        self.cursor.fetchall.return_value = [profile]
        self.scheduler.check_once()
        self.refresh.assert_called_once()
        self.assertNotIn(1, self.scheduler.failed_profiles)

    def test_stop_prevents_new_refreshes(self):
        self.cursor.fetchall.return_value = [self.profile(1)]
        self.scheduler.stop()
        self.scheduler.check_once()
        self.refresh.assert_not_called()

    def test_checks_actual_validity_even_if_expiry_is_far_away(self):
        profile = self.profile(1, remaining=864000)
        self.cursor.fetchall.return_value = [profile]
        self.scheduler.check_once()
        self.refresh.assert_not_called()
        self.quota.assert_called_once_with(profile["auth_json"], profile_id=1)
        self.update.assert_called_once_with(1, {"success": True})

    def test_recent_quota_is_not_queried_again(self):
        profile = self.profile(1, remaining=864000)
        profile["quota_last_updated"] = datetime.now(timezone.utc)
        self.cursor.fetchall.return_value = [profile]
        self.scheduler.check_once()
        self.quota.assert_not_called()

    def test_opaque_tokens_are_checked_through_quota_endpoint(self):
        profile = self.profile(1)
        profile["auth_json"]["tokens"]["access_token"] = "opaque"
        self.cursor.fetchall.return_value = [profile]
        self.scheduler.check_once()
        self.refresh.assert_not_called()
        self.quota.assert_called_once()

    def test_worker_runs_immediately_repeats_and_stops(self):
        scheduler = TokenRefreshScheduler(interval=0.01)
        ran_twice = threading.Event()
        count = []
        def check():
            count.append(1)
            if len(count) >= 2:
                ran_twice.set()
        with patch.object(scheduler, "check_once", side_effect=check):
            scheduler.start()
            try:
                self.assertTrue(ran_twice.wait(2))
            finally:
                scheduler.stop()
                scheduler.thread.join(timeout=2)
        self.assertFalse(scheduler.thread.is_alive())

    def test_worker_survives_scan_failure(self):
        scheduler = TokenRefreshScheduler(interval=0.01)
        recovered = threading.Event()
        def check():
            recovered.set()
            scheduler.stop()
        calls = []
        def scan():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("secret")
            check()
        with patch.object(scheduler, "check_once", side_effect=scan), patch("token_scheduler.logger"):
            scheduler.start()
            try:
                self.assertTrue(recovered.wait(2))
            finally:
                scheduler.stop()
                scheduler.thread.join(timeout=2)
        self.assertEqual(len(calls), 2)


class SchedulerIntegrationTests(unittest.TestCase):
    def test_disabled_account_is_checked_again_under_lock(self):
        with patch("quota_service.get_connection") as db, patch("quota_service._refresh_auth") as refresh:
            cursor = db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            auth = {"tokens": {"access_token": "old"}}
            cursor.fetchone.return_value = {"auth_json": auth, "is_active": False}
            self.assertEqual(_refresh_profile_auth(None, 1, auth, require_active=True), auth)
            refresh.assert_not_called()

    def test_application_lifespan_starts_and_stops_scheduler(self):
        import main
        async def run():
            with patch("main.init_db"), patch("main.TokenRefreshScheduler") as scheduler:
                worker = scheduler.return_value
                async with main.lifespan(main.app):
                    worker.start.assert_called_once()
                    worker.stop.assert_not_called()
                worker.stop.assert_called_once()
                worker.join.assert_called_once()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
