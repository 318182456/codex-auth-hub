import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import win32service
import windows_service
from auth_agent import default_auth_file, run_agent


class ServiceTests(unittest.TestCase):
    def service(self):
        service = object.__new__(windows_service.CodexAgentService)
        service.stop_event = threading.Event()
        service.ReportServiceStatus = Mock()
        return service

    def test_scm_stop_and_shutdown_signal_worker_without_killing_process(self):
        service = self.service()
        service.SvcStop()
        self.assertTrue(service.stop_event.is_set())
        service.ReportServiceStatus.assert_called_once_with(win32service.SERVICE_STOP_PENDING, waitHint=45000)
        service.stop_event.clear()
        service.SvcShutdown()
        self.assertTrue(service.stop_event.is_set())

    @patch("windows_service.run_agent", return_value=0)
    def test_service_uses_installed_auth_path_and_reports_failure(self, run):
        service = self.service()
        with patch.object(windows_service, "_config_path", Path("C:/agent/agent_config.json")), \
             patch.object(windows_service, "_auth_file", Path("C:/Users/login/.codex/auth.json")):
            service.SvcDoRun()
            self.assertEqual(run.call_args.kwargs["auth_file"], Path("C:/Users/login/.codex/auth.json"))
            self.assertIs(run.call_args.kwargs["stop"], service.stop_event)
            run.return_value = 1
            with self.assertRaises(RuntimeError):
                service.SvcDoRun()

    @patch("windows_service.servicemanager")
    def test_dispatch_enters_native_scm_dispatcher(self, manager):
        config, auth = Path("C:/agent/config.json"), Path("C:/Users/login/auth.json")
        with patch.object(windows_service, "_config_path", None), patch.object(windows_service, "_auth_file", None):
            self.assertEqual(windows_service.dispatch(config, auth), 0)
            manager.PrepareToHostSingle.assert_called_once_with(windows_service.CodexAgentService)
            manager.StartServiceCtrlDispatcher.assert_called_once()
            self.assertEqual(windows_service._auth_file, auth)

    def test_worker_stops_gracefully_releases_lock_and_preserves_explicit_auth_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "agent_config.json"
            config.write_text(json.dumps({"server_url": "http://localhost:8000"}))
            auth = root / "login-user" / "auth.json"
            stop = threading.Event()
            with patch("auth_agent.AuthAgent") as agent:
                def heartbeat(event):
                    self.assertEqual(default_auth_file(), auth)
                    event.set()
                agent.return_value.heartbeat_loop.side_effect = heartbeat
                self.assertEqual(run_agent(config, stop=stop, auth_file=auth), 0)
            self.assertTrue(stop.is_set())
            self.assertTrue(config.with_suffix(".log").exists())
            # A second instance can start after a normal SCM stop.
            self.assertEqual(run_agent(config, stop=stop, auth_file=auth), 0)


if __name__ == "__main__":
    unittest.main()
