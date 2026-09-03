"""
SKYWATCH — Standalone Web Version Server
Dedicated Air Threat Radar Web Service accessible via any browser
(PC, Mac, iPhone, Android, Tablet, LAN/WAN).
"""
import argparse
import asyncio
import logging
import os
import socket
import sys

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import threading
import time
import webbrowser
import uvicorn

import config
from core.db import db

def get_lan_ip() -> str:
    """Returns local network IP for accessing from phone/tablet on same Wi-Fi."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def print_banner(host: str, port: int):
    lan_ip = get_lan_ip()
    channels_count = len(db.get_all_channels(only_active=True))
    user_phone = db.get_setting("tg_phone") or "Authorized"
    has_session = bool(db.get_setting("session_string"))

    print("\n" + "=" * 75)
    print("  🛰️  SKYWATCH — ТАКТИЧНИЙ ВЕБ-РАДАР ПОВІТРЯНИХ ЗАГРОЗ УКРАЇНИ v2.0")
    print("=" * 75)
    print(f"  🌐 Локальний доступ:    http://localhost:{port}")
    if lan_ip != "127.0.0.1" and host in ("0.0.0.0", lan_ip):
        print(f"  📱 Доступ з телефону/LAN: http://{lan_ip}:{port}")
    print(f"  📡 Підключено каналів:  {channels_count} каналів (з папки 'Для монітору')")
    print(f"  🔑 Telegram статус:     {'🟢 ПІДКЛЮЧЕНО (' + user_phone + ')' if has_session else '🟠 ОЧІКУЄ АВТОРИЗАЦІЇ'}")
    print("=" * 75)
    print("  [*] Сервер працює у режимі Web-сервісу (FastAPI + WebSockets)")
    print("  [*] Натисніть Ctrl+C для зупинки сервера.\n")

def main():
    parser = argparse.ArgumentParser(description="SkyWatch Web Version Runner")
    parser.add_argument("--host", default=config.SERVER_HOST, help="Host IP to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=config.SERVER_PORT, help="Port to listen on (default: 8080)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open web browser")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")

    args = parser.parse_args()

    print_banner(args.host, args.port)

    # Open browser automatically if not disabled and running locally
    if not args.no_browser:
        target_url = f"http://localhost:{args.port}"
        def open_browser():
            time.sleep(1.2)
            webbrowser.open(target_url)
        threading.Thread(target=open_browser, daemon=True).start()

    # Run Uvicorn Web Server
    uvicorn.run(
        "server.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        access_log=False
    )

if __name__ == "__main__":
    main()
