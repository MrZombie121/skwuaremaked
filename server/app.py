"""
FastAPI Backend, WebSocket Hub, REST API & Telemetry Engine for SkyWatch Tactical Radar
"""
import asyncio
import logging
import os
import sys
import time

# Protect Windows console from UnicodeEncodeError on emojis in logs
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from typing import List, Set, Dict, Any, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel

import config
from core.db import db
from core.models import (
    RawTelegramMessage, ParsedThreatEvent, ActiveTarget, TargetType
)
from core.nlp_parser import TelegramThreatParser
from core.deduplicator import ThreatDeduplicator
from core.telegram_service import TelegramService
from core.simulator import TacticalSimulator
from core.neptun_service import NeptunApiService
from core.turso_db import turso_db
from core.gemini_service import gemini_analyst
from core.auth_bot import auth_bot, verify_telegram_widget_auth, pending_auth_sessions, pin_to_user_map, BOT_USERNAME
from core.alerts_service import alerts_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("SkyWatch.Server")

app = FastAPI(title="SkyWatch Tactical Air Threat Radar", version="2.2.0")

# Enable GZip compression for ultra-fast page load & payload transfer
app.add_middleware(GZipMiddleware, minimum_size=500)

# Enable CORS for cloud deployment and reverse proxies
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(BASE_DIR, "ui")
MARKERS_DIR = os.path.join(BASE_DIR, "markers")

# Custom static files class with caching headers for lightning-fast page loading
class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if path.endswith(('.png', '.jpg', '.jpeg', '.svg', '.woff2', '.woff', '.css', '.js', '.webmanifest', '.ico')):
            response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=3600"
        return response

app.mount("/markers", CachedStaticFiles(directory=MARKERS_DIR), name="markers")
app.mount("/ui", CachedStaticFiles(directory=UI_DIR), name="ui")

# Core singletons
parser = TelegramThreatParser()
deduplicator = ThreatDeduplicator()
connected_clients: Set[WebSocket] = set()
telegram_service: Optional[TelegramService] = None
simulator: Optional[TacticalSimulator] = None
neptun_service: Optional[NeptunApiService] = None

class ConnectionManager:
    @staticmethod
    async def connect(websocket: WebSocket):
        await websocket.accept()
        connected_clients.add(websocket)
        logger.info(f"WebSocket client connected. Active clients: {len(connected_clients)}")
        
        # Send initial state snapshot with recent logs & live alerts
        targets = [t.model_dump() for t in deduplicator.get_all_active()]
        channels = db.get_all_channels()
        tg_status = telegram_service.get_status() if telegram_service else {}
        sim_status = simulator.is_running if simulator else False
        recent_logs = db.get_recent_logs(150)
        alerts_summary = alerts_service.get_summary()
        
        await websocket.send_json({
            "type": "INITIAL_STATE",
            "data": {
                "targets": targets,
                "channels": channels,
                "telegram": tg_status,
                "simulator_active": sim_status,
                "logs": recent_logs,
                "alerts": alerts_summary,
                "config": {
                    "bounds": config.UKRAINE_BOUNDS,
                    "center": config.UKRAINE_CENTER,
                    "default_zoom": config.DEFAULT_ZOOM,
                    "folder_url": db.get_setting("folder_url", "https://t.me/addlist/syGYtBj5T9AxNzIy"),
                    "speeds": config.THREAT_SPEED_PROFILES
                }
            },
            "timestamp": time.time()
        })

    @staticmethod
    def disconnect(websocket: WebSocket):
        connected_clients.discard(websocket)
        logger.info(f"WebSocket client disconnected. Remaining: {len(connected_clients)}")

    @staticmethod
    async def broadcast(payload: dict):
        if not connected_clients:
            return
        dead_clients = []
        
        async def _safe_send(ws: WebSocket):
            try:
                await ws.send_json(payload)
            except Exception:
                dead_clients.append(ws)

        await asyncio.gather(*[_safe_send(c) for c in list(connected_clients)], return_exceptions=True)
        for dead in dead_clients:
            connected_clients.discard(dead)

async def on_neptun_snapshot_received(active_events: List[ParsedThreatEvent], active_ids: Set[str]):
    """Synchronizes active snapshot from Neptun API, removing any disappeared/neutralized targets immediately."""
    # 1. Reconcile and purge disappeared targets from this source
    removed_ids = deduplicator.sync_source_active_targets("Додаткове джерело", active_ids)
    
    # 2. Process all current active events
    has_new = False
    for event in active_events:
        results = deduplicator.process_event_multi(event)
        for target, is_new in results:
            if is_new:
                has_new = True
            if target.target_id != "CLEAR":
                db.save_or_update_target(target.model_dump())

    # 3. Broadcast updated active state
    all_targets = [t.model_dump() for t in deduplicator.get_all_active()]
    await ConnectionManager.broadcast({
        "type": "TARGETS_UPDATE",
        "data": {
            "targets": all_targets,
            "is_new": has_new,
            "removed_count": len(removed_ids)
        },
        "timestamp": time.time()
    })

