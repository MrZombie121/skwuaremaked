"""
Spatio-Temporal Cluster Deduplication & Kinematic Flight Trajectory Engine
Tracks multi-source aerial threats across Ukraine as individual separated units (no stacking),
calculates hazard danger cones, routes smoothly towards destination settlements,
orbits over targets upon arrival, and manages target lifecycle.
"""
import time
import math
from typing import Dict, List, Tuple, Optional
from core.models import ParsedThreatEvent, ActiveTarget, TargetType, ThreatStatus, HeadingDirection
from core.geo_engine import (
    haversine_distance_km,
    compute_spherical_bearing,
    generate_hazard_cone_polygon,
    calculate_projected_point
)
import config

def calculate_formation_offset(lat: float, lon: float, bearing_deg: float, index: int, total_count: int, spacing_km: float = 1.2) -> Tuple[float, float]:
    """Calculates tactical lateral echelon offset for individual units in a swarm so they fly separately."""
    if total_count <= 1:
        return lat, lon
    
    # Offset perpendicular to flight path (lateral spread)
    perp_bearing = (bearing_deg + 90.0) % 360.0
    offset_dist = (index - (total_count - 1) / 2.0) * spacing_km
    
    if offset_dist < 0:
        perp_bearing = (perp_bearing + 180.0) % 360.0
        offset_dist = abs(offset_dist)
    
    return calculate_projected_point(lat, lon, perp_bearing, distance_km=offset_dist)

