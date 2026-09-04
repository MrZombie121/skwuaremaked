"""
Pydantic Data Models & Enumerations for SkyWatch Air Monitoring System
Supports Comprehensive Threat Types, Tactical Subtypes, Kinematic Telemetry & Geo-Polygons.
"""
from enum import Enum
from typing import List, Optional, Dict, Any, Tuple
from pydantic import BaseModel, Field
import time
import uuid

class TargetType(str, Enum):
    SHAHED = "SHAHED"           # Дрон-камікадзе (Shahed-136 / Герань-2 / Мопед)
    JET_UAV = "JET_UAV"         # Реактивний БПЛА / RS / швидкісний дрон
    MISSILE = "MISSILE"         # Крилата ракета (Х-101, Калібр, Х-59/69)
    BALLISTIC = "BALLISTIC"     # Балістична / аеробалістична ракета (Іскандер-М, Кинджал, С-300/400)
    KAB = "KAB"                 # Керована авіаційна бомба (КАБ / ФАБ з УМПК)
    RECON = "RECON"             # Розвідувальний БПЛА (Орлан, Supercam, Zala, Мерлін)
    FPV = "FPV"                 # Тактичний дрон / FPV крило
    DECOY = "DECOY"             # Фальш-ціль / приманка (Пародія, Гербера)
    UNKNOWN = "UNKNOWN"         # Невизначена повітряна ціль

class ThreatStatus(str, Enum):
    ACTIVE = "ACTIVE"           # Активний політ за курсом
    CIRCLING = "CIRCLING"       # Кружляння / маневрування над районом
    DESTROYED = "DESTROYED"     # Збито / знешкоджено ППО
    LOST = "LOST"               # Локаційно втрачено / припинено супровід

class HeadingDirection(str, Enum):
    N = "N"       # Північ (0°)
    NE = "NE"     # Північний схід (45°)
    E = "E"       # Схід (90°)
    SE = "SE"     # Південний схід (135°)
    S = "S"       # Південь (180°)
    SW = "SW"     # Південний захід (225°)
    W = "W"       # Захід (270°)
    NW = "NW"     # Північно-західний (315°)
    UNKNOWN = "UNKNOWN"

DIRECTION_ANGLES: Dict[HeadingDirection, float] = {
    HeadingDirection.N: 0.0,
    HeadingDirection.NE: 45.0,
    HeadingDirection.E: 90.0,
    HeadingDirection.SE: 135.0,
    HeadingDirection.S: 180.0,
    HeadingDirection.SW: 225.0,
    HeadingDirection.W: 270.0,
    HeadingDirection.NW: 315.0,
    HeadingDirection.UNKNOWN: 0.0
}

class RawTelegramMessage(BaseModel):
    channel: str
    message_id: int
    reply_to_msg_id: Optional[int] = None
    text: str
    timestamp: float = Field(default_factory=time.time)

class ParsedThreatEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    source_channel: str
    message_id: Optional[int] = None
    reply_to_msg_id: Optional[int] = None
    raw_text: str
    target_type: TargetType = TargetType.SHAHED
    target_subtype: Optional[str] = None
    target_count: int = 1
    location_name: str
    region_name: Optional[str] = None
    lat: float
    lon: float
    heading: HeadingDirection = HeadingDirection.UNKNOWN
    heading_deg: float = 0.0
    destination: Optional[str] = None
    dest_lat: Optional[float] = None
    dest_lon: Optional[float] = None
    is_clear_signal: bool = False
    altitude_info: Optional[str] = None
    speed_kmh: Optional[float] = None
    timestamp: float = Field(default_factory=time.time)
    confidence: float = 0.85

class ActiveTarget(BaseModel):
    target_id: str
    target_type: TargetType
    target_subtype: Optional[str] = None
    count: int = 1
    status: ThreatStatus = ThreatStatus.ACTIVE
    current_lat: float
    current_lon: float
    current_location_name: str
    destination_name: Optional[str] = None
    dest_lat: Optional[float] = None
    dest_lon: Optional[float] = None
    eta_minutes: Optional[float] = None
    distance_to_dest_km: Optional[float] = None
    is_circling: bool = False
    circling_start: Optional[float] = None
    heading: HeadingDirection
    heading_deg: float
    speed_kmh: float = 185.0
    altitude_info: Optional[str] = None
    sources: List[str] = Field(default_factory=list)
    raw_reports: List[str] = Field(default_factory=list)
    first_seen: float
    last_updated: float
    confidence_score: float = 1.0
    trajectory: List[List[float]] = Field(default_factory=list) # [lat, lon, timestamp]
    hazard_cone: Optional[List[List[float]]] = None             # List of [lat, lon] polygon points

class ChannelInfo(BaseModel):
    id: int
    tg_channel_id: Optional[int] = None
    username: Optional[str] = None
    title: str
    folder_url: Optional[str] = None
    is_active: bool = True
    total_messages_parsed: int = 0
    threats_detected: int = 0
    last_message_at: float = 0.0
    added_at: float = Field(default_factory=time.time)

class WebSocketPayload(BaseModel):
    type: str
    data: Dict[str, Any]
    timestamp: float = Field(default_factory=time.time)
