import unittest
import json
import base64
import httpx
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from quota_service import (build_profile_quota, fetch_codex_quota, parse_quota_windows,
                           update_profile_quota_in_db, token_remaining_seconds,
                           build_profile_auth_status, auth_tokens_hash, update_profile_auth_status)
from quota_service import cleanup_quota_observations, trigger_quota_cycle


class QuotaCycleTriggerTests(unittest.TestCase):
    def call_trigger(self, events, status=200, catalog_status=200, model=""):
        self.requests = []
        def handle(request):
            self.requests.append(request)
            if request.method == "GET":
                return httpx.Response(catalog_status, json={"models": [
                    {"slug": "hidden", "visibility": "hide"},
                    {"slug": "available", "visibility": "list"}]})
            return httpx.Response(status, text=events, headers={"content-type": "text/event-stream"})
        client = httpx.Client(transport=httpx.MockTransport(handle))
        with patch("quota_service.httpx.Client", return_value=client), patch.dict("os.environ", {"QUOTA_TRIGGER_MODEL": model}):
            return trigger_quota_cycle({"tokens": {"access_token": "test-token"}})

    def test_uses_available_model_and_waits_for_completed(self):
        result = self.call_trigger('data: {"type":"response.created"}\n\ndata: {"type":"response.completed"}\n\n')
        self.assertEqual(result, {"success": True, "model": "available"})
        self.assertEqual([r.method for r in self.requests], ["GET", "POST"])
        request = self.requests[1]
        self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
        self.assertEqual(request.headers["Authorization"], "Bearer test-token")
        payload = json.loads(request.content)
        self.assertEqual(payload["model"], "available")
        self.assertFalse(payload["store"])
        self.assertTrue(payload["stream"])

    def test_http_success_without_completed_event_is_failure(self):
        for events in ('data: {"type":"response.failed"}\n\n',
                       'data: {"type":"response.incomplete"}\n\n',
                       'data: {"type":"error","message":"secret"}\n\n',
                       'data: {"type":"response.created"}\n\n', 'data: [DONE]\n\n'):
            with self.subTest(events=events):
                result = self.call_trigger(events)
                self.assertFalse(result["success"])
                self.assertNotIn("secret", json.dumps(result))

    def test_catalog_failure_does_not_send_inference(self):
        self.assertFalse(self.call_trigger("", catalog_status=401)["success"])
        self.assertEqual(len(self.requests), 1)

    def test_unavailable_configured_model_is_not_called(self):
        self.assertFalse(self.call_trigger("", model="missing")["success"])
        self.assertEqual(len(self.requests), 1)

    def test_inference_http_failure_does_not_retry(self):
        self.assertFalse(self.call_trigger("secret", status=429)["success"])
        self.assertEqual(len(self.requests), 2)

    def test_network_error_is_sanitized(self):
        with patch("quota_service.httpx.Client", side_effect=httpx.ConnectError("secret")):
            result = trigger_quota_cycle({"tokens": {"access_token": "test"}})
        self.assertFalse(result["success"])
        self.assertNotIn("secret", json.dumps(result))

    def test_no_access_token_does_not_make_requests(self):
        with patch("quota_service.httpx.Client") as client:
            self.assertFalse(trigger_quota_cycle({})["success"])
        client.assert_not_called()


class QuotaObservationCleanupTests(unittest.TestCase):
    def test_cleanup_commits_batch_and_returns_deleted_count(self):
        with patch("quota_service.get_connection") as db:
            conn = db.return_value.__enter__.return_value
            cursor = conn.cursor.return_value.__enter__.return_value
            cursor.rowcount = 17
            self.assertEqual(cleanup_quota_observations(), 17)
            self.assertEqual(cursor.execute.call_args.args[1], (14, 5000))
            conn.commit.assert_called_once()

    def test_cleanup_error_does_not_commit(self):
        with patch("quota_service.get_connection") as db:
            conn = db.return_value.__enter__.return_value
            conn.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("failed")
            with self.assertRaises(RuntimeError):
                cleanup_quota_observations()
            conn.commit.assert_not_called()


