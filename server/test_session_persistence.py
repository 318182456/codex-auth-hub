import sqlite3
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from starlette.requests import Request

from main import get_current_user_profile, list_team_users, get_quota_history


class SessionPersistenceTests(unittest.TestCase):
    def test_quota_history_filters_by_beijing_day_and_preserves_observation(self):
        observed = datetime(2026, 10, 15, 16, 30, tzinfo=timezone.utc)
        windows = {"weekly": {"used_percent": 10, "reset_at": "2026-10-22T00:00:00+00:00"}}
        with patch("main.get_connection") as connection:
            cursor = connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchall.return_value = [{"observed_at": observed, "windows": windows}]
            result = get_quota_history(7, date(2026, 10, 16))
            profile_id, start, end = cursor.execute.call_args.args[1]
            self.assertEqual(profile_id, 7)
            self.assertEqual(start.astimezone(timezone.utc).isoformat(), "2026-10-15T16:00:00+00:00")
            self.assertEqual(end.astimezone(timezone.utc).isoformat(), "2026-10-16T16:00:00+00:00")
            self.assertEqual(result, [{"observed_at": observed.isoformat(), "windows": windows}])

    def test_offline_account_remains_visible_and_can_be_restored(self):
        # Execute the endpoint queries against records with an old heartbeat.
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript("""
            CREATE TABLE app_users (
                id INTEGER, username TEXT, display_name TEXT, role TEXT,
                is_active BOOLEAN, created_at TIMESTAMP, last_login TIMESTAMP
            );
            CREATE TABLE auth_profiles (id INTEGER, name TEXT);
            CREATE TABLE user_sessions (
                profile_id INTEGER, client_user TEXT, session_token TEXT,
                connected_at TIMESTAMP, last_heartbeat TIMESTAMP
            );
            INSERT INTO app_users VALUES (1, 'alice', 'Alice', 'user', 1, NULL, NULL);
            INSERT INTO auth_profiles VALUES (7, 'Codex1');
            INSERT INTO user_sessions VALUES
                (7, 'alice', 'saved-token', NULL, '2000-01-01 00:00:00');
        """)

        with patch("main.get_connection") as connection, patch(
            "main.get_current_user_from_request",
            return_value={"id": 1, "username": "alice", "display_name": "Alice", "role": "user"},
        ):
            cursor = connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            result = None

            def execute(sql, params=()):
                nonlocal result
                result = db.execute(sql.replace("%s", "?"), params)

            cursor.execute.side_effect = execute
            cursor.fetchone.side_effect = lambda: result.fetchone()
            cursor.fetchall.side_effect = lambda: result.fetchall()

            self.assertEqual(list_team_users()[0]["active_profile"]["id"], 7)
            request = Request({"type": "http", "headers": []})
            session = get_current_user_profile(request)["active_session"]
            self.assertEqual(session["profile_id"], 7)
            self.assertEqual(session["session_token"], "saved-token")


if __name__ == "__main__":
    unittest.main()
