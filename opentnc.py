"""
NA6D / AE6EO OpenTNC driver.

The OpenTNC (https://na6d.com/products/na6d-opentnc) is an RP2040 /
Zephyr RTOS hardware TNC with its own on-board AX.25 stack and modems
(1200 AFSK, 2400 V.26, 9600 G3RUH, plus FX.25 forward error
correction). It enumerates as a USB CDC-ACM serial port (baud rate
ignored) and offers a Command Mode at a ``cmd:`` prompt whose command
set its author describes as "based on, and largely compatible with"
the TAPR TNC-2's -- which is why the shared TerminalTNC machinery in
tnc.py (written against the KAM-XL, another TNC-2 descendant) fits it.

STATUS. Verified against a real NA6D OpenTNC (tools/opentnc_capture.py,
2026-10-19; transcript in tests/opentnc_capture_20261019.txt):

  - ``cmd:`` prompt; commands are echoed; ``HELP`` lists them with the
    shortest accepted abbreviation in capitals (``MYcall``,
    ``CARrierdetect``) -- the command table below is that list
  - a rejected command answers ``Unknown command: X`` and ``Type 'help'
    to list all commands`` (not the TNC-2 ``EH?``)
  - a bare query answers ``is VALUE`` (``MYCALL`` -> ``is N5BA-11``);
    ``DISPLAY`` lists every setting as ``KEY is VALUE`` with the key in
    its HELP casing, then a blank line and ``Heap allocs: N``
  - values come back mixed-case (``SQuelch``, ``OFf``, ``OPportunistic``)
  - not on this firmware: VERSION, STATUS, ECHO, MCOM, MRPT, MSTAMP,
    HEADERLN, PPERSIST, APRSDIGI, BEACON

MONITOR lines, seen later the same day and checked against the firmware
source (DumpPacket() in src/packetutil.c): "0 (???): <[To APRS from N5BA
via WIDE1-1 WIDE2-1: 03(UI) PID ^F0 payload]" -- see OPENTNC_MONITOR_RE.

Still UNVERIFIED: the connect/disconnect banners (TNC-2 convention, see
CONNECT_MARKERS).
KISS mode (OpenTNCKISS, below) depends on none of it.

What this driver deliberately does NOT do yet:

  - APRS transmit in Terminal Mode (APRS_TX_SUPPORTED = False). That
    path checks the TNC's STATUS reply before keying the radio, and the
    OpenTNC has no STATUS command; it needs its own checks first. Use
    KISS mode (OpenTNCKISS) to transmit APRS with the OpenTNC.
  - PBBS: the OpenTNC has no built-in mailbox like the KAM-XL's.
  - ``mode expert`` / ``plugh!`` (reboots into the USB bootloader) --
    never sent by this library.

Radio port numbering: the OpenTNC has one radio port. Its traffic is
reported as port 2 (packet.APRS_RADIO_PORT), the port this application
treats as VHF APRS, so the daemon's APRS handling, reception history
and dashboards work unchanged and KAM-XL and OpenTNC history stays
comparable.
"""

import re
import time

from typing import Any, Dict, List, Optional, Tuple, Union

from packet import APRS_RADIO_PORT, Packet, PacketParser, TNC2_HEADER_RE
from tnc import (
    CommandInfo,
    TerminalTNC,
    TNCError,
    logger,
)


# ---------------------------------------------------------------------------
# MONITOR output
# ---------------------------------------------------------------------------
#
# Printed by DumpPacket() in the firmware's src/packetutil.c, one line
# per packet. Seen on the real unit (2026-10-19), right after the prompt:
#
#   cmd:0 (???): <[To APRS from N5BA via WIDE1-1 WIDE2-1: 03(UI) PID ^F0 =3314.34N/09650.70W-N5BA KAM-XL ^0D ]
#
# From the source:
#
#   "%d %s(%s): %c[To "   packet counter (not a channel), optional signal
#                         details ("A01234 J0.05 B1200 F1"), AX.25 version
#                         ("2.0", "2.2", "???"), direction ('<' received)
#   DEST[*][[DAMA]] from SRC[*][ via DIGI[*] DIGI[*]...]:
#                         "*" on DEST/SRC is the command/response bit;
#                         on a digipeater it is the has-been-repeated bit
#   " %02X(UI) " ...      control field (4 hex digits with v2.2 extended
#                         sequence numbers) and frame type
#   "PID ^%02X "          when the frame has one
#   payload               printable ASCII as-is, every other byte as
#                         " ^%02X " (a space on each side)
#   "]"                   straight after the payload
#
# Only '<' (received) lines become packets.

