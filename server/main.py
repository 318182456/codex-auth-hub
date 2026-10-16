import os
import sys
import json
import base64
import uuid
import asyncio
from datetime import date, datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

# 兼容 starlette 1.6+ 与 fastapi 的 Router 启动参数
import starlette.routing
_orig_router_init = starlette.routing.Router.__init__
def _compat_router_init(self, *args, **kwargs):
    kwargs.pop("on_startup", None)
    kwargs.pop("on_shutdown", None)
    return _orig_router_init(self, *args, **kwargs)
starlette.routing.Router.__init__ = _compat_router_init

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Depends, BackgroundTasks
FastAPI.max_body_size = None
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse
from pydantic import BaseModel
from psycopg.errors import UniqueViolation

import secrets
from database import get_connection, init_db, hash_password, verify_password
from quota_service import fetch_codex_quota, update_profile_quota_in_db, format_duration, build_profile_quota, build_profile_auth_status
from token_scheduler import TokenRefreshScheduler
from agent_service import register_agent_routes

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = TokenRefreshScheduler()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()
        await asyncio.to_thread(scheduler.join)

app = FastAPI(title="Codex Auth Hub", version="1.1.0", lifespan=lifespan)
register_agent_routes(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 账号占用不随心跳过期；心跳仅用于判断客户端是否在线。
HEARTBEAT_ONLINE_THRESHOLD_SECONDS = 120  # 心跳在 120 秒内判定为实时在线
QUOTA_REFRESH_INTERVAL_SECONDS = 90

def get_real_client_ip(request: Request) -> str:
    """提取客户端真实 IP，兼容跨网段、VPN、Tailscale 与反向代理 (Nginx/FRP/Cloudflare)"""
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    x_real_ip = request.headers.get("x-real-ip")
    if x_real_ip:
        return x_real_ip.strip()
    return request.client.host if request.client else "unknown"

def extract_email_from_auth(auth_data: dict) -> str:
    tokens = auth_data.get("tokens", {})
    id_token = tokens.get("id_token") or tokens.get("access_token")
    if id_token and isinstance(id_token, str) and "." in id_token:
        try:
            parts = id_token.split(".")
            if len(parts) >= 2:
                payload_b64 = parts[1]
                padding = 4 - (len(payload_b64) % 4)
                if padding != 4:
                    payload_b64 += "=" * padding
                payload_json = base64.urlsafe_b64decode(payload_b64).decode("utf-8", errors="ignore")
                payload = json.loads(payload_json)
                email = payload.get("email") or payload.get("https://api.openai.com/profile", {}).get("email")
                if email:
                    return str(email)
        except Exception:
            pass
    return ""

def validate_auth_payload(auth_obj: Any) -> dict:
    if not isinstance(auth_obj, dict):
        raise HTTPException(status_code=400, detail="auth.json 必须是合法的 JSON 对象")
    tokens = auth_obj.get("tokens") or {}
    has_token = bool(tokens.get("access_token"))
    has_key = bool(auth_obj.get("OPENAI_API_KEY"))
    if not (has_token or has_key):
        raise HTTPException(
            status_code=400, 
            detail="上传的 JSON 未包含有效的 access_token 或 OPENAI_API_KEY"
        )
    return auth_obj

def sync_profile_quota(profile_id: int, auth_json: dict):
    """后台任务同步额度"""
    try:
        quota_info = fetch_codex_quota(auth_json, profile_id=profile_id)
        if quota_info.get("success"):
            update_profile_quota_in_db(profile_id, quota_info)
    except Exception as e:
        print(f"[QuotaSync] Error for profile {profile_id}: {e}")

class ProfileCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    auth_json: Dict[str, Any]
    max_users: Optional[int] = 0

class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    max_users: Optional[int] = None


class ProfileRename(BaseModel):
    name: str

class SessionApply(BaseModel):
    profile_id: int
    client_user: str
    client_hostname: str
    session_token: Optional[str] = None

class SessionHeartbeat(BaseModel):
    session_token: str

class SessionRelease(BaseModel):
    session_token: str

class UserRegister(BaseModel):
    username: str
    password: str
    display_name: Optional[str] = ""

class UserLogin(BaseModel):
    username: str
    password: str

def get_current_user_from_request(request: Request) -> Optional[dict]:
    token = None
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        token = request.query_params.get("token")
    if not token:
        return None

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.username, u.display_name, u.role, u.is_active
                FROM auth_tokens t
                JOIN app_users u ON t.user_id = u.id
                WHERE t.token = %s;
            """, (token,))
            user = cur.fetchone()
            if user and user["is_active"]:
                cur.execute("UPDATE auth_tokens SET last_used = CURRENT_TIMESTAMP WHERE token = %s;", (token,))
                conn.commit()
                return user
    return None

@app.post("/api/auth/register")
def register_user(req: UserRegister):
    username = req.username.strip()
    password = req.password.strip()
    display_name = (req.display_name or "").strip() or username
    if len(username) < 2:
        raise HTTPException(status_code=400, detail="用户名长度至少2个字符")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="密码长度至少4个字符")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_users WHERE username = %s;", (username,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="该用户名已存在，请直接登录或更换用户名")

            cur.execute("""
                INSERT INTO app_users (username, display_name, password_hash, role)
                VALUES (%s, %s, %s, 'user')
                RETURNING id, username, display_name, role;
            """, (username, display_name, hash_password(password)))
            user = cur.fetchone()

            token = secrets.token_hex(32)
            cur.execute("""
                INSERT INTO auth_tokens (token, user_id, username)
                VALUES (%s, %s, %s);
            """, (token, user["id"], username))
        conn.commit()

    return {
        "success": True,
        "token": token,
        "user": user
    }

@app.post("/api/auth/login")
def login_user(req: UserLogin):
    username = req.username.strip()
    password = req.password.strip()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, username, display_name, password_hash, role, is_active
                FROM app_users
                WHERE username = %s;
            """, (username,))
            user = cur.fetchone()
            if not user or not verify_password(user["password_hash"], password):
                raise HTTPException(status_code=401, detail="用户名或密码错误")

            if not user["is_active"]:
                raise HTTPException(status_code=403, detail="该账号已被管理员禁用")

            cur.execute("UPDATE app_users SET last_login = CURRENT_TIMESTAMP WHERE id = %s;", (user["id"],))

            token = secrets.token_hex(32)
            cur.execute("""
                INSERT INTO auth_tokens (token, user_id, username)
                VALUES (%s, %s, %s);
            """, (token, user["id"], username))
        conn.commit()

    return {
        "success": True,
        "token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"] or user["username"],
            "role": user["role"]
        }
    }

