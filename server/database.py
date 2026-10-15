import os
import sys
import json
import psycopg
from psycopg.rows import dict_row

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

def get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

config_path = os.path.join(get_base_dir(), "db_config.json")
config_data = {}
if os.path.exists(config_path):
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
    except Exception as e:
        print(f"[Config] 读取 db_config.json 异常: {e}")
else:
    # 自动生成默认模板，方便用户在别的机器上随时通过修改文件变更数据库连接
    try:
        default_cfg = {
            "DB_HOST": "172.28.26.174",
            "DB_PORT": 5432,
            "DB_USER": "postgres",
            "DB_PASSWORD": "postgres",
            "DB_NAME": "codex_auth_hub"
        }
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(default_cfg, f, indent=2, ensure_ascii=False)
        config_data = default_cfg
    except Exception:
        pass

DB_HOST = os.getenv("DB_HOST", config_data.get("DB_HOST", "172.28.26.174"))
DB_PORT = int(os.getenv("DB_PORT", config_data.get("DB_PORT", 5432)))
DB_USER = os.getenv("DB_USER", config_data.get("DB_USER", "postgres"))
DB_PASSWORD = os.getenv("DB_PASSWORD", config_data.get("DB_PASSWORD", "postgres"))
DB_NAME = os.getenv("DB_NAME", config_data.get("DB_NAME", "codex_auth_hub"))

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

def get_connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

import hashlib
import secrets

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return f"{salt}:{key.hex()}"

def verify_password(stored_password: str, provided_password: str) -> bool:
    if not stored_password or ":" not in stored_password:
        return False
    try:
        salt, key_hex = stored_password.split(":", 1)
        new_key = hashlib.pbkdf2_hmac('sha256', provided_password.encode('utf-8'), salt.encode('utf-8'), 100000)
        return secrets.compare_digest(new_key.hex(), key_hex)
    except Exception:
        return False

def init_db():
    with get_connection() as conn:
        with conn.cursor() as cur:
            # 0. 团队用户表 (按 user 分开不同的人)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(100) UNIQUE NOT NULL,
                    display_name VARCHAR(100) DEFAULT '',
                    password_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(20) DEFAULT 'user',
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    last_login TIMESTAMPTZ
                );
            """)

            # 0.1 用户登录令牌表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token VARCHAR(64) PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
                    username VARCHAR(100) NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    last_used TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 1. 认证配置/账号表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS auth_profiles (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) UNIQUE NOT NULL,
                    description TEXT DEFAULT '',
                    auth_json JSONB NOT NULL,
                    account_email VARCHAR(255) DEFAULT '',
                    is_active BOOLEAN DEFAULT TRUE,
                    max_users INTEGER DEFAULT 0,
                    plan_type VARCHAR(50) DEFAULT '',
                    quota_5h_used INTEGER DEFAULT 0,
                    quota_5h_reset_at TIMESTAMPTZ,
                    quota_5h_reset_seconds INTEGER DEFAULT 0,
                    quota_weekly_used INTEGER DEFAULT 0,
                    quota_weekly_reset_at TIMESTAMPTZ,
                    quota_weekly_reset_seconds INTEGER DEFAULT 0,
                    quota_last_updated TIMESTAMPTZ,
                    quota_raw JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 2. 客户端实时会话表 (带心跳检测与用户绑定)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_sessions (
                    id SERIAL PRIMARY KEY,
                    profile_id INTEGER NOT NULL REFERENCES auth_profiles(id) ON DELETE CASCADE,
                    client_user VARCHAR(100) NOT NULL,
                    client_hostname VARCHAR(100) NOT NULL,
                    client_ip VARCHAR(50) DEFAULT '',
                    session_token VARCHAR(64) UNIQUE NOT NULL,
                    last_heartbeat TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                    connected_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 3. 操作与审计日志表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id SERIAL PRIMARY KEY,
                    profile_id INTEGER REFERENCES auth_profiles(id) ON DELETE SET NULL,
                    profile_name VARCHAR(100) DEFAULT '',
                    client_user VARCHAR(100) NOT NULL,
                    client_hostname VARCHAR(100) NOT NULL,
                    client_ip VARCHAR(50) DEFAULT '',
                    action VARCHAR(50) NOT NULL,
                    details TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 兼容已有数据库，认证验证结果与额度缓存分开保存。
            cur.execute("""
                ALTER TABLE auth_profiles ADD COLUMN IF NOT EXISTS auth_status VARCHAR(20) DEFAULT 'unchecked';
                ALTER TABLE auth_profiles ADD COLUMN IF NOT EXISTS auth_status_error TEXT DEFAULT '';
                ALTER TABLE auth_profiles ADD COLUMN IF NOT EXISTS auth_checked_at TIMESTAMPTZ;
                ALTER TABLE auth_profiles ADD COLUMN IF NOT EXISTS auth_status_tokens_hash VARCHAR(64);
            """)

            # 登录机器 Agent 心跳；离线记录保留，便于区分离线与从未连接。
            cur.execute("""
                CREATE TABLE IF NOT EXISTS agent_connections (
                    agent_id VARCHAR(64) PRIMARY KEY,
                    hostname VARCHAR(255) NOT NULL,
                    version VARCHAR(40) NOT NULL,
                    client_ip VARCHAR(50) DEFAULT '',
                    watches JSONB NOT NULL DEFAULT '[]'::jsonb,
                    last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE agent_connections ADD COLUMN IF NOT EXISTS configured_watches JSONB NOT NULL DEFAULT '[]'::jsonb;
                ALTER TABLE agent_connections ADD COLUMN IF NOT EXISTS config_updated_at TIMESTAMPTZ;
                ALTER TABLE agent_connections ADD COLUMN IF NOT EXISTS current_account JSONB NOT NULL DEFAULT '{}'::jsonb;
                ALTER TABLE agent_connections ADD COLUMN IF NOT EXISTS binding_mode VARCHAR(20) NOT NULL DEFAULT 'auto';
            """)

            # 索引
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_profile_hb ON user_sessions(profile_id, last_heartbeat);
                CREATE INDEX IF NOT EXISTS idx_sessions_token ON user_sessions(session_token);
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(client_user);
                CREATE INDEX IF NOT EXISTS idx_logs_created ON activity_logs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tokens_user ON auth_tokens(user_id);
            """)

            # 检查是否有默认管理员，没有则自动创建
            cur.execute("SELECT COUNT(*) as count FROM app_users;")
            cnt = cur.fetchone()["count"]
            if cnt == 0:
                cur.execute("""
                    INSERT INTO app_users (username, display_name, password_hash, role)
                    VALUES (%s, %s, %s, %s);
                """, ("admin", "管理员", hash_password("admin123"), "admin"))
                print("[InitDB] 已创建默认管理员账号: admin / admin123")
        conn.commit()

if __name__ == "__main__":
    try:
        init_db()
        print("Database tables initialized successfully!")
    except Exception as e:
        print(f"\n[DB Connection Error] 详细报错信息:\n  {type(e).__name__}: {e}\n")
        import sys
        sys.exit(1)
