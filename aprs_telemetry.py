"""
APRS telemetry (APRS 1.0.1 chapter 13, plus the common extensions).

Stations -- often digipeaters and weather stations -- report up to five
analog channels and eight on/off bits:

    T#005,199,000,255,073,123,01101001

or, compressed (base91) inside a position comment:

    !3313.65N/09707.64W#digi |!!%B&Y"9!!|

Raw channel values mean nothing on their own. The station sends four
"definition" messages, addressed to itself, that say what they are:

    :W5NGU-3  :PARM.Battery,Temp,...        channel names (5 analog + 8 bits)
    :W5NGU-3  :UNIT.Volts,deg.F,...         units
    :W5NGU-3  :EQNS.0,0.075,0,0,1,-40,...   value = a*x^2 + b*x + c per channel
    :W5NGU-3  :BITS.11111111,Project Name   which bit value means "on", and a title

Modern senders put decimals (and negatives) in the analog fields; that
is accepted here, as aprs.fi does. Pure parsing, no I/O.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

ANALOG_CHANNELS = 5
BIT_CHANNELS = 8

_T_RE = re.compile(r"^T#(?P<seq>MIC|\d{1,5})?,?(?P<rest>.*)$", re.DOTALL)
_NUMBER_RE = re.compile(r"^-?(\d+(\.\d*)?|\.\d+)$")
_BITS_RE = re.compile(r"^[01]{1,8}")
_COMPRESSED_RE = re.compile(r"\|([!-{]{4,14})\|")


@dataclass(frozen=True)
class TelemetryReport:
    sequence: Optional[str]
    analog: Tuple[Optional[float], ...]   # raw values, None = not sent
    bits: Optional[str]                   # e.g. "01101001", None = not sent


def parse_telemetry(payload: str) -> Optional[TelemetryReport]:
    """Decode a ``T#`` packet, or compressed telemetry in a comment."""
    if not isinstance(payload, str):
        return None
    if payload.startswith("T#"):
        return _parse_uncompressed(payload)
    # Compressed telemetry rides in position reports' comments only;
    # elsewhere "|...|" is far more likely to be ordinary text.
    if payload[:1] in ("!", "=", "/", "@", "`", "'"):
        return _parse_compressed(payload)
    return None


def _parse_uncompressed(payload: str) -> Optional[TelemetryReport]:
    match = _T_RE.match(payload.strip())
    if match is None:
        return None
    fields = match.group("rest").split(",")
    analog: List[Optional[float]] = []
    bits = None
    for i, raw in enumerate(fields):
        raw = raw.strip()
        if len(analog) < ANALOG_CHANNELS and _NUMBER_RE.match(raw):
            analog.append(float(raw))
            continue
        if len(analog) < ANALOG_CHANNELS and raw == "":
            analog.append(None)
            continue
        found = _BITS_RE.match(raw)
        if found and len(analog) == ANALOG_CHANNELS:
            bits = found.group(0).ljust(8, "0")
        break
    if not any(v is not None for v in analog) and bits is None:
        return None
    while len(analog) < ANALOG_CHANNELS:
        analog.append(None)
    return TelemetryReport(match.group("seq"), tuple(analog), bits)


def _b91(pair: str) -> int:
    return (ord(pair[0]) - 33) * 91 + (ord(pair[1]) - 33)


def _parse_compressed(payload: str) -> Optional[TelemetryReport]:
    for match in _COMPRESSED_RE.finditer(payload):
        body = match.group(1)
        if len(body) % 2:
            continue
        pairs = [body[i:i + 2] for i in range(0, len(body), 2)]
        sequence = str(_b91(pairs[0]))
        values = [_b91(p) for p in pairs[1:]]
        bits = None
        if len(values) == ANALOG_CHANNELS + 1:
            bits = format(values.pop() & 0xFF, "08b")[::-1]
        analog = tuple(float(v) for v in values) + \
            (None,) * (ANALOG_CHANNELS - len(values))
        return TelemetryReport(sequence, analog, bits)
    return None


