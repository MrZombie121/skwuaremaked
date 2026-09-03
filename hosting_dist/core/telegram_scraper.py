"""
Telegram Channel Scraper (Telethon) & Tactical Feed Simulator
Listens to real-time Telegram channels or generates authentic simulated feeds for testing.
"""
import asyncio
import logging
import random
import time
from typing import Callable, List, Optional
from core.models import RawTelegramMessage
import config

logger = logging.getLogger("SkyWatch.Scraper")

# Realistic simulation corpus with multiple channels reporting simultaneous tracks
SIMULATION_SCENARIOS = [
    # Scenario 1: Group of Shaheds from South towards Central Ukraine
    [
        {"channel": "Николаевский Ванёк", "text": "2х Шахеда повз Очаків курсом на Вознесенськ / Миколаївщина"},
        {"channel": "monitor", "text": "БПЛА типу «Shahed» в районі Очакова, вектор на північний захід (Вознесенськ)"},
        {"channel": "Повітряні Сили ЗСУ", "text": "Ударні БПЛА на півдні Миколаївщини курсом на північ!"},
    ],
    # Scenario 2: Jet UAV (RS) fast cruise over Kirovohrad / Cherkasy
    [
        {"channel": "eRadar", "text": "Реактивний БПЛА в районі Кропивницький, летить у бік Умань на великій швидкості!"},
        {"channel": "Николаевский Ванёк", "text": "1х реактивний дрон повз Кропивницький, курс західний на Черкащину (Умань)"},
    ],
    # Scenario 3: Shaheds in Sumy / Poltava region
    [
        {"channel": "Повітряні Сили ЗСУ", "text": "Група Шахедів на Сумщині в районі Конотоп, курсом на Ніжин / Чернігівщина"},
        {"channel": "monitor", "text": "3х БПЛА повз Конотоп, вектор руху західний (Ніжин)"},
        {"channel": "Радар", "text": "Конотоп — дрони курсом на Прилуки та Ніжин"},
    ],
    # Scenario 4: Fast Shahed track near Kremenchuk / Dnipro
    [
        {"channel": "Николаевский Ванёк", "text": "Шахед на межі Полтавської та Дніпропетровської в районі Кременчук, вектор на південь"},
        {"channel": "monitor", "text": "БПЛА над Кременчуком, маневрує у бік Кам'янське / Дніпро"},
    ],
    # Scenario 5: Cruise missile simulation
    [
        {"channel": "Повітряні Сили ЗСУ", "text": "Крилата ракета в напрямку Павлоград з південного сходу!"},
        {"channel": "monitor", "text": "Ракета в районі Павлоград, курс північно-західний на Полтавщину"},
    ],
    # Scenario 6: Shahed heading towards Kyiv Oblast
    [
        {"channel": "Повітряні Сили ЗСУ", "text": "Шахеди на півдні Київщини в районі Біла Церква, курс на захід (Вінниччина)"},
        {"channel": "Николаевский Ванёк", "text": "Біла Церква — дрони летять у бік Житомира / Вінниці"},
    ]
]

class TelegramScraperService:
    def __init__(self, message_callback: Callable[[RawTelegramMessage], None]):
        self.message_callback = message_callback
        self.telethon_client = None
        self.is_running = False
        self.is_simulation = False
        self._sim_task: Optional[asyncio.Task] = None

    async def start(self):
        """Starts either real Telegram listener or auto-falls back to simulation."""
        self.is_running = True
        
        # Check if Telegram credentials are provided
        if config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH:
            try:
                from telethon import TelegramClient, events
                logger.info("Initializing Telethon Telegram Client...")
                self.telethon_client = TelegramClient(
                    config.TELEGRAM_SESSION_NAME,
                    config.TELEGRAM_API_ID,
                    config.TELEGRAM_API_HASH
                )
                await self.telethon_client.start()

                @self.telethon_client.on(events.NewMessage(chats=config.MONITORED_CHANNELS))
                async def handler(event):
                    chat = await event.get_chat()
                    channel_title = getattr(chat, 'title', getattr(chat, 'username', 'Telegram'))
                    msg = RawTelegramMessage(
                        channel=channel_title,
                        message_id=event.id,
                        text=event.raw_text,
                        timestamp=time.time()
                    )
                    await self.message_callback(msg)

                logger.info(f"Telethon listening on channels: {config.MONITORED_CHANNELS}")
                return
            except Exception as e:
                logger.warning(f"Telethon initialization failed ({e}). Starting in Simulator Mode.")

        # Fallback to simulation mode
        logger.info("Starting in Simulator Mode (realistic multi-channel threat generator).")
        self.is_simulation = True
        self._sim_task = asyncio.create_task(self._run_simulation_loop())

    async def _run_simulation_loop(self):
        """Generates authentic multi-channel threat traffic with staggered delays."""
        logger.info("Simulation loop running...")
        msg_id = 1000
        while self.is_running:
            # Pick a scenario
            scenario = random.choice(SIMULATION_SCENARIOS)
            # Send reports from multiple channels with small delays (to trigger deduplication!)
            for report in scenario:
                if not self.is_running:
                    break
                msg_id += 1
                msg = RawTelegramMessage(
                    channel=report["channel"],
                    message_id=msg_id,
                    text=report["text"],
                    timestamp=time.time()
                )
                await self.message_callback(msg)
                # Small realistic delay between channels reporting the same threat
                await asyncio.sleep(random.uniform(1.0, 3.5))

            # Delay before next threat event scenario
            await asyncio.sleep(random.uniform(5.0, 9.0))

    async def inject_manual_message(self, channel: str, text: str):
        """Allows injecting test messages directly from UI or CLI."""
        msg = RawTelegramMessage(
            channel=channel,
            message_id=random.randint(10000, 99999),
            text=text,
            timestamp=time.time()
        )
        await self.message_callback(msg)

    async def stop(self):
        self.is_running = False
        if self._sim_task:
            self._sim_task.cancel()
        if self.telethon_client:
            await self.telethon_client.disconnect()
        logger.info("Telegram Scraper stopped.")
