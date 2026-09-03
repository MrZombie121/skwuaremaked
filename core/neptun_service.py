"""
NEPTUN API Integration Service for SkyWatch (Live Air Threats & Navigation Engine)
Connects to https://neptun.in.ua WebSocket stream and REST API,
precisely places targets at their current GPS coordinates, and navigates them
towards the destination city specified in Neptun.
"""
import asyncio
import logging
import time
import aiohttp
from typing import Callable, Optional, Dict, Any, List
from core.models import ParsedThreatEvent, TargetType, HeadingDirection
from core.geo_engine import (
    extract_current_and_destination,
    find_all_locations_in_text,
    compute_spherical_bearing,
    calculate_projected_point
)

logger = logging.getLogger("SkyWatch.NeptunService")

class NeptunApiService:
    def __init__(self, event_callback: Callable[[ParsedThreatEvent], Any]):
        self.event_callback = event_callback
        self.base_url = "https://neptun.in.ua"
        self.ws_url = "wss://neptun.in.ua/api/v1/stream"
        self.is_enabled = False
        self.is_connected = False
        self._running_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._seen_threat_ids: Dict[str, float] = {}

    def _map_neptun_type(self, raw_type: str) -> TargetType:
        t = (raw_type or "").lower()
        if t in ("uav", "shahed", "drone"):
            return TargetType.SHAHED
        elif t in ("jet_uav", "mig31k", "rs"):
            return TargetType.JET_UAV
        elif t in ("missile", "cruise"):
            return TargetType.MISSILE
        elif t in ("ballistic", "iskander", "kinzhal"):
            return TargetType.BALLISTIC
        elif t in ("kab", "fab", "umpk"):
            return TargetType.KAB
        elif t in ("recon", "orlan", "supercam", "zala"):
            return TargetType.RECON
        elif t in ("fpv",):
            return TargetType.FPV
        elif t in ("decoy", "gerbera"):
            return TargetType.DECOY
        return TargetType.SHAHED

    def _bearing_to_heading(self, angle_deg: Optional[float]) -> HeadingDirection:
        if angle_deg is None:
            return HeadingDirection.UNKNOWN
        dirs = [
            (HeadingDirection.N, 337.5, 22.5),
            (HeadingDirection.NE, 22.5, 67.5),
            (HeadingDirection.E, 67.5, 112.5),
            (HeadingDirection.SE, 112.5, 157.5),
            (HeadingDirection.S, 157.5, 202.5),
            (HeadingDirection.SW, 202.5, 247.5),
            (HeadingDirection.W, 247.5, 292.5),
            (HeadingDirection.NW, 292.5, 337.5)
        ]
        for heading, min_a, max_a in dirs:
            if min_a > max_a:
                if angle_deg >= min_a or angle_deg < max_a:
                    return heading
            elif min_a <= angle_deg < max_a:
                return heading
        return HeadingDirection.UNKNOWN

    def _convert_neptun_threat(self, threat: Dict[str, Any]) -> Optional[ParsedThreatEvent]:
        try:
            tid = str(threat.get("id") or "")
            if not tid:
                return None

            # Current exact position from Neptun
            lat = float(threat.get("lat") or 0.0)
            lon = float(threat.get("lon") or 0.0)
            if lat == 0.0 or lon == 0.0:
                return None

            target_type = self._map_neptun_type(threat.get("type", "uav"))
            loc_name = threat.get("locality") or threat.get("district") or threat.get("region") or "Україна"
            
            # Heading from Neptun
            heading_deg = float(threat.get("heading") or (threat.get("velocity", {}) or {}).get("bearingDeg") or 0.0)
            
            qty = max(1, int(threat.get("count") or 1))
            status = threat.get("status", "active")
            is_clear = (status in ("resolved", "stale"))

            raw_text = threat.get("explanationShort") or f"{threat.get('title', 'Ціль')} в районі {loc_name}"

            # 1. Parse Destination City from explanationShort / locality
            dest_name = None
            dest_lat = None
            dest_lon = None

            curr_geo, dest_geo = extract_current_and_destination(raw_text)
            if dest_geo and (dest_geo[1] != lat or dest_geo[2] != lon):
                dest_name = dest_geo[0]
                dest_lat = dest_geo[1]
                dest_lon = dest_geo[2]
                heading_deg = compute_spherical_bearing(lat, lon, dest_lat, dest_lon)
            elif curr_geo and curr_geo[0].lower() != loc_name.lower():
                dest_name = curr_geo[0]
                dest_lat = curr_geo[1]
                dest_lon = curr_geo[2]
                heading_deg = compute_spherical_bearing(lat, lon, dest_lat, dest_lon)
            
            # If no explicit city in text, project forward destination based on bearing
            if not dest_lat and heading_deg != 0.0:
                p_lat, p_lon = calculate_projected_point(lat, lon, heading_deg, distance_km=70.0)
                dest_lat = round(p_lat, 4)
                dest_lon = round(p_lon, 4)
                dest_name = f"Курс {round(heading_deg)}°"

            heading = self._bearing_to_heading(heading_deg)

            return ParsedThreatEvent(
                event_id=f"nep_{tid}",
                source_channel="Додаткове джерело",
                raw_text=raw_text,
                target_type=target_type,
                target_count=qty,
                location_name=loc_name,
                region_name=threat.get("region"),
                lat=lat,
                lon=lon,
                heading=heading,
                heading_deg=heading_deg,
                destination=dest_name,
                dest_lat=dest_lat,
                dest_lon=dest_lon,
                is_clear_signal=is_clear,
                timestamp=time.time(),
                confidence=0.98
            )
        except Exception as e:
            logger.error(f"Error converting Neptun threat item: {e}")
            return None

    async def start(self):
        """Enables and starts live connection to Neptun."""
        if self.is_enabled:
            return
        self.is_enabled = True
        self._running_task = asyncio.create_task(self._connection_loop())
        self._poll_task = asyncio.create_task(self._poll_rest_loop())
        logger.info("Neptun API live service started (WebSocket + REST poller).")

    async def stop(self):
        """Disables and disconnects Neptun stream."""
        self.is_enabled = False
        self.is_connected = False
        if self._running_task:
            self._running_task.cancel()
            self._running_task = None
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        logger.info("Neptun API service stopped.")

    async def _poll_rest_loop(self):
        """Polls Neptun REST snapshot periodically every 6 seconds as recommended by docs."""
        while self.is_enabled:
            try:
                await self._fetch_rest_snapshot()
            except Exception as e:
                logger.debug(f"Neptun REST polling notice: {e}")
            await asyncio.sleep(6.0)

    async def _connection_loop(self):
        """Main WebSocket connection loop with auto-reconnect."""
        while self.is_enabled:
            try:
                if not self._session or self._session.closed:
                    self._session = aiohttp.ClientSession()

                # Fetch initial REST snapshot
                await self._fetch_rest_snapshot()

                # Open live WebSocket stream
                logger.info(f"Connecting to Neptun WebSocket: {self.ws_url}...")
                async with self._session.ws_connect(self.ws_url, heartbeat=25.0) as ws:
                    self.is_connected = True
                    logger.info("Connected to live Neptun WebSocket stream.")

                    async for msg in ws:
                        if not self.is_enabled:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_ws_message(msg.json())
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break

                    self.is_connected = False

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.is_connected = False
                logger.warning(f"Neptun API connection issue ({e}). Reconnecting in 5s...")
                await asyncio.sleep(5.0)

    async def _fetch_rest_snapshot(self):
        """Fetches threats snapshot via REST."""
        try:
            if not self._session or self._session.closed:
                self._session = aiohttp.ClientSession()
            async with self._session.get(f"{self.base_url}/api/v1/threats", timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    threats = data.get("threats", [])
                    for t in threats:
                        ev = self._convert_neptun_threat(t)
                        if ev:
                            await self.event_callback(ev)
        except Exception as e:
            logger.debug(f"Neptun snapshot fetch notice: {e}")

    async def _handle_ws_message(self, env: Dict[str, Any]):
        """Handles live WebSocket frame from Neptun."""
        try:
            msg_type = env.get("type")
            data = env.get("data")
            if not data:
                return

            if msg_type == "snapshot":
                threats = data.get("threats", [])
                for t in threats:
                    ev = self._convert_neptun_threat(t)
                    if ev:
                        await self.event_callback(ev)

            elif msg_type == "upsert":
                ev = self._convert_neptun_threat(data)
                if ev:
                    await self.event_callback(ev)

        except Exception as e:
            logger.error(f"Error handling Neptun WS frame: {e}")

    def get_status(self) -> Dict[str, Any]:
        return {
            "enabled": self.is_enabled,
            "connected": self.is_connected,
            "source": "https://neptun.in.ua"
        }
