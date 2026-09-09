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

from core.models import TargetType
from core.geo_engine import haversine_distance_km
from core.raions_data import UKRAINE_RAIONS_DATA
from core.turso_db import turso_db
from core.db import db

logger = logging.getLogger("SkyWatch.AlertsService")

class RaionAlertStatus(BaseModel):
    fid: int
    id: str
    name: str
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
            self.raion_alerts_state[fid_str] = RaionAlertStatus(
                fid=int(rdata.get("fid", fid_str)),
                id=fid_str,
                name=rdata["name"],
                level="NONE",
                threat_type=None,
                reason="Спокійно",
                started_at=None,
                duration_minutes=0,
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

    def find_raion_by_coords_and_name(self, lat: float, lon: float, loc_text: str = "") -> Optional[str]:
        """Finds the single closest/matching official Raion for exact GPS coordinates and text."""
        clean_text = loc_text.lower().strip()
        
        # 1. Direct name matching if raion keyword in text
        if clean_text:
            for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
                r_stem = rdata["name"].lower().replace("район", "").strip()
                if len(r_stem) >= 4 and r_stem in clean_text:
                    return fid_str

        # 2. Exact spatial distance matching to Raion centroid
        if 43.0 <= lat <= 54.0 and 20.0 <= lon <= 42.0:
            closest_fid = None
            min_dist = float("inf")

            for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
                c_lat, c_lon = rdata["center"]
                dist = haversine_distance_km(lat, lon, c_lat, c_lon)
                if dist < min_dist:
                    min_dist = dist
                    closest_fid = fid_str

            if closest_fid and min_dist <= 55.0:
                return closest_fid

        return None

    def update_from_active_threats(self, active_targets: List[Any]):
        """
        Correlates live radar targets with official Raions:
        - Shahed / Drone -> Level DRONE (🟡 Yellow Alert) for target's current & destination raions
        - Missile / Ballistic / KAB / Aircraft -> Level MISSILE (🔴 Red Alert)
        """
        now = time.time()
        threatened_raions: Dict[str, Tuple[str, str, str]] = {} # fid_str -> (level, type, reason)

        for tgt in active_targets:
            target_type = getattr(tgt, 'target_type', None)
            target_type_str = target_type.value if hasattr(target_type, 'value') else str(target_type or "SHAHED")
            
            c_lat = getattr(tgt, 'current_lat', 0.0)
            c_lon = getattr(tgt, 'current_lon', 0.0)
            loc_name = getattr(tgt, 'current_location_name', '') or ''
            dest_name = getattr(tgt, 'destination_name', '') or ''
            
            is_missile_tier = target_type_str in ("MISSILE", "BALLISTIC", "KAB", "AIRCRAFT")
            level = "MISSILE" if is_missile_tier else "DRONE"
            reason_text = "Ракетна небезпека / Балістика" if is_missile_tier else f"Загроза ударних БпЛА ({getattr(tgt, 'target_subtype', 'Shahed')})"

            # Match raion at current target position
            curr_fid = self.find_raion_by_coords_and_name(c_lat, c_lon, loc_name)
            if curr_fid:
                if curr_fid not in threatened_raions or (threatened_raions[curr_fid][0] == "DRONE" and level == "MISSILE"):
                    threatened_raions[curr_fid] = (level, target_type_str, reason_text)

            # Match raion at target destination
            d_lat = getattr(tgt, 'dest_lat', None)
            d_lon = getattr(tgt, 'dest_lon', None)
            if d_lat and d_lon:
                dest_fid = self.find_raion_by_coords_and_name(d_lat, d_lon, dest_name)
                if dest_fid:
                    if dest_fid not in threatened_raions or (threatened_raions[dest_fid][0] == "DRONE" and level == "MISSILE"):
                        threatened_raions[dest_fid] = (level, target_type_str, reason_text)

        # Apply radar threat states without overriding official missile alerts
        for fid_str, status in self.raion_alerts_state.items():
            if fid_str in threatened_raions:
                new_level, new_type, new_reason = threatened_raions[fid_str]
                if status.level != "MISSILE" or new_level == "MISSILE":
                    status.level = new_level
                    status.threat_type = new_type
                    status.reason = new_reason
                    if not status.started_at:
                        status.started_at = now
                status.updated_at = now
                status.duration_minutes = max(1, int((now - (status.started_at or now)) / 60.0))

    async def fetch_external_alerts(self):
        """Fetches live Ukrainian state alerts from alerts.in.ua using Turso key."""
        api_key = await self._get_active_api_key()
        if not api_key:
            return

        url = "https://api.alerts.in.ua/v1/alerts/active.json"
        now = time.time()
        
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
                        
                        active_raion_alerts: Dict[str, Tuple[str, str, str, Optional[float]]] = {} # fid -> (level, type, reason, started_at)

                        for a in alerts:
                            alert_type = str(a.get("alert_type") or "air_raid").lower()
                            loc_type = str(a.get("location_type") or "").lower()
                            loc_title = str(a.get("location_title") or "").strip()
                            loc_raion = str(a.get("location_raion") or "").strip()
                            loc_oblast = str(a.get("location_oblast") or "").strip()
                            notes = str(a.get("notes") or "").lower()

                            # Determine Alert Level (Yellow vs Red)
                            is_drone = alert_type == "drone" or "шахед" in notes or "бпла" in notes or "дрон" in notes
                            level = "DRONE" if is_drone else "MISSILE"
                            
                            reason = "Загроза ударних БпЛА" if is_drone else (
                                "Артилерійський обстріл" if alert_type == "artillery_shelling" else "Повітряна тривога"
                            )

                            # Parse start timestamp if available
                            started_at = now
                            started_str = a.get("started_at")
                            if started_str:
                                try:
                                    # Convert ISO timestamp
                                    started_at = time.mktime(time.strptime(started_str[:19], "%Y-%m-%dT%H:%M:%S"))
                                except Exception:
                                    started_at = now

                            # Find matching Raions
                            matched_fids = set()

                            # Case 1: Specific Raion alert
                            if loc_type == "raion" or loc_raion:
                                search_name = (loc_raion or loc_title).lower().replace("район", "").strip()
                                for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
                                    r_clean = rdata["name"].lower().replace("район", "").strip()
                                    if len(search_name) >= 4 and (search_name in r_clean or r_clean in search_name):
                                        matched_fids.add(fid_str)

                            # Case 2: Hromada or City alert
                            elif loc_type in ("hromada", "city"):
                                search_name = (loc_raion or loc_title).lower().replace("район", "").replace("громада", "").replace("м.", "").strip()
                                for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
                                    r_clean = rdata["name"].lower().replace("район", "").strip()
                                    if len(search_name) >= 4 and (search_name in r_clean or r_clean in search_name):
                                        matched_fids.add(fid_str)

                            # Case 3: Full Oblast alert
                            elif loc_type == "oblast":
                                clean_obl = (loc_oblast or loc_title).lower().replace("область", "").replace("ська", "").replace("зька", "").replace("цька", "").strip()
                                for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
                                    if clean_obl in rdata["name"].lower() or clean_obl in str(rdata.get("oblast", "")).lower():
                                        matched_fids.add(fid_str)

                            for fid_str in matched_fids:
                                if fid_str not in active_raion_alerts or (active_raion_alerts[fid_str][0] == "DRONE" and level == "MISSILE"):
                                    active_raion_alerts[fid_str] = (level, alert_type, reason, started_at)

                        # Update all 136 Raion states
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
                                # Not in active alerts -> return to transparent NONE
                                if status.level != "NONE":
                                    status.level = "NONE"
                                    status.threat_type = None
                                    status.reason = "Спокійно"
                                    status.started_at = None
                                    status.duration_minutes = 0
                                status.updated_at = now

                    elif resp.status == 401 or resp.status == 403:
                        logger.warning(f"alerts.in.ua auth error HTTP {resp.status}. Check API key in Turso database.")
                    else:
                        logger.warning(f"alerts.in.ua returned HTTP {resp.status}")

        except Exception as e:
            logger.debug(f"alerts.in.ua fetch notice: {e}")

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
