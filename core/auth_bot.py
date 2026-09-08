"""
Telegram Login Bot & HMAC Authenticator for SkyWatch Developer API
Bot: @skywatchlogin_bot
Token: 8911655594:AAFltUJ96Buzg1swAtneXPxQWuEnF6yNyv8
Validates official Telegram Login Widget and processes /start deep links for instant 1-click login.
"""
import asyncio
import hashlib
import hmac
import logging
import os
import time
import aiohttp
from typing import Dict, Any, Optional

from core.turso_db import turso_db

logger = logging.getLogger("SkyWatch.AuthBot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8911655594:AAFltUJ96Buzg1swAtneXPxQWuEnF6yNyv8")
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "skywatchlogin_bot")

# In-memory auth session states: code -> { verified: bool, user_data: dict, timestamp: float }
pending_auth_sessions: Dict[str, Dict[str, Any]] = {}

def verify_telegram_widget_auth(auth_data: Dict[str, Any], bot_token: str = BOT_TOKEN) -> bool:
    """Verifies cryptographic HMAC-SHA256 signature from official Telegram Login Widget."""
    received_hash = auth_data.get("hash", "")
    if not received_hash:
        return False

    data_check_list = []
    for k in sorted(auth_data.keys()):
        if k != "hash" and auth_data[k] is not None:
            data_check_list.append(f"{k}={auth_data[k]}")
    data_check_string = "\n".join(data_check_list)

    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    return hmac.compare_digest(calculated_hash, received_hash)

class TelegramAuthBotService:
    def __init__(self, bot_token: str = BOT_TOKEN):
        self.bot_token = bot_token
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self._offset = 0

    async def start(self):
        if self.is_running or not self.bot_token:
            return
        self.is_running = True
        self._task = asyncio.create_task(self._poll_updates())
        logger.info(f"Telegram Auth Bot @{BOT_USERNAME} started.")

    async def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info(f"Telegram Auth Bot @{BOT_USERNAME} stopped.")

    async def _poll_updates(self):
        """Long-polling loop to capture /start auth_... from developers."""
        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
        send_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        while self.is_running:
            try:
                async with aiohttp.ClientSession() as session:
                    params = {"offset": self._offset, "timeout": 20}
                    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            updates = data.get("result", [])
                            for u in updates:
                                self._offset = max(self._offset, u.get("update_id", 0) + 1)
                                msg = u.get("message") or {}
                                text = msg.get("text", "").strip()
                                from_user = msg.get("from") or {}
                                chat_id = msg.get("chat", {}).get("id")

                                if text.startswith("/start"):
                                    parts = text.split()
                                    session_code = parts[1].replace("auth_", "") if len(parts) > 1 and parts[1].startswith("auth_") else None
                                    
                                    tg_id = str(from_user.get("id"))
                                    first_name = from_user.get("first_name", "")
                                    last_name = from_user.get("last_name", "")
                                    username = from_user.get("username", "")

                                    # Register developer in Turso Cloud Database
                                    dev_user = await turso_db.register_or_get_api_user(
                                        telegram_id=tg_id,
                                        first_name=first_name,
                                        last_name=last_name,
                                        username=username or f"user_{tg_id}"
                                    )

                                    if session_code and session_code in pending_auth_sessions:
                                        pending_auth_sessions[session_code] = {
                                            "verified": True,
                                            "api_key": dev_user["api_key"],
                                            "username": dev_user.get("username") or username or tg_id,
                                            "telegram_id": tg_id,
                                            "first_name": first_name,
                                            "timestamp": time.time()
                                        }

                                    # Send confirmation message to user
                                    reply_text = (
                                        f"👋 <b>Вітаємо в SKYWATCH DEVELOPER API!</b>\n\n"
                                        f"✅ Ваш акаунт успішно підтверджено!\n"
                                        f"🔑 <b>Ваш персональний API-ключ:</b>\n<code>{dev_user['api_key']}</code>\n\n"
                                        f"Поверніться у браузер — авторизацію завершено, і ключ вже завантажено на сторінку!"
                                    )
                                    try:
                                        await session.post(send_url, json={
                                            "chat_id": chat_id,
                                            "text": reply_text,
                                            "parse_mode": "HTML"
                                        })
                                    except Exception as se:
                                        logger.debug(f"Error sending bot reply: {se}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Auth bot polling note: {e}")
                await asyncio.sleep(4.0)

# Global Auth Bot Singleton
auth_bot = TelegramAuthBotService()
