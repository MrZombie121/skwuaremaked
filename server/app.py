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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("SkyWatch.Server")

app = FastAPI(title="SkyWatch Tactical Air Threat Radar", version="2.0.0")

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
        if path.endswith(('.png', '.jpg', '.jpeg', '.svg', '.woff2', '.woff', '.css', '.js')):
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
        
        # Send initial state snapshot with recent logs
        targets = [t.model_dump() for t in deduplicator.get_all_active()]
        channels = db.get_all_channels()
        tg_status = telegram_service.get_status() if telegram_service else {}
        sim_status = simulator.is_running if simulator else False
        recent_logs = db.get_recent_logs(60)
        
        await websocket.send_json({
            "type": "INITIAL_STATE",
            "data": {
                "targets": targets,
                "channels": channels,
                "telegram": tg_status,
                "simulator_active": sim_status,
                "logs": recent_logs,
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
    # Skip any messages published more than 10 minutes (600 seconds) ago
    if (time.time() - raw_msg.timestamp) > 600:
        logger.debug(f"Skipping message older than 10 minutes ({int(time.time() - raw_msg.timestamp)}s)")
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
        
        if active or expired:
            targets_dump = [t.model_dump() for t in deduplicator.get_all_active()]
            await ConnectionManager.broadcast({
                "type": "KINEMATIC_TICK",
                "data": {
                    "targets": targets_dump,
                    "expired_ids": expired
                },
                "timestamp": time.time()
            })

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

    asyncio.create_task(kinematic_loop())
    logger.info("SkyWatch Backend Engine v2.0 initialized successfully.")

@app.on_event("shutdown")
async def shutdown_event():
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

@app.post("/api/admin/generate-report")
async def generate_admin_report(req: GeminiReportRequest):
    expected_key = db.get_setting("admin_secret_key") or config.ADMIN_SECRET_KEY
    if req.key != expected_key:
        raise HTTPException(status_code=403, detail="Доступ заборонено: невірний секретний ключ адміністратора")
    
    targets = deduplicator.get_all_active()
    result = await gemini_analyst.generate_tactical_summary(targets)
    return result

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
        "version": "2.0.0",
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
async def get_logs(limit: int = 60):
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