async def on_neptun_event_received(event: ParsedThreatEvent):
    """Callback for single threat events from Neptun API (spawns separate individual targets)."""
    results = deduplicator.process_event_multi(event)
    has_new = False
    
    for target, is_new in results:
        if is_new:
            has_new = True
        if target.target_id != "CLEAR":
            db.save_or_update_target(target.model_dump())
            db.record_threat_event({
                "target_id": target.target_id,
                "source_channel": event.source_channel,
                "raw_text": event.raw_text,
                "target_type": event.target_type.value,
                "location_name": target.current_location_name,
                "destination_name": target.destination_name,
                "lat": target.current_lat,
                "lon": target.current_lon,
                "heading": event.heading.value,
                "heading_deg": target.heading_deg,
                "timestamp": event.timestamp
            })

    all_targets = [t.model_dump() for t in deduplicator.get_all_active()]
    await ConnectionManager.broadcast({
        "type": "TARGETS_UPDATE",
        "data": {
            "targets": all_targets,
            "is_new": has_new
        },
        "timestamp": time.time()
    })

async def on_message_received(raw_msg: RawTelegramMessage):
    """Callback for all live messages received from Telegram or Simulator."""
    # Skip any messages published more than 15 minutes (900 seconds) ago
    if (time.time() - raw_msg.timestamp) > 900 or (time.time() - raw_msg.timestamp) < -300:
        logger.debug(f"Skipping stale message older than 15 minutes ({int(time.time() - raw_msg.timestamp)}s)")
        return

    # 1. Multi-threat NLP parsing (composite messages support)
    events: List[ParsedThreatEvent] = parser.parse_message_multi(
        text=raw_msg.text,
        source_channel=raw_msg.channel,
        message_id=raw_msg.message_id,
        reply_to_msg_id=raw_msg.reply_to_msg_id
    )
    is_threat = len(events) > 0 and any(not getattr(e, 'is_clear_signal', False) for e in events)

    # 2. Record message and statistics in JSON Database
    db.record_channel_message(
        channel_name=raw_msg.channel,
        message_id=raw_msg.message_id,
        text=raw_msg.text,
        is_threat=is_threat
    )

    # 3. Broadcast raw log to UI terminal
    await ConnectionManager.broadcast({
        "type": "RAW_LOG",
        "data": {
            "channel": raw_msg.channel,
            "text": raw_msg.text,
            "time": time.strftime("%H:%M:%S", time.localtime(raw_msg.timestamp)),
            "parsed": is_threat,
            "threat_count": len(events)
        },
        "timestamp": raw_msg.timestamp
    })

    if not events:
        return

    # 4. Process all extracted threat events as individual unstacked tracks
    has_new_threat = False
    for event in events:
        results = deduplicator.process_event_multi(event)
        for target, is_new in results:
            if is_new:
                has_new_threat = True

            # Persist target and history event in JSON DB
            if target.target_id != "CLEAR":
                db.save_or_update_target(target.model_dump())
                db.record_threat_event({
                    "target_id": target.target_id,
                    "source_channel": event.source_channel,
                    "raw_text": event.raw_text,
                    "target_type": event.target_type.value,
                    "location_name": target.current_location_name,
                    "destination_name": target.destination_name,
                    "lat": target.current_lat,
                    "lon": target.current_lon,
                    "heading": event.heading.value,
                    "heading_deg": target.heading_deg,
                    "timestamp": event.timestamp
                })

    # 5. Broadcast updated targets to UI
    all_targets = [t.model_dump() for t in deduplicator.get_all_active()]
    await ConnectionManager.broadcast({
        "type": "TARGETS_UPDATE",
        "data": {
            "targets": all_targets,
            "is_new": has_new_threat
        },
        "timestamp": time.time()
    })

async def kinematic_loop():
    """Kinematic engine: smoothly advances target positions along flight vectors every second."""
    while True:
        await asyncio.sleep(1.0)
        active = deduplicator.advance_kinematics(dt_seconds=1.0)
        expired = deduplicator.cleanup_expired()
        
        # Broadcast live air raid alert states from official service
        all_active_targets = deduplicator.get_all_active()
        alerts_summary = alerts_service.get_summary()

        targets_dump = [t.model_dump() for t in all_active_targets]
        await ConnectionManager.broadcast({
            "type": "KINEMATIC_TICK",
            "data": {
                "targets": targets_dump,
                "expired_ids": expired,
                "alerts": alerts_summary
            },
            "timestamp": time.time()
        })