@app.get("/api/auth/me")
def get_current_user_profile(request: Request):
    user = get_current_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或 Token 已失效")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.profile_id, p.name as profile_name, s.session_token, s.connected_at
                FROM user_sessions s
                JOIN auth_profiles p ON s.profile_id = p.id
                WHERE s.client_user = %s
                ORDER BY s.connected_at DESC
                LIMIT 1;
            """, (user["username"],))
            active_session = cur.fetchone()

    return {
        "user": {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"] or user["username"],
            "role": user["role"]
        },
        "active_session": {
            "profile_id": active_session["profile_id"],
            "profile_name": active_session["profile_name"],
            "session_token": active_session["session_token"],
            "connected_at": active_session["connected_at"].isoformat() if active_session and active_session["connected_at"] else None
        } if active_session else None
    }

@app.get("/api/users")
def list_team_users():
    """获取所有团队成员及其实时使用状态 (按 user 分开不同的人)"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.username, u.display_name, u.role, u.is_active, u.created_at, u.last_login,
                       s.profile_id, p.name as active_profile_name, s.connected_at, s.last_heartbeat
                FROM app_users u
                LEFT JOIN user_sessions s ON s.client_user = u.username
                LEFT JOIN auth_profiles p ON s.profile_id = p.id
                ORDER BY u.id ASC;
            """)
            rows = cur.fetchall()

    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "username": r["username"],
            "display_name": r["display_name"] or r["username"],
            "role": r["role"],
            "is_active": r["is_active"],
            "last_login": r["last_login"].isoformat() if r["last_login"] else None,
            "active_profile": {
                "id": r["profile_id"],
                "name": r["active_profile_name"],
                "connected_at": r["connected_at"].isoformat() if r["connected_at"] else None
            } if r["profile_id"] else None
        })
    return result

class UserCreateByAdmin(BaseModel):
    username: str
    password: str
    display_name: Optional[str] = ""
    role: Optional[str] = "user"

@app.post("/api/admin/users")
def admin_create_user(req: UserCreateByAdmin):
    """管理员添加新用户"""
    username = req.username.strip()
    password = req.password.strip()
    display_name = (req.display_name or "").strip() or username
    role = req.role if req.role in ("admin", "user") else "user"

    if len(username) < 2:
        raise HTTPException(status_code=400, detail="用户名长度至少2个字符")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="密码长度至少4个字符")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_users WHERE username = %s;", (username,))
            if cur.fetchone():
                raise HTTPException(status_code=400, detail="该用户名已存在，请勿重复添加")

            cur.execute("""
                INSERT INTO app_users (username, display_name, password_hash, role)
                VALUES (%s, %s, %s, %s)
                RETURNING id, username, display_name, role;
            """, (username, display_name, hash_password(password), role))
            user = cur.fetchone()
        conn.commit()

    return {"success": True, "user": user}

@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int):
    """管理员删除用户"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, username FROM app_users WHERE id = %s;", (user_id,))
            u = cur.fetchone()
            if not u:
                raise HTTPException(status_code=404, detail="未找到该用户")
            if u["username"] == "admin":
                raise HTTPException(status_code=400, detail="默认超级管理员账号 admin 不可删除")

            # 清理该用户的所有活跃会话
            cur.execute("DELETE FROM user_sessions WHERE client_user = %s;", (u["username"],))
            cur.execute("DELETE FROM app_users WHERE id = %s;", (user_id,))
        conn.commit()
    return {"success": True}

