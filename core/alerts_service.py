"""
Live Air Raid Alerts Service for SkyWatch Tactical Radar
Integrates with alerts.in.ua (alarms.in.ua) via key dynamically retrieved from Turso Cloud DB.
Classifies all 136 Official Ukrainian Raions (ADM2) into:
- NONE: 100% Fully Transparent (No color, no borders, 0 opacity)
- DRONE (🟡 Yellow Alert): Drone / Shahed Danger in this specific Raion
- MISSILE (🔴 Red Alert): Missile / Ballistic / Air Raid in this specific Raion
"""
import asyncio
import logging
import os
import time
import aiohttp
from typing import Dict, Any, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

from core.raions_data import UKRAINE_RAIONS_DATA
from core.turso_db import turso_db
from core.db import db

logger = logging.getLogger("SkyWatch.AlertsService")

# Permanent alert regions (>1000 days under occupation / constant threat)
PERMANENT_ALERT_OBLASTS = ("луганська", "автономна республіка крим", "крим")

class RaionAlertStatus(BaseModel):
    fid: int
    id: str
    name: str
    oblast: str = ""
    level: str = "NONE" # "NONE", "DRONE" (Yellow), "MISSILE" (Red)
    threat_type: Optional[str] = None
    reason: Optional[str] = None
    started_at: Optional[float] = None
    duration_minutes: int = 0
    updated_at: float = Field(default_factory=time.time)

