"""
Live Air Raid Alerts Service for SkyWatch Tactical Radar
Provides Raion-Level Granular Alerts (138 Raions of Ukraine) & Oblast-level aggregation:
- NONE (Clear / Спокійно)
- DRONE (🟡 Yellow Alert — Дронова небезпека / Шахеди)
- MISSILE (🔴 Red Alert — Ракетна небезпека / Балістика / Загальна тривога)
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

logger = logging.getLogger("SkyWatch.AlertsService")

# Import 138 Ukrainian Raions database
from generate_raions_geojson import UKRAINE_RAIONS_DATA

class RaionAlertStatus(BaseModel):
    id: str
    name: str
    oblast: str
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
        for rid, rdata in UKRAINE_RAIONS_DATA.items():
            self.raion_alerts_state[rid] = RaionAlertStatus(
                id=rid,
                name=rdata["name"],
                oblast=rdata["oblast"],
                level="NONE",
                threat_type=None,
                reason="Спокійно",
                started_at=None,
                duration_minutes=0,
                updated_at=now
            )

    def find_raions_by_location_or_coords(self, lat: float, lon: float, loc_text: str = "") -> List[str]:
        """Finds closest Raion IDs matching exact coordinates and text keywords."""
        matched_rids = []
        clean_text = loc_text.lower().strip()

        # 1. Keyword matching in raion name/oblast
        if clean_text:
            for rid, rdata in UKRAINE_RAIONS_DATA.items():
                r_name_clean = rdata["name"].lower()
                if any(k in clean_text for k in r_name_clean.replace("район", "").replace("область", "").split()):
                    matched_rids.append(rid)

        # 2. Spatial distance matching (if coordinates are valid)
        if (not matched_rids or len(matched_rids) > 2) and 43.0 <= lat <= 54.0 and 20.0 <= lon <= 42.0:
            closest_rid = None
            min_dist = float("inf")
            for rid, rdata in UKRAINE_RAIONS_DATA.items():
                c_lat, c_lon = rdata["center"]
                dist = haversine_distance_km(lat, lon, c_lat, c_lon)
                if dist < min_dist:
                    min_dist = dist
                    closest_rid = rid
            if closest_rid and min_dist <= 75.0:
                if closest_rid not in matched_rids:
                    matched_rids.append(closest_rid)

        return matched_rids

    def update_from_active_threats(self, active_targets: List[Any]):
        """
        Calculates granular Raion-level alerts directly from active radar targets:
        - Shahed / Drone / FPV -> Level DRONE (🟡 Yellow Alert) for target's current & destination raions
        - Missile / Ballistic / KAB / Aircraft -> Level MISSILE (🔴 Red Alert)
        """
        now = time.time()
        threatened_raions: Dict[str, Tuple[str, str, str]] = {} # rid -> (level, type, reason)

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

            # Find raions for current position
            curr_rids = self.find_raions_by_location_or_coords(c_lat, c_lon, loc_name)
            for rid in curr_rids:
                if rid not in threatened_raions or (threatened_raions[rid][0] == "DRONE" and level == "MISSILE"):
                    threatened_raions[rid] = (level, target_type_str, reason_text)

            # Find raions for destination position if provided
            d_lat = getattr(tgt, 'dest_lat', None)
            d_lon = getattr(tgt, 'dest_lon', None)
            if d_lat and d_lon:
                dest_rids = self.find_raions_by_location_or_coords(d_lat, d_lon, dest_name)
                for rid in dest_rids:
                    if rid not in threatened_raions or (threatened_raions[rid][0] == "DRONE" and level == "MISSILE"):
                        threatened_raions[rid] = (level, target_type_str, reason_text)

        # Update Raion states
        for rid, status in self.raion_alerts_state.items():
            if rid in threatened_raions:
                new_level, new_type, new_reason = threatened_raions[rid]
                if status.level != new_level:
                    status.level = new_level
                    status.threat_type = new_type
                    status.reason = new_reason
                    status.started_at = now
                status.updated_at = now
                status.duration_minutes = max(1, int((now - (status.started_at or now)) / 60.0))
            else:
                if status.level != "NONE" and (now - (status.started_at or now)) > 180:
                    status.level = "NONE"
                    status.threat_type = None
                    status.reason = "Відбій загрози"
                    status.started_at = None
                    status.duration_minutes = 0
                status.updated_at = now

    async def fetch_external_alerts(self):
        """Fetches live Ukrainian state alerts from public mirror endpoints."""
        url = "https://ubilling.net.ua/aerialalerts/"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        states = data.get("states", {})
                        now = time.time()

                        for region_name, is_alert in states.items():
                            clean_rname = region_name.lower().replace("область", "").replace("ська", "").strip()
                            if is_alert:
                                for rid, rdata in UKRAINE_RAIONS_DATA.items():
                                    if clean_rname in rdata["oblast"].lower():
                                        obj = self.raion_alerts_state[rid]
                                        if obj.level == "NONE":
                                            obj.level = "MISSILE"
                                            obj.reason = "Повітряна тривога"
                                            obj.started_at = now
                                        obj.duration_minutes = max(1, int((now - (obj.started_at or now)) / 60.0))
                                        obj.updated_at = now
                            else:
                                for rid, rdata in UKRAINE_RAIONS_DATA.items():
                                    if clean_rname in rdata["oblast"].lower():
                                        obj = self.raion_alerts_state[rid]
                                        if obj.level != "NONE" and not obj.threat_type:
                                            obj.level = "NONE"
                                            obj.reason = "Відбій тривоги"
                                            obj.started_at = None
                                            obj.duration_minutes = 0
                                            obj.updated_at = now
        except Exception as e:
            logger.debug(f"External alerts check note: {e}")

    def get_summary(self) -> Dict[str, Any]:
        """Returns structured JSON summary of granular Raion alerts for UI & WebSocket."""
        raions_list = [s.model_dump() for s in self.raion_alerts_state.values()]
        red_count = len([s for s in self.raion_alerts_state.values() if s.level == "MISSILE"])
        yellow_count = len([s for s in self.raion_alerts_state.values() if s.level == "DRONE"])
        
        return {
            "total_alerts": red_count + yellow_count,
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
        logger.info("Raion-level Air Alerts Monitoring Service started.")

    async def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Raion-level Air Alerts Monitoring Service stopped.")

    async def _run_loop(self):
        while self.is_running:
            try:
                await self.fetch_external_alerts()
            except Exception as e:
                logger.debug(f"Alerts loop error: {e}")
            await asyncio.sleep(15.0)

# Global Alerts Service Singleton
alerts_service = AirAlertsService()