UPSTREAM_PRODUCTION_URL = os.getenv("UPSTREAM_URL", "https://ua-skywatch.pp.ua")

async def production_upstream_sync_loop():
    """
    When running in local dev/test mode, continuously syncs live targets, telegram logs,
    and alerts from the live production server (https://ua-skywatch.pp.ua) without needing
    a local Telegram session. This lets you test newly added local UI/JS features with 100% real data!
    """
    if "RENDER" in os.environ or os.getenv("IS_PRODUCTION", "false").lower() == "true":
        return

    logger.info(f"Local test mode: Syncing live threat data from production ({UPSTREAM_PRODUCTION_URL})...")
    
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                # 1. Fetch live production targets
                async with session.get(f"{UPSTREAM_PRODUCTION_URL}/api/targets", timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        prod_targets = data.get("targets", [])
                        if prod_targets:
                            # Safely load into local deduplicator
                            deduplicator.active_targets = {
                                t["target_id"]: ActiveTarget(**t) for t in prod_targets
                            }

                # 2. Fetch live production logs
                async with session.get(f"{UPSTREAM_PRODUCTION_URL}/api/logs?limit=100", timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        prod_logs = data.get("logs", [])
                        if prod_logs:
                            db._write_json(os.path.join(db.data_dir, "messages_log.json"), prod_logs)

        except Exception as e:
            logger.debug(f"Upstream sync notice: {e}")

        await asyncio.sleep(2.5)

@app.on_event("startup")
async def startup_event():
    global telegram_service, simulator, neptun_service
    
    # Initialize Turso Cloud Schema & Sync Maintenance
    try:
        await turso_db.init_schema()
        await turso_db.get_maintenance_state()
    except Exception as te:
        logger.warning(f"Turso initialization notice: {te}")

    telegram_service = TelegramService(message_callback=on_message_received)
    simulator = TacticalSimulator(message_callback=on_message_received)
    neptun_service = NeptunApiService(event_callback=on_neptun_event_received, snapshot_callback=on_neptun_snapshot_received)
    
    # Start live air alerts service
    try:
        alerts_service.set_threats_provider(lambda: deduplicator.get_all_active())
        await alerts_service.start()
    except Exception as ae:
        logger.warning(f"Alerts service startup notice: {ae}")

    # Auto-start additional source (Neptun) if persisted as enabled in Turso / settings
    try:
        nep_saved = await turso_db.get_setting("neptun_enabled") or db.get_setting("neptun_enabled", "false")
        if str(nep_saved).lower() == "true":
            await neptun_service.start()
            logger.info("Automatically restored and started additional source (Neptun) from Turso Cloud settings.")
    except Exception as ne:
        logger.warning(f"Neptun auto-restore notice: {ne}")

    # Initialize Telegram client
    tg_connected = await telegram_service.initialize()
    if not tg_connected:
        logger.info("Telegram not authorized yet. Ready for live authorization or manual/simulator injection.")

    # Initialize Telegram Auth Bot for Developer Login
    try:
        await auth_bot.start()
    except Exception as be:
        logger.warning(f"Auth bot startup notice: {be}")

    asyncio.create_task(kinematic_loop())
    asyncio.create_task(production_upstream_sync_loop())
    logger.info("SkyWatch Backend Engine v2.1 initialized successfully.")

@app.on_event("shutdown")
async def shutdown_event():
    await auth_bot.stop()
    await alerts_service.stop()
    if simulator:
        simulator.stop()
    if neptun_service:
        await neptun_service.stop()
    if telegram_service:
        await telegram_service.stop()

@app.get("/")
async def root(key: Optional[str] = None, bypass: Optional[str] = None):
    expected_key = db.get_setting("admin_secret_key") or config.ADMIN_SECRET_KEY
    # Check if admin bypass is requested with valid secret key
    is_bypass = (key == expected_key) or (bypass == expected_key)
    
    if not is_bypass:
        state = await turso_db.get_maintenance_state()
        if state.get("maintenance_mode", False):
            return FileResponse(os.path.join(UI_DIR, "maintenance.html"))
            
    return FileResponse(os.path.join(UI_DIR, "index.html"))

@app.get("/test-radar")
async def test_radar_page(key: Optional[str] = None, bypass: Optional[str] = None):
    expected_key = db.get_setting("admin_secret_key") or config.ADMIN_SECRET_KEY
    is_bypass = (key == expected_key) or (bypass == expected_key)
    if not is_bypass:
        raise HTTPException(status_code=403, detail="Доступ заборонено: потрібен секретний ключ адміністратора")
    return FileResponse(os.path.join(UI_DIR, "index.html"))

@app.get("/system-control-panel")
async def admin_page(key: Optional[str] = None):
    expected_key = db.get_setting("admin_secret_key") or config.ADMIN_SECRET_KEY
    if not key or key != expected_key:
        raise HTTPException(status_code=403, detail="Доступ заборонено: невірний ключ доступу")
    return FileResponse(os.path.join(UI_DIR, "admin.html"))

# --- PWA SERVICE WORKER, MANIFEST & ICONS ---

@app.get("/sw.js")
async def service_worker():
    """PWA Service Worker providing offline map tile caching and background prefetching."""
    headers = {
        "Service-Worker-Allowed": "/",
        "Cache-Control": "no-cache, no-store, must-revalidate"
    }
    return FileResponse(os.path.join(UI_DIR, "sw.js"), media_type="application/javascript", headers=headers)

@app.get("/manifest.webmanifest")
@app.get("/manifest.json")
async def web_manifest():
    """PWA Web App Manifest for mobile installation and home screen shortcut."""
    headers = {"Cache-Control": "public, max-age=3600"}
    return FileResponse(os.path.join(UI_DIR, "manifest.webmanifest"), media_type="application/manifest+json", headers=headers)

@app.get("/favicon.ico")
async def favicon():
    """Favicon icon."""
    fav_path = os.path.join(UI_DIR, "icons", "favicon.ico")
    if os.path.exists(fav_path):
        return FileResponse(fav_path, media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})
    return Response(status_code=404)

# --- DEVELOPER PORTAL & OPENAI-COMPATIBLE AIRSPACE API (/v1/data) ---

@app.get("/developers")
@app.get("/developer")
@app.get("/api-docs")
async def developer_portal():
    return FileResponse(os.path.join(UI_DIR, "developer.html"))

# --- EMBEDDABLE MAP WIDGET & IFRAME SDK (/embed/map) ---

@app.get("/embed/map")
@app.get("/widget/map")
async def embed_map_widget(response: Response):
    """Embeddable live radar map for external websites (supports iframes)."""
    response.headers["X-Frame-Options"] = "ALLOWALL"
    response.headers["Content-Security-Policy"] = "frame-ancestors *"
    return FileResponse(os.path.join(UI_DIR, "embed.html"))

@app.get("/embed/skywatch-widget.js")
@app.get("/skywatch-widget.js")
async def embed_widget_script(response: Response):
    """Drop-in 1-line JS SDK for embedding live SkyWatch map into websites."""
    response.headers["Content-Type"] = "application/javascript; charset=utf-8"
    response.headers["Cache-Control"] = "public, max-age=86400"
    return FileResponse(os.path.join(UI_DIR, "skywatch-widget.js"))

class DeveloperAuthRequest(BaseModel):
    telegram_id: str
    first_name: Optional[str] = ""
    last_name: Optional[str] = ""
    username: Optional[str] = ""
    photo_url: Optional[str] = ""

@app.post("/api/dev/auth")
async def developer_auth(req: DeveloperAuthRequest):
    """Registers or logs in developer via Telegram and generates/returns sk-live-... key from Turso."""
    user = await turso_db.register_or_get_api_user(
        telegram_id=req.telegram_id,
        first_name=req.first_name or "",
        last_name=req.last_name or "",
        username=req.username or req.telegram_id,
        photo_url=req.photo_url or ""
    )
    return {"status": "ok", "api_key": user["api_key"], "username": user.get("username"), "telegram_id": user["telegram_id"]}

@app.post("/api/dev/create-auth-session")
async def create_auth_session():
    """Creates a temporary 1-click auth session for @skywatchlogin_bot stored in Turso."""
    import secrets
    code = f"sk_session_{secrets.token_hex(8)}"
    pending_auth_sessions[code] = {
        "verified": False,
        "api_key": None,
        "timestamp": time.time()
    }
    
    # Store session in Turso Cloud DB
    try:
        await turso_db.create_auth_session(code)
    except Exception as te:
        logger.debug(f"Turso create session note: {te}")

    # Clean up old sessions (> 10 mins)
    now = time.time()
    for c in list(pending_auth_sessions.keys()):
        if now - pending_auth_sessions[c].get("timestamp", 0) > 600:
            del pending_auth_sessions[c]

    return {
        "status": "ok",
        "session_code": code,
        "bot_username": BOT_USERNAME,
        "bot_url": f"https://t.me/{BOT_USERNAME}?start={code}"
    }

@app.get("/api/dev/check-auth-session")
async def check_auth_session(code: str):
    """Checks if developer has pressed Start in @skywatchlogin_bot."""
    # 1. Check in Turso Cloud DB (100% persistent across server restarts/workers)
    try:
        turso_res = await turso_db.check_auth_session(code)
        if turso_res and turso_res.get("verified"):
            return {
                "verified": True,
                "api_key": turso_res["api_key"],
                "username": turso_res["username"],
                "telegram_id": turso_res["telegram_id"]
            }
    except Exception as te:
        logger.debug(f"Turso check auth note: {te}")

    # 2. Check in memory
    sess = pending_auth_sessions.get(code)
    if sess and sess.get("verified"):
        return {
            "verified": True,
            "api_key": sess["api_key"],
            "username": sess["username"],
            "telegram_id": sess["telegram_id"]
        }
    return {"verified": False}

@app.post("/api/dev/telegram-widget-auth")
async def telegram_widget_auth(request: Request):
    """Verifies HMAC signature from official Telegram Login Widget."""
    data = await request.json()
    if not verify_telegram_widget_auth(data):
        raise HTTPException(status_code=400, detail="Недійсний цифровий підпис Telegram")

    user = await turso_db.register_or_get_api_user(
        telegram_id=str(data["id"]),
        first_name=data.get("first_name", ""),
        last_name=data.get("last_name", ""),
        username=data.get("username", f"user_{data['id']}"),
        photo_url=data.get("photo_url", "")
    )
    return {
        "status": "ok",
        "api_key": user["api_key"],
        "username": user.get("username"),
        "telegram_id": user["telegram_id"]
    }

class PinVerifyRequest(BaseModel):
    pin_or_username: str

@app.post("/api/dev/verify-pin")
async def verify_dev_pin(req: PinVerifyRequest):
    """Verifies 6-digit PIN from @skywatchlogin_bot or registers developer by username."""
    val = req.pin_or_username.strip()
    if not val:
        raise HTTPException(status_code=400, detail="Введіть PIN або @username")

    # 1. Check in Turso Cloud DB by 6-digit PIN
    try:
        turso_pin = await turso_db.verify_by_pin_code(val)
        if turso_pin and turso_pin.get("api_key"):
            return {
                "status": "ok",
                "api_key": turso_pin["api_key"],
                "username": turso_pin["username"],
                "telegram_id": turso_pin["telegram_id"]
            }
    except Exception as te:
        logger.debug(f"Turso pin lookup note: {te}")

    # 2. Check in memory by PIN
    if val in pin_to_user_map:
        data = pin_to_user_map[val]
        return {
            "status": "ok",
            "api_key": data["api_key"],
            "username": data["username"],
            "telegram_id": data["telegram_id"]
        }

    # 3. Register/get from Turso by username/ID
    clean_val = val.replace("@", "")
    user = await turso_db.register_or_get_api_user(
        telegram_id=clean_val,
        username=clean_val
    )
    return {
        "status": "ok",
        "api_key": user["api_key"],
        "username": user.get("username"),
        "telegram_id": user["telegram_id"]
    }

class DeveloperRegenRequest(BaseModel):
    telegram_id: str

@app.post("/api/dev/regenerate-key")
async def developer_regen_key(req: DeveloperRegenRequest):
    new_key = await turso_db.regenerate_user_api_key(req.telegram_id)
    if new_key:
        return {"status": "ok", "api_key": new_key}
    raise HTTPException(status_code=404, detail="Розробника не знайдено")

async def verify_api_key_auth(request: Request) -> Dict[str, Any]:
    """Authenticates sk-live-... API key from Authorization header or query param against Turso."""
    auth_header = request.headers.get("Authorization", "")
    api_key = None
    if auth_header.startswith("Bearer "):
        api_key = auth_header[7:].strip()
    elif "x-api-key" in request.headers:
        api_key = request.headers["x-api-key"].strip()
    elif "api_key" in request.query_params:
        api_key = request.query_params["api_key"].strip()

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Missing API key. Pass 'Authorization: Bearer sk-live-...' header. Get your key at /developers", "type": "invalid_request_error", "code": "invalid_api_key"}}
        )

    user = await turso_db.get_user_by_api_key(api_key)
    if not user:
        if api_key.startswith("sk-live-") and len(api_key) == 32:
            # Auto-register on Turso if valid format
            user = await turso_db.register_or_get_api_user(telegram_id=api_key[-8:], username="developer")
        else:
            raise HTTPException(
                status_code=401,
                detail={"error": {"message": "Invalid API key provided. Obtain your key at /developers", "type": "invalid_request_error", "code": "invalid_api_key"}}
            )

    # Increment request usage asynchronously in background
    asyncio.create_task(turso_db.increment_api_usage(api_key))
    return user

