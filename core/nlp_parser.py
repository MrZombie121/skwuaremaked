"""
High-Sensitivity & Smart Tactical Air Defense Threat NLP Parser for Ukrainian Telegram Channels
Strictly filters out non-tactical news, energy/power outage reports, chatter, prices, and past summaries.
Extracts multiple simultaneous threats from composite bulletins with explicit flight vector verification.
"""
import math
import re
import time
from typing import Optional, List, Dict, Any, Tuple
from core.models import TargetType, HeadingDirection, DIRECTION_ANGLES, ParsedThreatEvent
from core.geo_engine import (
    extract_current_and_destination,
    lookup_location,
    compute_spherical_bearing,
    infer_location_from_channel
)

class TelegramThreatParser:
    def __init__(self):
        # 1. Exclusion Patterns: Fundraising, Donations, Commercials
        self.donation_pattern = re.compile(
            r'(\b(збір|збираємо|донат\w*|монобанк\w*|send\.monobank|приватбанк\w*|картка|гривень|\d+\s*грн|спорядження|берці|бронежилет\w*|амуніці\w*|пошир\w*\s+(?:цей\s+)?збір|підпишіться|підписуйтесь|реклам\w*)\b|\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b|send\.monobank\.ua)',
            re.IGNORECASE
        )

        # 2. Exclusion Patterns: Power outages, DTEK, Utilities, Energy, Heat, Water
        self.energy_utilities_pattern = re.compile(
            r'\b(дтек|dtek|укренерго|відключен\w*\s+світл\w*|відключення\s+електроенергії|електроенергі\w*|електропостачан\w*|графік\w*\s+відключень|аварійні\s+відключення|де\s+світло|де\s+свет|без\s+світла|без\s+света|блекаут|знеструмлен\w*|комуналк\w*|опаленн\w*|водопостачан\w*|енергетик\w*|енергооб\'?єкт\w*|економити\s+електроенергію)\b',
            re.IGNORECASE
        )

        # 3. Exclusion Patterns: Chatter, discussions, prices, real estate, humor, weather
        self.chatter_discussion_pattern = re.compile(
            r'\b(квартир\w*|оренд\w*|цін\w*|долар\w*|євро|валют\w*|грош\w*|погод\w*|температур\w*|градус\w*|анекдот\w*|мем\w*|рекорд\w*|гінес\w*|кастрюл\w*|піздец\w*|аху\w*|тижнет\w*|без\s+комментариев|без\s+коментарів)\b',
            re.IGNORECASE
        )

        # 4. Exclusion Patterns: Historical summaries & aftermath of past attacks
        self.past_aftermath_pattern = re.compile(
            r'\b(після\s+нічної\s+атаки|після\s+обстрілу|наслідки\s+атаки|були\s+пошкоджені|пошкоджен\w*\s+об\'?єкт\w*|відновлювальн\w*\s+робот\w*|ліквідаці\w*\s+наслідків|за\s+минулу\s+добу|зведення\s+за\s+добу|підсумки\s+доби|сьогодні\s+вранці\s+було|the\s+telegraph|reuters|bloomberg|інтерв\'ю|аналітик\w*)\b',
            re.IGNORECASE
        )

        # 5. Exclusion Patterns: Greetings & casual check-ins
        self.casual_greetings_pattern = re.compile(
            r'^(доброго\s+ранку|добрий\s+ранок|на\s+добраніч|спокійної\s+ночі|гарного\s+дня|тихої\s+ночі|як\s+ви\??|як\s+справи\??)[!.,\s]*$',
            re.IGNORECASE
        )

        # 6. Exclusion Patterns: Threat Absence & Calm Status Summaries ("немає активності", "ракетоносії відсутні", "без запусків")
        self.threat_absence_negation_pattern = re.compile(
            r'\b(бойової\s+активност\w*\s+(?:наразі\s+)?нема[єе]|активност\w*\s+(?:стратегічної\s+авіації\s+|ворога\s+)?нема[єе]|ракетоносі[їі]\s+відсутн[іі]|нема[єе]\s+(?:запуск\w*|шахед\w*|ракет\w*|активності)|запуск\w*\s+шахед\w*\s+наразі\s+нема[єе]|відсутн[іі]\s+у\s+морі|не\s+зафіксовано|не\s+спостеріга[єе]ться|без\s+активност\w*|без\s+запуск\w*|загрози\s+на\s+ніч\s+на\s+середньому|на\s+середньому\s+рівні|прогноз\s+на\s+ніч|наразі\s+тихо|наразі\s+чисто|обстановка\s+спокійна|станом\s+на\s+зараз\s+(?:все\s+)?спокійно)\b',
            re.IGNORECASE
        )

        # Explicit Airborne Threat Keywords
        self.type_patterns = [
            (TargetType.BALLISTIC, re.compile(
                r'\b(іскандер-м|искандер-м|балістик\w*|балистик\w*|кинджал\w*|кинжал\w*|циркон\w*|с-300|с-400|х-22|х-32)\b',
                re.IGNORECASE
            )),
            (TargetType.KAB, re.compile(
                r'\b(каб\w*|фаб\w*|умпк|керован\w*\s+авіабомб\w*|пуск\w*\s+каб|скид\w*\s+каб|скид\w*)\b',
                re.IGNORECASE
            )),
            (TargetType.MISSILE, re.compile(
                r'\b(ракет\w*|х-101|х-555|х-59|х-69|калібр\w*|крилат\w*|небезпека\s+по\s+ракетах|пуск\s+ракети|пуски\s+ракет|пуск|пуски)\b',
                re.IGNORECASE
            )),
            (TargetType.AIRCRAFT, re.compile(
                r'\b(су-34\w*|су-35\w*|су-30\w*|су-24\w*|су-25\w*|су-57\w*|міг-31\w*|миг-31\w*|ту-95\w*|ту-22\w*|ту-160\w*|активність\s+та\b|активність\s+тактичної\s+авіації|тактичн\w*\s+авіаці\w*|стратегічн\w*\s+авіаці\w*|борт\w*\s+та\b|літак\w*\s+та\b|\bта\b\s+в\s+морі|\bта\b\s+над\s+морем|йде\s+на\s+пусков\w*|на\s+рубежах\s+пуск\w*|виліт\w*\s+су|виліт\w*\s+борт\w*)\b',
                re.IGNORECASE
            )),
            (TargetType.JET_UAV, re.compile(
                r'\b(реактивн\w*|швидкісн\w*\s+бпла|швидкісн\w*\s+дрон|jet\s*uav|реактив\w*)\b',
                re.IGNORECASE
            )),
            (TargetType.MISSILE, re.compile(
                r'\b(ракет\w*|х-101|х-555|х-59|х-69|калібр\w*|крилат\w*|небезпека\s+по\s+ракетах|пуск|пуски|пуски\s+ракет|пуск\s+ракети)\b',
                re.IGNORECASE
            )),
            (TargetType.RECON, re.compile(
                r'\b(розвідник\w*|орлан\w*|supercam\w*|zala\w*|мерлін\w*|ланцет\w*)\b',
                re.IGNORECASE
            )),
            (TargetType.FPV, re.compile(
                r'\b(fpv\w*|фпв\w*|дрон-камікадзе\s+fpv|акустика\s+fpv)\b',
                re.IGNORECASE
            )),
            (TargetType.DECOY, re.compile(
                r'\b(фальш\w*|приманка|гербер\w*|пароді\w*|імітатор\w*)\b',
                re.IGNORECASE
            )),
            (TargetType.SHAHED, re.compile(
                r'\b(шахед\w*|шахід\w*|shahed\w*|геран\w*|мопед\w*|бпла|дрон\w*|бплa|безпілотник\w*|повітрян\w*\s+ціл\w*|швидкісн\w*\s+ціл\w*)\b',
                re.IGNORECASE
            )),
        ]

        # Explicit Flight Action / Vector Verifiers
        self.flight_action_pattern = re.compile(
            r'\b(курсом\s+на|вектор\s+на|в\s+напрямку|у\s+напрямку|напрямок\s+|летить\s+на|летять\s+на|рух\s+у\s+напрямку|руха[єе]ться\s+на|на\s+підльоті|підлітає|заходить\s+на|пролітає|транзитом\s+повз|вздовж\s+русла|руслом\s+на|з\s+моря\s+до|з\s+акваторії|пуск\w*|скид\w*|тривога\s+по|загроза\s+по|укриття|чисто|відбій|збито|мінус|локаційно\s+втрачено)\b',
            re.IGNORECASE
        )

        # Direction patterns (Ukrainian & Russian)
        self.direction_patterns = [
            (HeadingDirection.NW, re.compile(r'\b(північн\w*-західн\w*|северо-западн\w*|на\s+пз|пн-зх|курсом\s+на\s+пн-зх|пз|сз)\b', re.I)),
            (HeadingDirection.NE, re.compile(r'\b(північн\w*-східн\w*|северо-восточн\w*|на\s+пс|пн-сх|курсом\s+на\s+пн-сх|пс|св)\b', re.I)),
            (HeadingDirection.SW, re.compile(r'\b(південн\w*-західн\w*|юго-западн\w*|на\s+пд-зх|курсом\s+на\s+пд-зх|пд-зх|юз)\b', re.I)),
            (HeadingDirection.SE, re.compile(r'\b(південн\w*-східн\w*|юго-восточн\w*|на\s+пд-сх|курсом\s+на\s+пд-сх|пд-сх|юв)\b', re.I)),
            (HeadingDirection.N,  re.compile(r'\b(північн\w*|северн\w*|на\s+північ|на\s+север|пн|курсом\s+на\s+пн)\b', re.I)),
            (HeadingDirection.S,  re.compile(r'\b(південн\w*|южн\w*|на\s+південь|на\s+юг|пд|курсом\s+на\s+пд)\b', re.I)),
            (HeadingDirection.W,  re.compile(r'\b(західн\w*|западн\w*|на\s+захід|на\s+запад|зх|курсом\s+на\s+зх)\b', re.I)),
            (HeadingDirection.E,  re.compile(r'\b(східн\w*|восточн\w*|на\s+схід|на\s+восток|сх|курсом\s+на\s+сх)\b', re.I)),
        ]

        # Quantity detection regex
        self.qty_patterns = [
            re.compile(r'(\d+)\s*(?:х|x|шт|од)?\s*(?:шахед|бпла|ракет|ціл|дрон|реактив|каб)', re.I),
            re.compile(r'(?:група|скупчення)\s*(?:з\s*)?(\d+)', re.I),
            re.compile(r'(\d+)\s*(?:х|x)\b', re.I),
            re.compile(r'\b(\d+)\s*(?:шт|од|штук)\b', re.I),
        ]

        # Target destroyed / clear keywords
        self.clear_patterns = re.compile(
            r'\b(чисто|відбій|збито|знищено|мінус|локаційно\s+втрачено|посадили|дорозвідка|перестав\s+існувати|припинено\s+існування)\b',
            re.IGNORECASE
        )

        # Altitude keywords
        self.altitude_patterns = [
            (re.compile(r'\b(надназьк\w*|наднизьк\w*|низько|бриючий)\b', re.I), "Низька (<150м)"),
            (re.compile(r'\b(високо|велика\s+висота)\b', re.I), "Велика (>2000м)"),
            (re.compile(r'(\d{2,4})\s*м(?:етр\w*)?', re.I), r'\1 м')
        ]

    def is_non_tactical_message(self, text: str) -> bool:
        """Strictly identifies and filters out non-tactical messages."""
        if not text or len(text.strip()) < 3:
            return True
        if self.donation_pattern.search(text):
            return True
        if self.energy_utilities_pattern.search(text):
            return True
        if self.chatter_discussion_pattern.search(text):
            return True
        if self.past_aftermath_pattern.search(text):
            return True
        if self.casual_greetings_pattern.search(text.strip()):
            return True
        if self.threat_absence_negation_pattern.search(text):
            return True
        return False

    def extract_target_type(self, text: str) -> Optional[Tuple[TargetType, str]]:
        """Extracts explicit threat type or returns None if no threat keywords present."""
        for target_type, pattern in self.type_patterns:
            m = pattern.search(text)
            if m:
                subtype = m.group(0).strip()
                return target_type, subtype
        return None

    def extract_quantity(self, text: str) -> int:
        for pattern in self.qty_patterns:
            match = pattern.search(text)
            if match:
                try:
                    return max(1, min(int(match.group(1)), 30))
                except (ValueError, IndexError):
                    pass
        if "пара" in text.lower():
            return 2
        return 1

    def extract_altitude(self, text: str) -> Optional[str]:
        for pattern, label in self.altitude_patterns:
            m = pattern.search(text)
            if m:
                return label if isinstance(label, str) else m.group(0)
        return None

    def _angle_to_heading(self, angle_deg: float) -> HeadingDirection:
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
        return HeadingDirection.NW

    def parse_single_clause(
        self,
        clause_text: str,
        source_channel: str = "Telegram",
        message_id: Optional[int] = None,
        reply_to_msg_id: Optional[int] = None
    ) -> Optional[ParsedThreatEvent]:
        """Parses a single threat sentence or line into a ParsedThreatEvent."""
        text = clause_text.strip()
        if not text or len(text) < 3:
            return None

        # Filter out non-tactical messages (DTEK, utilities, chatter, past aftermath)
        if self.is_non_tactical_message(text):
            return None

        # Check for target neutralization / clear signal
        is_clear = bool(self.clear_patterns.search(text))

        # Check for explicit threat keywords
        type_res = self.extract_target_type(text)
        has_flight_action = bool(self.flight_action_pattern.search(text))

        # If there is NO threat keyword AND NO flight action/alert semantic, REJECT!
        if not type_res and not has_flight_action and not is_clear:
            return None

        target_type, subtype = type_res if type_res else (TargetType.SHAHED, "Shahed")

        # 1. Geo Extraction (Current vs Destination)
        curr_geo, dest_geo = extract_current_and_destination(text)
        
        if not curr_geo and not dest_geo:
            inferred = infer_location_from_channel(source_channel)
            if inferred and (type_res or has_flight_action) and len(text) < 140:
                curr_geo = inferred
            elif is_clear and reply_to_msg_id:
                return ParsedThreatEvent(
                    source_channel=source_channel,
                    message_id=message_id,
                    reply_to_msg_id=reply_to_msg_id,
                    raw_text=text,
                    target_type=target_type,
                    location_name="Unknown",
                    lat=0.0,
                    lon=0.0,
                    is_clear_signal=True,
                    timestamp=time.time()
                )
            else:
                return None

        loc_tuple = dest_geo or curr_geo
        loc_name, lat, lon, region = loc_tuple

        qty = self.extract_quantity(text)
        altitude = self.extract_altitude(text)

        # 2. Flight Heading Vector & Destination Settlement Coordinates
        heading = HeadingDirection.UNKNOWN
        heading_deg = 0.0
        destination_name = None
        dest_lat = None
        dest_lon = None

        if curr_geo and dest_geo and curr_geo != dest_geo:
            destination_name = dest_geo[0]
            dest_lat = dest_geo[1]
            dest_lon = dest_geo[2]
            heading_deg = compute_spherical_bearing(curr_geo[1], curr_geo[2], dest_lat, dest_lon)
            heading = self._angle_to_heading(heading_deg)
            lat, lon = curr_geo[1], curr_geo[2]
            loc_name = curr_geo[0]
        elif dest_geo:
            destination_name = dest_geo[0]
            dest_lat = dest_geo[1]
            dest_lon = dest_geo[2]
            
            text_lower = text.lower()
            for h_enum, pattern in self.direction_patterns:
                if pattern.search(text_lower):
                    heading = h_enum
                    heading_deg = DIRECTION_ANGLES.get(h_enum, 0.0)
                    break
        else:
            destination_name = loc_name
            dest_lat = lat
            dest_lon = lon
            text_lower = text.lower()
            for h_enum, pattern in self.direction_patterns:
                if pattern.search(text_lower):
                    heading = h_enum
                    heading_deg = DIRECTION_ANGLES.get(h_enum, 0.0)
                    break

        return ParsedThreatEvent(
            source_channel=source_channel,
            message_id=message_id,
            reply_to_msg_id=reply_to_msg_id,
            raw_text=text,
            target_type=target_type,
            target_subtype=subtype,
            target_count=qty,
            location_name=loc_name,
            region_name=region,
            lat=lat,
            lon=lon,
            heading=heading,
            heading_deg=heading_deg,
            destination=destination_name,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            is_clear_signal=is_clear,
            altitude_info=altitude,
            timestamp=time.time(),
            confidence=0.92
        )

    def parse_message_multi(
        self,
        text: str,
        source_channel: str = "Telegram",
        message_id: Optional[int] = None,
        reply_to_msg_id: Optional[int] = None
    ) -> List[ParsedThreatEvent]:
        """
        Parses composite military bulletins that contain multiple simultaneous threat reports across lines.
        Returns: List of ParsedThreatEvent objects.
        """
        if not text or len(text.strip()) < 3:
            return []

        if self.is_non_tactical_message(text):
            return []

        # Split into distinct threat segments by newlines or bullet points
        lines = [line.strip() for line in re.split(r'[\r\n]+|[;•\u2022\u25b6\u25aa\u25b8]', text) if line.strip()]
        
        events = []
        for line in lines:
            if len(line) < 4:
                continue
            ev = self.parse_single_clause(line, source_channel, message_id, reply_to_msg_id)
            if ev:
                events.append(ev)

        # If multi-line split yielded nothing, attempt parsing the full text as single clause
        if not events:
            single = self.parse_single_clause(text, source_channel, message_id, reply_to_msg_id)
            if single:
                events.append(single)

        return events

    def parse_message(
        self,
        text: str,
        source_channel: str = "Telegram",
        message_id: Optional[int] = None,
        reply_to_msg_id: Optional[int] = None
    ) -> Optional[ParsedThreatEvent]:
        """Backward-compatible single-event parser entrypoint."""
        events = self.parse_message_multi(text, source_channel, message_id, reply_to_msg_id)
        return events[0] if events else None
