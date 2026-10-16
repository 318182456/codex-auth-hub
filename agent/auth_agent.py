"""Watch login hosts' auth files and push changes to an existing Auth Hub."""
import argparse
import base64
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
import uuid
from urllib import error, parse, request


LOG = logging.getLogger("auth_agent")
AGENT_VERSION = "1.3.0"
_AUTH_FILE_OVERRIDE = None


def default_auth_file():
    if _AUTH_FILE_OVERRIDE is not None:
        return _AUTH_FILE_OVERRIDE
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"


def current_account():
    try:
        auth, _ = read_auth(default_auth_file())
    except (OSError, ValueError, UnicodeError):
        return {"has_auth": False}
    return account_from_auth(auth)


def account_from_auth(auth):
    tokens = auth.get("tokens") or {}
    account_id = tokens.get("account_id") or ""
    email = ""
    for token in (tokens.get("id_token"), tokens.get("access_token")):
        try:
            encoded = token.split(".")[1]
            data = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            email = email or data.get("email") or data.get("https://api.openai.com/profile", {}).get("email", "")
            account_id = account_id or data.get("https://api.openai.com/auth", {}).get("chatgpt_account_id", "")
        except (AttributeError, IndexError, TypeError, ValueError, UnicodeError):
            continue
    return {"has_auth": True, "account_id": str(account_id)[:255], "email": str(email)[:255]}