class ThreatDeduplicator:
    def __init__(self):
        self.active_targets: Dict[str, ActiveTarget] = {}
        self.target_counter = 100
        self.msg_to_target_map: Dict[Tuple[str, int], str] = {}

    def _generate_target_code(self, target_type: TargetType) -> str:
        prefix = {
            TargetType.SHAHED: "SH",
            TargetType.JET_UAV: "RS",
            TargetType.MISSILE: "MS",
            TargetType.BALLISTIC: "BL",
            TargetType.KAB: "KB",
            TargetType.RECON: "RC",
            TargetType.FPV: "FP",
            TargetType.DECOY: "DC"
        }.get(target_type, "TG")
        self.target_counter += 1
        return f"TGT-{prefix}-{self.target_counter}"

    def _default_speed(self, target_type: TargetType) -> float:
        return config.THREAT_SPEED_PROFILES.get(target_type.value, 185.0)

    def _compute_eta(self, lat1: float, lon1: float, lat2: Optional[float], lon2: Optional[float], speed_kmh: float) -> Optional[float]:
        if lat2 is None or lon2 is None or speed_kmh <= 0:
            return None
        dist_km = haversine_distance_km(lat1, lon1, lat2, lon2)
        return round((dist_km / speed_kmh) * 60.0, 1)

    def process_event_multi(self, event: ParsedThreatEvent) -> List[Tuple[ActiveTarget, bool]]:
        """
        Splits grouped reports (e.g. 2x, 3x, 5x) into separate individual target tracks
        with tactical formation offsets so all units fly and display independently.
        """
        count = max(1, event.target_count)
        results = []
        
        for i in range(count):
            unit_event = event.model_copy()
            unit_event.target_count = 1
            
            if count > 1:
                unit_lat, unit_lon = calculate_formation_offset(
                    event.lat, event.lon, event.heading_deg, i, count, spacing_km=1.4
                )
                unit_event.lat = round(unit_lat, 4)
                unit_event.lon = round(unit_lon, 4)
            
            tgt, is_new = self._process_single_event(unit_event, sub_index=i)
            results.append((tgt, is_new))
            
        return results

    def process_event(self, event: ParsedThreatEvent) -> Tuple[ActiveTarget, bool]:
        """Backward-compatible single event entrypoint."""
        results = self.process_event_multi(event)
        return results[0] if results else (self._process_single_event(event)[0], False)

    def _process_single_event(self, event: ParsedThreatEvent, sub_index: int = 0) -> Tuple[ActiveTarget, bool]:
        """Processes a single individual target track (count=1)."""
        now = time.time()
        self.cleanup_expired()

        # 1. Telegram Reply-To Check
        if event.reply_to_msg_id:
            parent_key = (event.source_channel, event.reply_to_msg_id)
            if parent_key in self.msg_to_target_map:
                target_id = self.msg_to_target_map[parent_key]
                if target_id in self.active_targets:
                    target = self.active_targets[target_id]

                    if event.is_clear_signal:
                        target.status = ThreatStatus.DESTROYED
                        del self.active_targets[target_id]
                        target.raw_reports.append(f"[{event.source_channel}] (ЗБИТО/ВІДБІЙ) {event.raw_text}")
                        return target, False

                    if event.lat != 0.0 and event.lon != 0.0:
                        target.current_lat = (target.current_lat * 0.3) + (event.lat * 0.7)
                        target.current_lon = (target.current_lon * 0.3) + (event.lon * 0.7)
                        target.current_location_name = event.location_name

                    if event.destination:
                        target.destination_name = event.destination
                        target.dest_lat = event.dest_lat
                        target.dest_lon = event.dest_lon
                        target.is_circling = False
                        target.circling_start = None

                    if event.heading_deg != 0.0:
                        target.heading_deg = event.heading_deg
                        target.heading = event.heading
                    elif target.dest_lat is not None and target.dest_lon is not None:
                        target.heading_deg = compute_spherical_bearing(target.current_lat, target.current_lon, target.dest_lat, target.dest_lon)

                    target.distance_to_dest_km = haversine_distance_km(target.current_lat, target.current_lon, target.dest_lat, target.dest_lon) if target.dest_lat else None
                    target.eta_minutes = self._compute_eta(target.current_lat, target.current_lon, target.dest_lat, target.dest_lon, target.speed_kmh)
                    target.last_updated = event.timestamp
                    target.raw_reports.append(f"[{event.source_channel}] (ОНОВЛЕННЯ) {event.raw_text}")
                    target.trajectory.append([target.current_lat, target.current_lon, event.timestamp])
                    target.hazard_cone = generate_hazard_cone_polygon(target.current_lat, target.current_lon, target.heading_deg)

                    if event.message_id:
                        self.msg_to_target_map[(event.source_channel, event.message_id)] = target_id

                    return target, False

        # Standalone clear signal without target
        if event.is_clear_signal and (event.lat == 0.0 or not event.location_name or event.location_name == "Unknown"):
            dummy = ActiveTarget(
                target_id="CLEAR",
                target_type=TargetType.SHAHED,
                status=ThreatStatus.DESTROYED,
                current_lat=0.0,
                current_lon=0.0,
                current_location_name="Clear",
                heading=HeadingDirection.UNKNOWN,
                heading_deg=0.0,
                first_seen=now,
                last_updated=now
            )
            return dummy, False

        # 2. Spatial-Temporal Cluster Matching (for single unit correlation)
        best_match_id: Optional[str] = None
        min_distance = float("inf")

        for tid, target in self.active_targets.items():
            if target.target_type != event.target_type:
                continue

            time_delta = abs(event.timestamp - target.last_updated)
            if time_delta > config.DEDUPLICATION_TIME_WINDOW_SEC:
                continue

            dist_km = haversine_distance_km(event.lat, event.lon, target.current_lat, target.current_lon)
            if dist_km <= config.DEDUPLICATION_RADIUS_KM:
                if dist_km < min_distance:
                    min_distance = dist_km
                    best_match_id = tid

        if best_match_id:
            target = self.active_targets[best_match_id]
            
            if event.source_channel in ("Додаткове джерело", "NEPTUN API"):
                target.current_lat = event.lat
                target.current_lon = event.lon
            else:
                target.current_lat = (target.current_lat * 0.3) + (event.lat * 0.7)
                target.current_lon = (target.current_lon * 0.3) + (event.lon * 0.7)
            
            target.current_location_name = event.location_name
            target.count = 1 # Always 1 individual unit
            
            if event.destination:
                target.destination_name = event.destination
                target.dest_lat = event.dest_lat
                target.dest_lon = event.dest_lon
                target.is_circling = False
                target.circling_start = None

            if event.heading_deg != 0.0:
                target.heading_deg = event.heading_deg
                target.heading = event.heading
            elif target.dest_lat is not None and target.dest_lon is not None:
                target.heading_deg = compute_spherical_bearing(target.current_lat, target.current_lon, target.dest_lat, target.dest_lon)

            target.distance_to_dest_km = haversine_distance_km(target.current_lat, target.current_lon, target.dest_lat, target.dest_lon) if target.dest_lat else None
            target.eta_minutes = self._compute_eta(target.current_lat, target.current_lon, target.dest_lat, target.dest_lon, target.speed_kmh)

            if event.source_channel not in target.sources:
                target.sources.append(event.source_channel)
                target.confidence_score = min(1.0, target.confidence_score + 0.15)

            target.raw_reports.append(f"[{event.source_channel}] {event.raw_text}")
            if len(target.raw_reports) > 15:
                target.raw_reports = target.raw_reports[-15:]

            target.last_updated = event.timestamp
            target.trajectory.append([target.current_lat, target.current_lon, event.timestamp])
            target.hazard_cone = generate_hazard_cone_polygon(target.current_lat, target.current_lon, target.heading_deg)

            if event.message_id:
                self.msg_to_target_map[(event.source_channel, event.message_id)] = target.target_id

            return target, False

        # 3. Create New Individual Target Track (count=1)
        new_id = self._generate_target_code(event.target_type)
        speed = self._default_speed(event.target_type)
        spawn_lat = event.lat
        spawn_lon = event.lon

        if event.dest_lat is not None and event.dest_lon is not None and (event.lat != event.dest_lat or event.lon != event.dest_lon):
            target_dest_lat = event.dest_lat
            target_dest_lon = event.dest_lon
            dest_name = event.destination
            exact_bearing = compute_spherical_bearing(spawn_lat, spawn_lon, target_dest_lat, target_dest_lon)
        else:
            if event.heading_deg != 0.0:
                exact_bearing = event.heading_deg
            elif spawn_lat < 47.0:
                exact_bearing = 315.0
            elif spawn_lon > 35.5:
                exact_bearing = 270.0
            elif spawn_lat > 50.5:
                exact_bearing = 225.0
            else:
                exact_bearing = 280.0

            p_lat, p_lon = calculate_projected_point(spawn_lat, spawn_lon, exact_bearing, distance_km=80.0)
            target_dest_lat = round(p_lat, 4)
            target_dest_lon = round(p_lon, 4)
            dest_name = event.destination or f"Курс {round(exact_bearing)}°"

        eta = self._compute_eta(spawn_lat, spawn_lon, target_dest_lat, target_dest_lon, speed)
        rem_dist = haversine_distance_km(spawn_lat, spawn_lon, target_dest_lat, target_dest_lon)

        trajectory = [
            [spawn_lat, spawn_lon, event.timestamp - 300],
            [spawn_lat, spawn_lon, event.timestamp]
        ]
        hazard_cone = generate_hazard_cone_polygon(spawn_lat, spawn_lon, exact_bearing)

        new_target = ActiveTarget(
            target_id=new_id,
            target_type=event.target_type,
            target_subtype=event.target_subtype,
            count=1, # Always 1 unit per marker
            status=ThreatStatus.ACTIVE,
            current_lat=spawn_lat,
            current_lon=spawn_lon,
            current_location_name=event.location_name,
            destination_name=dest_name,
            dest_lat=target_dest_lat,
            dest_lon=target_dest_lon,
            eta_minutes=eta,
            distance_to_dest_km=rem_dist,
            is_circling=False,
            circling_start=None,
            heading=event.heading,
            heading_deg=exact_bearing,
            speed_kmh=speed,
            altitude_info=event.altitude_info,
            sources=[event.source_channel],
            raw_reports=[f"[{event.source_channel}] {event.raw_text}"],
            first_seen=event.timestamp,
            last_updated=event.timestamp,
            confidence_score=0.88,
            trajectory=trajectory,
            hazard_cone=hazard_cone
        )
        self.active_targets[new_id] = new_target

        if event.message_id:
            self.msg_to_target_map[(event.source_channel, event.message_id)] = new_id

        return new_target, True

    def advance_kinematics(self, dt_seconds: float = 1.0) -> List[ActiveTarget]:
        """
        Advances target positions smoothly along flight vectors.
        Upon reaching destination settlement, enters CIRCLING mode for CIRCLING_DURATION_SEC, then neutralizes.
        """
        now = time.time()
        expired_ids = []

        for tid, tgt in list(self.active_targets.items()):
            # 1. Circling / Loiter Mode
            if tgt.is_circling:
                tgt.status = ThreatStatus.CIRCLING
                time_circling = now - (tgt.circling_start or now)
                
                if time_circling >= config.CIRCLING_DURATION_SEC:
                    tgt.status = ThreatStatus.DESTROYED
                    expired_ids.append(tid)
                    del self.active_targets[tid]
                    continue

                orbit_angle = (2.0 * math.pi / config.CIRCLING_ORBIT_PERIOD_SEC) * time_circling
                dest_lat = tgt.dest_lat or tgt.current_lat
                dest_lon = tgt.dest_lon or tgt.current_lon

                d_lat = (config.CIRCLING_RADIUS_KM / 111.0) * math.cos(orbit_angle)
                d_lon = (config.CIRCLING_RADIUS_KM / (111.0 * math.cos(math.radians(dest_lat)))) * math.sin(orbit_angle)

                tgt.current_lat = dest_lat + d_lat
                tgt.current_lon = dest_lon + d_lon
                tgt.heading_deg = (math.degrees(orbit_angle + (math.pi / 2.0)) + 360.0) % 360.0
                tgt.eta_minutes = 0.0
                tgt.distance_to_dest_km = 0.0
                tgt.last_updated = now

                if not tgt.trajectory or (now - tgt.trajectory[-1][2]) >= 2.0:
                    tgt.trajectory.append([tgt.current_lat, tgt.current_lon, now])
                continue

            # 2. Linear flight towards destination settlement
            if tgt.heading_deg is not None and (now - tgt.last_updated) < config.TARGET_TTL_SECONDS:
                speed_kms = tgt.speed_kmh / 3600.0
                dist_km = speed_kms * dt_seconds

                if tgt.dest_lat is not None and tgt.dest_lon is not None:
                    rem_dist = haversine_distance_km(tgt.current_lat, tgt.current_lon, tgt.dest_lat, tgt.dest_lon)
                    tgt.distance_to_dest_km = round(rem_dist, 1)
                    
                    if rem_dist <= 0.85:
                        tgt.is_circling = True
                        tgt.status = ThreatStatus.CIRCLING
                        tgt.circling_start = now
                        tgt.eta_minutes = 0.0
                        tgt.last_updated = now
                        continue

                    tgt.heading_deg = compute_spherical_bearing(tgt.current_lat, tgt.current_lon, tgt.dest_lat, tgt.dest_lon)
                    tgt.eta_minutes = round((rem_dist / tgt.speed_kmh) * 60.0, 1)

                rad = math.radians(tgt.heading_deg)
                d_lat = (dist_km / 111.0) * math.cos(rad)
                d_lon = (dist_km / (111.0 * math.cos(math.radians(tgt.current_lat)))) * math.sin(rad)
                
                tgt.current_lat += d_lat
                tgt.current_lon += d_lon
                tgt.hazard_cone = generate_hazard_cone_polygon(tgt.current_lat, tgt.current_lon, tgt.heading_deg)
                
                if not tgt.trajectory or (now - tgt.trajectory[-1][2]) >= 5.0:
                    tgt.trajectory.append([tgt.current_lat, tgt.current_lon, now])
                    if len(tgt.trajectory) > 60:
                        tgt.trajectory = tgt.trajectory[-60:]

        return list(self.active_targets.values())

    def remove_target(self, target_id: str) -> Optional[ActiveTarget]:
        """Manually mark target as shot down / neutralized."""
        if target_id in self.active_targets:
            tgt = self.active_targets.pop(target_id)
            tgt.status = ThreatStatus.DESTROYED
            return tgt
        return None

    def cleanup_expired(self) -> List[str]:
        now = time.time()
        expired_ids = []
        for tid, target in list(self.active_targets.items()):
            if now - target.last_updated > config.TARGET_TTL_SECONDS:
                expired_ids.append(tid)
                del self.active_targets[tid]
        return expired_ids

    def get_all_active(self) -> List[ActiveTarget]:
        self.cleanup_expired()
        return list(self.active_targets.values())

    def clear_all(self):
        self.active_targets.clear()
        self.msg_to_target_map.clear()