@app.post("/api/admin/users/{username}/kick")
def admin_kick_user(username: str, request: Request):
    """管理员强制释放某用户的账号占用 (下线)"""
    client_ip = get_real_client_ip(request)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM user_sessions
                WHERE client_user = %s
                RETURNING profile_id, client_hostname;
            """, (username,))
            rows = cur.fetchall()
            for r in rows:
                cur.execute("""
                    INSERT INTO activity_logs (profile_id, client_user, client_hostname, client_ip, action, details)
                    VALUES (NULL, %s, %s, %s, 'KICK', '管理员在控制台强制释放其占用的账号');
                """, (username, r.get("client_hostname") or "unknown", client_ip))
        conn.commit()
    return {"success": True, "kicked_count": len(rows)}

@app.get("/api/profiles")
def list_profiles(background_tasks: BackgroundTasks):
    """获取所有服务端配置、当前在线人员名单以及【5小时与周剩余额度】"""
    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. 查询所有 profiles 及额度缓存
            cur.execute("""
                SELECT id, name, description, auth_json, account_email, is_active, max_users,
                       plan_type, quota_5h_used, quota_5h_reset_at, quota_5h_reset_seconds,
                       quota_weekly_used, quota_weekly_reset_at, quota_weekly_reset_seconds,
                       quota_last_updated, quota_raw,
                       auth_status, auth_status_error, auth_checked_at, auth_status_tokens_hash,
                       created_at, updated_at
                FROM auth_profiles
                ORDER BY id ASC;
            """)
            profiles = cur.fetchall()

            # 2. 查询活跃 sessions (永久落库保留，直到用户主动释放、切换或管理员释放)
            cur.execute("""
                SELECT s.profile_id, s.client_user, COALESCE(u.display_name, s.client_user) as display_name,
                       s.client_hostname, s.client_ip, s.session_token,
                       s.connected_at, s.last_heartbeat
                FROM user_sessions s
                LEFT JOIN app_users u ON s.client_user = u.username
                ORDER BY s.connected_at ASC;
            """)
            active_sessions = cur.fetchall()

            # 3. 统计各 profile 历史被使用的总人次
            cur.execute("""
                SELECT profile_id, COUNT(*) as total_count
                FROM activity_logs
                WHERE action = 'APPLY'
                GROUP BY profile_id;
            """)
            usage_counts = {row["profile_id"]: row["total_count"] for row in cur.fetchall()}

    sessions_by_profile: Dict[int, List[Dict[str, Any]]] = {}
    for s in active_sessions:
        pid = s["profile_id"]
        if pid not in sessions_by_profile:
            sessions_by_profile[pid] = []
        conn_at = s["connected_at"]
        duration_sec = int((now - conn_at).total_seconds()) if conn_at else 0
        disp_name = s.get("display_name") or s["client_user"]
        last_hb = s.get("last_heartbeat")
        is_online = (now - last_hb).total_seconds() <= HEARTBEAT_ONLINE_THRESHOLD_SECONDS if last_hb else False
        online_tag = "在线" if is_online else "在用"
        sessions_by_profile[pid].append({
            "client_user": s["client_user"],
            "display_name": disp_name,
            "client_hostname": s["client_hostname"],
            "client_ip": s["client_ip"],
            "session_token": s["session_token"],
            "is_online": is_online,
            "online_tag": online_tag,
            "connected_at": conn_at.isoformat() if conn_at else None,
            "duration_text": format_duration(duration_sec),
            "last_heartbeat": last_hb.isoformat() if last_hb else None,
        })

    result = []
    for p in profiles:
        pid = p["id"]
        users = sessions_by_profile.get(pid, [])

        # 检查是否需要后台静默刷新额度（超过90秒未更新或从未更新）
        last_quota_up = p["quota_last_updated"]
        should_refresh_quota = False
        if not last_quota_up:
            should_refresh_quota = True
        elif (now - last_quota_up).total_seconds() > QUOTA_REFRESH_INTERVAL_SECONDS:
            should_refresh_quota = True

        if should_refresh_quota and p["is_active"]:
            background_tasks.add_task(sync_profile_quota, pid, p["auth_json"])

        quota_data = build_profile_quota(p, now)

        result.append({
            "id": pid,
            "name": p["name"],
            "description": p["description"],
            "account_email": p["account_email"],
            "plan_type": p["plan_type"],
            "is_active": p["is_active"],
            "max_users": p["max_users"],
            "quota": quota_data,
            "auth": build_profile_auth_status(p),
            "created_at": p["created_at"].isoformat() if p["created_at"] else None,
            "updated_at": p["updated_at"].isoformat() if p["updated_at"] else None,
            "total_usage_count": usage_counts.get(pid, 0),
            "active_users_count": len(users),
            "active_users": users
        })
    return result

@app.get("/api/profiles/{profile_id}/quota-history")
def get_quota_history(profile_id: int, day: Optional[date] = None):
    """按北京时间查询当天最多 2000 次成功观测。"""
    china_tz = timezone(timedelta(hours=8))
    day = day or datetime.now(china_tz).date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=china_tz)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT observed_at, windows FROM quota_observations
                WHERE profile_id = %s AND observed_at >= %s AND observed_at < %s
                ORDER BY observed_at DESC, id DESC LIMIT 2000;
            """, (profile_id, start, start + timedelta(days=1)))
            rows = cur.fetchall()
    return [{"observed_at": row["observed_at"].isoformat(), "windows": row["windows"]}
            for row in rows]