class QuotaWindowTests(unittest.TestCase):
    def setUp(self):
        trigger = patch("quota_service.trigger_quota_cycle", return_value={"success": True})
        trigger.start()
        self.addCleanup(trigger.stop)
        self.now = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)

    def window(self, period, used, reset_in=3600):
        return {"limit_window_seconds": period, "used_percent": used,
                "reset_at": int((self.now + timedelta(seconds=reset_in)).timestamp()),
                "reset_after_seconds": reset_in + 100}

    def test_primary_can_be_the_only_weekly_window(self):
        windows = parse_quota_windows({"rate_limit": {
            "primary_window": self.window(604800, 3), "secondary_window": None
        }}, self.now)
        self.assertFalse(windows["five_hour"]["available"])
        self.assertIsNone(windows["five_hour"]["remaining_percent"])
        self.assertIsNone(windows["five_hour"]["reset_after_seconds"])
        self.assertTrue(windows["weekly"]["available"])
        self.assertEqual(windows["weekly"]["remaining_percent"], 97)
        self.assertEqual(windows["weekly"]["reset_after_seconds"], 3600)

    def test_both_windows_are_classified_by_duration_in_either_order(self):
        for periods in ((18000, 604800), (604800, 18000)):
            with self.subTest(periods=periods):
                windows = parse_quota_windows({"rate_limit": {
                    "primary_window": self.window(periods[0], 12.5),
                    "secondary_window": self.window(periods[1], 99),
                }}, self.now)
                self.assertEqual(windows["five_hour"]["window_seconds"], 18000)
                self.assertEqual(windows["weekly"]["window_seconds"], 604800)
                self.assertEqual(windows["five_hour"]["remaining_percent"], 87.5 if periods[0] == 18000 else 1)

    def test_missing_unknown_and_invalid_usage_do_not_become_full_quota(self):
        for data in ({}, {"rate_limit": {"primary_window": {"used_percent": 0}}},
                     {"rate_limit": {"primary_window": {"limit_window_seconds": 86400, "used_percent": 0}}}):
            windows = parse_quota_windows(data, self.now)
            self.assertTrue(all(w["remaining_percent"] is None for w in windows.values()))
        for used in (None, "0", -1, 101, True):
            windows = parse_quota_windows({"rate_limit": {"primary_window": self.window(604800, used)}}, self.now)
            self.assertIsNone(windows["weekly"]["remaining_percent"])

    def test_cached_raw_data_corrects_old_database_mapping(self):
        profile = {"quota_5h_used": 3, "quota_weekly_used": 0,
                   "quota_last_updated": self.now, "plan_type": "Team",
                   "quota_raw": {"rate_limit": {"primary_window": self.window(604800, 3)}}}
        quota = build_profile_quota(profile, self.now)
        self.assertTrue(quota["has_data"])
        self.assertIsNone(quota["five_hour"]["remaining_percent"])
        self.assertEqual(quota["weekly"]["remaining_percent"], 97)

    def test_fetch_and_database_cache_use_corrected_window_mapping(self):
        data = {"rate_limit": {"primary_window": self.window(604800, 3)}, "plan_type": "business"}
        with patch("quota_service.httpx.Client") as http:
            response = http.return_value.__enter__.return_value.get.return_value
            response.status_code = 200
            response.json.return_value = data
            result = fetch_codex_quota({"tokens": {"access_token": "test-token"}})
        self.assertTrue(result["success"])
        self.assertIsNone(result["primary_5h"]["used_percent"])
        self.assertEqual(result["secondary_weekly"]["used_percent"], 3)
        with patch("quota_service.get_connection") as connection:
            update_profile_quota_in_db(7, result)
            cursor = connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            parameters = cursor.execute.call_args_list[0].args[1]
            self.assertEqual(parameters[2:5], (None, None, None))
            self.assertEqual(parameters[5], 3)
            self.assertEqual(parameters[7], data["rate_limit"]["primary_window"]["reset_at"])
            self.assertEqual(parameters[8], result["observed_at"])
            snapshot, profile_id = cursor.execute.call_args_list[1].args[1]
            self.assertEqual(profile_id, 7)
            self.assertEqual(json.loads(snapshot)["weekly"]["used_percent"], 3)
            self.assertNotIn("test-token", snapshot)

    def test_cached_relative_countdown_uses_query_time(self):
        raw = {"rate_limit": {"primary_window": {
            "limit_window_seconds": 604800, "used_percent": 50,
            "reset_after_seconds": 6 * 86400,
        }}}
        profile = {"quota_raw": raw, "quota_last_updated": self.now}
        quota = build_profile_quota(profile, self.now + timedelta(days=1))
        weekly = quota["weekly"]
        self.assertIsNone(weekly["reset_after_seconds"])
        self.assertIsNone(weekly["reset_at"])
        self.assertEqual(weekly["official_reset_at"], (self.now + timedelta(days=6)).isoformat())
        self.assertEqual(weekly["official_reset_source"], "estimated")
        expired = build_profile_quota(profile, self.now + timedelta(days=7))
        self.assertIsNone(expired["weekly"]["reset_after_seconds"])
        self.assertEqual(expired["weekly"]["used_percent"], 50)

    def test_absolute_reset_takes_priority_and_does_not_extend(self):
        raw = {"rate_limit": {"primary_window": self.window(604800, 50)}}
        quota = build_profile_quota({"quota_raw": raw, "quota_last_updated": self.now},
                                    self.now + timedelta(minutes=30))
        self.assertIsNone(quota["weekly"]["reset_after_seconds"])
        self.assertIsNone(quota["weekly"]["reset_source"])
        self.assertEqual(quota["weekly"]["official_reset_source"], "official")

    def test_failed_query_is_not_recorded_as_a_reset(self):
        with patch("quota_service.get_connection") as connection:
            update_profile_quota_in_db(7, {"success": False, "error": "network"})
            connection.assert_not_called()