OPENTNC_MONITOR_RE: re.Pattern = re.compile(
    r"^(?:cmd:)?\s*"
    r"(?P<counter>\d+)\s+(?P<details>[^(]*?)\s*\((?P<version>[^)]*)\):\s*"
    r"(?P<direction>\S)\[To\s+(?P<destination>\S+)\s+from\s+(?P<source>\S+?)"
    r"(?:\s+via\s+(?P<digipeaters>[^:]+?))?:"
    r"(?P<rest>.*)$"
)

_OPENTNC_FRAME_RE: re.Pattern = re.compile(
    r"^\s*(?P<control>[0-9A-Fa-f]{4}|[0-9A-Fa-f]{2})"
    r"\((?P<frame_type>[A-Za-z]+)[^)]*\) "
    r"(?:PID \^(?P<pid>[0-9A-Fa-f]{2}) )?"
    r"(?P<body>.*)\]\s*$",
    re.DOTALL,
)

_ESCAPE_RE: re.Pattern = re.compile(r" \^([0-9A-F]{2}) ")

# DEST/SRC suffixes that aren't part of the callsign.
_ADDRESS_FLAGS_RE: re.Pattern = re.compile(r"(?:\*|\[DAMA\])+$")

# A packet line longer than this without its closing "]" is given up on.
_MAX_PARTIAL = 4096


def unescape_monitor_text(text: str) -> str:
    """
    Undo the OpenTNC's " ^XX " escaping of non-printable bytes, then
    present the payload the way every other path does (kiss.info_text):
    latin-1, CRs as line breaks, trailing line breaks dropped. (A
    payload that itself contains " ^41 " can't be told apart; that's the
    firmware's format.)
    """
    from kiss import info_text

    data = _ESCAPE_RE.sub(
        lambda m: chr(int(m.group(1), 16)), text,
    ).encode("latin-1", "replace")

    return info_text(data)


class OpenTNCPacketParser(PacketParser):
    """
    PacketParser for OpenTNC MONITOR lines (format above). Each OpenTNC
    packet is one self-contained line ending in "]", so it is emitted as
    soon as that line arrives -- no waiting for the next header. TNC-2
    style lines are still understood (the base parser), in case other
    firmware builds print those.
    """

    def __init__(self, default_port: int = APRS_RADIO_PORT) -> None:
        super().__init__(header_re=TNC2_HEADER_RE, default_port=default_port)
        self._ready: List[Packet] = []
        self._partial: Optional[str] = None

    def feed(self, text: str) -> List[Packet]:
        return self._take(super().feed(text))

    def flush(self) -> List[Packet]:
        packets = self._take(super().flush())
        if self._partial is not None:
            logger.debug("OpenTNC: unterminated monitor line dropped: %r",
                         self._partial)
            self._partial = None
        return packets

    def flush_pending(self) -> List[Packet]:
        return self._take(super().flush_pending())

    def _take(self, packets: List[Packet]) -> List[Packet]:
        packets = packets + self._ready
        self._ready = []
        return packets

    def _process_line(self, line: str) -> Optional[Packet]:
        if self._partial is not None:
            if OPENTNC_MONITOR_RE.match(line):
                logger.debug("OpenTNC: unterminated monitor line dropped: %r",
                             self._partial)
                self._partial = None
            else:
                line = self._partial + line
                self._partial = None

        match = OPENTNC_MONITOR_RE.match(line)

        if match is None:
            return super()._process_line(line)

        if not match.group("rest").rstrip().endswith("]"):
            if len(line) < _MAX_PARTIAL:
                self._partial = line
            return None

        if match.group("direction") != "<":
            logger.debug("OpenTNC: not a received packet, skipped: %r", line)
            return None

        previous = self._finalize_pending()
        packet = self._packet_from_match(match, line)

        if previous is None:
            return packet

        self._ready.append(packet)
        return previous

    def _packet_from_match(self, match: "re.Match[str]", line: str) -> Packet:
        rest = match.group("rest")
        frame = _OPENTNC_FRAME_RE.match(rest)

        if frame is not None:
            frame_type: Optional[str] = frame.group("frame_type").upper()
            body = frame.group("body")
        else:
            frame_type = None
            body = rest.strip()
            if body.endswith("]"):
                body = body[:-1]

        digipeaters = match.group("digipeaters")

        return Packet(
            source=_ADDRESS_FLAGS_RE.sub("", match.group("source")),
            destination=_ADDRESS_FLAGS_RE.sub("", match.group("destination")),
            digipeaters=tuple(digipeaters.split()) if digipeaters else (),
            port=self._default_port,
            payload=unescape_monitor_text(body),
            raw=line[line.index("[To") - 1:],
            frame_type=frame_type,
        )