@app.post("/api/profiles/{profile_id}/refresh-quota")
def refresh_single_quota(profile_id: int):
    """手动立即刷新单个配置的官方额度"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, auth_json FROM auth_profiles WHERE id = %s;", (profile_id,))
            p = cur.fetchone()
            if not p:
                raise HTTPException(status_code=404, detail="未找到该配置")

    quota_info = fetch_codex_quota(p["auth_json"], profile_id=profile_id)
    if not quota_info.get("success"):
        raise HTTPException(status_code=502, detail=quota_info.get("error", "获取额度失败"))

    update_profile_quota_in_db(profile_id, quota_info)
    return {"success": True, "quota": quota_info}

@app.get("/api/profiles/{profile_id}/download")
@app.get("/api/profiles/{profile_id}/auth.json")
def download_auth_json(
    profile_id: int, 
    request: Request,
    user: Optional[str] = None,
    hostname: Optional[str] = None
):
    """
    专门的 auth.json 文件直接下载接口：
    1. 允许直接下载标准 auth.json 文件（附件形式，支持浏览器、curl、wget）
    2. 若携带 user 参数（担当姓名），将严格执行“单人单号”约束：自动清除该担当在其他账号上的旧占用，并在 DB 中登记当前下载
    3. 兼容跨网段机器下载与直接脚本调用
    """
    client_ip = get_real_client_ip(request)
    client_user = (user or "").strip()
    client_hostname = (hostname or "").strip() or "remote-host"

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, auth_json, is_active, max_users
                FROM auth_profiles WHERE id = %s;
            """, (profile_id,))
            profile = cur.fetchone()
            if not profile:
                raise HTTPException(status_code=404, detail="未找到该配置")
            if not profile["is_active"]:
                raise HTTPException(status_code=403, detail="该配置已被管理员停用")

            # 如果提供了担当姓名，登记并清除旧占用
            if client_user:
                # 检查人数上限
                if profile["max_users"] and profile["max_users"] > 0:
                    cur.execute("""
                        SELECT COUNT(*) as cnt FROM user_sessions
                        WHERE profile_id = %s
                          AND client_user != %s;
                    """, (profile_id, client_user))
                    cnt = cur.fetchone()["cnt"]
                    if cnt >= profile["max_users"]:
                        raise HTTPException(status_code=429, detail=f"该服务端使用人数已满 ({cnt}/{profile['max_users']})")

                # 清除该担当名下的任何旧账号记录
                cur.execute("""
                    DELETE FROM user_sessions
                    WHERE client_user = %s;
                """, (client_user,))

                session_token = str(uuid.uuid4())
                cur.execute("""
                    INSERT INTO user_sessions (profile_id, client_user, client_hostname, client_ip, session_token, last_heartbeat, connected_at)
                    VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                """, (profile_id, client_user, client_hostname, client_ip, session_token))

                cur.execute("""
                    INSERT INTO activity_logs (profile_id, profile_name, client_user, client_hostname, client_ip, action, details)
                    VALUES (%s, %s, %s, %s, %s, 'DOWNLOAD', '担当直接下载并激活 auth.json');
                """, (profile_id, profile["name"], client_user, client_hostname, client_ip))

            else:
                cur.execute("""
                    INSERT INTO activity_logs (profile_id, profile_name, client_user, client_hostname, client_ip, action, details)
                    VALUES (%s, %s, 'anonymous', %s, %s, 'DOWNLOAD', '网页或远程直接下载 auth.json');
                """, (profile_id, profile["name"], client_hostname, client_ip))

        conn.commit()

    file_content = json.dumps(profile["auth_json"], indent=2, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Disposition": 'attachment; filename="auth.json"',
        "Content-Type": "application/json; charset=utf-8"
    }
    return Response(content=file_content, media_type="application/json", headers=headers)