class TokenRefreshTests(unittest.TestCase):
    def full_quota(self):
        return {"rate_limit": {"primary_window": {
            "limit_window_seconds": 18000, "used_percent": 0}}}

    def jwt(self, expiry):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
        return f"header.{payload}.signature"

    def setUp(self):
        trigger = patch("quota_service.trigger_quota_cycle", return_value={"success": True})
        self.trigger = trigger.start()
        self.addCleanup(trigger.stop)
        self.auth = {"tokens": {"access_token": "old-access", "refresh_token": "old-refresh",
                                "id_token": "old-id", "account_id": "account"}, "extra": "keep"}
        self.http_patch = patch("quota_service.httpx.Client")
        self.http = self.http_patch.start().return_value.__enter__.return_value
        self.addCleanup(self.http_patch.stop)
        self.db_patch = patch("quota_service.get_connection")
        self.connection = self.db_patch.start().return_value.__enter__.return_value
        self.addCleanup(self.db_patch.stop)
        status_patch = patch("quota_service.update_profile_auth_status")
        self.status = status_patch.start()
        self.addCleanup(status_patch.stop)
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.fetchone.return_value = {"auth_json": self.auth}
        self.http.post.return_value = self.response(200, {"access_token": "new-access", "refresh_token": "new-refresh"})

    def response(self, status, data=None):
        response = MagicMock(status_code=status)
        response.json.return_value = data or {}
        return response

    def test_rotated_credentials_are_committed_before_retry_and_never_returned(self):
        def retry(*args, **kwargs):
            self.connection.commit.assert_called_once()
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer new-access")
            return self.response(200, self.full_quota())
        calls = []
        def query(*args, **kwargs):
            calls.append(kwargs["headers"]["Authorization"])
            return self.response(401) if len(calls) == 1 else retry(*args, **kwargs)
        self.http.get.side_effect = query
        result = fetch_codex_quota(self.auth, profile_id=3)
        self.assertTrue(result["success"])
        self.assertEqual(calls, ["Bearer old-access", "Bearer new-access"])
        saved = json.loads(self.cursor.execute.call_args.args[1][0])
        self.assertEqual(saved["tokens"]["refresh_token"], "new-refresh")
        self.assertEqual(saved["tokens"]["account_id"], "account")
        self.assertEqual(saved["tokens"]["id_token"], "old-id")
        self.assertEqual(saved["extra"], "keep")
        self.assertIn("last_refresh", saved)
        self.assertNotIn("new-access", json.dumps(result))
        self.assertEqual(self.auth["tokens"]["access_token"], "old-access")
        self.assertEqual(self.status.call_args.args[2]["tokens"], saved["tokens"])
        self.assertEqual(self.trigger.call_args.args[0]["tokens"], saved["tokens"])

    def test_failed_quota_retry_keeps_rotated_tokens_and_does_not_loop(self):
        for status in (401, 429, 500):
            with self.subTest(status=status):
                self.http.reset_mock()
                self.connection.reset_mock()
                self.http.get.side_effect = [self.response(401), self.response(status)]
                result = fetch_codex_quota(self.auth, profile_id=3)
                self.assertFalse(result["success"])
                self.http.post.assert_called_once()
                self.connection.commit.assert_called_once()
                self.assertEqual(self.http.get.call_count, 2)
                self.assertEqual(result["auth_status"], "invalid" if status == 401 else "error")

    def test_missing_refresh_token_does_not_request_oauth(self):
        self.auth["tokens"].pop("refresh_token")
        self.http.get.return_value = self.response(401)
        result = fetch_codex_quota(self.auth, profile_id=3)
        self.assertIn("缺少 refresh_token", result["error"])
        self.assertEqual(result["auth_status"], "invalid")
        self.http.post.assert_not_called()
        self.connection.commit.assert_not_called()

    def test_rejected_or_invalid_refresh_does_not_overwrite_credentials(self):
        for status, data in ((400, {"error": "old-refresh"}), (500, {}), (200, {}), (200, {"access_token": 123})):
            with self.subTest(status=status, data=data):
                self.http.get.return_value = self.response(401)
                self.http.post.return_value = self.response(status, data)
                self.cursor.reset_mock()
                self.connection.reset_mock()
                result = fetch_codex_quota(self.auth, profile_id=3)
                self.assertFalse(result["success"])
                self.assertEqual(result["auth_status"], "invalid" if status == 400 else "error")
                self.assertNotIn("old-refresh", result["error"])
                self.assertEqual(self.cursor.execute.call_count, 1)
                self.connection.commit.assert_not_called()

    def test_concurrent_refresh_reuses_latest_database_credentials(self):
        self.cursor.fetchone.return_value = {"auth_json": {"tokens": {"access_token": "already-refreshed"}}}
        self.http.get.side_effect = [self.response(401), self.response(200)]
        result = fetch_codex_quota(self.auth, profile_id=3)
        self.assertTrue(result["success"])
        self.http.post.assert_not_called()
        self.assertEqual(self.http.get.call_args.kwargs["headers"]["Authorization"], "Bearer already-refreshed")

    def test_healthy_token_does_not_refresh_or_read_database(self):
        self.http.get.return_value = self.response(200, self.full_quota())
        self.assertTrue(fetch_codex_quota(self.auth, profile_id=3)["success"])
        self.http.post.assert_not_called()
        self.cursor.execute.assert_not_called()
        self.trigger.assert_called_once_with(self.auth)

    def test_failed_quota_query_does_not_trigger_model(self):
        self.http.get.return_value = self.response(429)
        self.assertFalse(fetch_codex_quota(self.auth, profile_id=3)["success"])
        self.trigger.assert_not_called()

    def test_model_failure_does_not_invalidate_successful_quota(self):
        self.http.get.return_value = self.response(200, self.full_quota())
        self.trigger.return_value = {"success": False, "error": "周期触发请求失败"}
        with self.assertLogs("quota_service", level="WARNING"):
            result = fetch_codex_quota(self.auth, profile_id=3)
        self.assertTrue(result["success"])
        self.assertFalse(result["cycle_trigger"]["success"])

    def test_only_remaining_100_percent_triggers_model(self):
        for period in (18000, 604800):
            for used in (0, 1, 50, 100, None, True, "0"):
                with self.subTest(period=period, used=used):
                    self.trigger.reset_mock()
                    self.http.get.return_value = self.response(200, {"rate_limit": {"primary_window": {
                        "limit_window_seconds": period, "used_percent": used}}})
                    result = fetch_codex_quota(self.auth, profile_id=3)
                    self.assertTrue(result["success"])
                    if used == 0 and not isinstance(used, bool):
                        self.trigger.assert_called_once()
                    else:
                        self.trigger.assert_not_called()
                        self.assertTrue(result["cycle_trigger"]["skipped"])

    def test_missing_windows_do_not_trigger_model(self):
        self.http.get.return_value = self.response(200)
        result = fetch_codex_quota(self.auth, profile_id=3)
        self.assertTrue(result["cycle_trigger"]["skipped"])
        self.trigger.assert_not_called()

    def test_refresh_network_failure_does_not_expose_credentials(self):
        self.http.get.return_value = self.response(401)
        self.http.post.side_effect = httpx.ConnectError("secret old-refresh")
        result = fetch_codex_quota(self.auth, profile_id=3)
        self.assertFalse(result["success"])
        self.assertNotIn("old-refresh", result["error"])
        self.connection.commit.assert_not_called()

    def test_save_failure_prevents_retry_and_does_not_expose_credentials(self):
        self.http.get.return_value = self.response(401)
        self.connection.commit.side_effect = RuntimeError("secret new-refresh")
        result = fetch_codex_quota(self.auth, profile_id=3)
        self.assertFalse(result["success"])
        self.assertEqual(self.http.get.call_count, 1)
        self.assertNotIn("new-refresh", result["error"])

    def test_jwt_remaining_seconds_and_malformed_tokens(self):
        now = datetime(2026, 10, 14, tzinfo=timezone.utc)
        self.assertEqual(token_remaining_seconds(self.jwt(now.timestamp() + 120), now), 120)
        for token in ("opaque", "a.!.b", self.jwt("bad"), self.jwt(True), self.jwt(float("inf"))):
            self.assertIsNone(token_remaining_seconds(token, now))

    def test_near_expiry_refreshes_before_first_quota_request(self):
        for remaining in (300, 120, -1):
            with self.subTest(remaining=remaining):
                self.http.reset_mock()
                self.auth["tokens"]["access_token"] = self.jwt(datetime.now(timezone.utc).timestamp() + remaining)
                self.http.get.return_value = self.response(200)
                result = fetch_codex_quota(self.auth, profile_id=3)
                self.assertTrue(result["success"])
                self.http.post.assert_called_once()
                self.assertEqual(self.http.get.call_count, 1)
                self.assertEqual(self.http.get.call_args.kwargs["headers"]["Authorization"], "Bearer new-access")

    def test_long_lived_token_is_not_refreshed(self):
        self.auth["tokens"]["access_token"] = self.jwt(datetime.now(timezone.utc).timestamp() + 3600)
        self.http.get.return_value = self.response(200)
        self.assertTrue(fetch_codex_quota(self.auth, profile_id=3)["success"])
        self.http.post.assert_not_called()

    def test_proactive_refresh_does_not_refresh_again_on_401(self):
        self.auth["tokens"]["access_token"] = self.jwt(datetime.now(timezone.utc).timestamp() + 60)
        self.http.get.return_value = self.response(401)
        self.assertFalse(fetch_codex_quota(self.auth, profile_id=3)["success"])
        self.http.post.assert_called_once()
        self.assertEqual(self.http.get.call_count, 1)


