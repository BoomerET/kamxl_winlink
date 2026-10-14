"""
Parsing helpers for APRS position reports carried in AX.25 UI-frame
payloads (packet.py's Packet.payload).

APRS (Automatic Packet Reporting System) is an open, widely-documented
protocol layered on top of plain AX.25 UI frames -- unlike pbbs.py,
this isn't reverse-engineered from the KAM-XL manual, it's built from
the public APRS Protocol Reference spec. Still, per this project's
usual caution, treat it as unverified until checked against a real
captured live session (milestone 5's HEADER_RE bug is the reminder of
why that matters even for well-documented formats).

MVP SCOPE: position reports only. Supports uncompressed and
compressed APRS position reports ('!', '=', '/', '@') and Mic-E
position reports ('`', "'"). Mic-E requires the AX.25 destination
address because its latitude and hemisphere flags are encoded there.

Other APRS packet types (status, messages, objects, weather,
telemetry, etc.) are not decoded as positions.

Mic-E position decoding extracts latitude, longitude, symbol table,
symbol code, and the remaining raw comment. Optional Mic-E
speed/course, altitude, telemetry, status, and DAO precision
extensions are not decoded into separate fields. DAO refinements
are not applied to the returned coordinates.

COMPRESSED POSITIONS: APRS has two position encodings --
human-readable "uncompressed" (degrees-minutes text, e.g. "4903.50N")
and a denser base-91 "compressed" form (recognizable by a non-digit
symbol table character appearing immediately after the data type
identifier -- an uncompressed position always starts with a decimal
digit there -- followed by 4 base-91 latitude characters, 4 base-91
longitude characters, a symbol code, then 3 more bytes this module
doesn't decode -- see "NOT DECODED" below). Both are handled by
parse_position(), verified against the APRS Protocol Reference 1.0.1
(chapter 9)'s own worked examples: the spec's sample compressed field
"5L!!<*e7" decodes to exactly 49°30'00"N / 72°45'00"W, matching the
spec's own hand-computed result to the last digit, and independently
cross-checked against aprslib (a real, widely-used open-source APRS
parser: github.com/rossengeorgiev/aprs-python) to confirm the
"leading non-digit means compressed" detection rule and the specific
base-91 decode formula (``value = value * 91 + (ord(char) - 33)``,
big-endian/most-significant-digit-first).

NOT DECODED: the compressed format's course/speed, pre-calculated
radio range, and altitude (all packed into the 2 "cs" bytes
immediately after the symbol code, disambiguated by the following
Compression Type "T" byte) aren't extracted -- AprsPosition has no
field for any of them, matching this module's existing scope (the
uncompressed format's own optional course/speed/PHG comment
extensions aren't decoded either). A real, deliberate scope boundary,
not an oversight.

POSITION AMBIGUITY NOT FULLY MODELED: APRS allows trailing digits of
the minutes fields to be replaced with spaces to indicate reduced
precision (e.g. a station only willing to report to the nearest
degree). This parser treats an ambiguous digit as '0' for the purpose
of computing a decimal coordinate, which is the conventional
"most likely" interpretation, but doesn't track or expose the
ambiguity level itself -- a caller has no way to tell "exact" from
"rounded to a full degree" apart from the returned latitude/longitude
alone. Documented here as a known simplification rather than silently
getting it wrong.
"""

import math
import re

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AprsPosition:
    """
    A decoded APRS position report.

    ``symbol_table`` and ``symbol_code`` together select the map icon
    per the APRS spec's symbol tables (e.g. table "/" code ">" is a
    car, table "/" code "-" is a house) -- rendering that mapping is
    left to callers (the web map, milestone 7). ``timestamp`` is the
    raw APRS timestamp text (e.g. "092345z"), not decoded to a real
    date -- APRS timestamps carry no year and are ambiguous about
    UTC/local depending on their trailing letter, so turning them into
    an actual datetime needs a caller-supplied "as of" reference point
    this module doesn't have. ``comment`` is whatever free text
    followed the symbol code, verbatim. ``raw`` keeps the original
    payload text for debugging.
    """

    latitude: float
    longitude: float
    symbol_table: str
    symbol_code: str
    comment: str
    timestamp: Optional[str]
    raw: str


def _decode_latitude(digits: str, direction: str) -> float:
    # "DDMM.mm" -- 2-digit degrees, 2-digit minutes, '.', 2-digit
    # hundredths-of-a-minute. Ambiguous (space) digits are treated as
    # '0' -- see module docstring's "POSITION AMBIGUITY" note.
    digits = digits.replace(" ", "0")

    degrees = int(digits[0:2])
    minutes = float(digits[2:])

    decimal = degrees + minutes / 60.0

    return -decimal if direction == "S" else decimal


