"""
Google Gemini AI Tactical Intelligence Analyst for SkyWatch
Analyzes active airspace threats on the radar and generates concise, structured
operational bulletins for publication in Telegram channels.
Supports Gemini 3+ models (gemini-3.5-flash, gemini-3.7-flash, gemini-3.1-flash-lite, etc.).
"""
import os
import time
import logging
import aiohttp
from typing import List, Dict, Any, Optional
import config
from core.db import db
from core.models import ActiveTarget

logger = logging.getLogger("SkyWatch.Gemini")

DEFAULT_GEMINI_KEY = "AQ.Ab8RN6L3GMH9v0j1qcW-1wmoPVYsJMtVqer6gbDca3KRvQ6vNA"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", DEFAULT_GEMINI_KEY)

SYSTEM_INSTRUCTION = (
    "Ты тактический аналитик SkywatchUA. На основе переданного списка целей сформируй "
    "краткую оперативную сводку для Telegram-канала: основные направления удара, "
    "потенциальные города под угрозой по вектору движения, рекомендации по безопасности. "
    "Лаконично, без воды, с четким форматированием. Делай на украинськом"
)

class GeminiAnalystService:
    def __init__(self, api_key: str = GEMINI_API_KEY):
        self.api_key = api_key
        # Models 3.0+ in order of preference
        self.models = [
            "gemini-3.5-flash",
            "gemini-3.7-flash",
            "gemini-3.1-flash-lite",
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-flash-latest"
        ]

    def _get_active_api_key(self) -> str:
        saved_key = db.get_setting("gemini_api_key")
        if saved_key and saved_key.strip():
            return saved_key.strip()
        return os.getenv("GEMINI_API_KEY", self.api_key).strip()

    def _format_targets_prompt(self, targets: List[ActiveTarget]) -> str:
        if not targets:
            return "Станом на зараз повітряний простір України чистий. Активних повітряних загроз на радарі не зафіксовано."

        now_str = time.strftime("%H:%M:%S", time.localtime())
        lines = [
            f"ОПЕРАТИВНА ОБСТАНОВКА НА {now_str} (Київський час):",
            f"Всього зафіксовано повітряних цілей: {len(targets)}\n",
            "СПИСОК АКТИВНИХ ЦІЛЕЙ:"
        ]

        for idx, t in enumerate(targets, 1):
            dest = t.destination_name or f"Курс {round(t.heading_deg)}° ({t.heading.value})"
            loc = t.current_location_name or f"[{t.current_lat:.2f}, {t.current_lon:.2f}]"
            lines.append(
                f"{idx}. ID: {t.target_id} | Тип: {t.target_subtype or t.target_type.value} ({t.count}x) | "
                f"Позиція: {loc} | Напрямок / Ціль: {dest} | Азимут: {round(t.heading_deg)}° | "
                f"Швидкість: {round(t.speed_kmh)} км/год | ETA: {t.eta_minutes or '?'} хв | "
                f"Джерела: {', '.join(t.sources)}"
            )

        lines.append("\nСформуй чітке, структуроване зведення з емодзі для Telegram-каналу.")
        return "\n".join(lines)

    async def generate_tactical_summary(self, targets: List[ActiveTarget], api_key_override: Optional[str] = None) -> Dict[str, Any]:
        """Calls Gemini API (v3.5+ models) to produce an operational summary in Ukrainian."""
        active_key = (api_key_override or self._get_active_api_key()).strip()
        if not active_key:
            return {
                "status": "error",
                "message": "API-ключ Gemini не налаштовано. Введіть API ключ у полі в адмін-панелі."
            }

        prompt_text = self._format_targets_prompt(targets)
        
        payload = {
            "system_instruction": {
                "parts": [{"text": SYSTEM_INSTRUCTION}]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt_text}]
                }
            ],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 1024,
                "topP": 0.8
            }
        }

        last_error = "Unknown error"
        async with aiohttp.ClientSession() as session:
            for model_name in self.models:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={active_key}"
                headers = {
                    "Content-Type": "application/json",
                    "x-goog-api-key": active_key
                }

                try:
                    async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            candidates = data.get("candidates", [])
                            if candidates:
                                content_parts = candidates[0].get("content", {}).get("parts", [])
                                if content_parts:
                                    summary_text = content_parts[0].get("text", "").strip()
                                    return {
                                        "status": "ok",
                                        "model": model_name,
                                        "summary": summary_text,
                                        "targets_count": len(targets),
                                        "timestamp": time.time()
                                    }
                        else:
                            try:
                                err_data = await resp.json()
                                err_msg = err_data.get("error", {}).get("message", "")
                                err_reason = (err_data.get("error", {}).get("details", [{}])[0] or {}).get("reason", "")
                                if resp.status == 429:
                                    last_error = "Перевищено ліміт запитів Gemini API (429 Rate Limit). Спробуйте ще раз через 30-60 секунд."
                                elif "API_KEY_INVALID" in err_msg or "API_KEY_SERVICE_BLOCKED" in err_reason:
                                    last_error = f"API-ключ недійсний або заблокований Google ({err_reason or err_msg}). Створіть ключ на https://aistudio.google.com/app/apikey"
                                else:
                                    last_error = f"HTTP {resp.status}: {err_msg or err_data}"
                            except Exception:
                                err_body = await resp.text()
                                last_error = f"HTTP {resp.status}: {err_body}"
                            logger.warning(f"Gemini {model_name} failed: {last_error}")
                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"Gemini {model_name} exception: {e}")

        return {
            "status": "error",
            "message": f"Не вдалося згенерувати звіт через Gemini API: {last_error}"
        }

# Global Gemini Analyst Singleton
gemini_analyst = GeminiAnalystService()
