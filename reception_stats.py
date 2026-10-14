"""In-memory packet reception statistics for the KAM-XL monitor."""

from collections import Counter
from typing import Any, Dict

from packet import Packet


class ReceptionStats:
    """Aggregate successfully parsed AX.25 packets since startup."""

    def __init__(self) -> None:
        self._sources = Counter()
        self._digipeaters = Counter()
        self._total = 0
        self._used_digi_packets = 0
        self._no_used_digi_packets = 0

    def update(self, packet: Packet) -> None:
        self._total += 1
        self._sources[packet.source] += 1

        used = set()

        for entry in packet.digipeaters:
            if not entry.endswith("*"):
                continue

            callsign = entry[:-1].upper()

            if callsign.startswith((
                "WIDE", "TRACE", "RELAY", "TCPIP", "TCPXX"
            )):
                continue

            used.add(callsign)

        if used:
            self._used_digi_packets += 1

            for callsign in used:
                self._digipeaters[callsign] += 1
        else:
            self._no_used_digi_packets += 1

    def snapshot(self) -> Dict[str, Any]:
        return {
            "scope": "since_daemon_start",
            "total_packets": self._total,
            "unique_sources": len(self._sources),
            "top_sources": [
                {"callsign": callsign, "packets": count}
                for callsign, count in self._sources.most_common(10)
            ],
            "top_digipeaters": [
                {"callsign": callsign, "packets": count}
                for callsign, count in self._digipeaters.most_common(10)
            ],
            "used_digipeater_packets": self._used_digi_packets,
            "no_identified_digipeater_packets": self._no_used_digi_packets,
        }
