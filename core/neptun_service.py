"""
NEPTUN API Integration Service for SkyWatch (High-Precision Air Threats & Navigation Engine)
Connects to https://neptun.in.ua WebSocket stream and REST API,
precisely places targets at their current GPS coordinates, extracts heading/bearing,
detects destination settlements, and synchronizes active threats without phantom targets.
"""
import asyncio
import logging
import time
import aiohttp
from typing import Callable, Optional, Dict, Any, List, Set
import config
from core.models import ParsedThreatEvent, TargetType, HeadingDirection
from core.geo_engine import (
    extract_current_and_destination,
    find_all_locations_in_text,
    compute_spherical_bearing,
    calculate_projected_point,
    lookup_location
)

logger = logging.getLogger("SkyWatch.NeptunService")

class NeptunApiService:
    def __init__(self, event_callback: Callable[[ParsedThreatEvent], Any], snapshot_callback: Optional[Callable[[List[ParsedThreatEvent], Set[str]], Any]] = None):
        self.event_callback = event_callback
        self.snapshot_callback = snapshot_callback
        self.base_url = "https://neptun.in.ua"
        self.ws_url = "wss://neptun.in.ua/api/v1/stream"
        self.is_enabled = False
        self.is_connected = False
        self._running_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None

    def _map_neptun_type(self, raw_type: str, title_text: str = "") -> TargetType:
        combined = f"{raw_type or ''} {title_text or ''}".lower()
        if any(k in combined for k in ("ballistic", "iskander", "kinzhal", "іскандер", "кинджал", "циркон", "с-300", "с-400")):
            return TargetType.BALLISTIC
        elif any(k in combined for k in ("kab", "fab", "umpk", "каб", "фаб", "авіабомб")):
            return TargetType.KAB
        elif any(k in combined for k in ("jet_uav", "mig31k", "rs", "реактив")):
            return TargetType.JET_UAV
        elif any(k in combined for k in ("missile", "cruise", "ракета", "калібр", "х-101", "х-59")):
            return TargetType.MISSILE
        elif any(k in combined for k in ("recon", "orlan", "supercam", "zala", "мерлін", "розвідник")):
            return TargetType.RECON
        elif any(k in combined for k in ("fpv", "фпв")):
            return TargetType.FPV
        elif any(k in combined for k in ("decoy", "gerbera", "пародія", "фальш", "приманка")):
            return TargetType.DECOY
        elif any(k in combined for k in ("uav", "shahed", "drone", "шахед", "геран", "мопед", "бпла")):
            return TargetType.SHAHED
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

    def _extract_coordinates(self, threat: Dict[str, Any]) -> Optional[tuple[float, float]]:
        # 1. Direct fields
        for lat_k in ("lat", "latitude", "y"):
            for lon_k in ("lon", "lng", "longitude", "x"):
                if lat_k in threat and lon_k in threat:
                    try:
                        lat_v = float(threat[lat_k])
                        lon_v = float(threat[lon_k])
                        if 43.0 <= lat_v <= 54.0 and 20.0 <= lon_v <= 42.0:
                            return lat_v, lon_v
                    except (ValueError, TypeError):
                        pass

        # 2. Nested location / coordinates object or array
        loc_obj = threat.get("location") or threat.get("coordinates") or threat.get("point") or threat.get("position")
        if isinstance(loc_obj, (list, tuple)) and len(loc_obj) >= 2:
            try:
                lat_v, lon_v = float(loc_obj[0]), float(loc_obj[1])
                if 20.0 <= lat_v <= 42.0 and 43.0 <= lon_v <= 54.0:
                    lat_v, lon_v = lon_v, lat_v
                if 43.0 <= lat_v <= 54.0 and 20.0 <= lon_v <= 42.0:
                    return lat_v, lon_v
            except (ValueError, TypeError):
                pass
        elif isinstance(loc_obj, dict):
            try:
                lat_v = float(loc_obj.get("lat") or loc_obj.get("latitude") or 0.0)
                lon_v = float(loc_obj.get("lon") or loc_obj.get("lng") or loc_obj.get("longitude") or 0.0)
                if 43.0 <= lat_v <= 54.0 and 20.0 <= lon_v <= 42.0:
                    return lat_v, lon_v
            except (ValueError, TypeError):
                pass

        return None

    def _convert_neptun_threat(self, threat: Dict[str, Any]) -> Optional[ParsedThreatEvent]:
        try:
            # Skip general area alerts without specific coordinates to avoid phantom targets
            if threat.get("areaOnly") is True:
                return None

            status = str(threat.get("status") or threat.get("state") or "active").lower()
            if status not in ("active", "flying", "in_flight", "tracked"):
                return None

            coords = self._extract_coordinates(threat)
            if not coords:
                return None
            lat, lon = coords

            tid = str(threat.get("id") or threat.get("uid") or threat.get("uuid") or f"{lat:.4f}_{lon:.4f}")

            title = str(threat.get("title") or "")
            raw_type = str(threat.get("type") or threat.get("threatType") or threat.get("category") or "uav")
            target_type = self._map_neptun_type(raw_type, title)

            # Extract specific locality (take settlement before comma, e.g. "Миколаїв" from "Миколаїв, Миколаївська область")
            raw_locality = threat.get("locality")
            raw_region = threat.get("region")
            
            if raw_locality and isinstance(raw_locality, str) and raw_locality.strip():
                loc_name = raw_locality.split(",")[0].strip()
            elif threat.get("district"):
                loc_name = str(threat["district"]).split(",")[0].strip()
            elif raw_region:
                loc_name = str(raw_region).strip()
            else:
                loc_name = "Україна"
            
            # Ground truth Heading from Neptun
            heading_deg = 0.0
            for h_k in ("heading", "bearing", "bearingDeg", "directionDeg", "azimuth"):
                if h_k in threat and threat[h_k] is not None:
                    try:
                        heading_deg = float(threat[h_k])
                        break
                    except (ValueError, TypeError):
                        pass
            if heading_deg == 0.0:
                vel = threat.get("velocity") or threat.get("speed") or {}
                if isinstance(vel, dict):
                    try:
                        heading_deg = float(vel.get("bearingDeg") or vel.get("bearing") or vel.get("heading") or 0.0)
                    except (ValueError, TypeError):
                        pass

            # Ground truth Speed from Neptun
            speed_kmh = config.THREAT_SPEED_PROFILES.get(target_type.value, 185.0)
            vel = threat.get("velocity") or {}
            if isinstance(vel, dict) and vel.get("speedKmh"):
                try:
                    speed_kmh = float(vel["speedKmh"])
                except (ValueError, TypeError):
                    pass
            elif threat.get("speed"):
                try:
                    speed_kmh = float(threat["speed"])
                except (ValueError, TypeError):
                    pass

            qty = 1
            for q_k in ("count", "quantity", "qty", "amount"):
                if q_k in threat and threat[q_k] is not None:
                    try:
                        qty = max(1, int(threat[q_k]))
                        break
                    except (ValueError, TypeError):
                        pass

            raw_text = str(threat.get("explanationShort") or threat.get("description") or f"{title or 'Повітряна ціль'} в районі {loc_name}").strip()

            # Destination Extraction:
            # 1. Look for explicit target city in explanation (e.g. "курсом на Вознесенськ", "вектор на Умань")
            dest_name = None
            dest_lat = None
            dest_lon = None

            direct_dest = threat.get("destination") or threat.get("dest") or threat.get("targetLocality") or threat.get("directionLocality")
            if direct_dest and isinstance(direct_dest, str) and not direct_dest.lower().endswith(("область", "області", "щина")):
                dest_geo = lookup_location(direct_dest)
                if dest_geo:
                    dest_name = dest_geo[0]
                    dest_lat = dest_geo[1]
                    dest_lon = dest_geo[2]

            if not dest_lat:
                curr_geo, parsed_dest = extract_current_and_destination(raw_text)
                if parsed_dest and not parsed_dest[0].lower().endswith(("область", "області", "щина", "район")):
                    if abs(parsed_dest[1] - lat) > 0.02 or abs(parsed_dest[2] - lon) > 0.02:
                        dest_name = parsed_dest[0]
                        dest_lat = parsed_dest[1]
                        dest_lon = parsed_dest[2]

            # 2. If no explicit destination town, project forward along Neptun's exact heading
            if not dest_lat:
                if heading_deg != 0.0:
                    p_lat, p_lon = calculate_projected_point(lat, lon, heading_deg, distance_km=80.0)
                    dest_lat = round(p_lat, 4)
                    dest_lon = round(p_lon, 4)
                    dest_name = f"Курс {round(heading_deg)}° ({self._bearing_to_heading(heading_deg).value})"
                else:
                    if lat < 47.0:
                        default_h = 315.0
                    elif lon > 35.5:
                        default_h = 270.0
                    elif lat > 50.5:
                        default_h = 225.0
                    else:
                        default_h = 280.0
                    heading_deg = default_h
                    p_lat, p_lon = calculate_projected_point(lat, lon, default_h, distance_km=80.0)
                    dest_lat = round(p_lat, 4)
                    dest_lon = round(p_lon, 4)
                    dest_name = f"Курс {round(default_h)}° ({self._bearing_to_heading(default_h).value})"

            heading = self._bearing_to_heading(heading_deg)

            return ParsedThreatEvent(
                event_id=f"nep_{tid}",
                source_channel="Додаткове джерело",
                raw_text=raw_text,
                target_type=target_type,
                target_count=qty,
                location_name=loc_name,
                region_name=raw_region,
                lat=lat,
                lon=lon,
                heading=heading,
                heading_deg=heading_deg,
                speed_kmh=speed_kmh,
                destination=dest_name,
                dest_lat=dest_lat,
                dest_lon=dest_lon,
                is_clear_signal=False,
                timestamp=time.time(),
                confidence=0.98
            )
        except Exception as e:
            logger.error(f"Error converting Neptun threat item: {e}")
            return None

    async def _handle_threats_list(self, threats: List[Dict[str, Any]]):
        active_events = []
        active_ids = set()

        for t in threats:
            ev = self._convert_neptun_threat(t)
            if ev:
                active_events.append(ev)
                active_ids.add(ev.event_id)

        if self.snapshot_callback:
            await self.snapshot_callback(active_events, active_ids)
        else:
            for ev in active_events:
                await self.event_callback(ev)

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
        """Polls Neptun REST snapshot periodically every 5 seconds for 100% reliability."""
        while self.is_enabled:
            try:
                await self._fetch_rest_snapshot()
            except Exception as e:
                logger.debug(f"Neptun REST polling notice: {e}")
            await asyncio.sleep(5.0)

    async def _connection_loop(self):
        """Main WebSocket connection loop with auto-reconnect."""
        while self.is_enabled:
            try:
                if not self._session or self._session.closed:
                    self._session = aiohttp.ClientSession()

                # Fetch initial REST snapshot
                await self._fetch_rest_snapshot()

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
                    await self._handle_threats_list(threats)
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
                await self._handle_threats_list(threats)

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
