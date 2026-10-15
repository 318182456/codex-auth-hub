"""Agent liveness is determined exclusively by the hub's clock."""
import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import Request, HTTPException
from pydantic import BaseModel, Field

from database import get_connection


AGENT_TIMEOUT_SECONDS = 90


class AgentWatch(BaseModel):
    profile_id: int | None = Field(default=None, gt=0)
    name: str = Field(default="", max_length=100)
    status: Literal["waiting_auth", "retrying", "synced", "pending"] = "pending"


class CurrentAccount(BaseModel):
    account_id: str = Field(default="", max_length=255)
    email: str = Field(default="", max_length=255)
    has_auth: bool = False


class AgentHeartbeat(BaseModel):
    agent_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    hostname: str = Field(min_length=1, max_length=255)
    version: str = Field(default="1.2.0", max_length=40)
    watches: list[AgentWatch] = Field(default_factory=list, max_length=200)
    current_account: CurrentAccount = Field(default_factory=CurrentAccount)


def agent_heartbeat(payload: AgentHeartbeat, request: Request):
    # Report the direct peer rather than trusting arbitrary forwarded headers.
    ip = request.client.host if request.client else "unknown"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO agent_connections (agent_id, hostname, version, client_ip, watches, current_account, last_heartbeat)
                VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (agent_id) DO UPDATE SET
                    hostname = EXCLUDED.hostname, version = EXCLUDED.version,
                    client_ip = EXCLUDED.client_ip, watches = EXCLUDED.watches,
                    current_account = EXCLUDED.current_account,
                    last_heartbeat = CURRENT_TIMESTAMP
                RETURNING configured_watches, binding_mode;
            """, (payload.agent_id, payload.hostname, payload.version, ip,
                  json.dumps([watch.model_dump() for watch in payload.watches]),
                  json.dumps(payload.current_account.model_dump())))
            row = cur.fetchone()
            watches = row["configured_watches"]
            if row["binding_mode"] == "auto":
                match = match_current_account(cur, payload.current_account)
                watches = [{"profile_id": match["id"], "name": match["name"], "auth_file": ""}] if match else []
                cur.execute("UPDATE agent_connections SET configured_watches = %s WHERE agent_id = %s;",
                            (json.dumps(watches), payload.agent_id))
        conn.commit()
    return {"success": True, "heartbeat_interval": 30, "watches": watches}


def match_current_account(cur, account):
    if not account.has_auth:
        return None
    if account.account_id:
        cur.execute("""
            SELECT id, name FROM auth_profiles
            WHERE auth_json->'tokens'->>'account_id' = %s
              AND (%s = '' OR account_email = '' OR LOWER(account_email) = LOWER(%s))
            ORDER BY id LIMIT 2;
        """, (account.account_id, account.email, account.email))
        matches = cur.fetchall()
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None  # Duplicate identities require an explicit binding.
    if account.email:
        cur.execute("""
            SELECT id, name FROM auth_profiles WHERE LOWER(account_email) = LOWER(%s)
            AND (%s = '' OR COALESCE(auth_json->'tokens'->>'account_id', '') = '')
            ORDER BY id LIMIT 2;
        """, (account.email, account.account_id))
        matches = cur.fetchall()
        if len(matches) == 1:
            return matches[0]
    return None


def build_agent_status(row, now):
    last_seen = row["last_heartbeat"]
    online = 0 <= (now - last_seen).total_seconds() <= AGENT_TIMEOUT_SECONDS
    return {**row, "last_heartbeat": last_seen.isoformat(), "is_online": online}


def list_agents():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT agent_id, hostname, version, client_ip, watches, last_heartbeat,
                       configured_watches, config_updated_at, current_account, binding_mode
                FROM agent_connections ORDER BY hostname, agent_id;
            """)
            rows = cur.fetchall()
    now = datetime.now(timezone.utc)
    return [build_agent_status(row, now) for row in rows]


class AgentBinding(BaseModel):
    profile_id: int | None = Field(default=None, gt=0)


def bind_agent(agent_id: str, payload: AgentBinding):
    with get_connection() as conn:
        with conn.cursor() as cur:
            watches = []
            mode = "auto"
            if payload.profile_id:
                cur.execute("SELECT id, name FROM auth_profiles WHERE id = %s;", (payload.profile_id,))
                profile = cur.fetchone()
                if not profile:
                    raise HTTPException(status_code=400, detail="所选账号已不存在，请刷新后重新选择")
                watches = [{"profile_id": profile["id"], "name": profile["name"], "auth_file": ""}]
                mode = "manual"
            cur.execute("""UPDATE agent_connections SET binding_mode = %s, configured_watches = %s,
                           config_updated_at = CURRENT_TIMESTAMP WHERE agent_id = %s RETURNING agent_id;""",
                        (mode, json.dumps(watches), agent_id))
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Agent 不存在")
        conn.commit()
    return {"success": True}


def register_agent_routes(app):
    app.add_api_route("/api/agents/heartbeat", agent_heartbeat, methods=["POST"], tags=["agents"])
    app.add_api_route("/api/agents", list_agents, methods=["GET"], tags=["agents"])
    app.add_api_route("/api/agents/{agent_id}/binding", bind_agent, methods=["PUT"], tags=["agents"])
