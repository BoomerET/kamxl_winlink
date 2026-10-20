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

# A tracker without a GPS fix often reports 0 N, 0 E ("Null Island",
# in the Gulf of Guinea). Nobody is really there, so it isn't a position.
_NULL_ISLAND_TOLERANCE = 0.01   # degrees, about half a mile


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
    not an exception. A report of 0 N, 0 E (a tracker without a GPS
    fix) is also "no position", so it never lands on the map.
    """
    position = _parse_position(payload, destination)
    if (
        position is not None
        and abs(position.latitude) < _NULL_ISLAND_TOLERANCE
        and abs(position.longitude) < _NULL_ISLAND_TOLERANCE
    ):
        return None
    return position


def _parse_position(
    payload: str,
    destination: Optional[str] = None,
) -> Optional[AprsPosition]:
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
_MESSAGE_ID_RE = re.compile(r"[A-Za-z0-9]{1,5}")


def format_aprs_message(
    addressee: str,
    message: str,
    paclen: int = 128,
    message_id: Optional[str] = None,
) -> str:
    """
    Format an APRS message information field for an AX.25 UI frame.

    Does not include AX.25 source, destination, digipeater path,
    or the TNC's SENDPAC carriage return.

    ``message_id`` (1-5 alphanumerics) is appended as ``{ID``, which
    asks the recipient to acknowledge with ``ackID``. Without it the
    message is sent "unacknowledged" and no ack will ever arrive.

    This function performs no hardware I/O.
    """
    if not isinstance(addressee, str):
        raise ValueError("Addressee must be a string")

    addressee = addressee.strip().upper()

    # Callsign with optional SSID, or a service name with a short
    # alphanumeric suffix such as WHO-IS (APRS addressees are just
    # 9 characters; real APRS-IS services use this form).
    if not re.fullmatch(r"[A-Z0-9]{1,6}(?:-[A-Z0-9]{1,2})?", addressee):
        raise ValueError("Invalid APRS addressee")

    if len(addressee) > 9:
        raise ValueError("APRS addressee exceeds 9 characters")

    if not isinstance(message, str) or not message:
        raise ValueError("APRS message must be a nonempty string")

    if not all(0x20 <= ord(char) <= 0x7E for char in message):
        raise ValueError("APRS message must contain printable ASCII only")

    if any(char in message for char in ("|", "~", "{")):
        raise ValueError("APRS message contains reserved characters")

    payload = f":{addressee:<9}:{message}"

    if message_id is not None:
        if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(
            message_id
        ):
            raise ValueError("Invalid APRS message ID")

        payload += "{" + message_id

    if len(payload.encode("ascii")) > paclen:
        raise ValueError(
            f"APRS payload exceeds PACLEN ({paclen} bytes)"
        )

    return payload

@dataclass(frozen=True)
class AprsMessage:
    """Decoded APRS message, acknowledgment, or rejection."""

    addressee: str
    text: str
    message_id: Optional[str]
    kind: str
    raw: str
    # APRS 1.1 "reply-ack" (``text{MM}AA``): an ack for our message
    # ID ``AA`` piggybacked on the sender's own message ``MM``.
    reply_ack: Optional[str] = None


def parse_aprs_message(payload: str) -> Optional[AprsMessage]:
    """
    Parse a standard APRS message information field.

    Returns None for non-message or malformed payloads.
    Does not perform hardware I/O or generate acknowledgments.
    """
    if not isinstance(payload, str):
        return None

    # APRS message format:
    # :ADDRESSEE:message text{ID
    # The addressee occupies exactly nine characters.
    if len(payload) < 11 or payload[0] != ":" or payload[10] != ":":
        return None

    address_field = payload[1:10]
    addressee = address_field.strip().upper()

    # Callsigns, plus the wider 9-character names bulletins and
    # services use (BLN1HOTG, NWS-WARN, NWS_ADVIS). Sending stays
    # strict (format_aprs_message); this only affects what we can read.
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,8}", addressee):
        return None

    if address_field != f"{addressee:<9}":
        return None

    content = payload[11:]
    if not content:
        return None

    # Bulletins/announcements carry no message ID and are never acked;
    # a "{" in their text is just text.
    if bulletin_kind(addressee) is not None:
        return AprsMessage(
            addressee=addressee,
            text=content.rstrip(),
            message_id=None,
            kind="bulletin",
            raw=payload,
        )

    # APRS acknowledgment/rejection: ackID or rejID.
    for prefix, kind in (("ack", "ack"), ("rej", "rej")):
        if content.startswith(prefix):
            message_id = content[len(prefix):].rstrip()

            # Reply-ack capable stations may send ``ackMM}`` or
            # ``ackMM}AA``; the acknowledged ID is MM either way.
            if "}" in message_id:
                message_id = message_id.split("}", 1)[0]

            if _MESSAGE_ID_RE.fullmatch(message_id):
                return AprsMessage(
                    addressee=addressee,
                    text="",
                    message_id=message_id,
                    kind=kind,
                    raw=payload,
                )

    message_id = None
    reply_ack = None
    text = content

    if "{" in content:
        text, message_id = content.rsplit("{", 1)
        message_id = message_id.rstrip()

        # APRS 1.1 reply-ack: ``{MM}`` or ``{MM}AA``.
        if "}" in message_id:
            message_id, reply_ack = message_id.split("}", 1)
            reply_ack = reply_ack or None

            if reply_ack is not None and not _MESSAGE_ID_RE.fullmatch(
                reply_ack
            ):
                return None

        if not _MESSAGE_ID_RE.fullmatch(message_id):
            return None

    if not text:
        return None

    return AprsMessage(
        addressee=addressee,
        text=text,
        message_id=message_id,
        kind="message",
        raw=payload,
        reply_ack=reply_ack,
    )

_BULLETIN_RE = re.compile(
    r"^BLN(?:(?P<line>[0-9])(?P<group>[A-Z0-9]{1,5})?|(?P<ann>[A-Z]))$"
)


def bulletin_kind(addressee: str) -> Optional[str]:
    """
    Classify a message addressee as a broadcast (APRS 1.0.1 ch. 14).

    "bulletin"      BLN0-BLN9 (general bulletin, line 0-9)
    "group"         BLN<digit><group>, e.g. BLN1WX, BLN1HOTG
    "announcement"  BLNA-BLNZ
    "nws"           NWS-*/NWS_* weather service bulletins
    None            anything else (an ordinary addressee)
    """
    if not isinstance(addressee, str):
        return None
    value = addressee.strip().upper()
    match = _BULLETIN_RE.match(value)
    if match:
        if match.group("ann"):
            return "announcement"
        return "group" if match.group("group") else "bulletin"
    if re.match(r"^NWS[-_][A-Z0-9]{1,5}$", value):
        return "nws"
    return None


def format_aprs_ack(addressee: str, message_id: str) -> str:
    """Build an APRS acknowledgment payload without transmitting."""
    if not isinstance(message_id, str) or not re.fullmatch(
        r"[A-Za-z0-9]{1,5}", message_id
    ):
        raise ValueError("Invalid APRS acknowledgment message ID")

    return format_aprs_message(
        addressee=addressee,
        message=f"ack{message_id}",
        paclen=128,
    )


_THIRD_PARTY_RE = re.compile(
    r"^\}(?P<source>[A-Za-z0-9]{1,6}(?:-[A-Za-z0-9]{1,2})?)>"
    r"[^:]*:(?P<payload>.*)$",
    re.DOTALL,
)


def unwrap_third_party(source: str, payload: str):
    """
    Unwrap an APRS third-party (``}``) packet into (source, payload).

    Messages that reach RF from the internet (APRS-IS services such as
    ANSRVR or a phone app, gated by an iGate) arrive as
    ``}W1ABC>APRS,TCPIP,IGATE*::N5BA     :ack12`` -- the RF source is
    the iGate, the real originator is inside. Returns the inputs
    unchanged for ordinary packets or a malformed header.
    """
    if not isinstance(payload, str) or not payload.startswith("}"):
        return source, payload

    match = _THIRD_PARTY_RE.match(payload)

    if match is None:
        return source, payload

    return match.group("source").upper(), match.group("payload")


def format_aprs_position(
    latitude: float,
    longitude: float,
    symbol_table: str = "/",
    symbol_code: str = "-",
    comment: str = "",
    messaging: bool = True,
) -> str:
    """
    Build an uncompressed APRS position report (no timestamp).

    ``=DDMM.mmN/DDDMM.mmW-comment`` -- the ``=`` data type says this
    station can receive APRS messages (``!`` if ``messaging`` is
    False). Default symbol ``/-`` is a house. Performs no I/O.
    """
    for name, value, limit in (
        ("latitude", latitude, 90.0), ("longitude", longitude, 180.0)
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"APRS {name} must be a number")
        if not math.isfinite(value) or abs(value) > limit:
            raise ValueError(f"APRS {name} out of range")

    if symbol_table not in ("/", "\\") and not re.fullmatch(
        r"[0-9A-Z]", symbol_table or ""
    ):
        raise ValueError("Invalid APRS symbol table")

    if (
        not isinstance(symbol_code, str)
        or len(symbol_code) != 1
        or not 0x21 <= ord(symbol_code) <= 0x7E
    ):
        raise ValueError("Invalid APRS symbol code")

    if not isinstance(comment, str):
        raise ValueError("APRS comment must be a string")

    if len(comment) > 43:
        raise ValueError("APRS position comment exceeds 43 characters")

    if not all(0x20 <= ord(char) <= 0x7E for char in comment):
        raise ValueError("APRS comment must contain printable ASCII only")

    if any(char in comment for char in ("|", "~")):
        raise ValueError("APRS comment contains reserved characters")

    def _dm(value: float, degree_digits: int, hemis: str) -> str:
        # Work in hundredths of a minute so 59.999' rounds up cleanly
        # into the next degree instead of printing "60.00".
        total = int(round(abs(value) * 6000))
        degrees, hundredths = divmod(total, 6000)
        minutes = hundredths / 100
        hemisphere = hemis[0] if value >= 0 else hemis[1]
        return f"{degrees:0{degree_digits}d}{minutes:05.2f}{hemisphere}"

    lat = _dm(latitude, 2, "NS")
    lon = _dm(longitude, 3, "EW")

    return (
        f"{'=' if messaging else '!'}{lat}{symbol_table}{lon}"
        f"{symbol_code}{comment}"
    )


# ---------------------------------------------------------------------------
# Weather reports (APRS 1.0.1 chapter 12)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WeatherReport:
    """
    Decoded APRS weather data. Every field is None when the station
    didn't report it (or sent the spec's "..."/spaces placeholder).
    Units are as transmitted: degrees, mph, °F, inches, percent, mb.
    """

    wind_direction: Optional[int] = None
    wind_speed_mph: Optional[int] = None
    wind_gust_mph: Optional[int] = None
    temperature_f: Optional[int] = None
    rain_1h_in: Optional[float] = None
    rain_24h_in: Optional[float] = None
    rain_since_midnight_in: Optional[float] = None
    humidity_pct: Optional[int] = None
    pressure_mb: Optional[float] = None
    luminosity_wm2: Optional[int] = None
    snow_24h_in: Optional[float] = None
    # MMDDHHMM from a positionless report, verbatim (no year/timezone).
    timestamp: Optional[str] = None
    # Whatever followed the weather fields (software/unit type, text).
    comment: str = ""


# key -> (width, signed)
_WX_FIELDS = {
    "g": (3, False), "t": (3, True), "r": (3, False), "p": (3, False),
    "P": (3, False), "h": (2, False), "b": (5, False), "L": (3, False),
    "l": (3, False), "s": (3, False), "#": (3, False),
}

_POSITIONLESS_WX_RE = re.compile(
    r"^_(?P<ts>[0-9]{8})c(?P<c>[0-9. ]{3})s(?P<s>[0-9. ]{3})(?P<rest>.*)$",
    re.DOTALL,
)
_WIND_PREFIX_RE = re.compile(r"^(?P<c>[0-9. ]{3})/(?P<s>[0-9. ]{3})")


def _wx_int(text: str, signed: bool = False) -> Optional[int]:
    """Parse one fixed-width value; None for the missing-value forms."""
    if text.strip(". ") == "":
        return None
    pattern = r"-?[0-9]+" if signed else r"[0-9]+"
    if not re.fullmatch(pattern, text.strip()):
        raise ValueError(text)
    return int(text.strip())


def _parse_wx_fields(rest: str, values: dict) -> str:
    """Consume gNNNtNNN... fields into ``values``; return what's left."""
    while rest:
        key = rest[0]
        spec = _WX_FIELDS.get(key)
        if spec is None:
            break
        width, signed = spec
        raw = rest[1:1 + width]
        if len(raw) < width or key in values:
            break
        try:
            value = _wx_int(raw, signed)
        except ValueError:
            break  # not a weather field after all, e.g. "tRSW" unit tag
        values[key] = value
        rest = rest[1 + width:]
    return rest


