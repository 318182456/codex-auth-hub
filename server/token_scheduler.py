"""服务端独立 token 保活任务，无需用户访问页面。"""
import logging
import threading
import time
from datetime import datetime, timezone

import httpx

from database import get_connection
from quota_service import (TOKEN_REFRESH_WINDOW_SECONDS, _refresh_profile_auth, token_remaining_seconds,
                           fetch_codex_quota, update_profile_quota_in_db, update_profile_auth_status)

logger = logging.getLogger(__name__)
TOKEN_CHECK_INTERVAL_SECONDS = 60
TOKEN_FAILURE_RETRY_SECONDS = 300
QUOTA_CHECK_INTERVAL_SECONDS = 90


class TokenRefreshScheduler:
    def __init__(self, interval=TOKEN_CHECK_INTERVAL_SECONDS):
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="token-refresh", daemon=True)
        self.failed_profiles = {}

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def join(self):
        self.thread.join()

    def _run(self):
        # 启动立即检查；上一轮完成后再等待，避免刷新任务重叠。
        while not self.stop_event.is_set():
            try:
                self.check_once()
            except Exception:
                # 不输出数据库连接串、token 或远端响应正文。
                logger.error("[TokenRefresh] 账号扫描失败，下一轮重试")
            self.stop_event.wait(self.interval)

    def check_once(self):
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, auth_json, quota_last_updated FROM auth_profiles WHERE is_active = TRUE ORDER BY id;")
                profiles = cur.fetchall()

        active_ids = {p["id"] for p in profiles}
        self.failed_profiles = {pid: state for pid, state in self.failed_profiles.items() if pid in active_ids}
        with httpx.Client(timeout=8.0) as client:
            for profile in profiles:
                if self.stop_event.is_set():
                    break
                pid = profile["id"]
                auth = profile["auth_json"]
                tokens = auth.get("tokens") or {}
                if not tokens.get("access_token"):
                    self.failed_profiles.pop(pid, None)
                    continue
                remaining = token_remaining_seconds(tokens.get("access_token"))
                due = remaining is not None and remaining <= TOKEN_REFRESH_WINDOW_SECONDS and tokens.get("refresh_token")
                last_quota = profile.get("quota_last_updated")
                quota_due = last_quota is None or (datetime.now(timezone.utc) - last_quota).total_seconds() >= QUOTA_CHECK_INTERVAL_SECONDS
                if not due and not quota_due:
                    self.failed_profiles.pop(pid, None)
                    continue
                # 失败后间隔 5 分钟重试；上传新凭据后取消旧失败的等待。
                previous = self.failed_profiles.get(pid)
                if previous and previous[0] == tokens and time.monotonic() < previous[1]:
                    continue
                refresh_finished = False
                try:
                    if due:
                        auth = _refresh_profile_auth(client, pid, auth, require_active=True)
                        refresh_finished = True
                    if quota_due:
                        quota = fetch_codex_quota(auth, profile_id=pid)
                        if not quota.get("success"):
                            raise ValueError("额度查询或凭据刷新失败")
                        update_profile_quota_in_db(pid, quota)
                except Exception as exc:
                    if due and not refresh_finished:
                        try:
                            update_profile_auth_status(pid, {"success": False,
                                "auth_status": getattr(exc, "auth_status", "error"),
                                "error": str(exc) if isinstance(exc, ValueError) else "认证验证暂时失败，请稍后重试"}, auth)
                        except Exception:
                            logger.error("[AuthStatus] 配置 %s 验证状态保存失败", pid)
                    self.failed_profiles[pid] = (dict(tokens), time.monotonic() + TOKEN_FAILURE_RETRY_SECONDS)
                    logger.warning("[TokenRefresh] 配置 %s 刷新失败，5 分钟后重试；若持续失败请重新登录并上传 auth.json", pid)
                else:
                    self.failed_profiles.pop(pid, None)
                    logger.info("[TokenRefresh] 配置 %s 凭据检查完成", pid)