def read_auth(path):
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("auth file too large")
    auth = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(auth, dict):
        raise ValueError("auth must be an object")
    tokens = auth.get("tokens") or {}
    if not isinstance(tokens, dict):
        raise ValueError("tokens must be an object")
    if not (isinstance(tokens.get("access_token"), str) and tokens["access_token"].strip()
            or isinstance(auth.get("OPENAI_API_KEY"), str) and auth["OPENAI_API_KEY"].strip()):
        raise ValueError("missing credentials")
    # Ignore formatting-only changes; persist only the hash, never the credentials.
    digest = hashlib.sha256(json.dumps(auth, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return auth, digest


def load_config(path):
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    url = config.get("server_url", "").rstrip("/")
    parsed = parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("server_url 必须为 http(s) 服务端地址，不能包含账号、查询参数或片段")
    interval = float(config.get("poll_seconds", 3))
    if not 1 <= interval <= 3600:
        raise ValueError("poll_seconds 必须为 1 至 3600 秒")
    # Account watches are always assigned by the hub, including when upgrading
    # from a legacy local config that still contains a watches property.
    return url, interval, []


def prepare_watches(url, watches, base_dir):
    if not isinstance(watches, list) or len(watches) > 200:
        raise ValueError("服务端监测配置无效")
    watches = [dict(watch) for watch in watches]
    seen = set()
    targets = set()
    for watch in watches:
        pid = watch.get("profile_id")
        name = str(watch.get("name", "")).strip()
        if pid is not None and (type(pid) is not int or pid <= 0):
            raise ValueError("profile_id 必须为正整数")
        if pid is None and not 1 <= len(name) <= 100:
            raise ValueError("未指定 profile_id 时，name 必须为 1 至 100 个字符")
        path_value = watch.get("auth_file")
        auth_path = Path(os.path.expandvars(path_value)).expanduser() if path_value else default_auth_file()
        if not auth_path.is_absolute():
            auth_path = base_dir / auth_path
        watch["path"] = auth_path.resolve()
        target = f"id:{pid}" if pid is not None else f"name:{name}"
        key = f"{url}|{target}|{watch['path']}"
        if key in seen or target in targets:
            raise ValueError("同一账号不能配置多个监测来源")
        seen.add(key)
        targets.add(target)
        watch["key"] = hashlib.sha256(key.encode()).hexdigest()
        watch["name"] = name
    return watches


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Credentials must never follow a redirect to a different destination.
        return None


def upload(url, watch, auth):
    headers = {}
    token_env = os.environ.get("CODEX_AUTH_HUB_TOKEN")
    if token_env:
        headers["Authorization"] = f"Bearer {token_env}"
    if watch.get("profile_id") is not None:
        endpoint = f"/api/profiles/{watch['profile_id']}/replace-file"
        boundary = "----CodexAuthAgentBoundary"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"auth.json\"\r\n"
                "Content-Type: application/json\r\n\r\n").encode()
        body += json.dumps(auth).encode() + f"\r\n--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        endpoint = "/api/profiles"
        body = json.dumps({"name": watch["name"], "description": watch.get("description", ""),
                           "max_users": watch.get("max_users", 0), "auth_json": auth}).encode()
        headers["Content-Type"] = "application/json"
    req = request.Request(url + endpoint, data=body, headers=headers, method="POST")
    with request.build_opener(NoRedirect()).open(req, timeout=20) as response:
        result = json.loads(response.read(65536))
    if result.get("success") is not True or type(result.get("profile_id")) is not int:
        raise ValueError("unexpected server response")
    return result["profile_id"]


class AuthAgent:
    def __init__(self, url, watches, state_path):
        self.url = url
        self.watches = watches
        self.state_path = state_path
        self.pending = {}
        self.retry = {}
        self.read_errors = set()
        self.config_lock = threading.Lock()
        self.remote_watches = None
        self.applied_watches = None
        self.bound_identity = None
        self.agent_id = uuid.uuid5(uuid.NAMESPACE_URL,
                                  f"{socket.gethostname()}|{state_path.resolve()}|{url}").hex
        try:
            self.state = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(self.state, dict):
                raise ValueError("invalid state")
        except FileNotFoundError:
            self.state = {}

    def save_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        temp.replace(self.state_path)

    def heartbeat(self):
        watches = []
        for watch in self.watches:
            key = watch["key"]
            saved = self.state.get(key, {})
            status = ("waiting_auth" if key in self.read_errors else
                      "retrying" if key in self.retry else
                      "pending" if key in self.pending or not saved.get("digest") else "synced")
            watches.append({"profile_id": saved.get("profile_id") or watch.get("profile_id"),
                            "name": watch["name"], "status": status})
        observed_account = current_account()
        body = json.dumps({"agent_id": self.agent_id, "hostname": socket.gethostname(),
                           "version": AGENT_VERSION, "watches": watches,
                           "current_account": observed_account}).encode()
        headers = {"Content-Type": "application/json"}
        if os.environ.get("CODEX_AUTH_HUB_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['CODEX_AUTH_HUB_TOKEN']}"
        req = request.Request(self.url + "/api/agents/heartbeat", data=body, headers=headers, method="POST")
        with request.build_opener(NoRedirect()).open(req, timeout=10) as response:
            result = json.loads(response.read(65536))
        if result.get("success") is not True:
            raise ValueError("unexpected heartbeat response")
        configured = result.get("watches")
        if configured is None:
            raise ValueError("hub does not support remote configuration")
        # Validate before handing the new configuration to the polling thread.
        prepared = prepare_watches(self.url, configured, self.state_path.parent)
        with self.config_lock:
            self.remote_watches = (configured, prepared, observed_account)

    def apply_remote_config(self):
        with self.config_lock:
            remote = self.remote_watches
            self.remote_watches = None
        if remote is None or (remote[0], remote[2]) == self.applied_watches:
            return
        self.applied_watches = (remote[0], remote[2])
        self.watches = remote[1]
        self.bound_identity = remote[2]
        self.pending.clear()
        self.retry.clear()
        self.read_errors.clear()
        LOG.info("已应用服务端配置，监测 %s 个账号", len(self.watches))

    def heartbeat_loop(self, stop):
        failed = False
        while not stop.is_set():
            try:
                self.heartbeat()
                if failed:
                    LOG.info("Agent 心跳连接已恢复")
                failed = False
            except Exception as exc:
                if not failed:
                    status = f"HTTP {exc.code}" if isinstance(exc, error.HTTPError) else type(exc).__name__
                    LOG.warning("Agent 心跳失败（%s），请检查服务端版本与连接", status)
                failed = True
            stop.wait(30)

    def check_once(self):
        self.apply_remote_config()
        # A login switch must not upload the new account into the previous
        # heartbeat's binding while waiting for the hub to identify it.
        if self.bound_identity is not None and current_account() != self.bound_identity:
            self.pending.clear()
            return
        for index, watch in enumerate(self.watches, 1):
            key = watch["key"]
            try:
                auth, digest = read_auth(watch["path"])
            except (OSError, ValueError, UnicodeError):
                self.pending.pop(key, None)
                if key not in self.read_errors:
                    LOG.warning("账号 #%s：等待有效的 auth.json（文件缺失、写入中或已退出登录）", index)
                    self.read_errors.add(key)
                continue
            self.read_errors.discard(key)
            if self.bound_identity is not None and account_from_auth(auth) != self.bound_identity:
                self.pending.pop(key, None)
                continue
            saved = self.state.get(key, {})
            if saved.get("digest") == digest:
                self.pending.pop(key, None)
                continue
            # Require two consecutive reads to avoid uploading partially written files.
            if self.pending.get(key) != digest:
                self.pending[key] = digest
                self.retry.pop(key, None)
                continue
            failures, due = self.retry.get(key, (0, 0))
            if time.monotonic() < due:
                continue
            target = dict(watch)
            # Bind by ID after creation: renaming in the panel must not create duplicates.
            if saved.get("profile_id"):
                target["profile_id"] = saved["profile_id"]
            try:
                pid = upload(self.url, target, auth)
            except Exception as exc:
                delay = min(300, 5 * 2 ** min(failures, 6))
                self.retry[key] = failures + 1, time.monotonic() + delay
                status = f"HTTP {exc.code}" if isinstance(exc, error.HTTPError) else type(exc).__name__
                LOG.warning("账号 #%s：上传失败（%s），%s 秒后重试", index, status, delay)
                continue
            self.state[key] = {"digest": digest, "profile_id": pid}
            self.retry.pop(key, None)
            try:
                self.save_state()
            except OSError:
                LOG.error("无法保存同步状态；请检查状态目录写入权限")
                # Retry persistence on the next poll, without repeating the upload.
                self.state_dirty = True
            LOG.info("账号 #%s：认证已自动同步，服务端账号 ID=%s", index, pid)
        if getattr(self, "state_dirty", False):
            try:
                self.save_state()
                self.state_dirty = False
            except OSError:
                pass


def run_agent(config_path, stop=None, auth_file=None, check=False):
    global _AUTH_FILE_OVERRIDE
    previous = _AUTH_FILE_OVERRIDE
    _AUTH_FILE_OVERRIDE = auth_file
    try:
        return _run_agent(config_path, stop, check)
    finally:
        _AUTH_FILE_OVERRIDE = previous


def _run_agent(config_path, stop, check):
    if stop is None:
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    else:
        # Services run in Session 0 without a usable console. All Agent output
        # goes to its rotating file handler instead of inherited stdio handles.
        logging.basicConfig(level=logging.INFO, handlers=[logging.NullHandler()])
    config_path = config_path.resolve()
    lock_file = None
    handler = None
    try:
        url, interval, watches = load_config(config_path)
        if check:
            LOG.info("连接配置有效，监测账号由服务端设置")
            return 0
        if os.name == "nt":
            import msvcrt
            lock_file = config_path.with_suffix(".lock").open("a+b")
            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                LOG.error("该配置的 Agent 已运行，请勿重复启动")
                lock_file.close()
                return 1
        handler = RotatingFileHandler(config_path.with_suffix(".log"), maxBytes=1024 * 1024,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(handler)
        agent = AuthAgent(url, watches, config_path.with_suffix(".state.json"))
    except (OSError, ValueError, TypeError, AttributeError):
        LOG.error("配置或同步状态无效，请检查 agent_config.json、路径和文件权限")
        if lock_file is not None:
            lock_file.close()
        if handler is not None:
            LOG.removeHandler(handler)
            handler.close()
        return 1
    if stop is None:
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    LOG.info("Agent 已启动，等待服务端下发监测账号，每 %s 秒检查一次文件", interval)
    heartbeat_thread = threading.Thread(target=agent.heartbeat_loop, args=(stop,), daemon=True)
    heartbeat_thread.start()
    try:
        while not stop.is_set():
            agent.check_once()
            stop.wait(interval)
    finally:
        stop.set()
        heartbeat_thread.join(timeout=11)
        if lock_file is not None:
            lock_file.close()
        LOG.info("Agent 已停止")
        LOG.removeHandler(handler)
        handler.close()
    return 0


def main():
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Codex 认证 Agent / Windows 服务")
    parser.add_argument("--config", type=Path, default=base / "agent_config.json")
    parser.add_argument("--check", action="store_true", help="仅检查配置，不读取或上传认证")
    parser.add_argument("--service", action="store_true", help="由 Windows 服务管理器调用")
    parser.add_argument("--auth-file", type=Path, help="实际登录用户的认证路径（服务安装时自动记录）")
    args = parser.parse_args()
    if args.service and not args.check:
        if args.auth_file is None or not args.auth_file.is_absolute():
            parser.error("Windows 服务必须指定绝对路径 --auth-file")
        from windows_service import dispatch
        return dispatch(args.config.resolve(), args.auth_file.resolve())
    return run_agent(args.config, auth_file=args.auth_file.resolve() if args.auth_file else None, check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
