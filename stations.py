"""
In-memory "who's where" station database, built from unconnected
APRS position-report traffic seen on the KAM-XL's MONITOR stream.

Milestone 7 (APRS mapping). In-memory only, by explicit design choice
(offered against persisting to SQLite; in-memory won for the MVP) --
restarting the daemon starts the station list over empty, and it
rebuilds naturally as new position traffic arrives, the same way a
real APRS station's own screen would after a power cycle.
Each station retains its latest known position and up to 25 distinct
consecutive positions for map trails. History is held in memory only
and is discarded when the station expires or the daemon restarts.

StationTracker.update() is meant to be fed every Packet the daemon's
monitor loop sees (see kamxl_daemon.py) -- it's cheap to call for
every packet, including non-APRS ones, since it just returns None
immediately for anything that isn't a decodable position report.
"""

import dataclasses
import time

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from aprs import parse_position, parse_weather
from packet import Packet

@dataclass(frozen=True)
class PositionPoint:
    """One APRS position in a station's trail."""

    latitude: float
    longitude: float
    timestamp: float

@dataclass(frozen=True)
class PacketRecord:
    """One recently received APRS UI packet."""

    timestamp: float
    destination: str
    digipeaters: Tuple[str, ...]
    port: int
    payload: str
    raw: str
    frame_type: Optional[str]

@dataclass(frozen=True)
class Station:
    """
    The latest known state of one station.

    ``callsign`` includes the SSID if the packet's source had one
    (e.g. "N5BA-9") -- different SSIDs are treated as distinct
    stations, per normal APRS convention (a callsign's SSID
    conventionally identifies a specific device/use, e.g. -9 for a
    mobile tracker vs the base -1). ``last_heard`` is a time.time()
    epoch timestamp of the packet that produced this state.
    ``packet_count`` is how many position reports this station has
    contributed since the tracker was created (i.e. since the daemon
    last started).
    """

    callsign: str
    latitude: float
    longitude: float
    symbol_table: str
    symbol_code: str
    comment: str
    last_heard: float
    packet_count: int
    history: Tuple[PositionPoint, ...] = ()
    recent_packets: Tuple[PacketRecord, ...] = ()
    # Latest decoded weather (aprs.WeatherReport as a dict) and when it
    # was heard; None for stations that never sent weather.
    weather: Optional[dict] = None
    weather_at: Optional[float] = None

class StationTracker:
    """
    Decodes APRS position reports out of Packets and maintains one
    Station per source callsign, retaining the latest report and a
    bounded history of up to 25 distinct positions.

    Only ordinary AX.25 UI frames (Packet.frame_type is None or "UI")
    are ever considered -- connect-session control/supervisory frames
    (frame_type like "C", "I00", "rr1", "UA", "D", ...) are AX.25
    link-layer chatter, never APRS payloads, so attempting to parse
    them would be pure wasted work at best and a false decode at
    worst. This check happens before parse_position() is even called.
    """

    # Weather from a station we have no position for yet is held this
    # long, waiting for its first position beacon.
    PENDING_WEATHER_MAX_AGE = 3600
    PENDING_WEATHER_MAX_STATIONS = 500

    def __init__(self) -> None:
        self._stations: Dict[str, Station] = {}
        self._pending_weather: Dict[str, Tuple[dict, float]] = {}

    def update(
        self, packet: Packet, now: Optional[float] = None
    ) -> Optional[Station]:
        """
        Feed one Packet in.

        Returns the resulting Station if the packet's payload decoded
        as an APRS position report, None otherwise (not a UI frame,
        not APRS, or a position format not yet supported -- e.g.
        -- see aprs.py).
        """
        if packet.frame_type not in (None, "UI"):
            return None

        if now is None:
            now = time.time()

        existing = self._stations.get(packet.source)
        position = parse_position(
            packet.payload, destination=packet.destination
        )

        weather_report = parse_weather(packet.payload, position)
        weather = (
            dataclasses.asdict(weather_report)
            if weather_report is not None else None
        )

        # A station must first be established by a valid position
        # report. Non-position APRS packets can then be retained --
        # weather from a positionless report (e.g. "_10172218c051...")
        # is held until that station's position beacon arrives.
        if position is None and existing is None:
            if weather is not None:
                self._remember_pending_weather(packet.source, weather, now)
            return None

        record = PacketRecord(
            timestamp=now,
            destination=packet.destination,
            digipeaters=packet.digipeaters,
            port=packet.port,
            payload=packet.payload,
            raw=packet.raw,
            frame_type=packet.frame_type,
        )

        recent_packets = (
            (*(existing.recent_packets if existing else ()), record)
        )[-25:]

        if position is None:
            changes = {"recent_packets": recent_packets}
            if weather is not None:
                changes.update(weather=weather, weather_at=now)
            station = dataclasses.replace(existing, **changes)
            self._stations[packet.source] = station
            return station

        existing = self._stations.get(packet.source)
        packet_count = (existing.packet_count + 1) if existing else 1
        history = existing.history if existing else ()

        point = PositionPoint(
            latitude=position.latitude,
            longitude=position.longitude,
            timestamp=now,
        )

        # Avoid adding identical consecutive positions.
        if (
            not history
            or history[-1].latitude != point.latitude
            or history[-1].longitude != point.longitude
        ):
            history = (*history, point)[-25:]

        # Weather: from this packet if it carried any, else keep the
        # last known (or a pending positionless report).
        weather_at = now if weather is not None else None
        if weather is None and existing is not None:
            weather, weather_at = existing.weather, existing.weather_at
        if weather is None:
            pending = self._pending_weather.pop(packet.source, None)
            if pending and now - pending[1] <= self.PENDING_WEATHER_MAX_AGE:
                weather, weather_at = pending

        station = Station(
            callsign=packet.source,
            latitude=position.latitude,
            longitude=position.longitude,
            symbol_table=position.symbol_table,
            symbol_code=position.symbol_code,
            comment=position.comment,
            last_heard=now,
            packet_count=packet_count,
            history=history,
            recent_packets=recent_packets,
            weather=weather,
            weather_at=weather_at,
        )

        self._stations[packet.source] = station

        return station

    def _remember_pending_weather(
        self, callsign: str, weather: dict, now: float
    ) -> None:
        self._pending_weather[callsign] = (weather, now)
        if len(self._pending_weather) > self.PENDING_WEATHER_MAX_STATIONS:
            oldest = min(
                self._pending_weather, key=lambda c: self._pending_weather[c][1]
            )
            del self._pending_weather[oldest]

    def list_stations(self) -> List[Station]:
        """
        All known stations, sorted by callsign for stable output.
        """
        return sorted(
            self._stations.values(), key=lambda station: station.callsign
        )

    def get_station(self, callsign: str) -> Optional[Station]:
        return self._stations.get(callsign)

    def expire_stale(
        self, max_age_seconds: float, now: Optional[float] = None
    ) -> int:
        """Remove stations older than the specified age.

        Returns the number of stations removed.
        """
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")

        if now is None:
            now = time.time()

        expired = [
            callsign
            for callsign, station in self._stations.items()
            if now - station.last_heard > max_age_seconds
        ]

        for callsign in expired:
            del self._stations[callsign]

        return len(expired)
