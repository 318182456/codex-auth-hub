import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import client_gui


class ClientStartupTests(unittest.TestCase):
    def start_client(self, config_path):
        interpreter = tk.Tcl()
        string_var = tk.StringVar
        root = MagicMock()

        def setup_ui(app):
            app.release_btn = MagicMock()

        with (
            patch.object(client_gui, "get_config_path", return_value=str(config_path)),
            patch.object(client_gui.tk, "StringVar", side_effect=lambda **kw: string_var(master=interpreter, **kw)),
            patch.object(client_gui.CodexClientApp, "setup_ui", setup_ui),
            patch.object(client_gui.CodexClientApp, "update_user_display"),
            patch.object(client_gui.CodexClientApp, "refresh_profiles"),
            patch.object(client_gui.CodexClientApp, "start_auto_refresh"),
            patch.object(client_gui.threading, "Thread") as thread,
        ):
            app = client_gui.CodexClientApp(root)
        return app, root, thread

    def test_startup_without_saved_session(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "hub_client_config.json"
            for config in (None, "{invalid json", "{}"):
                with self.subTest(config=config):
                    if config is not None:
                        config_path.write_text(config, encoding="utf-8")
                    app, root, thread = self.start_client(config_path)
                    self.assertIsNone(app.session_token)
                    self.assertIsNone(app.active_profile_id)
                    self.assertIsNone(app.active_profile_name)
                    self.assertFalse(app.is_heartbeat_running)
                    root.after.assert_called_once_with(500, app.prompt_login_if_needed)
                    thread.assert_not_called()

    def test_saved_session_is_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "hub_client_config.json"
            config_path.write_text(json.dumps({
                "auth_token": "test-auth-token",
                "session_token": "test-session-token",
                "active_profile_id": 7,
                "active_profile_name": "test-profile",
            }), encoding="utf-8")
            app, _, thread = self.start_client(config_path)
            self.assertEqual(app.auth_token, "test-auth-token")
            self.assertEqual(app.session_token, "test-session-token")
            self.assertEqual(app.active_profile_id, 7)
            self.assertEqual(app.active_profile_name, "test-profile")
            self.assertTrue(app.is_heartbeat_running)
            app.release_btn.configure.assert_called_once_with(state=tk.NORMAL)
            self.assertEqual(thread.call_count, 2)


if __name__ == "__main__":
    unittest.main()