def parse_weather(
    payload: str, position: Optional["AprsPosition"] = None
) -> Optional[WeatherReport]:
    """
    Decode an APRS weather report, or return None.

    Handles the two common forms:

    * Positionless: ``_MMDDHHMMcDDDsSSSgGGGtTTTrRRRpPPPPPPhHHbBBBBB...``
      (position comes from the station's separate beacons).
    * Position report with the weather symbol ``_``: the comment starts
      ``DDD/SSS`` (wind direction/speed) followed by ``gGGGtTTT...``.
      Pass the already-decoded ``position``.

    Requires at least one real measurement so ordinary stations that
    happen to use the WX symbol don't decode as empty weather.
    """
    if not isinstance(payload, str):
        return None

    values: dict = {}
    wind_dir = wind_speed = None
    timestamp = None

    try:
        match = _POSITIONLESS_WX_RE.match(payload)
        if match:
            timestamp = match.group("ts")
            wind_dir = _wx_int(match.group("c"))
            wind_speed = _wx_int(match.group("s"))
            rest = match.group("rest")
        elif position is not None and position.symbol_code == "_":
            rest = position.comment
            wind = _WIND_PREFIX_RE.match(rest)
            if wind:
                wind_dir = _wx_int(wind.group("c"))
                wind_speed = _wx_int(wind.group("s"))
                rest = rest[wind.end():]
        else:
            return None
    except ValueError:
        return None

    rest = _parse_wx_fields(rest, values)

    def hundredths(key):
        return None if values.get(key) is None else values[key] / 100

    humidity = values.get("h")
    if humidity == 0:
        humidity = 100  # the spec's "h00" means 100%

    luminosity = values.get("L")
    if values.get("l") is not None:
        luminosity = values["l"] + 1000

    report = WeatherReport(
        wind_direction=wind_dir if wind_dir is None or wind_dir <= 360 else None,
        wind_speed_mph=wind_speed,
        wind_gust_mph=values.get("g"),
        temperature_f=(
            values.get("t")
            if values.get("t") is None or -99 <= values["t"] <= 199
            else None
        ),
        rain_1h_in=hundredths("r"),
        rain_24h_in=hundredths("p"),
        rain_since_midnight_in=hundredths("P"),
        humidity_pct=humidity if humidity is None or humidity <= 100 else None,
        pressure_mb=None if values.get("b") is None else values["b"] / 10,
        luminosity_wm2=luminosity,
        snow_24h_in=None if values.get("s") is None else values["s"] / 10,
        timestamp=timestamp,
        comment=rest.strip(),
    )

    measured = [
        v for k, v in vars(report).items()
        if k not in ("timestamp", "comment") and v is not None
    ]
    return report if measured else None


