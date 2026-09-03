"""
High-Sensitivity & Smart Multi-Threat NLP Extractor for Ukrainian Air Defense Telegram Channels
Extracts multiple simultaneous threats from composite multi-line military bulletins,
classifies target subtypes, speeds, directions, and detects target neutralization / all-clear signals.
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
        # 1. Exclusion Patterns (Fundraising, Donations, Bank cards, Commercial Ads)
        self.donation_pattern = re.compile(
            r'(\b(збір|збираємо|донат\w*|монобанк\w*|send\.monobank|приватбанк\w*|картка|гривень|\d+\s*грн|спорядження|берці|бронежилет\w*|амуніці\w*|пошир\w*\s+(?:цей\s+)?збір|підпишіться|підписуйтесь|реклам\w*)\b|\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b|send\.monobank\.ua)',
            re.IGNORECASE
        )

        # 2. Exclusion Patterns (News articles, analytical summaries, press)
        self.news_analytics_pattern = re.compile(
            r'\b(the\s+telegraph|the\s+times|reuters|bloomberg|статт\w*|виток\w*\s+документів|супутников\w*\s+знімк\w*|завод\w*\s+«?алабуг\w*|алабуг\w*|виробля\w*\s+тисяч|площ\w*\s+складів|підсумки\s+доби|зведення\s+за\s+добу|за\s+минулу\s+добу|за\s+минулу\s+ніч|у\s+нещодавніх\s+обстрілах|сьогодні\s+вранці\s+було|повідомляє\s+видання|інтерв\'ю|аналітик\w*|експерт\w*)\b',
            re.IGNORECASE
        )

        # 3. Exclusion Patterns (Greetings, casual chitchat without alert content)
        self.casual_chitchat_pattern = re.compile(
            r'^(доброго\s+ранку|добрий\s+ранок|на\s+добраніч|спокійної\s+ночі|гарного\s+дня|тихої\s+ночі|як\s+ви\??|як\s+справи\??)[!.,\s]*$',
            re.IGNORECASE
        )

        # Target Type RegEx Patterns with Subtype Resolution
        self.type_patterns = [
            (TargetType.BALLISTIC, re.compile(
                r'\b(іскандер-м|искандер-м|балістик\w*|балистик\w*|кинджал\w*|кинжал\w*|циркон\w*|с-300|с-400|х-22|х-32)\b',
                re.IGNORECASE
            )),
            (TargetType.KAB, re.compile(
                r'\b(каб\w*|фаб\w*|умпк|керован\w*\s+авіабомб\w*|пуск\w*\s+каб|скид\w*\s+каб)\b',
                re.IGNORECASE
            )),
            (TargetType.JET_UAV, re.compile(
                r'\b(реактивн\w*|швидкісн\w*|jet\s*uav|реактив\w*)\b',
                re.IGNORECASE
            )),
            (TargetType.MISSILE, re.compile(
                r'\b(ракет\w*|х-101|х-555|х-59|х-69|калібр\w*|крилат\w*|небезпека\s+по\s+ракетах)\b',
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
                r'\b(шахед\w*|шахід\w*|shahed\w*|геран\w*|мопед\w*|бпла|дрон\w*|бплa|безпілотник\w*)\b',
                re.IGNORECASE
            )),
        ]

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

        # Quantity detection regex (e.g. "2х", "3 шахеда", "пара", "група з 4")
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
        """Filters out non-tactical messages (donations, analytics, commercials, greetings)."""
        if self.donation_pattern.search(text):
            return True
        if self.news_analytics_pattern.search(text):
            return True
        if len(text) > 400 and not re.search(r'\b(увага|тривог\w*|небезпек\w*|курсом\s+на|вектор\w*|пуск\w*|шахед\w*|ракет\w*|летить|летять|вибух\w*)\b', text, re.I):
            return True
        if self.casual_chitchat_pattern.search(text.strip()):
            return True
        return False

    def extract_target_type(self, text: str) -> Tuple[TargetType, Optional[str]]:
        for target_type, pattern in self.type_patterns:
            m = pattern.search(text)
            if m:
                subtype = m.group(0).strip()
                return target_type, subtype
        return TargetType.SHAHED, "Shahed"

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
        if not text or len(text) < 2:
            return None

        # Check for target neutralization / clear signal
        is_clear = bool(self.clear_patterns.search(text))

        # 1. Geo Extraction (Current vs Destination)
        curr_geo, dest_geo = extract_current_and_destination(text)
        
        # Infer regional fallback from channel title if missing
        if not curr_geo and not dest_geo:
            inferred = infer_location_from_channel(source_channel)
            if inferred and len(text) < 180:
                curr_geo = inferred
            elif is_clear and reply_to_msg_id:
                return ParsedThreatEvent(
                    source_channel=source_channel,
                    message_id=message_id,
                    reply_to_msg_id=reply_to_msg_id,
                    raw_text=text,
                    target_type=TargetType.SHAHED,
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

        # 2. Target Type & Subtype & Quantity
        target_type, subtype = self.extract_target_type(text)
        qty = self.extract_quantity(text)
        altitude = self.extract_altitude(text)

        # 3. Flight Heading Vector & Destination Settlement Coordinates
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
        if not text or len(text.strip()) < 2:
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