@app.get("/v1/data")
@app.post("/v1/data")
@app.get("/v1/threats")
async def get_v1_airspace_data(request: Request):
    """
    OpenAI-compatible live airspace intelligence endpoint.
    Returns all real-time coordinates, directions, speeds, destinations, and hazard sectors.
    """
    user = await verify_api_key_auth(request)
    now = int(time.time())
    active_targets = deduplicator.get_all_active()

    counts = {
        "total_threats": len(active_targets),
        "shahed": len([t for t in active_targets if t.target_type == TargetType.SHAHED]),
        "jet_uav": len([t for t in active_targets if t.target_type == TargetType.JET_UAV]),
        "missile": len([t for t in active_targets if t.target_type == TargetType.MISSILE]),
        "ballistic": len([t for t in active_targets if t.target_type == TargetType.BALLISTIC]),
        "kab": len([t for t in active_targets if t.target_type == TargetType.KAB]),
        "aircraft": len([t for t in active_targets if t.target_type == TargetType.AIRCRAFT]),
        "recon": len([t for t in active_targets if t.target_type == TargetType.RECON]),
        "fpv": len([t for t in active_targets if t.target_type == TargetType.FPV]),
        "decoy": len([t for t in active_targets if t.target_type == TargetType.DECOY]),
        "air_alert_active": len(active_targets) > 0
    }

    formatted_threats = []
    for t in active_targets:
        formatted_threats.append({
            "id": t.target_id,
            "object": "airspace.threat",
            "type": t.target_type.value,
            "subtype": t.target_subtype or t.target_type.value,
            "count": t.count,
            "status": t.status.value,
            "coordinates": {
                "lat": round(t.current_lat, 5),
                "lon": round(t.current_lon, 5)
            },
            "location": {
                "locality": t.current_location_name,
                "region": getattr(t, 'region_name', None) or "Україна"
            },
            "target": {
                "destination": t.destination_name,
                "dest_lat": t.dest_lat,
                "dest_lon": t.dest_lon,
                "distance_km": t.distance_to_dest_km,
                "eta_minutes": t.eta_minutes
            },
            "flight": {
                "heading": t.heading.value,
                "heading_deg": round(t.heading_deg, 1),
                "speed_kmh": round(t.speed_kmh, 1),
                "altitude": t.altitude_info or "Стандартна",
                "is_circling": t.is_circling
            },
            "sources": t.sources,
            "confidence": t.confidence_score,
            "hazard_cone": t.hazard_cone or [],
            "trajectory": t.trajectory or [],
            "created_at": int(t.first_seen),
            "updated_at": int(t.last_updated)
        })

    return JSONResponse(content={
        "object": "list",
        "created": now,
        "model": "skywatch-c4isr-v2.1",
        "summary": counts,
        "data": formatted_threats,
        "usage": {
            "developer": user.get("username") or user.get("telegram_id"),
            "requests_total": user.get("requests_count", 0) + 1
        }
    })

