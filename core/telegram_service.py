"""
Telethon Telegram Core Service for SkyWatch (StringSession Architecture)
Uses StringSession stored in data/settings.json for zero-lock operation.
Directly syncs folder https://t.me/addlist/syGYtBj5T9AxNzIy (31 channels) and streams
real-time air threat messages to Desktop Radar UI.
"""
import asyncio
import logging
import os
import re
import time
from typing import Callable, Dict, List, Optional, Any, Tuple
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError
from telethon.tl.functions.chatlists import CheckChatlistInviteRequest, JoinChatlistInviteRequest

import config
from core.db import db
from core.turso_db import turso_db
from core.models import RawTelegramMessage

logger = logging.getLogger("SkyWatch.TelegramService")

class TelegramService:
    def __init__(self, message_callback: Callable[[RawTelegramMessage], Any]):
        self.message_callback = message_callback
        self.client: Optional[TelegramClient] = None
        self.is_connected = False
        self.is_authorized = False
        self.user_info: Optional[Dict[str, Any]] = None
        
        # Pending 2FA / Login state
        self._pending_phone: Optional[str] = None
        self._pending_phone_hash: Optional[str] = None

        # Monitored entities
        self._listening_chats: List[Any] = []
        self._event_handler = None

    async def _get_credentials_async(self) -> Tuple[int, str, str]:
        # Priority: 1. Environment Variable -> 2. Turso Cloud DB -> 3. Local JSON DB -> 4. config.py
        api_id_val = os.getenv("TG_API_ID") or await turso_db.get_setting("tg_api_id") or db.get_setting("tg_api_id") or config.TELEGRAM_API_ID
        api_hash_val = os.getenv("TG_API_HASH") or await turso_db.get_setting("tg_api_hash") or db.get_setting("tg_api_hash") or config.TELEGRAM_API_HASH
        session_str = os.getenv("TG_SESSION_STRING") or await turso_db.get_setting("session_string") or db.get_setting("session_string") or config.TELEGRAM_SESSION_STRING or ""
        
        try:
            api_id = int(api_id_val) if api_id_val else 0
        except (ValueError, TypeError):
            api_id = 0
            
        return api_id, str(api_hash_val or ""), str(session_str)

    async def initialize(self) -> bool:
        """Initializes Telethon client using StringSession stored in Turso/Environment/settings.json."""
        api_id, api_hash, session_str = await self._get_credentials_async()
        
        if not api_id or not api_hash:
            logger.info("Telegram API credentials not configured yet. Awaiting user login.")
            return False

        try:
            logger.info(f"Connecting Telethon client with StringSession (API ID: {api_id})...")
            self.client = TelegramClient(StringSession(session_str), api_id, api_hash)
            await self.client.connect()
            self.is_connected = True

            if await self.client.is_user_authorized():
                self.is_authorized = True
                me = await self.client.get_me()
                self.user_info = {
                    "id": me.id,
                    "first_name": me.first_name,
                    "username": me.username,
                    "phone": me.phone
                }
                logger.info(f"Telegram client authorized as: {me.first_name} (@{me.username or me.phone})")
                
                # Save string session to Turso and Local DB
                new_session_str = self.client.session.save()
                if new_session_str != session_str:
                    db.set_setting("session_string", new_session_str)
                    await turso_db.set_setting("session_string", new_session_str)

                # Auto-sync the 31 channels folder and start live listening
                default_folder = db.get_setting("folder_url") or config.TELEGRAM_FOLDER_URL or "https://t.me/addlist/syGYtBj5T9AxNzIy"
                asyncio.create_task(self.sync_folder(default_folder))
                return True
            else:
                logger.info("Telegram session is not authorized yet.")
                return False
        except Exception as e:
            logger.error(f"Failed to initialize Telethon: {e}")
            return False

    # --- Authorization Flow ---
    async def request_auth_code(self, api_id: int, api_hash: str, phone: str) -> Dict[str, Any]:
        """Step 1: Save credentials and request fresh verification code (cleans old expired sessions)."""
        db.set_setting("tg_api_id", str(api_id))
        db.set_setting("tg_api_hash", str(api_hash))
        db.set_setting("tg_phone", str(phone))

        if self.client:
            try:
                if self.client.is_connected():
                    await self.client.disconnect()
            except Exception:
                pass

        # Always start with fresh empty StringSession to prevent "used under two different IP" errors
        self.client = TelegramClient(StringSession(""), api_id, api_hash)
        await self.client.connect()
        self.is_connected = True

        try:
            sent_code = await self.client.send_code_request(phone)
            self._pending_phone = phone
            self._pending_phone_hash = sent_code.phone_code_hash
            logger.info(f"Auth code requested for phone: {phone}")
            return {"status": "code_sent", "phone": phone, "phone_code_hash": sent_code.phone_code_hash}
        except Exception as e:
            logger.error(f"Error sending code: {e}")
            return {"status": "error", "message": str(e)}

    async def submit_auth_code(self, code: str, password_2fa: Optional[str] = None) -> Dict[str, Any]:
        """Step 2: Submit verification code or 2FA password."""
        if not self.client or not self._pending_phone or not self._pending_phone_hash:
            return {"status": "error", "message": "No active login request found. Request code first."}

        try:
            if password_2fa:
                await self.client.sign_in(password=password_2fa)
            else:
                await self.client.sign_in(
                    phone=self._pending_phone,
                    code=code,
                    phone_code_hash=self._pending_phone_hash
                )

            self.is_authorized = True
            me = await self.client.get_me()
            self.user_info = {
                "id": me.id,
                "first_name": me.first_name,
                "username": me.username,
                "phone": me.phone
            }
            logger.info(f"Successfully logged in as {me.first_name} (@{me.username})")

            # Save StringSession locally and to Turso Cloud
            string_session = self.client.session.save()
            db.set_setting("session_string", string_session)
            await turso_db.set_setting("session_string", string_session)
            await turso_db.set_setting("tg_phone", str(me.phone or ""))

            # Start folder sync and listener
            default_folder = db.get_setting("folder_url") or "https://t.me/addlist/syGYtBj5T9AxNzIy"
            asyncio.create_task(self.sync_folder(default_folder))

            return {"status": "success", "user": self.user_info}

        except SessionPasswordNeededError:
            logger.info("2FA password required.")
            return {"status": "2fa_required", "message": "2-Step Verification password is required."}
        except (PhoneCodeInvalidError, PasswordHashInvalidError) as e:
            return {"status": "error", "message": f"Invalid verification code or password: {e}"}
        except Exception as e:
            logger.error(f"Login failed: {e}")
            return {"status": "error", "message": str(e)}

    # --- Telegram Folder / Addlist Parser (https://t.me/addlist/syGYtBj5T9AxNzIy) ---
    def _extract_chatlist_slug(self, folder_url: str) -> Optional[str]:
        match = re.search(r'addlist/([a-zA-Z0-9_\-]+)', folder_url)
        if match:
            return match.group(1)
        if re.match(r'^[a-zA-Z0-9_\-]+$', folder_url.strip()):
            return folder_url.strip()
        return None

    async def sync_folder(self, folder_url: str) -> Dict[str, Any]:
        """
        Parses a Telegram chatlist folder invite link (e.g. https://t.me/addlist/syGYtBj5T9AxNzIy),
        fetches all 31 channels, joins them if needed, saves to channels.json, and starts live listener.
        """
        slug = self._extract_chatlist_slug(folder_url)
        if not slug:
            return {"status": "error", "message": f"Invalid Telegram folder URL format: {folder_url}"}

        db.set_setting("folder_url", folder_url)

        if not self.client or not self.is_authorized:
            logger.warning(f"Saved folder URL {folder_url} in DB, but Telegram client is not authorized yet.")
            return {"status": "saved_offline", "folder_url": folder_url, "channels": db.get_all_channels()}

        try:
            logger.info(f"Resolving Telegram Chatlist Folder (slug: {slug})...")
            invite_result = await self.client(CheckChatlistInviteRequest(slug=slug))
            
            chats_found = []
            folder_title = getattr(invite_result, 'title', 'Для монітору')
            folder_chats = list(getattr(invite_result, 'chats', []))

            for chat in folder_chats:
                channel_info = {
                    "id": chat.id,
                    "title": getattr(chat, 'title', 'Channel'),
                    "username": getattr(chat, 'username', None)
                }
                chats_found.append(channel_info)

            # Join missing peers in the chatlist folder if any
            missing_peers = getattr(invite_result, 'missing_peers', [])
            if missing_peers:
                logger.info(f"Joining {len(missing_peers)} new channels from folder...")
                try:
                    await self.client(JoinChatlistInviteRequest(slug=slug, peers=missing_peers))
                except Exception as je:
                    logger.warning(f"Note on joining folder channels: {je}")

            # Store all channels in JSON DB
            if chats_found:
                db.bulk_upsert_folder_channels(chats_found, folder_url)
                logger.info(f"Successfully imported {len(chats_found)} channels from folder '{folder_title}' into data/channels.json.")

            # Attach live listener to all folder chat entities
            self._listening_chats = folder_chats
            await self.restart_listener()

            return {
                "status": "success",
                "folder_title": folder_title,
                "folder_url": folder_url,
                "imported_channels": chats_found,
                "total_channels": len(db.get_all_channels())
            }

        except Exception as e:
            logger.error(f"Failed to sync Telegram folder ({folder_url}): {e}")
            return {"status": "error", "message": str(e)}

    # --- Real-Time Channel Listener ---
    async def start_listener(self):
        """Subscribes Telethon to all active channels."""
        if not self.client or not self.is_authorized:
            return

        target_chats = self._listening_chats
        if not target_chats:
            active_channels = db.get_all_channels(only_active=True)
            target_chats = [ch.get("tg_channel_id") or ch.get("username") for ch in active_channels if ch.get("tg_channel_id") or ch.get("username")]

        if not target_chats:
            logger.info("No active channels configured in DB to listen.")
            return

        # Remove existing handler if any
        if self._event_handler:
            try:
                self.client.remove_event_handler(self._event_handler)
            except Exception:
                pass

        logger.info(f"Setting up real-time listener for {len(target_chats)} channels...")

        @self.client.on(events.NewMessage(chats=target_chats))
        async def on_new_message(event):
            try:
                chat = await event.get_chat()
                channel_title = getattr(chat, 'title', getattr(chat, 'username', 'Telegram Channel'))
                
                # Extract reply_to_msg_id if this message is a reply update
                reply_to_id = getattr(event, 'reply_to_msg_id', None)
                if not reply_to_id and hasattr(event, 'message') and event.message and event.message.reply_to:
                    reply_to_id = getattr(event.message.reply_to, 'reply_to_msg_id', None)

                msg = RawTelegramMessage(
                    channel=channel_title,
                    message_id=event.id,
                    reply_to_msg_id=reply_to_id,
                    text=event.raw_text,
                    timestamp=time.time()
                )
                logger.info(f"Telegram Message from [{channel_title}]: {event.raw_text[:60]}...")
                await self.message_callback(msg)
            except Exception as ex:
                logger.error(f"Error handling live Telegram message: {ex}")

        self._event_handler = on_new_message
        logger.info(f"Telethon live listener is actively monitoring {len(target_chats)} Telegram channels.")

    async def restart_listener(self):
        """Restarts listener when channels list changes."""
        await self.start_listener()

    def get_status(self) -> Dict[str, Any]:
        """Returns overall Telegram status, user profile, active channels, and folder config."""
        api_id, _ = self._get_credentials()
        return {
            "is_connected": self.is_connected,
            "is_authorized": self.is_authorized,
            "has_credentials": bool(api_id),
            "user": self.user_info,
            "folder_url": db.get_setting("folder_url", "https://t.me/addlist/syGYtBj5T9AxNzIy"),
            "channels_count": len(db.get_all_channels(only_active=True)),
            "total_channels": len(db.get_all_channels())
        }

    async def inject_manual_message(self, channel: str, text: str):
        msg = RawTelegramMessage(
            channel=channel,
            message_id=int(time.time()),
            text=text,
            timestamp=time.time()
        )
        await self.message_callback(msg)

    async def stop(self):
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        self.is_connected = False
        self.is_authorized = False
