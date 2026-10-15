import json
import asyncio
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import httpx

from pydantic import ValidationError
from starlette.requests import Request

# Load the application's existing Starlette compatibility shim before its routers.
from main import app
from agent_service import (AgentHeartbeat, agent_heartbeat, build_agent_status, list_agents,
                           CurrentAccount, match_current_account, AgentBinding, bind_agent)
from fastapi import HTTPException


class AgentServiceTests(unittest.TestCase):
    @patch("agent_service.get_connection")
    def test_http_routes_validate_heartbeat_and_expose_agent_status(self, db):
        cur = db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {"configured_watches": [], "binding_mode": "auto"}
        cur.fetchall.return_value = [{"agent_id": "node", "hostname": "login-host",
            "version": "1.1.0", "client_ip": "127.0.0.1", "watches": [],
            "last_heartbeat": datetime.now(timezone.utc)}]

        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub") as client:
                response = await client.post("/api/agents/heartbeat", json={"agent_id": "node", "hostname": "login-host"})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["success"])
                response = await client.get("/api/agents")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()[0]["is_online"])
                response = await client.post("/api/agents/heartbeat", json={"agent_id": "bad/id", "hostname": "host"})
                self.assertEqual(response.status_code, 422)
        asyncio.run(check())

    def test_online_threshold_and_reconnect(self):
        now = datetime.now(timezone.utc)
        for age, expected in ((0, True), (90, True), (91, False), (3600, False), (-1, False)):
            row = {"agent_id": "node", "last_heartbeat": now - timedelta(seconds=age)}
            self.assertEqual(build_agent_status(row, now)["is_online"], expected)
        self.assertTrue(build_agent_status({"last_heartbeat": now}, now)["is_online"])

    @patch("agent_service.get_connection")
    def test_heartbeat_upserts_using_server_time_and_direct_peer(self, db):
        conn = db.return_value.__enter__.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {"configured_watches": [], "binding_mode": "auto"}
        payload = AgentHeartbeat(agent_id="abc", hostname="login-host", watches=[
            {"profile_id": 3, "status": "waiting_auth"}])
        req = Request({"type": "http", "client": ("192.0.2.4", 1000),
                       "headers": [(b"x-forwarded-for", b"spoof")]})
        self.assertTrue(agent_heartbeat(payload, req)["success"])
        conn = db.return_value.__enter__.return_value
        cur = conn.cursor.return_value.__enter__.return_value
        sql, args = cur.execute.call_args_list[0].args
        self.assertIn("ON CONFLICT (agent_id) DO UPDATE", sql)
        self.assertIn("CURRENT_TIMESTAMP", sql)
        self.assertEqual(args[3], "192.0.2.4")
        self.assertEqual(json.loads(args[4])[0]["status"], "waiting_auth")
        conn.commit.assert_called_once()

    def test_matching_prefers_account_id_and_rejects_ambiguity(self):
        cur = MagicMock()
        cur.fetchall.return_value = [{"id": 7, "name": "account"}]
        account = CurrentAccount(has_auth=True, account_id="acct", email="user@example.com")
        self.assertEqual(match_current_account(cur, account)["id"], 7)
        self.assertIn("account_id", cur.execute.call_args.args[0])
        cur.fetchall.return_value = [{"id": 7}, {"id": 8}]
        self.assertIsNone(match_current_account(cur, account))
        cur.reset_mock()
        self.assertIsNone(match_current_account(cur, CurrentAccount()))
        cur.execute.assert_not_called()
        cur.fetchall.side_effect = [[], [{"id": 9, "name": "email match"}]]
        self.assertEqual(match_current_account(cur, account)["id"], 9)
        self.assertIn("COALESCE", cur.execute.call_args.args[0])

    @patch("agent_service.get_connection")
    def test_auto_heartbeat_delivers_match_and_manual_binding_is_retained(self, db):
        cur = db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = {"configured_watches": [], "binding_mode": "auto"}
        cur.fetchall.return_value = [{"id": 7, "name": "account"}]
        req = Request({"type": "http", "client": ("192.0.2.4", 1000), "headers": []})
        payload = AgentHeartbeat(agent_id="node", hostname="host", current_account={"has_auth": True, "account_id": "acct"})
        self.assertEqual(agent_heartbeat(payload, req)["watches"][0]["profile_id"], 7)
        cur.fetchone.return_value = {"configured_watches": [{"profile_id": 9}], "binding_mode": "manual"}
        cur.fetchall.reset_mock()
        self.assertEqual(agent_heartbeat(payload, req)["watches"][0]["profile_id"], 9)
        cur.fetchall.assert_not_called()

    @patch("agent_service.get_connection")
    def test_manual_binding_and_restore_auto_validate_targets(self, db):
        cur = db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [{"id": 7, "name": "account"}, {"agent_id": "node"}]
        self.assertTrue(bind_agent("node", AgentBinding(profile_id=7))["success"])
        args = cur.execute.call_args.args[1]
        self.assertEqual(args[0], "manual")
        self.assertEqual(json.loads(args[1])[0]["profile_id"], 7)
        cur.fetchone.side_effect = [{"agent_id": "node"}]
        bind_agent("node", AgentBinding())
        self.assertEqual(cur.execute.call_args.args[1][0], "auto")
        cur.fetchone.side_effect = [None]
        with self.assertRaises(HTTPException) as ctx:
            bind_agent("node", AgentBinding(profile_id=999))
        self.assertEqual(ctx.exception.status_code, 400)

    @patch("agent_service.get_connection")
    def test_list_keeps_offline_records_and_returns_iso_time(self, db):
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
        cur = db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [{"agent_id": "old", "last_heartbeat": old}]
        result = list_agents()
        self.assertEqual(result[0]["last_heartbeat"], old.isoformat())
        self.assertFalse(result[0]["is_online"])

    def test_invalid_payload_is_rejected(self):
        for value in ({"agent_id": "bad/id", "hostname": "host"},
                      {"agent_id": "node", "hostname": ""},
                      {"agent_id": "node", "hostname": "host", "watches": [{"status": "secret"}]}):
            with self.assertRaises(ValidationError):
                AgentHeartbeat(**value)


if __name__ == "__main__":
    unittest.main()
