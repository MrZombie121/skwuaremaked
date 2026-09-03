"""
SKYWATCH Global Configuration & Tactical Air Radar Settings
Compatible with Local Desktop (PyQt6), Linux VPS, Docker, Render, Railway, Fly.io, Heroku
"""
import os
from typing import List, Dict

# Server network configuration (supports cloud $PORT and $HOST)
SERVER_HOST = os.getenv("HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("PORT", "8080"))

# Telegram API Configuration (Telethon)
# Can be set via Environment Variables on Cloud Hosting or saved in data/settings.json
TELEGRAM_API_ID = int(os.getenv("TG_API_ID", os.getenv("TELEGRAM_API_ID", "0")) or "0")
TELEGRAM_API_HASH = os.getenv("TG_API_HASH", os.getenv("TELEGRAM_API_HASH", ""))
TELEGRAM_SESSION_STRING = os.getenv("TG_SESSION_STRING", os.getenv("TELEGRAM_SESSION_STRING", ""))
TELEGRAM_FOLDER_URL = os.getenv("FOLDER_URL", "https://t.me/addlist/syGYtBj5T9AxNzIy")

# Map Boundaries for Ukraine (Leaflet Bounding Box)
UKRAINE_BOUNDS = [
    [43.8, 21.5],  # SW corner (including Crimea & Western borders)
    [52.8, 40.5]   # NE corner (including Sumy/Chernihiv & Luhansk)
]
UKRAINE_CENTER = [48.3794, 31.1656] # Geographic center of Ukraine
DEFAULT_ZOOM = 6.5
MIN_ZOOM = 5.0
MAX_ZOOM = 14.0

# Deduplication & Clustering Parameters
DEDUPLICATION_RADIUS_KM = 45.0          # Max distance to correlate into the same cluster
DEDUPLICATION_TIME_WINDOW_SEC = 600      # 10 minutes time window for message correlation
TARGET_TTL_SECONDS = 900                # 15 minutes without updates before marking inactive/clearing

# Admin & Maintenance System Settings
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "skywatch-secret-key-2026")
MAINTENANCE_MODE_DEFAULT = False

# Kinematics & Loitering Parameters
CIRCLING_DURATION_SEC = 6.0              # Circles over target upon arrival before fading out
CIRCLING_RADIUS_KM = 0.65                # Orbit radius in km over the destination city
CIRCLING_ORBIT_PERIOD_SEC = 2.4          # Time in seconds for one full 360° orbit

# Default Speed Profiles (km/h) for Flight Simulation
THREAT_SPEED_PROFILES: Dict[str, float] = {
    "SHAHED": 185.0,      # Shahed-136 / Geran-2 (~185 km/h)
    "JET_UAV": 490.0,     # Jet-powered UAV / RS (~490 km/h)
    "MISSILE": 860.0,     # Cruise Missile (Kh-101, Kalibr) (~860 km/h)
    "BALLISTIC": 3200.0,  # Ballistic Missile (Iskander-M, Kinzhal) (~3200 km/h)
    "KAB": 420.0,         # Guided Aerial Bomb / KAB (~420 km/h)
    "RECON": 120.0,       # Reconnaissance UAV (Orlan, Supercam, Zala) (~120 km/h)
    "FPV": 110.0,         # Tactical FPV Drone (~110 km/h)
    "DECOY": 175.0,       # Decoy / Simulator Drone (Parodiya, Gerber) (~175 km/h)
    "UNKNOWN": 200.0
}

# Hazard Projection Parameters
HAZARD_CONE_ANGLE_DEG = 35.0             # Angular width of danger corridor
HAZARD_PROJECTION_MINUTES = 25.0         # How far forward to project hazard sector