ON_OFF = ("ON", "OFF")

FX25_CONNECTION_CHOICES = ("OFF", "REACTIVE", "OPPORTUNISTIC", "ON")
FX25_ENCODING_CHOICES = ("OFF", "MINIMUM", "MAXIMUM", "AUTOMATIC")
CARRIER_CHOICES = ("SQUELCH", "AUDIO")


# From the firmware's own HELP/DISPLAY (see module docstring). Keys are
# full command names; the OpenTNC also accepts any abbreviation down to
# its HELP capitals.
OPENTNC_COMMANDS: Dict[str, CommandInfo] = {
    # Station
    "MYCALL": CommandInfo("string"),
    "UNPROTO": CommandInfo("string"),

    # Monitoring
    "MONITOR": CommandInfo("bool"),

    # Channel access. CARRIERDETECT is SQUELCH or AUDIO -- validated by
    # set_carrier(); AUDIOCARRIER is the AUDIO-mode threshold.
    "CARRIERDETECT": CommandInfo("string"),
    "AUDIOCARRIER": CommandInfo("int"),

    # Digipeating
    "DIGIPEAT": CommandInfo("bool"),

    # FX.25 forward error correction
    "FX25CONNECTION": CommandInfo("choice", choices=FX25_CONNECTION_CHOICES),
    "FX25ENCODING": CommandInfo("choice", choices=FX25_ENCODING_CHOICES),

    # Modem
    "BAUD": CommandInfo("int"),
    "MULTIBAUD": CommandInfo("bool"),
    "TXLEVEL": CommandInfo("int"),
    "TXATTEN": CommandInfo("int"),
    "PREEMPHASIS": CommandInfo("bool"),

    # AX.25 link / timing
    "AX25VERSION": CommandInfo("string"),
    "PACLEN": CommandInfo("int"),
    "MAXFRAME": CommandInfo("int"),
    "MAXXFRAME": CommandInfo("int"),
    "FRACK": CommandInfo("int"),
    "RETRY": CommandInfo("int"),
    "RESPTIME": CommandInfo("int"),
    "CHECK": CommandInfo("int"),
    "TXDELAY": CommandInfo("int"),
    "TXTAIL": CommandInfo("int"),
    "SLOTTIME": CommandInfo("int"),
    "PERSIST": CommandInfo("int"),
    "CONOK": CommandInfo("bool"),
    "CPACTIME": CommandInfo("bool"),
    "CR": CommandInfo("bool"),

    # Read-only here: changing these switches the TNC's whole operating
    # mode (KISS ON leaves Terminal Mode; MODE EXPERT unlocks firmware
    # commands). Use enter_kiss_mode() / the OpenTNCKISS driver instead.
    "KISS": CommandInfo("string", writable=False),
    "MODE": CommandInfo("string", writable=False),
}