@app.post("/api/profiles")
def create_profile(payload: ProfileCreate, request: Request, background_tasks: BackgroundTasks):
    """创建或更新认证配置"""
    validate_auth_payload(payload.auth_json)
    email = extract_email_from_auth(payload.auth_json)
    client_ip = get_real_client_ip(request)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO auth_profiles (name, description, auth_json, account_email, max_users, updated_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (name) DO UPDATE SET
                    description = EXCLUDED.description,
                    auth_json = EXCLUDED.auth_json,
                    account_email = CASE WHEN EXCLUDED.account_email != '' THEN EXCLUDED.account_email ELSE auth_profiles.account_email END,
                    max_users = EXCLUDED.max_users,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id, name, account_email;
            """, (
                payload.name.strip(),
                payload.description.strip(),
                json.dumps(payload.auth_json),
                email,
                payload.max_users or 0
            ))
            row = cur.fetchone()
            profile_id = row["id"]

            cur.execute("""
                INSERT INTO activity_logs (profile_id, profile_name, client_user, client_hostname, client_ip, action, details)
                VALUES (%s, %s, %s, %s, %s, 'UPLOAD', %s);
            """, (profile_id, payload.name, "admin", "dashboard", client_ip, "更新/创建认证配置"))
        conn.commit()

    # 异步拉取额度
    background_tasks.add_task(sync_profile_quota, profile_id, payload.auth_json)

    return {"success": True, "profile_id": profile_id, "name": payload.name, "account_email": email}

@app.post("/api/profiles/upload-file")
async def upload_profile_file(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    description: str = Form(""),
    max_users: int = Form(0),
    file: UploadFile = File(...)
):
    """支持直接上传 auth.json 文件"""
    content = await file.read()
    try:
        text = content.decode("utf-8").lstrip("\ufeff")
        auth_data = json.loads(text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件不是有效的 JSON: {e}")

    validate_auth_payload(auth_data)
    email = extract_email_from_auth(auth_data)
    client_ip = get_real_client_ip(request)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO auth_profiles (name, description, auth_json, account_email, max_users, updated_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (name) DO UPDATE SET
                    description = EXCLUDED.description,
                    auth_json = EXCLUDED.auth_json,
                    account_email = CASE WHEN EXCLUDED.account_email != '' THEN EXCLUDED.account_email ELSE auth_profiles.account_email END,
                    max_users = EXCLUDED.max_users,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id;
            """, (
                name.strip(),
                description.strip(),
                json.dumps(auth_data),
                email,
                max_users
            ))
            pid = cur.fetchone()["id"]

            cur.execute("""
                INSERT INTO activity_logs (profile_id, profile_name, client_user, client_hostname, client_ip, action, details)
                VALUES (%s, %s, %s, %s, %s, 'UPLOAD', %s);
            """, (pid, name, "admin", "dashboard", client_ip, f"上传文件: {file.filename}"))
        conn.commit()

    background_tasks.add_task(sync_profile_quota, pid, auth_data)

    return {"success": True, "profile_id": pid, "name": name, "account_email": email}