# ---------------------------------------------------------------------------
# Objects and items (APRS 1.0.1 chapter 11)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AprsObject:
    """
    An APRS object (``;NAME_____*DDHHMMz<position>``) or item
    (``)NAME!<position>``): something a station puts on the map on
    behalf of something else -- a repeater, a net, an event. ``live``
    is False for a "killed" object/item (``_`` instead of ``*``/``!``),
    which means "remove it from the map".
    """

    name: str
    kind: str            # "object" or "item"
    live: bool
    position: AprsPosition


_OBJECT_RE = re.compile(
    r"^;(?P<name>[\x20-\x7E]{9})(?P<state>[*_])(?P<ts>[0-9]{6}[zh/])(?P<pos>.*)$",
    re.DOTALL,
)
_ITEM_RE = re.compile(
    r"^\)(?P<name>[\x20-\x7E]{3,9}?)(?P<state>[!_])(?P<pos>.*)$",
    re.DOTALL,
)


def parse_object(payload: str) -> Optional[AprsObject]:
    """Decode an APRS object or item report, or return None."""
    if not isinstance(payload, str):
        return None

    match = _OBJECT_RE.match(payload)
    if match:
        position = parse_position("@" + match.group("ts") + match.group("pos"))
        kind, live = "object", match.group("state") == "*"
    else:
        match = _ITEM_RE.match(payload)
        if not match or "!" in match.group("name") or "_" in match.group("name"):
            return None
        position = parse_position("!" + match.group("pos"))
        kind, live = "item", match.group("state") == "!"

    name = match.group("name").strip()
    if position is None or not name:
        return None

    return AprsObject(name=name, kind=kind, live=live, position=position)


