"""
Google Gemini AI Tactical Intelligence Analyst for SkyWatch
Analyzes active airspace threats on the radar and generates concise, structured
operational bulletins for publication in Telegram channels.
Supports both Google Cloud Bearer tokens and standard Google AI Studio API keys (AIza...).
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

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6Kec0Zhyl-Mn6GkZh2hHd9cuFMtVL3GmooclHMTcOOR9g")
SYSTEM_INSTRUCTION = (
    "Ты тактический аналитик SkywatchUA. На основе переданного списка целей сформируй "
    "краткую оперативную сводку для Telegram-канала: основные направления удара, "
    "потенциальные города под угрозой по вектору движения, рекомендации по безопасности. "
    "Лаконично, без воды, с четким форматированием. Делай на украинськом"
)

class GeminiAnalystService:
    def __init__(self, api_key: str = GEMINI_API_KEY):
        self.api_key = api_key
        # Models to try in order of preference
        self.models = ["gemini-1.5-flash", "gemini-1.5-pro", "gemini-2.0-flash", "gemini-pro"]

    def _get_active_api_key(self) -> str:
        saved_key = db.get_setting("gemini_api_key")
        if saved_key and saved_key.strip():
            return saved_key.strip()
        return os.getenv("GEMINI_API_KEY", self.api_key).strip()

    def _format_targets_prompt(self, targets: List[ActiveTarget]) -> str:
        if not targets:
            return "Станом на зараз повітряний простір України чистий. Активних повітряних загроз на радарі не зафіксовано."

        now_str = time.strftime("%H:%M:%S", time.localtime())
        lines = [f"ОПЕРАТИВНА ОБСТАНОВКА НА {now_str} (Київський час):", f"Всього зафіксовано повітряних цілей: {len(targets)}\n", "СПИСОК АКТИВНИХ ЦІЛЕЙ:"]

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
        """Calls Gemini API to produce an operational summary in Ukrainian."""
        active_key = (api_key_override or self._get_active_api_key()).strip()
        if not active_key:
            return {
                "status": "error",
                "message": "API-ключ Gemini не налаштовано. Введіть API ключ у полі нижче або в налаштуваннях."
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

        # Check if Bearer OAuth token (starts with AQ. or ya29.) vs AI Studio API key (starts with AIza...)
        is_bearer = active_key.startswith("AQ.") or active_key.startswith("ya29.")
        
        last_error = "Unknown error"
        async with aiohttp.ClientSession() as session:
            for model_name in self.models:
                if is_bearer:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
                    headers = {
                        "Authorization": f"Bearer {active_key}",
                        "Content-Type": "application/json"
                    }
                else:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={active_key}"
                    headers = {
                        "Content-Type": "application/json"
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
