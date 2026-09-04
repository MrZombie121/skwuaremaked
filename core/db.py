"""
Thread-Safe, Human-Readable JSON Database Engine for SkyWatch
Stores all channels, settings, targets, logs and analytics in structured, editable JSON files.
Location: data/*.json
"""
import os
import json
import time
import threading
from typing import List, Dict, Any, Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

CHANNELS_FILE = os.path.join(DATA_DIR, "channels.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
TARGETS_FILE = os.path.join(DATA_DIR, "targets.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
MESSAGES_LOG_FILE = os.path.join(DATA_DIR, "messages_log.json")

class JsonDatabase:
    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self._lock = threading.RLock()
        self.init_db()

    def _read_json(self, filepath: str, default: Any) -> Any:
        with self._lock:
            if not os.path.exists(filepath):
                self._write_json(filepath, default)
                return default
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return default

    def _write_json(self, filepath: str, data: Any):
        with self._lock:
            temp_path = f"{filepath}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, filepath)

    def init_db(self):
        """Initializes default JSON files."""
        # 1. Settings JSON
        if not os.path.exists(SETTINGS_FILE):
            default_settings = {
                "folder_url": "https://t.me/addlist/syGYtBj5T9AxNzIy",
                "tg_api_id": "",
                "tg_api_hash": "",
                "tg_phone": "",
                "session_string": "",
                "maintenance_mode": "false",
                "maintenance_reason": "Тривають технічні роботи з оновлення серверного ядра. Моніторинг скоро відновиться.",
                "admin_secret_key": "skywatch-secret-key-2026"
            }
            self._write_json(SETTINGS_FILE, default_settings)
        else:
            # Ensure maintenance keys exist in settings.json
            settings = self._read_json(SETTINGS_FILE, {})
            updated = False
            if "maintenance_mode" not in settings:
                settings["maintenance_mode"] = "false"
                updated = True
            if "maintenance_reason" not in settings:
                settings["maintenance_reason"] = "Тривають технічні роботи з оновлення серверного ядра. Моніторинг скоро відновиться."
                updated = True
            if "admin_secret_key" not in settings:
                settings["admin_secret_key"] = "skywatch-secret-key-2026"
                updated = True
            if updated:
                self._write_json(SETTINGS_FILE, settings)

        # 2. Channels JSON
        if not os.path.exists(CHANNELS_FILE):
            self._write_json(CHANNELS_FILE, [])

        # 3. Targets JSON
        if not os.path.exists(TARGETS_FILE):
            self._write_json(TARGETS_FILE, {})

        # 4. History JSON
        if not os.path.exists(HISTORY_FILE):
            self._write_json(HISTORY_FILE, [])

        # 5. Messages Log JSON
        if not os.path.exists(MESSAGES_LOG_FILE):
            self._write_json(MESSAGES_LOG_FILE, [])

    # --- Settings API ---
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        settings = self._read_json(SETTINGS_FILE, {})
        val = settings.get(key)
        return str(val) if val is not None else default

    def set_setting(self, key: str, value: str):
        settings = self._read_json(SETTINGS_FILE, {})
        settings[key] = str(value)
        self._write_json(SETTINGS_FILE, settings)

    # --- Channels API ---
    def get_all_channels(self, only_active: bool = False) -> List[Dict[str, Any]]:
        channels = self._read_json(CHANNELS_FILE, [])
        if only_active:
            return [c for c in channels if c.get("is_active", True)]
        return channels

    def add_channel(self, title: str, username: Optional[str] = None, tg_channel_id: Optional[int] = None, folder_url: Optional[str] = None) -> int:
        channels = self._read_json(CHANNELS_FILE, [])
        clean_user = username.strip("@") if username else None
        
        for c in channels:
            if tg_channel_id and c.get("tg_channel_id") == tg_channel_id:
                c["title"] = title
                c["username"] = clean_user or c.get("username")
                c["folder_url"] = folder_url or c.get("folder_url")
                self._write_json(CHANNELS_FILE, channels)
                return c["id"]
            if clean_user and c.get("username") and str(c.get("username")).lower() == clean_user.lower():
                c["title"] = title
                c["folder_url"] = folder_url or c.get("folder_url")
                self._write_json(CHANNELS_FILE, channels)
                return c["id"]

        next_id = max([c["id"] for c in channels], default=0) + 1
        new_ch = {
            "id": next_id,
            "tg_channel_id": tg_channel_id,
            "username": clean_user,
            "title": title,
            "folder_url": folder_url,
            "is_active": True,
            "total_messages_parsed": 0,
            "threats_detected": 0,
            "last_message_at": 0,
            "added_at": time.time()
        }
        channels.append(new_ch)
        self._write_json(CHANNELS_FILE, channels)
        return next_id

    def bulk_upsert_folder_channels(self, new_channels: List[Dict[str, Any]], folder_url: str):
        """Adds or updates all channels discovered from Telegram chatlist folder link."""
        channels = self._read_json(CHANNELS_FILE, [])
        now = time.time()
        
        for ch in new_channels:
            tg_id = ch.get("id")
            clean_user = ch.get("username", "").strip("@") if ch.get("username") else None
            title = ch.get("title", "Telegram Channel")

            matched = False
            for existing in channels:
                if tg_id and existing.get("tg_channel_id") == tg_id:
                    existing["title"] = title
                    existing["username"] = clean_user or existing.get("username")
                    existing["folder_url"] = folder_url
                    matched = True
                    break
                if clean_user and existing.get("username") and str(existing.get("username")).lower() == clean_user.lower():
                    existing["title"] = title
                    existing["tg_channel_id"] = tg_id or existing.get("tg_channel_id")
                    existing["folder_url"] = folder_url
                    matched = True
                    break

            if not matched:
                next_id = max([c["id"] for c in channels], default=0) + 1
                channels.append({
                    "id": next_id,
                    "tg_channel_id": tg_id,
                    "username": clean_user,
                    "title": title,
                    "folder_url": folder_url,
                    "is_active": True,
                    "total_messages_parsed": 0,
                    "threats_detected": 0,
                    "last_message_at": 0,
                    "added_at": now
                })

        self._write_json(CHANNELS_FILE, channels)

    def toggle_channel(self, channel_id: int, is_active: bool):
        channels = self._read_json(CHANNELS_FILE, [])
        for c in channels:
            if c["id"] == channel_id:
                c["is_active"] = bool(is_active)
                break
        self._write_json(CHANNELS_FILE, channels)

    def delete_channel(self, channel_id: int):
        channels = self._read_json(CHANNELS_FILE, [])
        channels = [c for c in channels if c["id"] != channel_id]
        self._write_json(CHANNELS_FILE, channels)

    def record_channel_message(self, channel_name: str, message_id: int, text: str, is_threat: bool = False):
        now = time.time()
        
        channels = self._read_json(CHANNELS_FILE, [])
        for c in channels:
            title_match = c.get("title") and str(c.get("title")).lower() == channel_name.lower()
            user_match = c.get("username") and str(c.get("username")).lower() == channel_name.strip("@").lower()
            if title_match or user_match:
                c["total_messages_parsed"] = c.get("total_messages_parsed", 0) + 1
                if is_threat:
                    c["threats_detected"] = c.get("threats_detected", 0) + 1
                c["last_message_at"] = now
                break
        self._write_json(CHANNELS_FILE, channels)

        logs = self._read_json(MESSAGES_LOG_FILE, [])
        logs.insert(0, {
            "channel_title": channel_name,
            "message_id": message_id,
            "raw_text": text,
            "is_threat": bool(is_threat),
            "timestamp": now
        })
        if len(logs) > 200:
            logs = logs[:200]
        self._write_json(MESSAGES_LOG_FILE, logs)

    def get_recent_logs(self, limit: int = 60) -> List[Dict[str, Any]]:
        logs = self._read_json(MESSAGES_LOG_FILE, [])
        return logs[:limit]

    # --- Targets & History API ---
    def save_or_update_target(self, target_data: Dict[str, Any]):
        targets = self._read_json(TARGETS_FILE, {})
        tid = target_data["target_id"]
        targets[tid] = target_data
        self._write_json(TARGETS_FILE, targets)

    def record_threat_event(self, event_data: Dict[str, Any]):
        history = self._read_json(HISTORY_FILE, [])
        history.insert(0, event_data)
        if len(history) > 300:
            history = history[:300]
        self._write_json(HISTORY_FILE, history)

    def get_recent_threat_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        history = self._read_json(HISTORY_FILE, [])
        return history[:limit]

    def get_system_stats(self) -> Dict[str, Any]:
        channels = self.get_all_channels()
        active_channels = len([c for c in channels if c.get("is_active", True)])
        total_msgs = sum(c.get("total_messages_parsed", 0) for c in channels)
        total_threats = sum(c.get("threats_detected", 0) for c in channels)
        
        return {
            "total_channels": len(channels),
            "active_channels": active_channels,
            "total_messages": total_msgs,
            "total_threats": total_threats,
            "folder_url": self.get_setting("folder_url", "https://t.me/addlist/syGYtBj5T9AxNzIy")
        }

# Global JSON Database Singleton
db = JsonDatabase()