class OpenTNC(TerminalTNC):
    MODEL = "OpenTNC"
    NAME = "opentnc"

    COMMANDS = OPENTNC_COMMANDS

    # One MONITOR line (format above); OpenTNCPacketParser also accepts
    # TNC-2 lines.
    HEADER_RE = OPENTNC_MONITOR_RE

    # Single radio port, reported as the APRS port (see module docstring).
    DEFAULT_RADIO_PORT = APRS_RADIO_PORT

    # OpenTNC packets are complete lines and are emitted at once; this
    # covers the TNC-2 fallback, which has no end-of-packet marker.
    MONITOR_IDLE_FLUSH = 0.5

    # The Terminal Mode APRS transmit checks need a STATUS command, which
    # this firmware doesn't have (see module docstring). KISS mode works.
    APRS_TX_SUPPORTED = False

    # No built-in mailbox. Winlink over its connected mode is untried.
    HAS_PBBS = False
    HAS_WINLINK = False

    # Seen on the real unit: "Unknown command: X". EH? kept for other
    # TNC-2-style firmware builds.
    ERROR_MARKERS = ("Unknown command", "EH?", "?EH")

    # Spelled out in full: a modern parser may not accept the TNC-2
    # eight-letter abbreviation the KAM-XL driver sends.
    DISCONNECT_COMMAND = "DISCONNECT"

    CONNECT_MARKERS: Dict[str, Union[bytes, re.Pattern]] = {
        **TerminalTNC.CONNECT_MARKERS,
        "eh": re.compile(rb"Unknown command|EH\?|\?EH"),
    }

    # A bare query answers "is VALUE" (seen on the real unit); DISPLAY
    # lines are "KEY is VALUE".
    _IS_LINE_RE = re.compile(
        r"^\s*(?:(?P<key>[A-Za-z0-9!]+)\s+)?is\s+(?P<value>.*?)\s*$",
    )

    # Other TNC-2 reply shapes ("KEY VALUE", "KEY: VALUE", "KEY = VALUE"),
    # kept as a fallback for other firmware builds.
    _VALUE_LINE_RE = re.compile(
        r"^\s*(?P<key>[A-Za-z0-9!]+)\s*(?:[:=]\s*|\s+is\s+|\s+)(?P<value>.*?)\s*$",
        re.IGNORECASE,
    )

    # -----------------------------------------------------------------------
    # Parameter parsing
    # -----------------------------------------------------------------------

    def get(self, command: str) -> str:
        """
        Query a parameter and return its raw value.

        Looks for the first response line naming ``command`` (so an
        unrelated banner line ahead of it doesn't get mistaken for the
        value), falling back to the first line, like the KAM-XL driver.
        """
        command = command.upper()

        response = self.send_command(command)

        lines = [line for line in response.splitlines() if line.strip()]

        if not lines:
            return ""

        for line in lines:
            match = self._IS_LINE_RE.match(line)

            if match and (match.group("key") is None
                          or match.group("key").upper() == command):
                return match.group("value")

        for line in lines:
            match = self._VALUE_LINE_RE.match(line)

            if match and match.group("key").upper() == command:
                return match.group("value")

        match = self._VALUE_LINE_RE.match(lines[0])

        return match.group("value") if match else ""

    def get_configuration(self) -> Dict[str, str]:
        """
        Every setting from DISPLAY, keyed by its name upper-cased
        ("MYCALL", "CARRIERDETECT"), values as the TNC prints them
        ("N5BA-11", "SQuelch"). The trailing "Heap allocs" statistics
        line isn't a setting and is left out.
        """
        response = self.send_command("DISPLAY", command_timeout=20)
        config: Dict[str, str] = {}

        for line in response.splitlines():
            match = self._IS_LINE_RE.match(line)

            if match and match.group("key"):
                config[match.group("key").upper()] = match.group("value")

        return config

    def _strip_monitor_lines(self, text: str) -> str:
        """
        Remove MONITOR header lines that arrived interleaved with a
        command's response.

        TNC-2-style monitor output puts each packet's payload on its
        header line, with no blank line after it -- unlike the KAM-XL,
        whose header is followed by payload lines and a blank line.
        The base class's "header through the next blank line" rule
        would swallow the command's own reply here, so only the header
        lines themselves are dropped.
        """
        kept = []

        for line in text.splitlines():
            stripped = line.strip()

            # Includes a packet printed right after an idle prompt
            # ("cmd:0 (???): <[..."): that prompt belongs to no reply.
            if (OPENTNC_MONITOR_RE.match(stripped)
                    or TNC2_HEADER_RE.match(stripped)):
                continue

            kept.append(line)

        return "\r\n".join(kept)

    def make_packet_parser(self) -> PacketParser:
        return OpenTNCPacketParser(default_port=self.DEFAULT_RADIO_PORT)

    # -----------------------------------------------------------------------
    # OpenTNC-specific settings
    # -----------------------------------------------------------------------

    def save_settings(self, timeout: float = 10) -> str:
        """
        Write the current settings to the OpenTNC's flash (``SAVE!``).

        Without this, MYCALL, CARRIER and everything else reverts at the
        next power cycle.
        """
        return self.send_command("SAVE!", command_timeout=timeout)

    def get_carrier(self) -> str:
        """Return the carrier-detect mode, e.g. "SQUELCH" or "AUDIO"."""
        return self.get("CARRIERDETECT").strip().upper()

    def set_carrier(self, mode: str) -> str:
        """
        Set carrier detection: "SQUELCH" if the radio's squelch is closed
        when the channel is quiet, "AUDIO" if squelch is left open.
        Not saved to flash until save_settings().
        """
        mode = str(mode).strip().upper()

        if mode not in CARRIER_CHOICES:
            raise ValueError(
                f"CARRIER must be one of {CARRIER_CHOICES}, got {mode!r}"
            )

        self.set("CARRIERDETECT", mode)

        return self.get_carrier()

    def get_fx25(self) -> Tuple[str, str]:
        """Return (FX25Connection, FX25Encoding), e.g. ("OPPORTUNISTIC", "AUTOMATIC")."""
        return (
            self.get_typed("FX25CONNECTION"),
            self.get_typed("FX25ENCODING"),
        )

    def set_fx25(
        self,
        connection: Optional[str] = None,
        encoding: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        Change FX.25 forward error correction. Either argument may be
        left as None to keep its current value.

            connection: OFF | REACTIVE | OPPORTUNISTIC | ON
            encoding:   OFF | MINIMUM | MAXIMUM | AUTOMATIC

        Returns the values read back afterwards. Not saved to flash
        until save_settings().
        """
        if connection is not None:
            self.set_typed("FX25CONNECTION", connection)

        if encoding is not None:
            self.set_typed("FX25ENCODING", encoding)

        return self.get_fx25()

    # -----------------------------------------------------------------------
    # KISS
    # -----------------------------------------------------------------------

    def enter_kiss_mode(self, settle: float = 0.5) -> bytes:
        """
        Switch the OpenTNC into KISS mode (``KISS ON``).

        No ``cmd:`` prompt follows -- the port is binary KISS from here
        on, so this deliberately bypasses send_command(), which would
        otherwise wait for one and time out. Leave KISS with
        exit_kiss_mode() (C0 FF C0) or a power cycle.

        Returns whatever the TNC sent back within ``settle`` seconds,
        for the caller to inspect.
        """
        self._require_connection()

        self._drain_input()

        self.serial.write(b"KISS ON\r")
        self.serial.flush()

        data = bytearray()
        deadline = time.monotonic() + settle

        while time.monotonic() < deadline:
            chunk = self.serial.read(self.serial.in_waiting or 1)

            if chunk:
                data.extend(chunk)

        logger.debug("opentnc KISS ON response: %r", bytes(data))

        return bytes(data)

    # -----------------------------------------------------------------------
    # Not supported on this model
    # -----------------------------------------------------------------------

    def list_pbbs_messages(self, *args: Any, **kwargs: Any) -> List[Any]:
        raise TNCError("The OpenTNC has no built-in PBBS")

    def read_pbbs_message(self, *args: Any, **kwargs: Any) -> Any:
        raise TNCError("The OpenTNC has no built-in PBBS")


# ---------------------------------------------------------------------------
# KISS mode
# ---------------------------------------------------------------------------

from kiss import KISSTNC  # noqa: E402


class OpenTNCKISS(KISSTNC):
    """
    The OpenTNC in KISS mode: monitoring and APRS transmit with frames
    built here, so nothing depends on the (still unverified) Terminal
    Mode display formats above. Per NA6D's guide: ``KISS ON`` from the
    cmd: prompt enters KISS immediately; C0 FF C0 (or a power cycle)
    leaves it. KISS doesn't survive a power cycle, so nothing persists.

    The daemon does NOT take it out of KISS when it stops. Seen on the
    real unit (2026-10-19/20, twice): after C0 FF C0 and then KISS ON
    again without a power cycle, the OpenTNC still transmits (and hands
    the frames back) but never passes on another received packet until
    it's unplugged. Leaving it in KISS means a daemon restart never
    toggles it. Sending ``KISS ON`` to a unit that's already in KISS is
    harmless: a KISS decoder ignores bytes outside FEND-delimited
    frames. For the cmd: prompt, unplug and replug it.
    """

    MODEL = "OpenTNC"
    NAME = "opentnc"

    PORT_MAP = {0: APRS_RADIO_PORT}
    APRS_TX_CHANNEL = 0

    def enter_kiss_mode(self) -> None:
        self._write(b"\r")
        self._settle(0.3)
        self._write(b"KISS ON\r")
        reply = self._settle(0.5)
        logger.info("OpenTNC: KISS ON sent (reply %r)", reply[:80])

    def leave_kiss_mode(self) -> None:
        # Deliberately nothing sent -- see the class docstring.
        logger.info(
            "OpenTNC: left in KISS mode (leaving and re-entering KISS "
            "without a power cycle stops it receiving); unplug and replug "
            "it for the cmd: prompt"
        )
