"""Native SCM entry point for the frozen Agent executable."""
import threading

import servicemanager
import win32service
import win32serviceutil

from auth_agent import run_agent


_config_path = None
_auth_file = None


class CodexAgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = "CodexAuthHubAgent"
    _svc_display_name_ = "Codex Auth Hub Agent"
    _svc_description_ = "Automatically sync Codex login credentials and report Agent connectivity."

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = threading.Event()

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=45000)
        self.stop_event.set()

    def SvcShutdown(self):
        self.SvcStop()

    def SvcDoRun(self):
        if _config_path is None or _auth_file is None:
            raise RuntimeError("Service configuration was not initialized")
        code = run_agent(_config_path, stop=self.stop_event, auth_file=_auth_file)
        if code:
            raise RuntimeError(f"Agent exited with code {code}; check agent_config.log")


def dispatch(config_path, auth_file):
    global _config_path, _auth_file
    _config_path, _auth_file = config_path, auth_file
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(CodexAgentService)
    servicemanager.StartServiceCtrlDispatcher()
    return 0
