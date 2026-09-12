"""
Live Air Raid Alerts Service for SkyWatch Tactical Radar
Integrates with UkraineAlarms API (api.ukrainealarm.com) via key dynamically retrieved from Turso Cloud DB, settings, or env.
Periodically queries api.ukrainealarm.com every 30 seconds.
Classifies all 136 Official Ukrainian Raions (ADM2) into:
- NONE: 100% Fully Transparent (No color, no borders, 0 opacity)
- DRONE (🟡 Yellow Alert): Drone / Shahed Danger directly from UkraineAlarms (Yellow alert level)
- MISSILE (🔴 Red Alert): Missile / Ballistic / Air Raid directly from UkraineAlarms (Red alert level)

Alert statuses are strictly applied at Raion level (not whole Oblasts unless specified).
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Set
import aiohttp
from pydantic import BaseModel, Field

from core.raions_data import UKRAINE_RAIONS_DATA
from core.turso_db import turso_db
from core.db import db

logger = logging.getLogger("SkyWatch.AlertsService")

DEFAULT_UKRAINE_ALARMS_KEY = "6859f947:06fa78350dc6fd2caed8a39e31c281ae"
PERMANENT_ALERT_OBLASTS = ("луганська", "автономна республіка крим", "крим")

# Renamed / decommunized raions mapping to standard 136 names
ALIASES = {
    "берестинський": "красноградський",
    "самарівський": "новомосковський",
    "шептицький": "червоноградський",
    "звягельський": "новоград волинський",
    "володимирський": "володимир волинський",
    "дністровський": "дністровський",
}

def normalize_name(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    for ch in ["’", "ʼ", "`", "ʻ", "'", '"', "«", "»"]:
        s = s.replace(ch, "")
    s = s.replace("район", "").replace("р-н", "").replace("місто", "").replace("м.", "").strip()
    s = s.replace("-", " ")
    return " ".join(s.split())

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
        self._dist_to_fid: Dict[str, str] = {}
        self._comm_to_dist: Dict[str, str] = {}
        self._state_to_fids: Dict[str, List[str]] = {}
        self._norm_name_to_fid: Dict[str, str] = {}
        self._threats_provider = None

        self._init_default_states()
        self._load_region_mappings()

    def _init_default_states(self):
        now = time.time()
        for fid_str, rdata in UKRAINE_RAIONS_DATA.items():
            oblast = rdata.get("oblast", "")
            obl_lower = oblast.lower()
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

    def _load_region_mappings(self):
        for fid, r in UKRAINE_RAIONS_DATA.items():
            norm = normalize_name(r["name"])
            self._norm_name_to_fid[norm] = fid

        # Load cached regions hierarchy
        data_paths = [
            os.path.join(os.path.dirname(__file__), "..", "data", "ua_alarm_regions.json"),
            "data/ua_alarm_regions.json",
            "hosting_dist/data/ua_alarm_regions.json"
        ]
        loaded = False
        for p in data_paths:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self._build_region_maps(data)
                    loaded = True
                    break
                except Exception as e:
                    logger.debug(f"Notice loading regions from {p}: {e}")

        # Fallback mappings for special top-level regions
        self._state_to_fids["16"] = [fid for fid, r in UKRAINE_RAIONS_DATA.items() if "луган" in r["oblast"].lower()]
        self._state_to_fids["9999"] = [fid for fid, r in UKRAINE_RAIONS_DATA.items() if "крим" in r["oblast"].lower()]
        
        kh_fid = self._norm_name_to_fid.get("харківський")
        if kh_fid:
            self.state_to_fids_set("1293", [kh_fid])
            self._comm_to_dist["1293"] = "dist_kharkiv_comm"
            self._dist_to_fid["dist_kharkiv_comm"] = kh_fid

        zp_fid = self._norm_name_to_fid.get("запорізький")
        if zp_fid:
            self.state_to_fids_set("564", [zp_fid])
            self._comm_to_dist["564"] = "dist_zp_comm"
            self._dist_to_fid["dist_zp_comm"] = zp_fid

        kyiv_fids = [fid for fid, r in UKRAINE_RAIONS_DATA.items() if "київ" in r["oblast"].lower()]
        self._state_to_fids["31"] = kyiv_fids

    def state_to_fids_set(self, key: str, fids: List[str]):
        self._state_to_fids[key] = fids

    def _build_region_maps(self, data: dict):
        for st in data.get("states", []):
            s_id = str(st.get("regionId") or "")
            state_fids = []

            for d in st.get("regionChildIds", []):
                d_id = str(d.get("regionId") or "")
                d_name = d.get("regionName") or ""
                d_norm = normalize_name(d_name)
                target_norm = ALIASES.get(d_norm, d_norm)

                fid = self._norm_name_to_fid.get(target_norm)
                if not fid:
                    for r_norm, r_fid in self._norm_name_to_fid.items():
                        if (len(target_norm) >= 4 and target_norm in r_norm) or (len(r_norm) >= 4 and r_norm in target_norm):
                            fid = r_fid
                            break

                if fid:
                    self._dist_to_fid[d_id] = fid
                    state_fids.append(fid)
                    for c in d.get("regionChildIds", []):
                        c_id = str(c.get("regionId") or "")
                        self._comm_to_dist[c_id] = d_id

            if state_fids:
                self._state_to_fids[s_id] = list(set(state_fids))

    async def _get_active_api_key(self) -> str:
        key = await turso_db.get_setting("alerts_api_key")
        if key and key.strip():
            return key.strip()
        saved = db.get_setting("alerts_api_key")
        if saved and saved.strip():
            return saved.strip()
        env_key = os.getenv("ALERTS_API_KEY", "").strip()
        if env_key:
            return env_key
        return DEFAULT_UKRAINE_ALARMS_KEY

    def set_threats_provider(self, provider):
        self._threats_provider = provider

    def update_from_active_threats(self, active_targets: List[Any]):
        pass

    async def fetch_external_alerts(self):
        """Fetches live Ukrainian state alerts from UkraineAlarms API every 30s."""
        api_key = await self._get_active_api_key()
        now = time.time()

        # Permanent alert regions (Luhansk & Crimea)
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
            self._apply_alerts_snapshot(active_raion_alerts, now)
            return

        url = "https://api.ukrainealarm.com/api/v3/alerts"
        headers = {
            "Authorization": api_key,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SkyWatch Tactical Radar/2.1"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        alerts_data = await resp.json()
                        if isinstance(alerts_data, list):
                            for item in alerts_data:
                                reg_id = str(item.get("regionId") or "")
                                reg_type = str(item.get("regionType") or "")
                                active_alerts = item.get("activeAlerts") or []
                                if not active_alerts:
                                    continue

                                has_red = False
                                has_yellow = False
                                threat_type = "air_raid"
                                reason = ""
                                started_at = None

                                for alert in active_alerts:
                                    a_type = str(alert.get("type") or "AIR").upper()
                                    levels = alert.get("activeAlertLevels") or []

                                    if a_type in ("ARTILLERY", "URBAN_FIGHTS", "CHEMICAL", "NUCLEAR"):
                                        has_red = True
                                        threat_type = a_type.lower()

                                    for lvl in levels:
                                        lvl_name = str(lvl.get("alertLevel") or "").lower()
                                        if lvl_name == "red":
                                            has_red = True
                                            if not reason and lvl.get("reason"):
                                                reason = lvl.get("reason")
                                        elif lvl_name == "yellow":
                                            has_yellow = True
                                            if not reason and lvl.get("reason"):
                                                reason = lvl.get("reason")

                                        created_str = lvl.get("createdAt")
                                        if created_str and not started_at:
                                            try:
                                                started_at = datetime.fromisoformat(created_str.replace("Z", "+00:00")).timestamp()
                                            except Exception:
                                                started_at = now

                                if has_red:
                                    level = "MISSILE"
                                    if not reason:
                                        reason = "Повітряна тривога (Червоний рівень)"
                                elif has_yellow:
                                    level = "DRONE"
                                    if not reason:
                                        reason = "Загроза ударних БпЛА (Жовтий рівень)"
                                else:
                                    level = "MISSILE"
                                    if not reason:
                                        reason = "Повітряна тривога"

                                # Target specific Raion (NOT the whole oblast)
                                target_fids: Set[str] = set()
                                if reg_type == "District":
                                    fid = self._dist_to_fid.get(reg_id)
                                    if fid:
                                        target_fids.add(fid)
                                elif reg_type == "Community":
                                    parent_dist = self._comm_to_dist.get(reg_id)
                                    if parent_dist:
                                        fid = self._dist_to_fid.get(parent_dist)
                                        if fid:
                                            target_fids.add(fid)
                                elif reg_type == "State":
                                    fids = self._state_to_fids.get(reg_id)
                                    if fids:
                                        target_fids.update(fids)

                                for fid in target_fids:
                                    # MISSILE tier has higher severity than DRONE tier
                                    if fid not in active_raion_alerts or (active_raion_alerts[fid][0] == "DRONE" and level == "MISSILE"):
                                        active_raion_alerts[fid] = (level, threat_type, reason, started_at or now)

                        self._apply_alerts_snapshot(active_raion_alerts, now)

                    elif resp.status in (401, 403):
                        logger.warning(f"UkraineAlarms auth error HTTP {resp.status}. Check API key.")
                        self._apply_alerts_snapshot(active_raion_alerts, now)
                    else:
                        logger.warning(f"UkraineAlarms returned HTTP {resp.status}")
                        self._apply_alerts_snapshot(active_raion_alerts, now)

        except Exception as e:
            logger.debug(f"UkraineAlarms fetch notice: {e}")
            self._apply_alerts_snapshot(active_raion_alerts, now)

    def _apply_alerts_snapshot(self, active_raion_alerts: Dict[str, Tuple[str, str, str, Optional[float]]], now: float):
        """Applies exact alert states to all 136 Raions, reverting inactive ones (отбой) to transparent NONE."""
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
        logger.info("Official UkraineAlarms live air defense monitoring service started (30s polling cycle).")

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
            await asyncio.sleep(30.0)

# Global Alerts Service Singleton
alerts_service = AirAlertsService()
