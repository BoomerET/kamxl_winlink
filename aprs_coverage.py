"""
Coverage: which digipeaters hear this station, and how well it hears
others, broken down by "station setup" (radio + antenna) so setups can
be compared -- e.g. a mag-mount in the garage vs a J-pole at 16 ft.

Pure helpers -- no I/O. The daemon tags every transmission and every
received packet with the current setup label (stored in the history
database), records each time a digipeater repeats one of our
transmissions or an iGate sends it back from APRS-IS, and
ReceptionHistory.coverage_report() aggregates it with these helpers.
"""

import math
import re
from typing import Iterable, Optional

# Path entries that are routing aliases rather than a station.
_ALIAS_RE = re.compile(
    r"^(WIDE|TRACE|RELAY|TCPIP|TCPXX|NOGATE|RFONLY|GATE|ECHO|TEMP|qA)",
    re.IGNORECASE,
)

SETUP_MAX_LENGTH = 40
SETUP_KEY = "station_setup"

# A copy heard this long after a transmission still counts for it.
# (Digipeats come back in seconds; APRS-IS round trips can take longer.)
MATCH_WINDOW_SECONDS = 900


def is_alias(hop: str) -> bool:
    return bool(_ALIAS_RE.match(hop.rstrip("*")))


def first_hop(digipeaters: Iterable[str]) -> Optional[str]:
    """
    The digipeater that heard a packet directly from its sender, or
    None if the packet wasn't digipeated.

    Many TNCs (the KAM-XL included) mark only the *last* digipeater
    that repeated a packet with ``*``; every hop before it has been
    used too. So the first used hop is the first entry of the path,
    provided the path has a ``*`` at all. Returns "" when that hop is
    an alias (a digipeater that doesn't insert its own callsign), i.e.
    digipeated but by an unidentified station.
    """
    hops = [h for h in (digipeaters or ()) if h]
    starred = [i for i, h in enumerate(hops) if h.endswith("*")]
    if not starred:
        return None
    first = hops[0].rstrip("*").upper()
    return "" if is_alias(first) else first


def normalize_setup(value) -> str:
    """Validate a setup label: free text, up to 40 printable characters."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Setup must be text")
    value = " ".join(value.split())
    if len(value) > SETUP_MAX_LENGTH:
        raise ValueError(
            f"Setup must be at most {SETUP_MAX_LENGTH} characters"
        )
    if any(not ch.isprintable() for ch in value):
        raise ValueError("Setup must be printable text")
    return value


def distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles."""
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))
