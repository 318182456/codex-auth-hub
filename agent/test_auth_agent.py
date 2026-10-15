import json
import base64
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from auth_agent import AuthAgent, load_config, prepare_watches, upload, current_account


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth.json"
        self.write_auth("first")
        default_path = patch("auth_agent.default_auth_file", return_value=self.auth)
        default_path.start()
        self.addCleanup(default_path.stop)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"server_url": "http://localhost:8000", "watches": [
            {"name": "node", "auth_file": "auth.json"}]}), encoding="utf-8")
        url, _, _ = load_config(self.config)
        watches = prepare_watches(url, [{"name": "node", "auth_file": "auth.json"}], self.root)
        self.agent = AuthAgent(url, watches, self.root / "state.json")

    def write_auth(self, token):
        self.auth.write_text(json.dumps({"tokens": {"access_token": token}}), encoding="utf-8")

    @patch("auth_agent.upload", return_value=7)
    def test_login_upload_once_restart_skip_and_relogin_replace_by_id(self, send):
        self.agent.check_once()
        send.assert_not_called()
        self.agent.check_once()
        send.assert_called_once()
        self.agent.check_once()
        restarted = AuthAgent(self.agent.url, self.agent.watches, self.agent.state_path)
        restarted.check_once()
        self.assertEqual(send.call_count, 1)
        self.write_auth("new-login")
        restarted.check_once()
        restarted.check_once()
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.args[1]["profile_id"], 7)
        state = self.agent.state_path.read_text()
        self.assertNotIn("new-login", state)
        self.assertNotIn("access_token", state)

    @patch("auth_agent.time.monotonic", return_value=100)
    @patch("auth_agent.upload", side_effect=[OSError("secret"), 7])
    def test_network_failure_retries_and_new_login_bypasses_delay(self, send, now):
        self.agent.check_once()
        with self.assertLogs("auth_agent", level="WARNING") as logs:
            self.agent.check_once()
        self.assertNotIn("secret", "".join(logs.output))
        self.agent.check_once()
        self.assertEqual(send.call_count, 1)
        self.assertFalse(self.agent.state_path.exists())
        now.return_value = 106
        self.agent.check_once()
        self.assertEqual(send.call_count, 2)

    @patch("auth_agent.upload", side_effect=[OSError(), 7])
    def test_changed_auth_bypasses_retry_and_invalid_or_missing_file_is_ignored(self, send):
        self.agent.check_once()
        self.agent.check_once()
        self.write_auth("replacement")
        self.agent.check_once()
        self.agent.check_once()
        self.assertEqual(send.call_count, 2)
        for content in ("{", "[]", '{"tokens": []}', '{"tokens": "bad"}', '{}'):
            self.auth.write_text(content)
            self.agent.check_once()
        self.auth.unlink()
        self.agent.check_once()
        self.assertEqual(send.call_count, 2)

    @patch("auth_agent.upload", return_value=7)
    def test_logout_mid_write_requires_stable_file_again(self, send):
        self.agent.check_once()
        self.auth.write_text('{}')
        self.agent.check_once()
        self.write_auth("first")
        self.agent.check_once()
        send.assert_not_called()
        self.agent.check_once()
        send.assert_called_once()

    def test_config_rejects_duplicate_target_and_invalid_server(self):
        self.config.write_text(json.dumps({"server_url": "file:///tmp"}))
        with self.assertRaises(ValueError):
            load_config(self.config)
        for watches in ([{"name": "node"}, {"name": "node"}], [{"profile_id": -1}]):
            with self.assertRaises(ValueError):
                prepare_watches("http://hub", watches, self.root)
        self.config.write_text(json.dumps({"server_url": "http://hub"}))
        self.assertEqual(load_config(self.config)[2], [])
        self.config.write_text(json.dumps({"server_url": "http://hub", "watches": [{"name": "legacy"}]}))
        self.assertEqual(load_config(self.config)[2], [])

    @patch("auth_agent.request.build_opener")
    def test_heartbeat_has_identity_and_sync_status_without_credentials(self, opener):
        response = opener.return_value.open.return_value.__enter__.return_value
        response.read.return_value = b'{"success":true,"watches":[]}'
        key = self.agent.watches[0]["key"]
        self.agent.read_errors.add(key)
        self.agent.heartbeat()
        req = opener.return_value.open.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, "http://localhost:8000/api/agents/heartbeat")
        self.assertEqual(payload["watches"][0]["status"], "waiting_auth")
        self.assertNotIn("access_token", req.data.decode())
        self.assertNotIn(str(self.auth), req.data.decode())
        restarted = AuthAgent(self.agent.url, self.agent.watches, self.agent.state_path)
        self.assertEqual(restarted.agent_id, self.agent.agent_id)
        self.agent.read_errors.clear()
        self.agent.state[key] = {"profile_id": 7, "digest": "hash"}
        self.agent.heartbeat()
        payload = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertEqual(payload["watches"][0]["profile_id"], 7)
        self.assertEqual(payload["watches"][0]["status"], "synced")

    def test_identity_decodes_only_account_metadata(self):
        payload = {"email": "user@example.com", "https://api.openai.com/auth": {"chatgpt_account_id": "acct"}}
        token = "h." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".s"
        self.auth.write_text(json.dumps({"tokens": {"access_token": token}}))
        self.assertEqual(current_account(), {"has_auth": True, "account_id": "acct", "email": "user@example.com"})

    @patch("auth_agent.upload", return_value=7)
    @patch("auth_agent.request.build_opener")
    def test_remote_binding_applies_without_restart_and_switch_blocks_old_target(self, opener, send):
        response = opener.return_value.open.return_value.__enter__.return_value
        response.read.return_value = b'{"success":true,"watches":[{"profile_id":7}]}'
        self.agent.watches = []
        self.agent.heartbeat()
        self.agent.check_once()
        self.agent.check_once()
        self.assertEqual(send.call_args.args[1]["profile_id"], 7)
        self.auth.write_text(json.dumps({"tokens": {"access_token": "new", "account_id": "different"}}))
        self.agent.check_once()
        self.agent.check_once()
        self.assertEqual(send.call_count, 1)
        response.read.return_value = b'{"success":true,"watches":[{"profile_id":9}]}'
        self.agent.heartbeat()
        self.agent.check_once()
        self.agent.check_once()
        self.assertEqual(send.call_args.args[1]["profile_id"], 9)
        response.read.return_value = b'{"success":true,"watches":[]}'
        self.agent.heartbeat()
        self.agent.check_once()
        self.assertEqual(self.agent.watches, [])

    @patch("auth_agent.request.build_opener")
    def test_heartbeat_recovers_without_stopping_sync_loop(self, opener):
        stop = threading.Event()
        opener.return_value.open.side_effect = OSError("secret-token")
        with patch.object(stop, "wait", side_effect=lambda _: stop.set()):
            with self.assertLogs("auth_agent", level="WARNING") as logs:
                self.agent.heartbeat_loop(stop)
        self.assertNotIn("secret-token", "".join(logs.output))

    def test_real_http_json_and_multipart_upload(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers["Content-Type"],
                                 self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"success":true,"profile_id":7}')

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            auth = {"tokens": {"access_token": "test-token"}}
            self.assertEqual(upload(url, {"name": "测试账号"}, auth), 7)
            self.assertEqual(upload(url, {"profile_id": 7}, auth), 7)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(received[0][0], "/api/profiles")
        self.assertEqual(json.loads(received[0][2])["name"], "测试账号")
        self.assertEqual(received[1][0], "/api/profiles/7/replace-file")
        self.assertIn(b'name="file"; filename="auth.json"', received[1][2])
        self.assertIn(b'test-token', received[1][2])


if __name__ == "__main__":
    unittest.main()