def _decode_longitude(digits: str, direction: str) -> float:
    # "DDDMM.mm" -- 3-digit degrees, same minutes shape as latitude.
    digits = digits.replace(" ", "0")

    degrees = int(digits[0:3])
    minutes = float(digits[3:])

    decimal = degrees + minutes / 60.0

    return -decimal if direction == "W" else decimal


# Base-91 printable-ASCII range used by the compressed position
# format -- '!' (33) through '{' (123), 91 characters total. Per the
# spec's "Base-91 Notation": each character's numeric value is its
# ASCII code minus 33.
_BASE91_MIN = 0x21  # "!"
_BASE91_MAX = 0x7B  # "{"


def _base91_decode(chars: str) -> int:
    """
    Decode a base-91 printable-ASCII string into its numeric value.

    Big-endian (most significant digit first), same as ordinary
    decimal-string-to-int conversion but base 91 instead of base 10 --
    verified against the spec's own worked example (chars "<*e7"
    decodes to 20427156, matching its hand-computed longitude math
    exactly) and cross-checked against aprslib's ``base91.to_decimal``
    (same algorithm, different implementation). Assumes every
    character is already known to be in the valid ['!'..'{'] range --
    callers validate that first (see ``_decode_compressed_position``)
    so a stray out-of-range byte degrades to "not a position" rather
    than a wrong number here.
    """
    value = 0

    for char in chars:
        value = value * 91 + (ord(char) - _BASE91_MIN)

    return value


def _decode_compressed_position(
    symbol_table: str,
    fields: str,
    comment: str,
    timestamp: Optional[str],
    raw: str,
) -> Optional[AprsPosition]:
    """
    Decode the 12 bytes following the compressed format's leading
    symbol table character: 4 latitude + 4 longitude + 1 symbol code
    + 2 "cs" + 1 compression-type byte (the last 3 aren't decoded --
    see module docstring's "NOT DECODED" note).

    Only the latitude/longitude characters are validated against the
    strict base-91 range -- the trailing cs/compression-type bytes
    are allowed to be anything (including a literal space, the
    spec's own "no course/speed/range data" sentinel for the first cs
    byte) since this module never inspects their value. Returns None
    if the latitude or longitude characters fall outside ['!'..'{'],
    same "skip, don't guess" degradation as a ValueError elsewhere in
    this module -- a malformed real-world packet shouldn't produce a
    wrong position.
    """
    lat_chars = fields[0:4]
    lon_chars = fields[4:8]
    symbol_code = fields[8]

    for chars in (lat_chars, lon_chars):
        for char in chars:
            if not (_BASE91_MIN <= ord(char) <= _BASE91_MAX):
                return None

    latitude = 90.0 - (_base91_decode(lat_chars) / 380926.0)
    longitude = -180.0 + (_base91_decode(lon_chars) / 190463.0)

    return AprsPosition(
        latitude=latitude,
        longitude=longitude,
        symbol_table=symbol_table,
        symbol_code=symbol_code,
        comment=comment,
        timestamp=timestamp,
        raw=raw,
    )


# Compressed position, no timestamp: data type '!' or '='.
#
#   =/5L!!<*e7>7P[with course/speed
#
# Symbol table character (usually "/" or "\", but real APRS traffic
# also uses a digit or uppercase letter here for "alternate table with
# overlay" -- same as the uncompressed format's own sym_table group
# above, so matched just as permissively: any single character), then
# the fixed 12-byte field (lat + lon + symbol code + cs + compression
# type -- see _decode_compressed_position), then a free-text comment.
#
# parse_position() only reaches this regex after _POSITION_RE/
# _POSITION_WITH_TIMESTAMP_RE have already failed to match -- that
# ordering, not a restrictive character class here, is what
# disambiguates compressed from uncompressed (an uncompressed position
# always starts with a rigid "DDMM.mm" digit run that a real
# compressed payload's base-91 bytes essentially never happen to
# form). The 12-byte field's own character range is intentionally
# broad (space through "~") rather than strictly base-91 --
# _decode_compressed_position does the precise validation where it
# matters (lat/lon only), since the trailing cs bytes are allowed to
# include a literal space (the spec's "no course/speed/range data"
# sentinel).
_COMPRESSED_POSITION_RE = re.compile(
    r"^[!=]"
    r"(?P<sym_table>.)"
    r"(?P<fields>[ -~]{12})"
    r"(?P<comment>.*)$"
)

# Compressed position, with timestamp: data type '/' or '@'.
#
#   @092345z/5L!!<*e7>{?!with radio range
#
# Same shape as above, preceded by the same 7-character APRS
# timestamp the uncompressed-with-timestamp format uses.
_COMPRESSED_POSITION_WITH_TIMESTAMP_RE = re.compile(
    r"^[/@]"
    r"(?P<timestamp>\d{6}[zh/])"
    r"(?P<sym_table>.)"
    r"(?P<fields>[ -~]{12})"
    r"(?P<comment>.*)$"
)


