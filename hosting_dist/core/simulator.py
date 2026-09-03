"""
Tactical Air Threat Simulation Engine for SkyWatch
Generates authentic multi-channel threat traffic across Ukrainian regions with realistic
staggered delays, multi-channel correlation, reply updates, and air raid alert scenarios.
"""
import asyncio
import logging
import random
import time
from typing import Callable, List, Dict, Optional
from core.models import RawTelegramMessage

logger = logging.getLogger("SkyWatch.Simulator")

SIMULATION_SCENARIOS = [
    # Scenario 1: Group of Shaheds from Black Sea towards Odesa & Mykolaiv
    [
        {"channel": "Николаевский Ванёк", "text": "2х Шахеда з Моря до Овідіополю / Одеська область"},
        {"channel": "ОДЕССА ИНФО LIVE - радар Одеса", "text": "1х Санжейка, 10км. 2х Кружат в АЧМ. Вектор на Овідіополь та Чорноморськ"},
        {"channel": "War Monitor", "text": "2 шахеда з Чорного моря курсом на Овідіополь / Чорноморськ"},
    ],
    # Scenario 2: Jet UAV (RS) fast cruise over Kirovohrad / Cherkasy
    [
        {"channel": "🔊⚠️єРадар | Повітряна тривога | Ракетна небезпека", "text": "Реактивний БПЛА в районі Кропивницький, летить у бік Умань на великій швидкості!"},
        {"channel": "Николаевский Ванёк", "text": "1х реактивний дрон повз Кропивницький, курс західний на Черкащину (Умань)"},
        {"channel": "Оперативно → by HLEP", "text": "Швидкісний БпЛА типу «Jet» на межі Кіровоградщини та Черкащини у напрямку Умані"},
    ],
    # Scenario 3: Shaheds in Sumy / Poltava / Chernihiv region
    [
        {"channel": "Повітряні Сили ЗС України / Air Force of the Armed Forces of Ukraine", "text": "Група Шахедів на Сумщині в районі Конотоп, курсом на Ніжин / Чернігівщина"},
        {"channel": "Монітор Північ", "text": "3х БПЛА повз Конотоп, вектор руху західний на Ніжин та Прилуки"},
        {"channel": "112 | Чернігів", "text": "Конотоп — дрони курсом на Ніжин та Бахмач"},
    ],
    # Scenario 4: Fast Shahed track near Kremenchuk / Dnipro
    [
        {"channel": "ППО Полтава Нічні мисливці", "text": "Шахед на межі Полтавської та Дніпропетровської в районі Кременчук, вектор на південь"},
        {"channel": "monitor", "text": "БПЛА над Кременчуком, маневрує у бік Кам'янське / Дніпро"},
    ],
    # Scenario 5: Cruise missile & Ballistic launch simulation
    [
        {"channel": "Повітряні Сили ЗС України / Air Force of the Armed Forces of Ukraine", "text": "🚨 Крилата ракета в напрямку Павлоград з південного сходу!"},
        {"channel": "Повітряний простір | Напрямок ракет", "text": "Ракета в районі Павлоград, курс північно-західний на Полтавщину (Миргород)"},
    ],
    # Scenario 6: Shahed heading towards Kyiv Oblast
    [
        {"channel": "Повітряні Сили ЗС України / Air Force of the Armed Forces of Ukraine", "text": "Шахеди на півдні Київщини в районі Біла Церква, курс на захід (Фастів)"},
        {"channel": "Николаевский Ванёк", "text": "Біла Церква — дрони летять у бік Фастова / Житомирщини"},
    ],
    # Scenario 7: Zaporizhzhia Tactical FPV and Shahed movement
    [
        {"channel": "▵ 👁Очі Всюди🇺🇦▵", "text": "БпЛА руслом на Розумівку."},
        {"channel": "∆✙🍒Гнила черешня🇺🇦✙∆", "text": "Шахед повз Розумівку у бік Запоріжжя (Кічкас / Піски)"},
        {"channel": "Повітряні Сили ЗС України / Air Force of the Armed Forces of Ukraine", "text": "🛵 м.Запоріжжя: БпЛА в напрямку міста з півдня"},
    ],
    # Scenario 8: Frontline KAB strike alert
    [
        {"channel": "Херсонщина Моніторинг🇺🇦", "text": "Пуски КАБ тактичною авіацією в напрямку Берислав / Тягинка!"},
        {"channel": "Николаевский Ванёк", "text": "КАБ на Бериславський район"},
    ],
    # Scenario 9: Multi-threat Composite Bulletin
    [
        {
            "channel": "Повітряний простір | Напрямок ракет",
            "text": "Миколаївщина:\n2х реактивних БпЛА курсом на Очаків з акваторії Чорного моря.\n\nЗапорізька область:\n1х БпЛА над Запоріжжям.\n\nДніпропетровщина:\n1х БпЛА в районі Першотравенську.\n\nХарківщина:\n1х БпЛА на околицях Краснокутська."
        }
    ]
]

class TacticalSimulator:
    def __init__(self, message_callback: Callable[[RawTelegramMessage], Any]):
        self.message_callback = message_callback
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self._msg_id_counter = 500000

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Tactical Simulator started.")

    def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
        logger.info("Tactical Simulator stopped.")

    async def _run_loop(self):
        logger.info("Tactical simulation scenario loop active.")
        while self.is_running:
            scenario = random.choice(SIMULATION_SCENARIOS)
            
            for report in scenario:
                if not self.is_running:
                    break
                self._msg_id_counter += 1
                msg = RawTelegramMessage(
                    channel=report["channel"],
                    message_id=self._msg_id_counter,
                    text=report["text"],
                    timestamp=time.time()
                )
                await self.message_callback(msg)
                await asyncio.sleep(random.uniform(1.2, 3.5))

            await asyncio.sleep(random.uniform(8.0, 15.0))