class AirAlertsService:
    def __init__(self):
        self.raion_alerts_state: Dict[str, RaionAlertStatus] = {}
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self._init_default_states()

    def _init_default_states(self):
        now = time.time()
        for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
            oblast = rdata.get("oblast", "")
            obl_lower = oblast.lower()
            
            # Luhansk Oblast & Crimea have permanent air alert
            is_permanent = any(p in obl_lower for p in PERMANENT_ALERT_OBLASTS)
            
            self.raion_alerts_state[fid_str] = RaionAlertStatus(
                fid=int(rdata.get("fid", fid_str)),
                id=fid_str,
                name=rdata["name"],
                oblast=oblast,
                level="MISSILE" if is_permanent else "NONE",
                threat_type="AIR_RAID" if is_permanent else None,
                reason="Тривога понад 1000 днів (Окупована територія)" if is_permanent else "Спокійно",
                started_at=now - (1000 * 86400) if is_permanent else None,
                duration_minutes=1440000 if is_permanent else 0,
                updated_at=now
            )

    async def _get_active_api_key(self) -> str:
        """Retrieves alerts API key dynamically from Turso Cloud DB, settings, or env."""
        key = await turso_db.get_setting("alerts_api_key")
        if key and key.strip():
            return key.strip()
        saved = db.get_setting("alerts_api_key")
        if saved and saved.strip():
            return saved.strip()
        return os.getenv("ALERTS_API_KEY", "").strip()

    def update_from_active_threats(self, active_targets: List[Any]):
        """
        No-op: Tactical air radar solely reflects real official state alerts
        and does NOT invent synthetic alerts in raions along flight paths.
        """
        pass

    def _match_alert_to_raions(self, loc_title: str, loc_oblast: str, loc_raion: str, loc_type: str) -> Set[str]:
        """Maps an incoming alerts.in.ua item to matching official 136 raion FIDs."""
        matched: Set[str] = set()
        t_title = (loc_title or "").strip().lower()
        t_obl = (loc_oblast or "").strip().lower()
        t_raion = (loc_raion or "").strip().lower()

        # 1. Crimea & Sevastopol
        if any(k in t_title or k in t_obl for k in ("крим", "севастопол", "crimea", "sevastopol")):
            for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
                if "крим" in rdata.get("oblast", "").lower():
                    matched.add(fid_str)
            return matched

        # 2. Luhansk Oblast
        if "луган" in t_title or "луган" in t_obl:
            for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
                if "луган" in rdata.get("oblast", "").lower():
                    matched.add(fid_str)
            return matched

        # 3. Kyiv City or Kyiv Oblast
        if ("київ" in t_title or "київ" in t_obl) and ("область" in t_title or "область" in t_obl or loc_type in ("oblast", "city") or "м. київ" in t_title or t_title == "київ"):
            for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
                if "київ" in rdata.get("oblast", "").lower():
                    matched.add(fid_str)
            return matched

        # 4. Specific Raion matching
        target_raion_name = t_raion or (t_title if loc_type == "raion" or "район" in t_title else "")
        if target_raion_name:
            clean_r = target_raion_name.replace("район", "").replace("р-н", "").strip()
            for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
                r_name_clean = rdata["name"].lower().replace("район", "").replace("р-н", "").strip()
                if clean_r and (clean_r == r_name_clean or clean_r in r_name_clean or r_name_clean in clean_r):
                    if t_obl:
                        clean_obl = t_obl.replace("область", "").replace("обл.", "").replace("ська", "").replace("зька", "").replace("цька", "").strip()
                        r_obl_clean = rdata.get("oblast", "").lower().replace("область", "").replace("ська", "").replace("зька", "").replace("цька", "").strip()
                        if clean_obl and (clean_obl in r_obl_clean or r_obl_clean in clean_obl):
                            matched.add(fid_str)
                    else:
                        matched.add(fid_str)
            if matched:
                return matched

        # 5. Full Oblast matching
        target_obl_name = t_obl or (t_title if loc_type == "oblast" or "область" in t_title else "")
        if target_obl_name:
            clean_obl = target_obl_name.replace("область", "").replace("обл.", "").replace("ська", "").replace("зька", "").replace("цька", "").strip()
            if clean_obl:
                for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
                    r_obl_clean = rdata.get("oblast", "").lower().replace("область", "").replace("ська", "").replace("зька", "").replace("цька", "").strip()
                    if clean_obl in r_obl_clean or r_obl_clean in clean_obl:
                        matched.add(fid_str)
            if matched:
                return matched

        # 6. City / Hromada matching against Raion name
        clean_title = t_title.replace("район", "").replace("громада", "").replace("м.", "").strip()
        if len(clean_title) >= 4:
            for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
                r_name_clean = rdata["name"].lower().replace("район", "").strip()
                if clean_title in r_name_clean or r_name_clean in clean_title:
                    matched.add(fid_str)

        return matched

    async def fetch_external_alerts(self):
        """Fetches live Ukrainian state alerts from alerts.in.ua using Turso key."""
        api_key = await self._get_active_api_key()
        now = time.time()
        
        # Default active raion alerts map with permanent alert regions
        active_raion_alerts: Dict[str, Tuple[str, str, str, Optional[float]]] = {} # fid -> (level, type, reason, started_at)
        for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
            obl_lower = rdata.get("oblast", "").lower()
            if any(p in obl_lower for p in PERMANENT_ALERT_OBLASTS):
                active_raion_alerts[fid_str] = (
                    "MISSILE",
                    "air_raid",
                    "Тривога понад 1000 днів (Окупована територія)",
                    now - (1000 * 86400)
                )

        if not api_key:
            # Apply permanent baseline and return
            self._apply_alerts_snapshot(active_raion_alerts, now)
            return

        url = "https://api.alerts.in.ua/v1/alerts/active.json"
        
        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Mozilla/5.0 (SkyWatch Radar Engine v2.1)"
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        alerts = data.get("alerts", [])

                        drone_keywords = ("шахед", "шахід", "бпла", "дрон", "герань", "uav", "drone", "гербера", "імітатор", "мопед")
                        missile_keywords = ("ракета", "балістик", "крилат", "пуск", "х-101", "х-22", "х-59", "х-69", "х-47", "калібр", "іскандер", "кинджал", "ту-95", "ту-22", "міг-31", "каб", "авіаці", "артобстріл", "артилерій", "рсзв", "град", "с-300", "с-400")

                        for a in alerts:
                            alert_type = str(a.get("alert_type") or "air_raid").lower().strip()
                            threat_type = str(a.get("threat_type") or "").lower().strip()
                            loc_type = str(a.get("location_type") or "").lower().strip()
                            loc_title = str(a.get("location_title") or "").strip()
                            loc_raion = str(a.get("location_raion") or "").strip()
                            loc_oblast = str(a.get("location_oblast") or "").strip()
                            notes = str(a.get("notes") or "").lower().strip()
                            description = str(a.get("description") or "").lower().strip()

                            # Determine Alert Level (Yellow vs Red)
                            has_drone = (
                                alert_type in ("drone", "uav", "shahed") or
                                threat_type in ("drone", "uav", "shahed", "бпла", "шахед", "дрон") or
                                any(kw in notes for kw in drone_keywords) or
                                any(kw in description for kw in drone_keywords)
                            )
                            has_missile = (
                                alert_type in ("missile", "artillery_shelling", "nuclear", "chemical") or
                                threat_type in ("missile", "ballistic", "kab", "aviation", "artillery") or
                                any(kw in notes for kw in missile_keywords) or
                                any(kw in description for kw in missile_keywords)
                            )

                            if has_drone and not has_missile:
                                level = "DRONE"
                                reason = "Загроза ударних БпЛА"
                            elif has_missile:
                                level = "MISSILE"
                                reason = "Ракетна небезпека" if alert_type != "artillery_shelling" else "Артилерійський обстріл"
                            else:
                                level = "MISSILE"
                                reason = "Повітряна тривога"

                            # Parse start timestamp if available
                            started_at = now
                            started_str = a.get("started_at")
                            if started_str:
                                try:
                                    started_at = time.mktime(time.strptime(started_str[:19], "%Y-%m-%dT%H:%M:%S"))
                                except Exception:
                                    started_at = now

                            matched_fids = self._match_alert_to_raions(loc_title, loc_oblast, loc_raion, loc_type)

                            for fid_str in matched_fids:
                                # MISSILE tier has higher severity than DRONE tier
                                if fid_str not in active_raion_alerts or (active_raion_alerts[fid_str][0] == "DRONE" and level == "MISSILE"):
                                    active_raion_alerts[fid_str] = (level, alert_type, reason, started_at)

                        # Apply fresh snapshot to all 136 Raions
                        self._apply_alerts_snapshot(active_raion_alerts, now)

                    elif resp.status in (401, 403):
                        logger.warning(f"alerts.in.ua auth error HTTP {resp.status}. Check API key in Turso database.")
                        self._apply_alerts_snapshot(active_raion_alerts, now)
                    else:
                        logger.warning(f"alerts.in.ua returned HTTP {resp.status}")
                        self._apply_alerts_snapshot(active_raion_alerts, now)

        except Exception as e:
            logger.debug(f"alerts.in.ua fetch notice: {e}")
            self._apply_alerts_snapshot(active_raion_alerts, now)

    def _apply_alerts_snapshot(self, active_raion_alerts: Dict[str, Tuple[str, str, str, Optional[float]]], now: float):
        """Applies exact alert states to all 136 Raions, reverting inactive ones to transparent NONE."""
        for fid_str, status in self.raion_alerts_state.items():
            if fid_str in active_raion_alerts:
                new_level, new_type, new_reason, new_started = active_raion_alerts[fid_str]
                status.level = new_level
                status.threat_type = new_type
                status.reason = new_reason
                if not status.started_at or status.level != new_level:
                    status.started_at = new_started or now
                status.updated_at = now
                status.duration_minutes = max(1, int((now - (status.started_at or now)) / 60.0))
            else:
                if status.level != "NONE":
                    status.level = "NONE"
                    status.threat_type = None
                    status.reason = "Спокійно"
                    status.started_at = None
                    status.duration_minutes = 0
                status.updated_at = now

    def get_summary(self) -> Dict[str, Any]:
        """Returns structured JSON summary of official 136 Raions alerts."""
        raions_list = [s.model_dump() for s in self.raion_alerts_state.values()]
        active_raions = [s for s in self.raion_alerts_state.values() if s.level != "NONE"]
        red_count = len([s for s in active_raions if s.level == "MISSILE"])
        yellow_count = len([s for s in active_raions if s.level == "DRONE"])
        
        return {
            "total_alerts": len(active_raions),
            "red_alerts": red_count,
            "yellow_alerts": yellow_count,
            "raions": raions_list,
            "alerts": raions_list,
            "updated_at": time.time()
        }

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Official alerts.in.ua live air defense monitoring service started.")

    async def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Alerts service stopped.")

    async def _run_loop(self):
        while self.is_running:
            try:
                await self.fetch_external_alerts()
            except Exception as e:
                logger.debug(f"Alerts loop notice: {e}")
            await asyncio.sleep(10.0)

# Global Alerts Service Singleton
alerts_service = AirAlertsService()