# Standard CTCSS tones. APRS frequency objects send only the integer
# part ("T110" means 110.9 Hz), so map it back to the real tone.
_CTCSS_TONES = (
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8,
    97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8,
    136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9, 171.3,
    173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5,
    206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
)
_CTCSS_BY_INT = {int(t): t for t in _CTCSS_TONES}


def parse_frequency_info(name: str, comment: str) -> Optional[dict]:
    """
    Repeater/frequency details from an object, per the APRS frequency
    spec: name like ``147.000-T`` or ``146.94MHz``, comment like
    ``T110 -060 R30m`` (tone, offset in 10 kHz units, range).

    Returns a dict (frequency_mhz, offset_khz, tone_hz, dcs_code,
    range_miles, each possibly None) or None if no frequency is found.
    """
    text = f"{name or ''} {comment or ''}"
    freq = re.search(r"(?<![0-9.])([0-9]{2,4}\.[0-9]{2,4})(?:\s*MHz)?", text)
    if not freq:
        return None
    frequency = float(freq.group(1))
    # Amateur bands with repeaters (10 m, 6 m, 2 m, 1.25 m, 70 cm,
    # 33 cm, 23 cm) -- keeps "updated 12.30" style text from reading
    # as a frequency.
    bands = ((28.0, 29.7), (50.0, 54.0), (144.0, 148.0), (219.0, 225.0),
             (420.0, 450.0), (902.0, 928.0), (1240.0, 1300.0))
    if not any(lo <= frequency <= hi for lo, hi in bands):
        return None

    info = {
        "frequency_mhz": frequency,
        "offset_khz": None,
        "tone_hz": None,
        "dcs_code": None,
        "range_miles": None,
    }

    tone = re.search(r"(?:^|\s)[TC]([0-9]{3})\b", comment or "")
    if tone:
        value = int(tone.group(1))
        info["tone_hz"] = _CTCSS_BY_INT.get(value, float(value))

    dcs = re.search(r"(?:^|\s)D([0-7]{3})\b", comment or "")
    if dcs:
        info["dcs_code"] = dcs.group(1)

    offset = re.search(r"(?:^|\s)([+-][0-9]{3})\b", comment or "")
    if offset:
        info["offset_khz"] = int(offset.group(1)) * 10
    elif re.search(r"[0-9][-+]", name or ""):
        # Name-only convention "147.000-": standard offset for the band.
        sign = -1 if "-" in name.split(freq.group(1), 1)[-1][:1] else 1
        standard = 600 if frequency < 300 else 5000
        info["offset_khz"] = sign * standard

    rng = re.search(r"(?:^|\s)R([0-9]{2,3})([mk])\b", comment or "")
    if rng:
        miles = int(rng.group(1))
        info["range_miles"] = miles if rng.group(2) == "m" else round(miles * 0.621)

    return info