# Uncompressed position, no timestamp: data type '!' or '='.
#
#   !4903.50N/07201.75W-Test comment
#
# Latitude "DDMM.mm" (with optional space-for-ambiguity digits),
# N/S, symbol table character, longitude "DDDMM.mm" (same shape),
# E/W, symbol code character, then a free-text comment.
_POSITION_RE = re.compile(
    r"^[!=]"
    r"(?P<lat>\d{2}[\d ]{2}\.[\d ]{2})(?P<lat_dir>[NS])"
    r"(?P<sym_table>.)"
    r"(?P<lon>\d{3}[\d ]{2}\.[\d ]{2})(?P<lon_dir>[EW])"
    r"(?P<sym_code>.)"
    r"(?P<comment>.*)$"
)

# Uncompressed position, with timestamp: data type '/' or '@'.
#
#   /092345z4903.50N/07201.75W-Test comment
#
# Same position shape as above, preceded by a 7-character APRS
# timestamp (6 digits + a type letter -- 'z'/'/' = day/hour/minute,
# 'h' = hour/minute/second, see spec).
_POSITION_WITH_TIMESTAMP_RE = re.compile(
    r"^[/@]"
    r"(?P<timestamp>\d{6}[zh/])"
    r"(?P<lat>\d{2}[\d ]{2}\.[\d ]{2})(?P<lat_dir>[NS])"
    r"(?P<sym_table>.)"
    r"(?P<lon>\d{3}[\d ]{2}\.[\d ]{2})(?P<lon_dir>[EW])"
    r"(?P<sym_code>.)"
    r"(?P<comment>.*)$"
)

def _mice_destination_digits(destination: str) -> Optional[str]:
    """
    Extract six latitude digits from a Mic-E destination address.

    0-9 represent digits directly.
    A-J and P-Y encode digits 0-9.
    K, L, and Z are ambiguity indicators, initially treated as zero.
    """
    if len(destination) != 6:
        return None

    digits = []

    for char in destination.upper():
        if "0" <= char <= "9":
            digits.append(char)
        elif "A" <= char <= "J":
            digits.append(str(ord(char) - ord("A")))
        elif "P" <= char <= "Y":
            digits.append(str(ord(char) - ord("P")))
        elif char in "KLZ":
            digits.append("0")
        else:
            return None

    return "".join(digits)

def _decode_mice_position(
    payload: str,
    destination: str,
) -> Optional[AprsPosition]:
    """
    Decode an APRS Mic-E position report.

    Latitude and hemisphere flags are encoded in the destination
    address; longitude and symbol information are in the payload.
    """
    # Mic-E requires six encoded destination characters and at least
    # nine information-field characters, including the data type.
    if len(destination) != 6 or len(payload) < 9:
        return None

    # Reject non-byte characters: monitor data should have been
    # decoded losslessly using Latin-1.
    if any(ord(char) > 255 for char in payload):
        return None

    digits = _mice_destination_digits(destination)
    if digits is None:
        return None

    # Mic-E ambiguity characters represent trailing unspecified digits.
    ambiguity = 0
    for char in reversed(destination.upper()):
        if char in "KLZ":
            ambiguity += 1
        else:
            break

    degrees = int(digits[:2])
    minutes = int(digits[2:4])
    hundredths = int(digits[4:6])

    if degrees > 90 or minutes >= 60 or hundredths >= 100:
        return None

    # Center latitude within the ambiguous area.  For 1-3 missing
    # digits, set the first missing digit to 5.  For 4 missing digits,
    # the midpoint of the 0-59 minute range is 30 minutes.
    centered_digits = list(digits)
    if ambiguity >= 4:
        centered_digits[2] = "3"
    elif ambiguity:
        centered_digits[6 - ambiguity] = "5"

    centered = "".join(centered_digits)
    latitude = (
        int(centered[:2])
        + (int(centered[2:4]) + int(centered[4:6]) / 100.0) / 60.0
    )

    # Character 4 selects the latitude hemisphere (P-Z = north).
    if destination[3].upper() not in "PQRSTUVWXYZ":
        latitude = -latitude

    # Mic-E longitude occupies payload bytes 1 through 3.
    # Each encoded byte has an offset of 28.
    longitude_degrees = ord(payload[1]) - 28
    longitude_minutes = ord(payload[2]) - 28
    longitude_hundredths = ord(payload[3]) - 28

    # Reject malformed Mic-E longitude fields before normalization.
    if not (
        0 <= longitude_degrees <= 199
        and 0 <= longitude_minutes <= 119
        and 0 <= longitude_hundredths <= 99
    ):
        return None

    # Destination character 5 indicates the +100 degree offset.
    if destination[4].upper() in "PQRSTUVWXYZ":
        longitude_degrees += 100

    # Normalize the Mic-E longitude degree encoding.
    if 180 <= longitude_degrees <= 189:
        longitude_degrees -= 80
    elif longitude_degrees >= 190:
        longitude_degrees -= 190

    if longitude_minutes >= 60:
        longitude_minutes -= 60

    # Reject invalid coordinates rather than plot incorrect positions.
    if not (
        0 <= longitude_degrees <= 180
        and 0 <= longitude_minutes < 60
        and 0 <= longitude_hundredths < 100
    ):
        return None

    # Center the longitude *minutes*, not the complete coordinate.
    longitude_minutes_decimal = longitude_minutes + longitude_hundredths / 100.0
    if ambiguity == 4:
        longitude_minutes_decimal = 30.0
    elif ambiguity == 3:
        longitude_minutes_decimal = (
            math.floor(longitude_minutes_decimal / 10.0) + 0.5
        ) * 10.0
    elif ambiguity == 2:
        longitude_minutes_decimal = math.floor(longitude_minutes_decimal) + 0.5
    elif ambiguity == 1:
        longitude_minutes_decimal = (
            math.floor(longitude_minutes_decimal * 10.0) + 0.5
        ) / 10.0

    longitude = longitude_degrees + longitude_minutes_decimal / 60.0

    # Character 6 selects east/west (P-Z = west).
    if destination[5].upper() in "PQRSTUVWXYZ":
        longitude = -longitude

    # Mic-E symbol code and symbol table occupy bytes 7 and 8.
    return AprsPosition(
        latitude=latitude,
        longitude=longitude,
        symbol_table=payload[8],
        symbol_code=payload[7],
        comment=payload[9:],
        timestamp=None,
        raw=payload,
    )

