import json
import base64
import hashlib
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
import httpx
from database import get_connection

USAGE_API_URL = "https://chatgpt.com/backend-api/wham/usage"
TOKEN_REFRESH_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_REFRESH_WINDOW_SECONDS = 300
QUOTA_OBSERVATION_RETENTION_DAYS = 14
QUOTA_OBSERVATION_CLEANUP_BATCH_SIZE = 5000
MODEL_CATALOG_URL = "https://api.openai.com/v1/models"
MODEL_RESPONSE_URL = "https://api.openai.com/v1/responses"


def trigger_quota_cycle(auth_json: dict) -> dict:
    """用一次短模型请求尝试启动额度周期；失败不覆盖已查询的额度。"""
    token = (auth_json.get("tokens") or {}).get("access_token")
    if not token:
        return {"success": False, "error": "缺少模型请求所需的 access_token"}
    model = os.getenv("QUOTA_TRIGGER_MODEL", "").strip()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=15.0) as client:
            catalog = client.get(MODEL_CATALOG_URL, headers=headers)
            if catalog.status_code != 200:
                return {"success": False, "error": f"模型列表查询失败 (HTTP {catalog.status_code})"}
            models = [m.get("slug") for m in catalog.json().get("models", [])
                      if isinstance(m, dict) and m.get("visibility") == "list" and m.get("slug")]
            if model and model not in models:
                return {"success": False, "error": "指定模型不在当前账号可用模型列表中"}
            if not models:
                return {"success": False, "error": "当前账号未返回可用模型"}
            model = model or models[0]
            deadline = time.monotonic() + 30
            with client.stream("POST", MODEL_RESPONSE_URL, headers={**headers, "Accept": "text/event-stream"},
                               json={"model": model, "input": [{"role": "user", "content": "Reply only OK."}],
                                     "instructions": "Reply with exactly OK. Do not use tools.",
                                     "store": False, "stream": True}) as response:
                if response.status_code != 200:
                    return {"success": False, "model": model,
                            "error": f"周期触发请求失败 (HTTP {response.status_code})"}
                data_lines = []
                for line in response.iter_lines():
                    if time.monotonic() > deadline:
                        return {"success": False, "model": model, "error": "周期触发请求超时"}
                    if line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif not line and data_lines:
                        data = "\n".join(data_lines)
                        data_lines = []
                        if data == "[DONE]":
                            break
                        event = json.loads(data)
                        kind = event.get("type")
                        if kind == "response.completed":
                            return {"success": True, "model": model}
                        if kind in ("response.failed", "response.incomplete", "error"):
                            return {"success": False, "model": model, "error": "周期触发模型请求未完成"}
        return {"success": False, "model": model, "error": "周期触发响应中断，未确认完成"}
    except Exception:
        # 不回传异常或模型响应正文，避免凭据和远端内容进入日志/API。
        return {"success": False, "error": "周期触发请求失败，请稍后重试"}


def cleanup_quota_observations() -> int:
    """分批删除过期快照，避免积压历史在一次事务中全部删除。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM quota_observations WHERE id IN (
                    SELECT id FROM quota_observations
                    WHERE observed_at < CURRENT_TIMESTAMP - %s * INTERVAL '1 day'
                    ORDER BY observed_at, id
                    LIMIT %s
                );
            """, (QUOTA_OBSERVATION_RETENTION_DAYS, QUOTA_OBSERVATION_CLEANUP_BATCH_SIZE))
            deleted = cur.rowcount
        conn.commit()
    return deleted


class TokenRefreshError(ValueError):
    def __init__(self, message, auth_status="error"):
        super().__init__(message)
        self.auth_status = auth_status


def auth_tokens_hash(auth_json: dict) -> str:
    return hashlib.sha256(json.dumps(auth_json.get("tokens") or {}, sort_keys=True).encode()).hexdigest()


def build_profile_auth_status(profile: dict) -> dict:
    if not profile.get("is_active", True):
        return {"status": "disabled", "error": "", "checked_at": None}
    if not (profile.get("auth_json", {}).get("tokens") or {}).get("access_token"):
        return {"status": "unsupported", "error": "当前认证不支持账号额度验证", "checked_at": None}
    matches = profile.get("auth_status_tokens_hash") == auth_tokens_hash(profile.get("auth_json") or {})
    checked = profile.get("auth_checked_at") if matches else None
    return {"status": profile.get("auth_status", "unchecked") if matches else "unchecked",
            "error": profile.get("auth_status_error", "") if matches else "",
            "checked_at": checked.isoformat() if checked else None}