@app.get("/api/alerts")
async def get_live_alerts():
    """Returns live Ukrainian air raid alerts summary by Oblast (Drone / Missile)."""
    return alerts_service.get_summary()

@app.get("/api/maintenance/status")
async def get_maintenance_status():
    state = await turso_db.get_maintenance_state()
    return state

class MaintenanceToggleRequest(BaseModel):
    key: str
    enabled: bool
    reason: Optional[str] = None
    end_timestamp: Optional[int] = None

class GeminiReportRequest(BaseModel):
    key: str
    gemini_key: Optional[str] = None

@app.post("/api/admin/generate-report")
async def generate_admin_report(req: GeminiReportRequest):
    expected_key = db.get_setting("admin_secret_key") or config.ADMIN_SECRET_KEY
    if req.key != expected_key:
        raise HTTPException(status_code=403, detail="Доступ заборонено: невірний секретний ключ адміністратора")
    
    if req.gemini_key and req.gemini_key.strip():
        db.set_setting("gemini_api_key", req.gemini_key.strip())
        try:
            await turso_db.set_setting("gemini_api_key", req.gemini_key.strip())
        except Exception:
            pass

    targets = deduplicator.get_all_active()
    result = await gemini_analyst.generate_tactical_summary(targets, api_key_override=req.gemini_key)
    return result

