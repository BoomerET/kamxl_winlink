"""
In-memory "who's where" station database, built from unconnected
APRS position-report traffic seen on the KAM-XL's MONITOR stream.

Milestone 7 (APRS mapping). In-memory only, by explicit design choice
(offered against persisting to SQLite; in-memory won for the MVP) --
restarting the daemon starts the station list over empty, and it
rebuilds naturally as new position traffic arrives, the same way a
real APRS station's own screen would after a power cycle. No history
is kept beyond each station's single latest-known position.

StationTracker.update() is meant to be fed every Packet the daemon's
monitor loop sees (see kamxl_daemon.py) -- it's cheap to call for
every packet, including non-APRS ones, since it just returns None
immediately for anything that isn't a decodable position report.
"""

import time

from dataclasses import dataclass
from typing import Dict, List, Optional

from aprs import parse_position
from packet import Packet


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


class StationTracker:
    """
    Decodes APRS position reports out of Packets and maintains one
    Station per source callsign, keeping only the most recent report.

    Only ordinary AX.25 UI frames (Packet.frame_type is None or "UI")
    are ever considered -- connect-session control/supervisory frames
    (frame_type like "C", "I00", "rr1", "UA", "D", ...) are AX.25
    link-layer chatter, never APRS payloads, so attempting to parse
    them would be pure wasted work at best and a false decode at
    worst. This check happens before parse_position() is even called.
    """

    def __init__(self) -> None:
        self._stations: Dict[str, Station] = {}

    def update(
        self, packet: Packet, now: Optional[float] = None
    ) -> Optional[Station]:
        """
        Feed one Packet in.

        Returns the resulting Station if the packet's payload decoded
        as an APRS position report, None otherwise (not a UI frame,
        not APRS, or a position format not yet supported -- e.g.
        compressed -- see aprs.py).
        """
        if packet.frame_type not in (None, "UI"):
            return None

        position = parse_position(packet.payload)

        if position is None:
            return None

        if now is None:
            now = time.time()

        existing = self._stations.get(packet.source)
        packet_count = (existing.packet_count + 1) if existing else 1

        station = Station(
            callsign=packet.source,
            latitude=position.latitude,
            longitude=position.longitude,
            symbol_table=position.symbol_table,
            symbol_code=position.symbol_code,
            comment=position.comment,
            last_heard=now,
            packet_count=packet_count,
        )

        self._stations[packet.source] = station

        return station

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