# -- definitions -----------------------------------------------------------

@dataclass
class TelemetryDefinitions:
    names: List[str] = field(default_factory=lambda: [""] * 13)
    units: List[str] = field(default_factory=lambda: [""] * 13)
    # (a, b, c) per analog channel; value = a*x^2 + b*x + c
    equations: List[Tuple[float, float, float]] = field(
        default_factory=lambda: [(0.0, 1.0, 0.0)] * ANALOG_CHANNELS
    )
    bit_sense: str = "11111111"
    project: str = ""

    def apply(self, text: str) -> bool:
        """Apply one definition message's text. True if it was one."""
        kind, _, rest = text.partition(".")
        kind = kind.strip().upper()
        if kind in ("PARM", "UNIT"):
            items = [v.strip() for v in rest.split(",")][:13]
            target = self.names if kind == "PARM" else self.units
            for i, value in enumerate(items):
                target[i] = value
            return True
        if kind == "EQNS":
            numbers = []
            for raw in rest.split(",")[:15]:
                raw = raw.strip()
                try:
                    numbers.append(float(raw) if raw else 0.0)
                except ValueError:
                    return False
            numbers += [0.0, 1.0, 0.0] * 5
            self.equations = [
                tuple(numbers[i * 3:i * 3 + 3]) for i in range(ANALOG_CHANNELS)
            ]
            return True
        if kind == "BITS":
            sense, _, project = rest.partition(",")
            sense = sense.strip()
            if _BITS_RE.match(sense):
                self.bit_sense = sense[:8].ljust(8, "1")
            self.project = project.strip()[:23]
            return True
        return False

    def value(self, channel: int, raw: Optional[float]) -> Optional[float]:
        if raw is None:
            return None
        a, b, c = self.equations[channel]
        return a * raw * raw + b * raw + c

    def bit_on(self, channel: int, bits: str) -> bool:
        return bits[channel] == self.bit_sense[channel]


def is_definition(text: str) -> bool:
    return isinstance(text, str) and text[:5].upper() in (
        "PARM.", "UNIT.", "EQNS.", "BITS."
    )


def build_series(
    callsign: str,
    reports: List[Tuple[float, TelemetryReport]],
    definitions: TelemetryDefinitions,
) -> Dict:
    """
    Turn (received_at, report) pairs (oldest first) into chart-ready
    series. Channels that never carried a value are left out.
    """
    # A station that named its channels (PARM) leaves unused ones blank.
    named = any(definitions.names[:ANALOG_CHANNELS])
    analog = []
    for ch in range(ANALOG_CHANNELS):
        if named and not definitions.names[ch]:
            continue
        points = [
            [t, definitions.value(ch, r.analog[ch])]
            for t, r in reports if r.analog[ch] is not None
        ]
        if not points:
            continue
        values = [v for _, v in points]
        analog.append({
            "channel": ch + 1,
            "name": definitions.names[ch] or f"Channel {ch + 1}",
            "unit": definitions.units[ch],
            "points": points,
            "latest": values[-1],
            "min": min(values),
            "max": max(values),
        })

    bits = []
    with_bits = [(t, r.bits) for t, r in reports if r.bits]
    if with_bits:
        for ch in range(BIT_CHANNELS):
            name = definitions.names[ANALOG_CHANNELS + ch]
            points = [[t, definitions.bit_on(ch, b)] for t, b in with_bits]
            # Unnamed bits that never change are noise; skip them.
            if not name and len({on for _, on in points}) < 2:
                continue
            bits.append({
                "bit": ch + 1,
                "name": name or f"Bit {ch + 1}",
                "points": points,
                "latest": points[-1][1],
            })

    return {
        "callsign": callsign,
        "project": definitions.project,
        "defined": any(definitions.names) or any(
            eq != (0.0, 1.0, 0.0) for eq in definitions.equations
        ),
        "reports": len(reports),
        "first_heard": reports[0][0] if reports else None,
        "last_heard": reports[-1][0] if reports else None,
        "analog": analog,
        "bits": bits,
    }