class GeminiSaveKeyRequest(BaseModel):
    key: str
    gemini_key: str

@app.post("/api/admin/gemini/save-key")
async def save_gemini_key(req: GeminiSaveKeyRequest):
    expected_key = db.get_setting("admin_secret_key") or config.ADMIN_SECRET_KEY
    if req.key != expected_key:
        raise HTTPException(status_code=403, detail="Доступ заборонено")
    
    clean_k = req.gemini_key.strip()
    db.set_setting("gemini_api_key", clean_k)
    try:
        await turso_db.set_setting("gemini_api_key", clean_k)
    except Exception:
        pass
    return {"status": "ok", "saved": True}

@app.get("/api/admin/gemini/get-key")
async def get_gemini_key(key: Optional[str] = None):
    expected_key = db.get_setting("admin_secret_key") or config.ADMIN_SECRET_KEY
    if key != expected_key:
        raise HTTPException(status_code=403, detail="Доступ заборонено")
    
    saved_k = db.get_setting("gemini_api_key") or os.getenv("GEMINI_API_KEY", "")
    return {"gemini_key": saved_k}

class AlertsSaveKeyRequest(BaseModel):
    key: str
    alerts_api_key: str

@app.post("/api/admin/alerts/save-key")
async def save_alerts_key(req: AlertsSaveKeyRequest):
    expected_key = db.get_setting("admin_secret_key") or config.ADMIN_SECRET_KEY
    if req.key != expected_key:
        raise HTTPException(status_code=403, detail="Доступ заборонено")
    
    clean_k = req.alerts_api_key.strip()
    db.set_setting("alerts_api_key", clean_k)
    try:
        await turso_db.set_setting("alerts_api_key", clean_k)
    except Exception:
        pass
    # Force immediate refresh
    asyncio.create_task(alerts_service.fetch_external_alerts())
    return {"status": "ok", "saved": True}