class AuthStatusTests(unittest.TestCase):
    def setUp(self):
        self.auth = {"tokens": {"access_token": "test"}}
        self.profile = {"is_active": True, "auth_json": self.auth, "auth_status": "invalid",
                        "auth_status_error": "请重新登录", "auth_checked_at": datetime.now(timezone.utc),
                        "auth_status_tokens_hash": auth_tokens_hash(self.auth)}

    def test_verified_state_includes_time_and_reason_without_credentials(self):
        result = build_profile_auth_status(self.profile)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["error"], "请重新登录")
        self.assertIsNotNone(result["checked_at"])
        self.assertNotIn("tokens", result)

    def test_new_credentials_do_not_inherit_old_status(self):
        self.profile["auth_json"] = {"tokens": {"access_token": "new"}}
        self.assertEqual(build_profile_auth_status(self.profile), {"status": "unchecked", "error": "", "checked_at": None})

    def test_disabled_and_api_key_accounts_have_distinct_states(self):
        self.profile["is_active"] = False
        self.assertEqual(build_profile_auth_status(self.profile)["status"], "disabled")
        self.profile["is_active"] = True
        self.profile["auth_json"] = {"OPENAI_API_KEY": "key"}
        self.assertEqual(build_profile_auth_status(self.profile)["status"], "unsupported")

    def test_status_update_guards_against_replaced_credentials(self):
        with patch("quota_service.get_connection") as db:
            update_profile_auth_status(3, {"success": False, "auth_status": "invalid", "error": "401"}, self.auth)
            conn = db.return_value.__enter__.return_value
            cursor = conn.cursor.return_value.__enter__.return_value
            sql, args = cursor.execute.call_args.args
            self.assertIn("COALESCE(auth_json->'tokens'", sql)
            self.assertEqual(args[:2], ("invalid", "401"))
            self.assertEqual(json.loads(args[-1]), self.auth["tokens"])
            conn.commit.assert_called_once()

    def test_success_clears_previous_error(self):
        with patch("quota_service.get_connection") as db:
            update_profile_auth_status(3, {"success": True}, self.auth)
            cursor = db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            self.assertEqual(cursor.execute.call_args.args[1][:2], ("valid", ""))


if __name__ == "__main__":
    unittest.main()
