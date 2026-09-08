"""
TURSO Cloud Database Connector & Developer API Auth Engine for SkyWatch
Stores maintenance config, settings, developer accounts (sk-live-...), and auth sessions.
Persists across all deployments, containers, and server restarts.
"""
import os
import time
import logging
import secrets
import random
import aiohttp
from typing import Dict, Any, Optional, List

import config
from core.db import db

logger = logging.getLogger("SkyWatch.Turso")

def generate_sk_live_key() -> str:
    """Generates a secure OpenAI-style API key: sk-live-(24 hex chars)."""
    return f"sk-live-{secrets.token_hex(12)}"

class TursoDatabaseClient:
    def __init__(self):
        self.db_url = os.getenv("TURSO_DATABASE_URL", "https://skwatchdb-mrzombie121.aws-us-west-2.turso.io")
        self.auth_token = os.getenv("TURSO_AUTH_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODg0NTI2MzYsImlkIjoiMDFhMDY4MTUtNDkwMS03YzY4LTk1OTAtNDQwMmE1MThlMWEyIiwia2lkIjoiLVdZY3BUd0V6MFU1NUxrWDYySHh4QzdJUV9XcGZCdHJLR1ZzMVZpcmVKMCIsInJpZCI6IjIzMzQ5NWQ3LWUzMzgtNDIyMC04NDlmLWRmMDNlY2U4YThlZCJ9.GGI9BUS3apLBedo1iOwjej6cixfw2k9fIYrQKfMyRvtzBZP1W43WrvI7QcuKmBH3bvZxnvKiIjC_3oESqZ66Aw")
        
        if self.db_url.startswith("libsql://"):
            self.http_url = self.db_url.replace("libsql://", "https://")
        else:
            self.http_url = self.db_url

        self.endpoint = f"{self.http_url}/v2/pipeline"
        self._headers = {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json"
        }

    async def execute_query(self, sql: str, args: list = None) -> Optional[Dict[str, Any]]:
        """Executes SQL statement on Turso database over HTTPS pipeline."""
        if args is None:
            args = []

        params = []
        for a in args:
            if isinstance(a, bool):
                params.append({"type": "integer", "value": "1" if a else "0"})
            elif isinstance(a, int):
                params.append({"type": "integer", "value": str(a)})
            elif isinstance(a, float):
                params.append({"type": "float", "value": a})
            elif a is None:
                params.append({"type": "null"})
            else:
                params.append({"type": "text", "value": str(a)})

        payload = {
            "requests": [
                {
                    "type": "execute",
                    "stmt": {
                        "sql": sql,
                        "args": params
                    }
                },
                {"type": "close"}
            ]
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.endpoint, headers=self._headers, json=payload, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results", [])
                        if results and results[0].get("type") == "ok":
                            return results[0].get("response", {}).get("result", {})
                    else:
                        text = await resp.text()
                        logger.error(f"Turso API error HTTP {resp.status}: {text}")
        except Exception as e:
            logger.error(f"Failed to query Turso cloud database: {e}")
        return None

    async def init_schema(self):
        """Creates maintenance_config, system_settings, api_users, and auth_sessions tables on Turso."""
        # 1. Maintenance Config Table
        sql_maint = """
        CREATE TABLE IF NOT EXISTS maintenance_config (
            id TEXT PRIMARY KEY,
            is_enabled INTEGER DEFAULT 0,
            reason TEXT,
            end_timestamp INTEGER,
            updated_at INTEGER
        );
        """
        await self.execute_query(sql_maint)

        # 2. System Settings Table
        sql_settings = """
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at INTEGER
        );
        """
        await self.execute_query(sql_settings)

        # 3. Developer Accounts Table
        sql_users = """
        CREATE TABLE IF NOT EXISTS api_users (
            telegram_id TEXT PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            username TEXT,
            photo_url TEXT,
            api_key TEXT UNIQUE,
            created_at INTEGER,
            last_used_at INTEGER,
            requests_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1
        );
        """
        await self.execute_query(sql_users)
        await self.execute_query("CREATE INDEX IF NOT EXISTS idx_api_users_key ON api_users(api_key);")

        # 4. Telegram Bot Auth Sessions & PINs Table
        sql_sessions = """
        CREATE TABLE IF NOT EXISTS auth_sessions (
            session_code TEXT PRIMARY KEY,
            pin_code TEXT,
            telegram_id TEXT,
            username TEXT,
            api_key TEXT,
            verified INTEGER DEFAULT 0,
            created_at INTEGER
        );
        """
        await self.execute_query(sql_sessions)
        await self.execute_query("CREATE INDEX IF NOT EXISTS idx_auth_pin ON auth_sessions(pin_code);")
        
        # Insert default maintenance row if not exists
        check_sql = "SELECT id FROM maintenance_config WHERE id = 'main';"
        res = await self.execute_query(check_sql)
        if res and not res.get("rows"):
            insert_sql = "INSERT INTO maintenance_config (id, is_enabled, reason, end_timestamp, updated_at) VALUES ('main', 0, 'Тривають планові технічні роботи.', 0, ?);"
            await self.execute_query(insert_sql, [int(time.time())])
            logger.info("Initialized Turso schema (maintenance, settings, api_users, auth_sessions).")

    # --- System Settings API ---
    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        sql = "SELECT value FROM system_settings WHERE key = ?;"
        res = await self.execute_query(sql, [key])
        if res and res.get("rows"):
            return str(res["rows"][0][0].get("value", ""))
        return default

    async def set_setting(self, key: str, value: str):
        now = int(time.time())
        sql = """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at;
        """
        await self.execute_query(sql, [key, value, now])

    # --- Maintenance State API ---
    async def get_maintenance_state(self) -> Dict[str, Any]:
        sql = "SELECT is_enabled, reason, end_timestamp, updated_at FROM maintenance_config WHERE id = 'main';"
        res = await self.execute_query(sql)
        
        if res and res.get("rows"):
            row = res["rows"][0]
            is_enabled = bool(int(row[0].get("value", 0)))
            reason = str(row[1].get("value", ""))
            end_ts = int(row[2].get("value", 0))
            
            db.set_setting("maintenance_mode", "true" if is_enabled else "false")
            db.set_setting("maintenance_reason", reason)
            db.set_setting("maintenance_end_ts", str(end_ts))

            return {
                "maintenance_mode": is_enabled,
                "reason": reason,
                "end_timestamp": end_ts
            }
        
        is_maint = db.get_setting("maintenance_mode", "false").lower() == "true"
        reason = db.get_setting("maintenance_reason", "Тривають планові технічні роботи.")
        end_ts = int(db.get_setting("maintenance_end_ts", "0") or "0")
        return {
            "maintenance_mode": is_maint,
            "reason": reason,
            "end_timestamp": end_ts
        }

    async def set_maintenance_state(self, is_enabled: bool, reason: str, end_timestamp: int) -> bool:
        now = int(time.time())
        sql = """
        INSERT INTO maintenance_config (id, is_enabled, reason, end_timestamp, updated_at)
        VALUES ('main', ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            is_enabled = excluded.is_enabled,
            reason = excluded.reason,
            end_timestamp = excluded.end_timestamp,
            updated_at = excluded.updated_at;
        """
        res = await self.execute_query(sql, [1 if is_enabled else 0, reason, end_timestamp, now])
        
        db.set_setting("maintenance_mode", "true" if is_enabled else "false")
        db.set_setting("maintenance_reason", reason)
        db.set_setting("maintenance_end_ts", str(end_timestamp))
        return res is not None

    # --- DEVELOPER API USERS & sk-live-... KEYS (TURSO CLOUD) ---
    async def register_or_get_api_user(
        self,
        telegram_id: str,
        first_name: str = "",
        last_name: str = "",
        username: str = "",
        photo_url: str = ""
    ) -> Dict[str, Any]:
        tg_id_str = str(telegram_id).strip()
        now = int(time.time())
        
        sql_check = "SELECT telegram_id, first_name, last_name, username, photo_url, api_key, created_at, requests_count, is_active FROM api_users WHERE telegram_id = ?;"
        res = await self.execute_query(sql_check, [tg_id_str])
        
        if res and res.get("rows"):
            row = res["rows"][0]
            sql_upd = "UPDATE api_users SET first_name = ?, last_name = ?, username = ?, photo_url = ? WHERE telegram_id = ?;"
            await self.execute_query(sql_upd, [first_name, last_name, username, photo_url, tg_id_str])
            
            return {
                "telegram_id": tg_id_str,
                "first_name": first_name or str(row[1].get("value", "")),
                "last_name": last_name or str(row[2].get("value", "")),
                "username": username or str(row[3].get("value", "")),
                "photo_url": photo_url or str(row[4].get("value", "")),
                "api_key": str(row[5].get("value", "")),
                "created_at": int(row[6].get("value", now)),
                "requests_count": int(row[7].get("value", 0)),
                "is_active": bool(int(row[8].get("value", 1)))
            }

        new_api_key = generate_sk_live_key()
        sql_insert = """
        INSERT INTO api_users (telegram_id, first_name, last_name, username, photo_url, api_key, created_at, last_used_at, requests_count, is_active)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1);
        """
        await self.execute_query(sql_insert, [tg_id_str, first_name, last_name, username, photo_url, new_api_key, now, now])

        return {
            "telegram_id": tg_id_str,
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "photo_url": photo_url,
            "api_key": new_api_key,
            "created_at": now,
            "requests_count": 0,
            "is_active": True
        }

    async def get_user_by_api_key(self, api_key: str) -> Optional[Dict[str, Any]]:
        clean_key = str(api_key).strip()
        if not clean_key.startswith("sk-live-"):
            return None

        sql = "SELECT telegram_id, first_name, last_name, username, api_key, requests_count, is_active FROM api_users WHERE api_key = ?;"
        res = await self.execute_query(sql, [clean_key])
        
        if res and res.get("rows"):
            row = res["rows"][0]
            is_active = bool(int(row[6].get("value", 1)))
            if not is_active:
                return None
            return {
                "telegram_id": str(row[0].get("value", "")),
                "first_name": str(row[1].get("value", "")),
                "last_name": str(row[2].get("value", "")),
                "username": str(row[3].get("value", "")),
                "api_key": str(row[4].get("value", "")),
                "requests_count": int(row[5].get("value", 0)),
                "is_active": is_active
            }
        return None

    async def increment_api_usage(self, api_key: str):
        clean_key = str(api_key).strip()
        now = int(time.time())
        sql = "UPDATE api_users SET requests_count = requests_count + 1, last_used_at = ? WHERE api_key = ?;"
        await self.execute_query(sql, [now, clean_key])

    async def regenerate_user_api_key(self, telegram_id: str) -> Optional[str]:
        tg_id_str = str(telegram_id).strip()
        new_key = generate_sk_live_key()
        sql = "UPDATE api_users SET api_key = ? WHERE telegram_id = ?;"
        res = await self.execute_query(sql, [new_key, tg_id_str])
        return new_key if res is not None else None

    # --- PERSISTENT AUTH SESSIONS & PIN CODES (TURSO CLOUD) ---
    async def create_auth_session(self, session_code: str) -> str:
        """Stores a new pending session in Turso."""
        now = int(time.time())
        sql = """
        INSERT INTO auth_sessions (session_code, pin_code, verified, created_at)
        VALUES (?, '', 0, ?)
        ON CONFLICT(session_code) DO UPDATE SET created_at = excluded.created_at;
        """
        await self.execute_query(sql, [session_code, now])
        return session_code

    async def verify_auth_session_by_bot(self, session_code: str, pin_code: str, telegram_id: str, username: str, api_key: str):
        """Marks session as verified in Turso when user starts bot."""
        sql = """
        INSERT INTO auth_sessions (session_code, pin_code, telegram_id, username, api_key, verified, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(session_code) DO UPDATE SET
            pin_code = excluded.pin_code,
            telegram_id = excluded.telegram_id,
            username = excluded.username,
            api_key = excluded.api_key,
            verified = 1;
        """
        await self.execute_query(sql, [session_code, pin_code, telegram_id, username, api_key, int(time.time())])

    async def check_auth_session(self, session_code: str) -> Optional[Dict[str, Any]]:
        """Checks if session was verified in Turso."""
        sql = "SELECT verified, telegram_id, username, api_key FROM auth_sessions WHERE session_code = ?;"
        res = await self.execute_query(sql, [session_code])
        if res and res.get("rows"):
            row = res["rows"][0]
            verified = bool(int(row[0].get("value", 0)))
            if verified:
                return {
                    "verified": True,
                    "telegram_id": str(row[1].get("value", "")),
                    "username": str(row[2].get("value", "")),
                    "api_key": str(row[3].get("value", ""))
                }
        return None

    async def verify_by_pin_code(self, pin_code: str) -> Optional[Dict[str, Any]]:
        """Looks up 6-digit PIN in Turso auth_sessions."""
        clean_pin = pin_code.strip()
        sql = "SELECT telegram_id, username, api_key FROM auth_sessions WHERE pin_code = ?;"
        res = await self.execute_query(sql, [clean_pin])
        if res and res.get("rows"):
            row = res["rows"][0]
            return {
                "telegram_id": str(row[0].get("value", "")),
                "username": str(row[1].get("value", "")),
                "api_key": str(row[2].get("value", ""))
            }
        return None

# Global Turso Database Client
turso_db = TursoDatabaseClient()