@app.get("/api/admin/alerts/get-key")
async def get_alerts_key(key: Optional[str] = None):
    expected_key = db.get_setting("admin_secret_key") or config.ADMIN_SECRET_KEY
    if key != expected_key:
        raise HTTPException(status_code=403, detail="Доступ заборонено")
    
    saved_k = await turso_db.get_setting("alerts_api_key") or db.get_setting("alerts_api_key") or os.getenv("ALERTS_API_KEY", "")
    return {"alerts_api_key": saved_k}

@app.post("/api/admin/maintenance")
async def toggle_maintenance_mode(req: MaintenanceToggleRequest):
    expected_key = db.get_setting("admin_secret_key") or config.ADMIN_SECRET_KEY
    if req.key != expected_key:
        raise HTTPException(status_code=403, detail="Невірний секретний ключ адміністратора")
    
    current_state = await turso_db.get_maintenance_state()
    reason = req.reason if req.reason is not None else current_state.get("reason", "Тривають технічні роботи.")
    end_ts = req.end_timestamp if req.end_timestamp is not None else current_state.get("end_timestamp", 0)

    await turso_db.set_maintenance_state(
        is_enabled=req.enabled,
        reason=reason,
        end_timestamp=end_ts
    )
    
    return {
        "status": "ok",
        "maintenance_mode": req.enabled,
        "reason": reason,
        "end_timestamp": end_ts
    }

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "SkyWatch",
        "version": "2.2.0",
        "timestamp": time.time(),
        "telegram_connected": telegram_service.is_connected if telegram_service else False,
        "active_targets": len(deduplicator.get_all_active()),
        "simulator_active": simulator.is_running if simulator else False
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ConnectionManager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ConnectionManager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ConnectionManager.disconnect(websocket)

# --- CHANNELS & FOLDER REST API ---

@app.get("/api/channels")
async def get_channels():
    return {
        "folder_url": db.get_setting("folder_url", "https://t.me/addlist/syGYtBj5T9AxNzIy"),
        "channels": db.get_all_channels()
    }

class AddChannelRequest(BaseModel):
    title: str
    username: Optional[str] = None
    folder_url: Optional[str] = None

@app.post("/api/channels/add")
async def add_channel(req: AddChannelRequest):
    cid = db.add_channel(title=req.title, username=req.username, folder_url=req.folder_url)
    if telegram_service:
        await telegram_service.restart_listener()
    return {"status": "ok", "channel_id": cid}

class ToggleChannelRequest(BaseModel):
    channel_id: int
    is_active: bool

@app.post("/api/channels/toggle")
async def toggle_channel(req: ToggleChannelRequest):
    db.toggle_channel(req.channel_id, req.is_active)
    if telegram_service:
        await telegram_service.restart_listener()
    return {"status": "ok"}

@app.delete("/api/channels/{channel_id}")
async def delete_channel(channel_id: int):
    db.delete_channel(channel_id)
    if telegram_service:
        await telegram_service.restart_listener()
    return {"status": "ok"}

class FolderSyncRequest(BaseModel):
    folder_url: str

@app.post("/api/folder/sync")
async def sync_folder(req: FolderSyncRequest):
    """Parses and syncs a Telegram chatlist folder link (e.g. https://t.me/addlist/syGYtBj5T9AxNzIy)."""
    if not telegram_service:
        raise HTTPException(status_code=500, detail="Telegram service is not initialized")
    
    result = await telegram_service.sync_folder(req.folder_url)
    await ConnectionManager.broadcast({
        "type": "CHANNELS_UPDATE",
        "data": {
            "channels": db.get_all_channels(),
            "folder_url": req.folder_url
        },
        "timestamp": time.time()
    })
    return result