def parse_position(
    payload: str,
    destination: Optional[str] = None,
) -> Optional[AprsPosition]:
    """
    Parse an AX.25 UI-frame payload as an APRS position report, either
    uncompressed or compressed (see module docstring for both).

    Returns None if the payload isn't a position report at all (any
    other APRS data type, or non-APRS traffic entirely) or doesn't
    match either encoding's expected shape. Deliberately permissive
    like pbbs.py's parsers: a format surprise means "no position",
    not an exception.
    """
    if not payload:
        return None

    # Mic-E encodes latitude in the AX.25 destination address
    # and longitude in the information payload.
    if payload[0] in ("`", "'"):
        if destination is None:
            return None
        return _decode_mice_position(payload, destination)

    match = _POSITION_RE.match(payload)
    timestamp = None

    if match is None:
        match = _POSITION_WITH_TIMESTAMP_RE.match(payload)

        if match is not None:
            timestamp = match.group("timestamp")

    if match is not None:
        try:
            latitude = _decode_latitude(
                match.group("lat"), match.group("lat_dir")
            )
            longitude = _decode_longitude(
                match.group("lon"), match.group("lon_dir")
            )
        except ValueError:
            # Shouldn't happen given the regex's own digit/space
            # constraints, but a malformed real-world packet degrading
            # to "no position" beats an unhandled exception taking
            # down the station tracker.
            return None

        return AprsPosition(
            latitude=latitude,
            longitude=longitude,
            symbol_table=match.group("sym_table"),
            symbol_code=match.group("sym_code"),
            comment=match.group("comment"),
            timestamp=timestamp,
            raw=payload,
        )

    # A packet beginning with an uncompressed latitude must not
    # fall through to compressed decoding if malformed.
    #
    # Otherwise, its ASCII digits may be accepted as Base91 data,
    # creating a plausible but completely incorrect map position.

    # Reject malformed uncompressed-looking positions without
    # rejecting compressed positions with digit symbol-table overlays.
    position_start = 1

    if payload[0] in ("/", "@"):
        position_start = 8

    # Uncompressed latitude starts with DDMM.mm followed by N/S.
    # A malformed packet may have unexpected characters after
    # that recognizable latitude field.
    candidate = payload[position_start:]

    if re.match(r"^\d{4}\.\d{2}[NS]", candidate):
        return None

    # Not uncompressed -- try the compressed format.
    match = _COMPRESSED_POSITION_RE.match(payload)

    timestamp = None

    if match is None:
        match = _COMPRESSED_POSITION_WITH_TIMESTAMP_RE.match(payload)

        if match is not None:
            timestamp = match.group("timestamp")

    if match is None:
        return None

    return _decode_compressed_position(
        match.group("sym_table"),
        match.group("fields"),
        match.group("comment"),
        timestamp,
        payload,
    )