def update_profile_auth_status(profile_id: int, result: dict, checked_auth: dict):
    """仅更新本次实际验证的凭据，避免覆盖刚上传新 JSON 的状态。"""
    status = "valid" if result.get("success") else result.get("auth_status", "error")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE auth_profiles SET auth_status = %s, auth_status_error = %s,
                    auth_checked_at = CURRENT_TIMESTAMP, auth_status_tokens_hash = %s
                WHERE id = %s AND COALESCE(auth_json->'tokens', '{}'::jsonb) = %s::jsonb;
            """, (status, "" if result.get("success") else result.get("error", "验证失败"),
                  auth_tokens_hash(checked_auth), profile_id, json.dumps(checked_auth.get("tokens") or {})))
        conn.commit()


def token_remaining_seconds(token: str, now: Optional[datetime] = None) -> Optional[int]:
    """JWT 到期时间仅用作刷新提示，不作为认证验证结果。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        expiry = payload.get("exp")
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
            return None
        return int(expiry - (now or datetime.now(timezone.utc)).timestamp())
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _refresh_auth(client, auth_json: dict) -> dict:
    tokens = auth_json.get("tokens") or {}
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise TokenRefreshError("Token已过期且缺少 refresh_token，请重新登录并上传 auth.json", "invalid")
    try:
        response = client.post(TOKEN_REFRESH_URL, json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CODEX_CLIENT_ID,
        })
    except httpx.HTTPError:
        raise ValueError("Token刷新网络请求失败，请稍后重试") from None
    if response.status_code != 200:
        # 不返回 OAuth 响应正文，避免凭据进入接口响应或日志。
        if response.status_code in (400, 401, 403):
            raise TokenRefreshError("refresh_token 已失效或被撤销，请重新登录并上传 auth.json", "invalid")
        raise ValueError(f"Token刷新失败 (HTTP {response.status_code})，请稍后重试")
    try:
        data = response.json()
    except ValueError:
        raise ValueError("Token刷新接口返回无效 JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"].strip():
        raise ValueError("Token刷新接口未返回有效 access_token")
    new_tokens = dict(tokens)
    for key in ("access_token", "refresh_token", "id_token"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            new_tokens[key] = value
    return {**auth_json, "tokens": new_tokens,
            "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}


def _refresh_profile_auth(client, profile_id: int, stale_auth: dict, require_active: bool = False) -> dict:
    # 行锁覆盖读取、OAuth 刷新和落库，避免多线程/多进程重复使用轮换凭据。
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT auth_json, is_active FROM auth_profiles WHERE id = %s FOR UPDATE;", (profile_id,))
            profile = cur.fetchone()
            if not profile:
                raise ValueError("未找到该配置")
            current_auth = profile["auth_json"]
            if require_active and not profile["is_active"]:
                return current_auth
            if current_auth.get("tokens") != stale_auth.get("tokens"):
                return current_auth
            refreshed = _refresh_auth(client, current_auth)
            cur.execute("UPDATE auth_profiles SET auth_json = %s::jsonb, updated_at = CURRENT_TIMESTAMP WHERE id = %s;",
                        (json.dumps(refreshed), profile_id))
        conn.commit()
    return refreshed

def format_duration(seconds: Optional[int]) -> str:
    if seconds is None or seconds < 0:
        return "未知"
    if seconds >= 86400:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days}天{hours}小时" if hours > 0 else f"{days}天"
    elif seconds >= 3600:
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        return f"{hours}小时{mins}分" if mins > 0 else f"{hours}小时"
    elif seconds >= 60:
        mins = seconds // 60
        secs = seconds % 60
        return f"{mins}分{secs}秒" if secs > 0 else f"{mins}分"
    else:
        return f"{seconds}秒"

def format_plan_type(raw_plan: str) -> str:
    """将 OpenAI 内部晦涩代号 (如 SELF_SERVE_BUSINESS_PROLITE) 转换为通用的订阅套餐名称"""
    if not raw_plan:
        return "Team 团队版"
    p = raw_plan.upper().strip()
    if any(k in p for k in ["PROLITE", "BUSINESS", "TEAM"]):
        return "Team 团队版"
    if "ENTERPRISE" in p:
        return "Enterprise 企业版"
    if "PLUS" in p:
        return "Plus 会员"
    if "PRO" in p:
        return "Pro 专业版"
    if "FREE" in p:
        return "Free 免费版"
    return raw_plan


def parse_quota_windows(data: dict, now: Optional[datetime] = None,
                        observed_at: Optional[datetime] = None) -> Dict[str, Any]:
    """按官方返回的周期识别窗口，primary/secondary 不代表固定周期。"""
    now = now or datetime.now(timezone.utc)
    windows = {}
    for key, name in (("five_hour", "5小时额度"), ("weekly", "本周额度")):
        windows[key] = {
            "name": name, "available": False, "window_seconds": None,
            "used_percent": None, "remaining_percent": None,
            "reset_after_seconds": None, "reset_after_text": "未知",
            "reset_at": None, "reset_at_epoch": None, "limit_reached": False,
            "reset_source": None,
        }
    rate_limit = data.get("rate_limit") or {}
    for field in ("primary_window", "secondary_window"):
        window = rate_limit.get(field)
        if not isinstance(window, dict):
            continue
        period = window.get("limit_window_seconds")
        key = {18000: "five_hour", 604800: "weekly"}.get(period)
        if key is None:
            continue
        used = window.get("used_percent")
        if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
            used = None
        reset_epoch = window.get("reset_at")
        reset_time = None
        if isinstance(reset_epoch, (int, float)) and not isinstance(reset_epoch, bool):
            try:
                reset_time = datetime.fromtimestamp(reset_epoch, tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                reset_epoch = None
        else:
            reset_epoch = None
        seconds = window.get("reset_after_seconds")
        reported_seconds = seconds if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else None
        reset_source = "official" if reset_time is not None else None
        if reset_time is None and isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            try:
                reset_time = (observed_at or now) + timedelta(seconds=max(0, seconds))
                reset_epoch = reset_time.timestamp()
                reset_source = "estimated"
            except (ValueError, OverflowError):
                seconds = None
        if reset_time is not None:
            seconds = max(0, int((reset_time - now).total_seconds()))
        elif isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            seconds = max(0, int(seconds))
        else:
            seconds = None
        windows[key].update({
            "available": True, "window_seconds": period,
            "used_percent": used, "remaining_percent": 100 - used if used is not None else None,
            "reset_after_seconds": seconds, "reset_after_text": format_duration(seconds),
            "reset_at": reset_time.isoformat() if reset_time else None,
            "reset_at_epoch": reset_epoch, "limit_reached": used is not None and used >= 100,
            "reset_source": reset_source,
            "reported_reset_after_seconds": reported_seconds,
        })
    return windows


def build_profile_quota(profile: dict, now: Optional[datetime] = None) -> Dict[str, Any]:
    """从原始缓存重建额度，兼容此前缓存中错误的固定窗口映射。"""
    raw = profile.get("quota_raw") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    updated = profile.get("quota_last_updated")
    windows = parse_quota_windows(raw, now, observed_at=updated)
    return {
        "has_data": bool(updated) and any(w["available"] for w in windows.values()),
        "plan_type": profile.get("plan_type") or "CODEX",
        **windows,
        "last_updated": updated.isoformat() if updated else None,
    }

def fetch_codex_quota(auth_json: dict, profile_id: Optional[int] = None) -> Dict[str, Any]:
    result = _fetch_codex_quota(auth_json, profile_id)
    checked_auth = result.pop("_checked_auth", auth_json)
    if profile_id is not None:
        try:
            update_profile_auth_status(profile_id, result, checked_auth)
        except Exception:
            logging.getLogger(__name__).error("[AuthStatus] 配置 %s 验证状态保存失败", profile_id)
    if result.get("success") and any(
        window.get("available") and window.get("remaining_percent") == 100
        for window in (result.get("primary_5h") or {}, result.get("secondary_weekly") or {})
    ):
        result["cycle_trigger"] = trigger_quota_cycle(checked_auth)
        if not result["cycle_trigger"]["success"]:
            logging.getLogger(__name__).warning("[QuotaCycle] 配置 %s 模型请求未完成：%s", profile_id,
                                                result["cycle_trigger"]["error"])
    elif result.get("success"):
        result["cycle_trigger"] = {"success": True, "skipped": True}
    return result


def _fetch_codex_quota(auth_json: dict, profile_id: Optional[int] = None) -> Dict[str, Any]:
    """从 OpenAI 官方 backend-api 获取当前账号的 5小时与周额度"""
    tokens = auth_json.get("tokens") or {}
    def outcome(**result):
        return {**result, "_checked_auth": auth_json}
    token = tokens.get("access_token") or tokens.get("id_token")
    if not token:
        return outcome(success=False, auth_status="unsupported", error="未找到 access_token，无法获取官方额度")

    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    try:
        with httpx.Client(timeout=8.0) as client:
            remaining = token_remaining_seconds(token)
            did_refresh = False
            if remaining is not None and remaining <= TOKEN_REFRESH_WINDOW_SECONDS and tokens.get("refresh_token"):
                try:
                    refreshed = (_refresh_profile_auth(client, profile_id, auth_json)
                                 if profile_id is not None else _refresh_auth(client, auth_json))
                except ValueError as e:
                    return outcome(success=False, error=str(e), auth_status=getattr(e, "auth_status", "error"))
                except Exception:
                    return outcome(success=False, error="Token刷新凭据读取或保存失败，请检查服务端数据库")
                auth_json = refreshed
                headers["Authorization"] = f"Bearer {refreshed.get('tokens', {}).get('access_token', '')}"
                did_refresh = True
            resp = client.get(USAGE_API_URL, headers=headers)
            if resp.status_code == 401:
                if did_refresh:
                    return outcome(success=False, auth_status="invalid", error="Token刷新后仍无法认证 (401 Unauthorized)，请重新登录并上传 auth.json")
                try:
                    refreshed = (_refresh_profile_auth(client, profile_id, auth_json)
                                 if profile_id is not None else _refresh_auth(client, auth_json))
                except ValueError as e:
                    return outcome(success=False, error=str(e), auth_status=getattr(e, "auth_status", "error"))
                except Exception:
                    return outcome(success=False, error="Token刷新凭据读取或保存失败，请检查服务端数据库")
                auth_json = refreshed
                # 刷新结果先保存，即便随后的额度查询失败，也不丢失轮换后的 token。
                headers["Authorization"] = f"Bearer {refreshed.get('tokens', {}).get('access_token', '')}"
                resp = client.get(USAGE_API_URL, headers=headers)
                if resp.status_code == 401:
                    return outcome(success=False, auth_status="invalid", error="Token刷新后仍无法认证 (401 Unauthorized)，请重新登录并上传 auth.json")
            if resp.status_code == 429:
                return outcome(success=False, auth_status="error", error="查询频率超限 (429 Rate Limit)")
            if resp.status_code != 200:
                return outcome(success=False, auth_status="error", error=f"API返回错误状态码: {resp.status_code}")

            data = resp.json()
    except Exception as e:
        return outcome(success=False, auth_status="error", error="额度验证请求失败，请稍后重试")

    email = data.get("email") or ""
    raw_plan = (data.get("plan_type") or "").upper()
    plan_type = format_plan_type(raw_plan)
    rate_limit = data.get("rate_limit") or {}
    observed_at = datetime.now(timezone.utc)
    windows = parse_quota_windows(data, observed_at)
    return outcome(**{
        "success": True,
        "email": email,
        "plan_type": plan_type,
        "limit_reached": rate_limit.get("limit_reached", False),
        "primary_5h": windows["five_hour"],
        "secondary_weekly": windows["weekly"],
        "raw": data,
        "observed_at": observed_at.isoformat(),
    })


def update_profile_quota_in_db(profile_id: int, quota_info: Dict[str, Any]):
    """将获取到的额度写回数据库缓存"""
    if not quota_info.get("success"):
        return

    p5 = quota_info["primary_5h"]
    pw = quota_info["secondary_weekly"]

    raw = quota_info.get("raw") or {}
    p_reset_at = p5.get("reset_at_epoch")
    s_reset_at = pw.get("reset_at_epoch")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE auth_profiles SET
                    plan_type = %s,
                    account_email = CASE WHEN account_email = '' THEN %s ELSE account_email END,
                    quota_5h_used = %s,
                    quota_5h_reset_seconds = %s,
                    quota_5h_reset_at = TO_TIMESTAMP(%s),
                    quota_weekly_used = %s,
                    quota_weekly_reset_seconds = %s,
                    quota_weekly_reset_at = TO_TIMESTAMP(%s),
                    quota_last_updated = %s,
                    quota_raw = %s
                WHERE id = %s;
            """, (
                quota_info.get("plan_type", ""),
                quota_info.get("email", ""),
                p5["used_percent"],
                p5["reset_after_seconds"],
                p_reset_at,
                pw["used_percent"],
                pw["reset_after_seconds"],
                s_reset_at,
                quota_info.get("observed_at") or datetime.now(timezone.utc),
                json.dumps(raw),
                profile_id
            ))
            # 与缓存同事务保存每次成功查询；只保存额度字段，不保存认证凭据。
            cur.execute("""
                INSERT INTO quota_observations (profile_id, observed_at, windows)
                SELECT id, quota_last_updated, %s::jsonb FROM auth_profiles WHERE id = %s;
            """, (json.dumps({"five_hour": p5, "weekly": pw}), profile_id))
        conn.commit()
