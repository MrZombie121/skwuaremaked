"""
TURSO Cloud Database Connector & Maintenance Manager for SkyWatch
Uses Turso HTTP API (libSQL pipeline) to synchronize maintenance status, end time, and messages.
Persists across all deployments, containers, and server restarts.
"""
import os
import time
import logging
import aiohttp
from typing import Dict, Any, Optional

import config
from core.db import db

logger = logging.getLogger("SkyWatch.Turso")

class TursoDatabaseClient:
    def __init__(self):
        self.db_url = os.getenv("TURSO_DATABASE_URL", "https://skwatchdb-mrzombie121.aws-us-west-2.turso.io")
        self.auth_token = os.getenv("TURSO_AUTH_TOKEN", "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODg0NTI2MzYsImlkIjoiMDFhMDY4MTUtNDkwMS03YzY4LTk1OTAtNDQwMmE1MThlMWEyIiwia2lkIjoiLVdZY3BUd0V6MFU1NUxrWDYySHh4QzdJUV9XcGZCdHJLR1ZzMVZpcmVKMCIsInJpZCI6IjIzMzQ5NWQ3LWUzMzgtNDIyMC04NDlmLWRmMDNlY2U4YThlZCJ9.GGI9BUS3apLBedo1iOwjej6cixfw2k9fIYrQKfMyRvtzBZP1W43WrvI7QcuKmBH3bvZxnvKiIjC_3oESqZ66Aw")
        
        # Convert libsql:// to https://
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
                async with session.post(self.endpoint, headers=self._headers, json=payload, timeout=aiohttp.ClientTimeout(total=6)) as resp:
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
        """Creates maintenance_config and system_settings tables on Turso cloud DB if not exists."""
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

        sql_settings = """
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at INTEGER
        );
        """
        await self.execute_query(sql_settings)
        
        # Insert default row if not exists
        check_sql = "SELECT id FROM maintenance_config WHERE id = 'main';"
        res = await self.execute_query(check_sql)
        if res and not res.get("rows"):
            insert_sql = "INSERT INTO maintenance_config (id, is_enabled, reason, end_timestamp, updated_at) VALUES ('main', 0, 'Тривають планові технічні роботи.', 0, ?);"
            await self.execute_query(insert_sql, [int(time.time())])
            logger.info("Initialized Turso maintenance table schema.")

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Gets a persistent setting from Turso cloud database."""
        sql = "SELECT value FROM system_settings WHERE key = ?;"
        res = await self.execute_query(sql, [key])
        if res and res.get("rows"):
            return str(res["rows"][0][0].get("value", ""))
        return default

    async def set_setting(self, key: str, value: str):
        """Saves a persistent setting to Turso cloud database."""
        now = int(time.time())
        sql = """
        INSERT INTO system_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at;
        """
        await self.execute_query(sql, [key, value, now])

    async def get_maintenance_state(self) -> Dict[str, Any]:
        """Fetches live maintenance state from Turso (with JSON local fallback)."""
        sql = "SELECT is_enabled, reason, end_timestamp, updated_at FROM maintenance_config WHERE id = 'main';"
        res = await self.execute_query(sql)
        
        if res and res.get("rows"):
            row = res["rows"][0]
            # row: [is_enabled, reason, end_timestamp, updated_at]
            is_enabled = bool(int(row[0].get("value", 0)))
            reason = str(row[1].get("value", ""))
            end_ts = int(row[2].get("value", 0))
            
            # Sync to local db
            db.set_setting("maintenance_mode", "true" if is_enabled else "false")
            db.set_setting("maintenance_reason", reason)
            db.set_setting("maintenance_end_ts", str(end_ts))

            return {
                "maintenance_mode": is_enabled,
                "reason": reason,
                "end_timestamp": end_ts
            }
        
        # Local fallback if Turso unreachable
        is_maint = db.get_setting("maintenance_mode", "false").lower() == "true"
        reason = db.get_setting("maintenance_reason", "Тривають планові технічні роботи.")
        end_ts = int(db.get_setting("maintenance_end_ts", "0") or "0")
        return {
            "maintenance_mode": is_maint,
            "reason": reason,
            "end_timestamp": end_ts
        }

    async def set_maintenance_state(self, is_enabled: bool, reason: str, end_timestamp: int) -> bool:
        """Updates maintenance state on Turso cloud database."""
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
        
        # Update local JSON db
        db.set_setting("maintenance_mode", "true" if is_enabled else "false")
        db.set_setting("maintenance_reason", reason)
        db.set_setting("maintenance_end_ts", str(end_timestamp))
        
        return res is not None

# Global Turso Database Client
turso_db = TursoDatabaseClient()