@app.post("/api/profiles/{profile_id}/replace-file")
async def replace_profile_auth_file(
    profile_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...)
):
    """直接上传新的 auth.json 替换指定账号节点的凭据"""
    content = await file.read()
    try:
        text = content.decode("utf-8").lstrip("\ufeff")
        auth_data = json.loads(text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件不是有效的 JSON: {e}")

    validate_auth_payload(auth_data)
    email = extract_email_from_auth(auth_data)
    client_ip = get_real_client_ip(request)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM auth_profiles WHERE id = %s;", (profile_id,))
            p = cur.fetchone()
            if not p:
                raise HTTPException(status_code=404, detail="未找到该账号配置")
            p_name = p["name"]

            cur.execute("""
                UPDATE auth_profiles
                SET auth_json = %s,
                    account_email = CASE WHEN %s != '' THEN %s ELSE account_email END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s;
            """, (json.dumps(auth_data), email, email, profile_id))

            cur.execute("""
                INSERT INTO activity_logs (profile_id, profile_name, client_user, client_hostname, client_ip, action, details)
                VALUES (%s, %s, %s, %s, %s, 'REPLACE', %s);
            """, (profile_id, p_name, "admin", "dashboard", client_ip, f"替换认证文件: {file.filename}"))
        conn.commit()

    background_tasks.add_task(sync_profile_quota, profile_id, auth_data)
    return {"success": True, "profile_id": profile_id, "name": p_name, "account_email": email}

@app.patch("/api/profiles/{profile_id}/name")
def rename_profile(profile_id: int, payload: ProfileRename):
    name = payload.name.strip()
    if not name or len(name) > 100:
        raise HTTPException(status_code=400, detail="账号名称需为 1 至 100 个字符")
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE auth_profiles SET name = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s RETURNING id, name;
                """, (name, profile_id))
                profile = cur.fetchone()
                if not profile:
                    raise HTTPException(status_code=404, detail="配置不存在或已被删除")
            conn.commit()
    except UniqueViolation:
        raise HTTPException(status_code=409, detail="该账号名称已存在，请使用其他名称") from None
    return {"success": True, "profile_id": profile["id"], "name": profile["name"]}


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: int, request: Request):
    client_ip = get_real_client_ip(request)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM auth_profiles WHERE id = %s;", (profile_id,))
            p = cur.fetchone()
            if not p:
                raise HTTPException(status_code=404, detail="配置不存在或已被删除")
            profile_name = p["name"]

            # 1. 记录删除审计日志（profile_id 设为 NULL 避免外键约束冲突）
            cur.execute("""
                INSERT INTO activity_logs (profile_id, profile_name, client_user, client_hostname, client_ip, action, details)
                VALUES (NULL, %s, %s, %s, %s, 'DELETE', %s);
            """, (profile_name, "admin", "dashboard", client_ip, f"删除节点配置: {profile_name}"))

            # 2. 清理关联的活跃会话
            cur.execute("DELETE FROM user_sessions WHERE profile_id = %s;", (profile_id,))

            # 3. 删除配置本身
            cur.execute("DELETE FROM auth_profiles WHERE id = %s;", (profile_id,))
        conn.commit()
    return {"success": True}

@app.post("/api/sessions/apply")
def apply_session(req: SessionApply, request: Request):
    """客户端请求使用某服务端的 auth.json，并登记在线会话"""
    client_ip = get_real_client_ip(request)
    session_token = req.session_token or str(uuid.uuid4())

    with get_connection() as conn:
        with conn.cursor() as cur:
            # 1. 查找配置
            cur.execute("""
                SELECT id, name, auth_json, is_active, max_users, plan_type,
                       quota_5h_used, quota_weekly_used
                FROM auth_profiles WHERE id = %s;
            """, (req.profile_id,))
            profile = cur.fetchone()
            if not profile:
                raise HTTPException(status_code=404, detail="未找到对应的服务端认证配置")
            if not profile["is_active"]:
                raise HTTPException(status_code=403, detail="该配置已被管理员停用")

            # 2. 检查人数上限
            if profile["max_users"] and profile["max_users"] > 0:
                cur.execute("""
                    SELECT COUNT(*) as cnt FROM user_sessions
                    WHERE profile_id = %s
                      AND client_user != %s
                      AND session_token != %s;
                """, (req.profile_id, req.client_user, session_token))
                cnt = cur.fetchone()["cnt"]
                if cnt >= profile["max_users"]:
                    raise HTTPException(status_code=429, detail=f"该服务端使用人数已满 ({cnt}/{profile['max_users']})")

            # 3. 核心约束：一个担当同一时间只能取一个认证json！
            # 获取新的前，先在数据库中查找并清除该担当名下所有正在使用的旧记录
            cur.execute("""
                SELECT profile_id FROM user_sessions
                WHERE client_user = %s OR session_token = %s;
            """, (req.client_user, session_token))
            old_rows = cur.fetchall()
            for old_r in old_rows:
                old_pid = old_r["profile_id"]
                if old_pid != req.profile_id:
                    cur.execute("""
                        INSERT INTO activity_logs (profile_id, client_user, client_hostname, client_ip, action, details)
                        VALUES (%s, %s, %s, %s, 'SWITCH', '担当获取新认证，系统自动清除旧账号占用');
                    """, (old_pid, req.client_user, req.client_hostname, client_ip))

            cur.execute("""
                DELETE FROM user_sessions
                WHERE client_user = %s OR session_token = %s;
            """, (req.client_user, session_token))

            # 4. 插入当前最新的担当会话
            cur.execute("""
                INSERT INTO user_sessions (profile_id, client_user, client_hostname, client_ip, session_token, last_heartbeat, connected_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
            """, (
                req.profile_id,
                req.client_user,
                req.client_hostname,
                client_ip,
                session_token
            ))

            # 5. 记录日志
            cur.execute("""
                INSERT INTO activity_logs (profile_id, profile_name, client_user, client_hostname, client_ip, action, details)
                VALUES (%s, %s, %s, %s, %s, 'APPLY', %s);
            """, (req.profile_id, profile["name"], req.client_user, req.client_hostname, client_ip, "担当下载并应用最新凭证"))
        conn.commit()

    return {
        "success": True,
        "session_token": session_token,
        "profile_id": req.profile_id,
        "profile_name": profile["name"],
        "auth_json": profile["auth_json"],
        "heartbeat_interval": 30
    }

@app.post("/api/sessions/heartbeat")
def heartbeat(req: SessionHeartbeat):
    """客户端心跳上报"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE user_sessions
                SET last_heartbeat = CURRENT_TIMESTAMP
                WHERE session_token = %s
                RETURNING id, profile_id;
            """, (req.session_token,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="会话已失效，请重新连接")
        conn.commit()
    return {"success": True, "status": "alive"}

@app.post("/api/sessions/release")
def release_session(req: SessionRelease, request: Request):
    """客户端退出或切换时释放会话"""
    client_ip = get_real_client_ip(request)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM user_sessions
                WHERE session_token = %s
                RETURNING profile_id, client_user, client_hostname;
            """, (req.session_token,))
            row = cur.fetchone()
            if row:
                cur.execute("""
                    INSERT INTO activity_logs (profile_id, profile_name, client_user, client_hostname, client_ip, action, details)
                    VALUES (%s, '', %s, %s, %s, 'RELEASE', '客户端主动断开连接');
                """, (row["profile_id"], row["client_user"], row["client_hostname"], client_ip))
        conn.commit()
    return {"success": True}

@app.get("/api/logs")
def get_recent_logs(limit: int = 50):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, profile_id, profile_name, client_user, client_hostname, client_ip, action, details, created_at
                FROM activity_logs
                ORDER BY id DESC
                LIMIT %s;
            """, (limit,))
            logs = cur.fetchall()
            for l in logs:
                if l["created_at"]:
                    l["created_at"] = l["created_at"].isoformat()
    return logs

def get_packages_dir() -> str:
    """定位离线分发包目录，支持源码运行、exe运行及同级放置"""
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        candidates.append(os.path.join(exe_dir, "packages"))
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(os.path.join(meipass, "packages"))

    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(base_dir, "packages"))
    candidates.append(os.path.join(os.path.dirname(base_dir), "packages"))

    for c in candidates:
        if os.path.isdir(c):
            return c
    fallback = candidates[0] if candidates else os.path.join(base_dir, "packages")
    os.makedirs(fallback, exist_ok=True)
    return fallback

@app.get("/api/packages/info")
def get_packages_info():
    """获取所有托管在服务端的离线环境安装包状态与元数据"""
    pkg_dir = get_packages_dir()
    package_defs = [
        {
            "id": "node",
            "name": "Node.js v20 LTS 运行环境",
            "filename": "node-v20-x64.msi",
            "version": "v20.18.0 (x64)",
            "type": "msi",
            "target": "nodejs",
            "description": "官方 Node.js 64位 LTS 离线静默安装包 (免连外网)"
        },
        {
            "id": "codex_cli",
            "name": "Codex 命令行终端 (@openai/codex)",
            "filename": "openai-codex.tgz",
            "version": "0.162.1",
            "type": "npm",
            "target": "codex_cli",
            "description": "OpenAI 官方 Codex CLI 离线 npm 压缩包 (npm install -g)"
        },
        {
            "id": "vscode_ext",
            "name": "Codex VS Code 插件 (OpenAI's coding agent)",
            "filename": "openai.chatgpt.vsix",
            "version": "26.1007.21434",
            "type": "vsix",
            "target": "vscode_extension",
            "description": "VS Code 官方 Codex / ChatGPT 插件全量完整离线包 (522 MB)"
        }
    ]

    results = []
    for p in package_defs:
        fpath = os.path.join(pkg_dir, p["filename"])
        exists = os.path.isfile(fpath)
        size = os.path.getsize(fpath) if exists else 0
        if size >= 1024 * 1024:
            size_str = f"{size / (1024 * 1024):.1f} MB"
        elif size >= 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size} B"

        results.append({
            **p,
            "exists": exists,
            "size": size,
            "size_str": size_str,
            "download_url": f"/api/packages/download/{p['filename']}" if exists else None
        })
    return {"packages": results, "directory": pkg_dir}

@app.get("/api/packages/download/{filename}")
def download_package(filename: str):
    """安全下载离线安装包（流式传输支持大文件）"""
    safe_name = os.path.basename(filename)
    pkg_dir = get_packages_dir()
    target_path = os.path.join(pkg_dir, safe_name)
    if not os.path.isfile(target_path):
        raise HTTPException(status_code=404, detail=f"离线包 {safe_name} 不存在")

    return FileResponse(
        target_path,
        filename=safe_name,
        media_type="application/octet-stream"
    )


if getattr(sys, "frozen", False):
    static_dir = os.path.join(sys._MEIPASS, "static")
else:
    static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
