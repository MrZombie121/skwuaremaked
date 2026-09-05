"""
Tactical AI Intelligence Analyst for SkyWatch
Supports:
1. Google AI Studio (Gemini 2.5/3.5+ models with AIzaSy... or AQ... keys)
2. OpenRouter AI (sk-or-v1-...)
3. Groq AI (gsk_...)
4. OpenAI (sk-...)
Generates structured, high-accuracy Ukrainian operational bulletins for Telegram channels.
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
        # Models in order of preference
        self.gemini_models = [
            "gemini-2.5-flash",
            "gemini-flash-latest",
            "gemini-3.5-flash",
            "gemini-3.7-flash",
            "gemini-3.1-flash-lite",
            "gemini-2.5-pro",
            "gemini-pro-latest"
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

    async def _call_openai_compatible(self, session: aiohttp.ClientSession, endpoint: str, model: str, api_key: str, prompt_text: str) -> Optional[str]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        if "openrouter" in endpoint:
            headers["HTTP-Referer"] = "https://skywatchua.onrender.com"
            headers["X-Title"] = "SkyWatch Radar"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt_text}
            ],
            "temperature": 0.3,
            "max_tokens": 1024
        }
        async with session.post(endpoint, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=25)) as resp:
            if resp.status == 200:
                data = await resp.json()
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "").strip()
            err_text = await resp.text()
            logger.warning(f"OpenAI compatible {endpoint} ({model}) error HTTP {resp.status}: {err_text}")
        return None

    async def generate_tactical_summary(self, targets: List[ActiveTarget], api_key_override: Optional[str] = None) -> Dict[str, Any]:
        """Calls AI provider to produce an operational summary in Ukrainian."""
        active_key = (api_key_override or self._get_active_api_key()).strip()
        if not active_key:
            return {
                "status": "error",
                "message": "API-ключ не налаштовано. Отримайте безкоштовний ключ на https://aistudio.google.com/app/apikey і вставте в поле нижче."
            }

        prompt_text = self._format_targets_prompt(targets)

        async with aiohttp.ClientSession() as session:
            # 1. OpenRouter Key (sk-or-v1-...)
            if active_key.startswith("sk-or-"):
                models = ["google/gemini-2.5-flash", "google/gemini-flash-1.5", "meta-llama/llama-3.3-70b-instruct"]
                for m in models:
                    try:
                        res = await self._call_openai_compatible(session, "https://openrouter.ai/api/v1/chat/completions", m, active_key, prompt_text)
                        if res:
                            return {"status": "ok", "model": f"OpenRouter ({m})", "summary": res, "targets_count": len(targets), "timestamp": time.time()}
                    except Exception as e:
                        logger.warning(f"OpenRouter error: {e}")

            # 2. Groq Key (gsk_...)
            elif active_key.startswith("gsk_"):
                try:
                    res = await self._call_openai_compatible(session, "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile", active_key, prompt_text)
                    if res:
                        return {"status": "ok", "model": "Groq LLaMA-3.3 70B", "summary": res, "targets_count": len(targets), "timestamp": time.time()}
                except Exception as e:
                    logger.warning(f"Groq error: {e}")

            # 3. Standard OpenAI Key (sk-...)
            elif active_key.startswith("sk-") and not active_key.startswith("sk-or-"):
                try:
                    res = await self._call_openai_compatible(session, "https://api.openai.com/v1/chat/completions", "gpt-4o-mini", active_key, prompt_text)
                    if res:
                        return {"status": "ok", "model": "OpenAI GPT-4o-mini", "summary": res, "targets_count": len(targets), "timestamp": time.time()}
                except Exception as e:
                    logger.warning(f"OpenAI error: {e}")

            # 4. Google Gemini API (Standard Google AI Studio AIzaSy... or Cloud Token)
            gemini_payload = {
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
            for model_name in self.gemini_models:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={active_key}"
                headers = {
                    "Content-Type": "application/json",
                    "x-goog-api-key": active_key
                }

                try:
                    async with session.post(url, headers=headers, json=gemini_payload, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            candidates = data.get("candidates", [])
                            if candidates:
                                content_parts = candidates[0].get("content", {}).get("parts", [])
                                if content_parts:
                                    summary_text = content_parts[0].get("text", "").strip()
                                    return {
                                        "status": "ok",
                                        "model": f"Google {model_name}",
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
                                    last_error = "Перевищено ліміт запитів API (429 Rate Limit). Зачекайте 30 секунд і спробуйте знову."
                                elif "API_KEY_INVALID" in err_msg or "UNAUTHENTICATED" in str(err_data):
                                    last_error = f"API-ключ недійсний. Отримайте робочий безкоштовний ключ на https://aistudio.google.com/app/apikey (формат: AIzaSy...)"
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
            "message": f"Не вдалося згенерувати звіт: {last_error}"
        }

# Global Gemini Analyst Singleton
gemini_analyst = GeminiAnalystService()