# --- TELEGRAM AUTHENTICATION REST API ---

@app.get("/api/telegram/status")
async def get_telegram_status():
    if telegram_service:
        return telegram_service.get_status()
    return {"is_connected": False, "is_authorized": False}

class TelegramCodeRequest(BaseModel):
    api_id: int
    api_hash: str
    phone: str

@app.post("/api/telegram/request-code")
async def request_telegram_code(req: TelegramCodeRequest):
    global telegram_service
    if not telegram_service:
        telegram_service = TelegramService(message_callback=on_message_received)
    result = await telegram_service.request_auth_code(req.api_id, req.api_hash, req.phone)
    return JSONResponse(content=result)

class TelegramLoginSubmit(BaseModel):
    code: str
    password_2fa: Optional[str] = None

@app.post("/api/telegram/login")
async def login_telegram(req: TelegramLoginSubmit):
    global telegram_service
    if not telegram_service:
        telegram_service = TelegramService(message_callback=on_message_received)
    result = await telegram_service.submit_auth_code(req.code, req.password_2fa)
    return JSONResponse(content=result)

# --- TARGETS, STATS & CONTROL API ---

@app.get("/api/logs")
async def get_logs(limit: int = 150):
    return {"logs": db.get_recent_logs(limit)}

@app.get("/api/targets")
async def get_targets():
    return {"targets": [t.model_dump() for t in deduplicator.get_all_active()]}

@app.get("/api/history")
async def get_history(limit: int = 50):
    return {"history": db.get_recent_threat_history(limit)}

@app.get("/api/stats")
async def get_stats():
    return db.get_system_stats()

class ManualInjectRequest(BaseModel):
    channel: str = "Manual Intercept"
    text: str

@app.post("/api/inject")
async def inject_message(req: ManualInjectRequest):
    raw_msg = RawTelegramMessage(
        channel=req.channel,
        message_id=int(time.time()),
        text=req.text,
        timestamp=time.time()
    )
    await on_message_received(raw_msg)
    return {"status": "ok"}

@app.post("/api/targets/{target_id}/neutralize")
async def neutralize_target(target_id: str):
    """Manually marks target as shot down / neutralized."""
    tgt = deduplicator.remove_target(target_id)
    if tgt:
        all_targets = [t.model_dump() for t in deduplicator.get_all_active()]
        await ConnectionManager.broadcast({
            "type": "TARGETS_UPDATE",
            "data": {
                "targets": all_targets,
                "neutralized_id": target_id
            },
            "timestamp": time.time()
        })
        return {"status": "neutralized", "target_id": target_id}
    return JSONResponse(status_code=404, content={"error": "Target not found"})

class SimulatorToggleRequest(BaseModel):
    enabled: bool

@app.post("/api/simulator/toggle")
async def toggle_simulator(req: SimulatorToggleRequest):
    if not simulator:
        raise HTTPException(status_code=500, detail="Simulator service not ready")
    if req.enabled:
        simulator.start()
    else:
        simulator.stop()
    return {"status": "ok", "simulator_active": simulator.is_running}

@app.get("/api/simulator/status")
async def get_simulator_status():
    return {"simulator_active": simulator.is_running if simulator else False}

@app.get("/api/neptun/status")
async def get_neptun_status():
    if neptun_service:
        return neptun_service.get_status()
    return {"enabled": False, "connected": False}

@app.post("/api/neptun/refresh")
async def refresh_neptun():
    if not neptun_service:
        raise HTTPException(status_code=500, detail="Neptun service not ready")
    await neptun_service.refresh_now()
    return {"status": "ok", "refreshed": True}

class NeptunToggleRequest(BaseModel):
    enabled: bool

@app.post("/api/neptun/toggle")
async def toggle_neptun(req: NeptunToggleRequest):
    if not neptun_service:
        raise HTTPException(status_code=500, detail="Neptun service not ready")
    
    if req.enabled:
        await neptun_service.start()
    else:
        await neptun_service.stop()

    # Save state to Turso Cloud DB and local DB
    val_str = "true" if req.enabled else "false"
    db.set_setting("neptun_enabled", val_str)
    try:
        await turso_db.set_setting("neptun_enabled", val_str)
    except Exception as te:
        logger.warning(f"Failed to persist Neptun state to Turso: {te}")

    return {"status": "ok", "neptun_active": neptun_service.is_enabled, "connected": neptun_service.is_connected}

@app.post("/api/clear")
async def clear_targets():
    deduplicator.clear_all()
    await ConnectionManager.broadcast({
        "type": "TARGETS_UPDATE",
        "data": {"targets": []},
        "timestamp": time.time()
    })
    return {"status": "cleared"}
