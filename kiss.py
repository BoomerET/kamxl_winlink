"""
KISS mode: talk to a TNC in raw AX.25 frames instead of Terminal Mode
text.

In Terminal Mode the TNC formats every monitored packet as text and
answers commands on the same serial stream, so this project has to
parse its display format (packet.py) and untangle monitor traffic from
command replies (see docs/daemon.md, "Known limitation"). In KISS mode
the TNC just passes frames: each received packet arrives as the exact
AX.25 bytes, and transmitting means handing it a frame we built. The
format is the same on every TNC, so a KISS station doesn't depend on
any one firmware's display quirks.

What KISS covers here: monitoring (the map, stations, history,
telemetry, the packet log) and APRS transmit (messages, ACKs, resends,
beacons). Connected mode -- PBBS, Winlink, the web terminal -- needs
the TNC's own AX.25 link layer, which only Terminal Mode exposes, so
those raise a clear error in KISS mode.

Contents:

    kiss_escape / kiss_frame / KISSDeframer   KISS framing (FEND/FESC)
    encode_address / encode_ui_frame          AX.25 frame building
    decode_ax25                               AX.25 frame parsing
    KISSPacketParser                          frames -> packet.Packet
    KISSTNC                                   the driver (serial or TCP)

Drivers built on KISSTNC:

    kiss.GenericKISS        a TNC already in KISS, or Direwolf's KISS
                            TCP port (tcp://localhost:8001)
    opentnc.OpenTNCKISS     sends KISS ON at connect, C0 FF C0 at close
    kamxl.KAMXLKISS         INTFACE KISS + RESET at connect; restores
                            INTFACE TERMINAL at close (unverified)

References: the KISS protocol (Chepponis & Karn, 1987) and the AX.25
Link Access Protocol v2.2 spec, sections 3 (frame structure) and 4.2
(control field).
"""

import os
import re
import select
import socket
import time

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import serial

from packet import APRS_RADIO_PORT, Packet
from tnc import TerminalTNC, TNCError, logger


# ---------------------------------------------------------------------------
# KISS framing
# ---------------------------------------------------------------------------

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

CMD_DATA = 0x00

# Leave KISS mode (documented for the KAM-XL and the OpenTNC).
KISS_EXIT = bytes([FEND, 0xFF, FEND])


def kiss_escape(data: bytes) -> bytes:
    """Escape FEND/FESC inside a frame's contents."""
    return (
        data.replace(bytes([FESC]), bytes([FESC, TFESC]))
            .replace(bytes([FEND]), bytes([FESC, TFEND]))
    )


def kiss_unescape(data: bytes) -> bytes:
    out = bytearray()
    escaped = False

    for byte in data:
        if escaped:
            out.append({TFEND: FEND, TFESC: FESC}.get(byte, byte))
            escaped = False
        elif byte == FESC:
            escaped = True
        else:
            out.append(byte)

    return bytes(out)


def kiss_frame(ax25: bytes, channel: int = 0) -> bytes:
    """Wrap an AX.25 frame as a KISS data frame for ``channel`` (0-15)."""
    if not 0 <= channel <= 15:
        raise ValueError(f"KISS channel must be 0-15, got {channel}")

    return bytes([FEND, (channel << 4) | CMD_DATA]) + kiss_escape(ax25) + bytes([FEND])


class KISSDeframer:
    """
    Splits a byte stream into KISS data frames.

    Bytes arrive in arbitrary chunks; a frame is complete at its closing
    FEND. Anything outside FENDs (a TNC's text banner, line noise) is
    discarded, as are non-data commands. Yields (channel, ax25_bytes).
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._in_frame = False

    def feed(self, data: bytes) -> List[Tuple[int, bytes]]:
        frames: List[Tuple[int, bytes]] = []

        for byte in data:
            if byte == FEND:
                if self._in_frame and self._buffer:
                    frame = self._finish()
                    if frame is not None:
                        frames.append(frame)
                # Every FEND (re)starts a frame; back-to-back FENDs are
                # just padding.
                self._buffer.clear()
                self._in_frame = True
            elif self._in_frame:
                self._buffer.append(byte)
                if len(self._buffer) > 4096:      # runaway: no FEND seen
                    self._buffer.clear()
                    self._in_frame = False

        return frames

    def _finish(self) -> Optional[Tuple[int, bytes]]:
        command = self._buffer[0]
        channel, kind = command >> 4, command & 0x0F

        if kind != CMD_DATA:
            return None

        return channel, kiss_unescape(bytes(self._buffer[1:]))


# ---------------------------------------------------------------------------
# AX.25
# ---------------------------------------------------------------------------

CONTROL_UI = 0x03
PID_NO_LAYER3 = 0xF0

_CALL_RE = re.compile(r"^([A-Z0-9]{1,6})(?:-(\d{1,2}))?$")


def parse_callsign(text: str) -> Tuple[str, int]:
    """'N5BA-10' -> ('N5BA', 10). Raises ValueError for anything AX.25 can't carry."""
    match = _CALL_RE.match(str(text).strip().upper())

    if not match or int(match.group(2) or 0) > 15:
        raise ValueError(f"Not a valid AX.25 address: {text!r}")

    return match.group(1), int(match.group(2) or 0)


def encode_address(text: str, last: bool, high_bit: bool = False) -> bytes:
    """
    One 7-byte AX.25 address field: six callsign characters shifted
    left one bit (space padded), then the SSID byte 0b?11SSSS? -- top bit
    is the command/response bit (destination/source) or has-been-
    repeated bit (digipeater), bottom bit marks the last address.
    """
    call, ssid = parse_callsign(text)
    out = bytes((ord(c) << 1) for c in call.ljust(6))
    ssid_byte = 0x60 | (ssid << 1) | (0x80 if high_bit else 0) | (0x01 if last else 0)
    return out + bytes([ssid_byte])


def encode_ui_frame(
    source: str,
    destination: str,
    path: Sequence[str],
    info: bytes,
) -> bytes:
    """
    An AX.25 UI frame (what APRS uses): addresses, control 0x03, PID 0xF0,
    then the information field. Sent as a v2 command frame (destination
    C bit set, source clear); digipeaters not yet repeated.
    """
    path = [p for p in path if p]

    if len(path) > 8:
        raise ValueError("AX.25 allows at most 8 digipeaters")

    frame = encode_address(destination, last=False, high_bit=True)
    frame += encode_address(source, last=not path)

    for i, digi in enumerate(path):
        frame += encode_address(digi, last=(i == len(path) - 1))

    return frame + bytes([CONTROL_UI, PID_NO_LAYER3]) + info


@dataclass(frozen=True)
class AX25Frame:
    source: str
    destination: str
    digipeaters: Tuple[str, ...]   # TNC-2 style: "*" after the last repeater
    frame_type: str                # "UI", "I", "RR", "SABM", ...
    info: bytes


def _decode_address(field: bytes) -> Tuple[str, bool, bool]:
    """-> (callsign text, high bit, is-last)."""
    chars = [b >> 1 for b in field[:6]]

    if any(not (0x30 <= c <= 0x39 or 0x41 <= c <= 0x5A or c == 0x20) for c in chars):
        raise ValueError("bad address character")

    call = bytes(chars).decode("ascii").rstrip()

    if not call:
        raise ValueError("empty address")

    ssid = (field[6] >> 1) & 0x0F
    text = f"{call}-{ssid}" if ssid else call
    return text, bool(field[6] & 0x80), bool(field[6] & 0x01)


def _frame_type(control: int) -> str:
    if control & 0x01 == 0:
        return "I"

    if control & 0x03 == 0x01:
        return {0x01: "RR", 0x05: "RNR", 0x09: "REJ", 0x0D: "SREJ"}.get(control & 0x0F, "S")

    return {
        0x03: "UI", 0x2F: "SABM", 0x6F: "SABME", 0x43: "DISC",
        0x0F: "DM", 0x63: "UA", 0x87: "FRMR", 0xAF: "XID", 0xE3: "TEST",
    }.get(control & ~0x10 & 0xFF, "U")


def decode_ax25(frame: bytes) -> AX25Frame:
    """
    Parse an AX.25 frame. Raises ValueError for anything malformed.

    Digipeater marking follows the TNC-2 convention the rest of this
    project (and the KAM-XL) uses: only the *last* digipeater whose
    has-been-repeated bit is set gets a "*".
    """
    addresses: List[Tuple[str, bool]] = []
    offset = 0

    while True:
        if len(frame) < offset + 7:
            raise ValueError("frame ends inside the address field")

        text, high, last = _decode_address(frame[offset:offset + 7])
        addresses.append((text, high))
        offset += 7

        if last:
            break

        if len(addresses) > 10:
            raise ValueError("too many addresses")

    if len(addresses) < 2:
        raise ValueError("frame needs a source and destination")

    if len(frame) <= offset:
        raise ValueError("frame has no control field")

    control = frame[offset]
    offset += 1
    kind = _frame_type(control)

    info = b""
    if kind in ("UI", "I"):
        if len(frame) <= offset:
            raise ValueError("frame has no PID")
        info = frame[offset + 1:]       # skip the PID

    digis = addresses[2:]
    repeated = [i for i, (_, high) in enumerate(digis) if high]
    starred = repeated[-1] if repeated else None

    return AX25Frame(
        source=addresses[1][0],
        destination=addresses[0][0],
        digipeaters=tuple(
            call + ("*" if i == starred else "") for i, (call, _) in enumerate(digis)
        ),
        frame_type=kind,
        info=info,
    )


def info_text(info: bytes) -> str:
    """
    Information field as text, the way the Terminal Mode path presents
    it: latin-1, so every byte survives (Mic-E uses bytes above 0x7F);
    CRs become line breaks; trailing line breaks dropped.
    """
    text = info.decode("latin-1").replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n")


# ---------------------------------------------------------------------------
# Frames -> Packets
# ---------------------------------------------------------------------------

class KISSPacketParser:
    """
    Drop-in for packet.PacketParser in the daemon's monitor loop:
    feed() takes what KISSTNC.read_available() returned (raw bytes as
    latin-1 text) and returns packet.Packet objects, the same shape the
    Terminal Mode parser produces. KISS frames are self-delimiting, so
    there is never anything left to flush.
    """

    def __init__(self, port_map: Dict[int, int]) -> None:
        self._deframer = KISSDeframer()
        self._port_map = dict(port_map)
        self.dropped = 0

    def feed(self, text: str) -> List[Packet]:
        packets: List[Packet] = []

        for channel, ax25 in self._deframer.feed(text.encode("latin-1")):
            try:
                frame = decode_ax25(ax25)
            except ValueError as exc:
                self.dropped += 1
                logger.debug("kiss: dropped undecodable frame (%s): %r", exc, ax25)
                continue

            payload = info_text(frame.info)
            path = "".join("," + d for d in frame.digipeaters)

            packets.append(Packet(
                source=frame.source,
                destination=frame.destination,
                digipeaters=frame.digipeaters,
                port=self._port_map.get(channel, 10 + channel),
                payload=payload,
                raw=f"{frame.source}>{frame.destination}{path} <{frame.frame_type}>:{payload}",
                frame_type=frame.frame_type,
            ))

        return packets

    def flush(self) -> List[Packet]:
        return []


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------

class TCPTransport:
    """
    A KISS-over-TCP connection (Direwolf's KISS port, a soundmodem,
    another KISS server) with the slice of pyserial's interface the
    drivers use.
    """

    # Nobody else is reading our socket; the daemon's transmit paths
    # check this the same way they check a serial port's exclusive lock.
    exclusive = True

    def __init__(self, host: str, port: int, timeout: float = 0.25) -> None:
        self.host, self.port = host, port
        self._timeout = timeout
        self._sock = socket.create_connection((host, port), timeout=10)
        self._sock.settimeout(timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.is_open = True

    def _closed(self) -> None:
        self.close()
        raise TNCError(f"KISS TCP connection to {self.host}:{self.port} closed")

    @property
    def in_waiting(self) -> int:
        if not self.is_open:
            return 0

        readable, _, _ = select.select([self._sock], [], [], 0)

        if not readable:
            return 0

        self._sock.setblocking(False)
        try:
            peeked = self._sock.recv(65536, socket.MSG_PEEK)
        except BlockingIOError:
            return 0
        finally:
            self._sock.settimeout(self._timeout)

        if not peeked:
            self._closed()

        return len(peeked)

    def read(self, n: int = 1) -> bytes:
        if not self.is_open:
            return b""

        try:
            data = self._sock.recv(max(1, n))
        except socket.timeout:
            return b""

        if not data:
            self._closed()

        return data

    def write(self, data: bytes) -> int:
        self._sock.sendall(data)
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        while self.in_waiting:
            self.read(65536)

    def close(self) -> None:
        if self.is_open:
            self.is_open = False
            self._sock.close()


def parse_tcp_address(port: str) -> Optional[Tuple[str, int]]:
    """'tcp://localhost:8001' -> ('localhost', 8001); None if not tcp://."""
    match = re.match(r"^tcp://(\[[^\]]+\]|[^:/]+):(\d+)/?$", str(port).strip(), re.I)

    if not match:
        return None

    return match.group(1).strip("[]"), int(match.group(2))


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------

def _terminal_only(name: str):
    def method(self, *args: Any, **kwargs: Any) -> Any:
        raise TNCError(
            f"{name}() needs Terminal Mode; this station runs the {self.MODEL} "
            f"in KISS mode (monitoring and APRS only). Connected mode, PBBS, "
            f"Winlink and TNC commands aren't available in KISS mode."
        )

    method.__name__ = name
    return method


class KISSTNC(TerminalTNC):
    """
    Base KISS driver. ``port`` is a serial device (/dev/ttyACM0, COM9) or
    tcp://host:port. Subclasses say how to get their TNC into and out
    of KISS (enter_kiss_mode / leave_kiss_mode) and how KISS channels
    map to this application's radio port numbers.
    """

    MODEL = "KISS TNC"
    NAME = "kiss"
    MODE = "kiss"

    # KISS channel -> radio port as the rest of the app numbers them
    # (port 2 is APRS -- see packet.APRS_RADIO_PORT).
    PORT_MAP: Dict[int, int] = {0: APRS_RADIO_PORT}

    # KISS channel APRS is transmitted on.
    APRS_TX_CHANNEL = 0

    # Frames are built here, so there are no TNC display formats to
    # verify; subclasses can still opt out (see kamxl.KAMXLKISS).
    APRS_TX_SUPPORTED = True

    # KISS passes raw frames: no command mode, no connected mode.
    HAS_PBBS = False
    HAS_WINLINK = False

    # The TNC is in KISS mode; there's no command prompt to talk to.
    PROMPT = b""

    def __init__(
        self,
        port: str,
        baudrate: int = 19200,
        timeout: float = 0.25,
        mycall: Optional[str] = None,
    ) -> None:
        super().__init__(port, baudrate=baudrate, timeout=timeout)
        self.mycall: Optional[str] = mycall
        self._entered_kiss = False

    # -- connection -------------------------------------------------------

    def connect(self) -> None:
        if self.serial and self.serial.is_open:
            return

        tcp = parse_tcp_address(self.port)

        if tcp:
            self.serial = self._connect_tcp(*tcp)
        else:
            options: Dict[str, Any] = {
                "port": self.port, "baudrate": self.baudrate, "bytesize": 8,
                "parity": "N", "stopbits": 1, "timeout": self.timeout,
            }
            if os.name == "posix":
                options["exclusive"] = True
            self.serial = serial.Serial(**options)
            time.sleep(0.1)

        self.enter_kiss_mode()
        self._entered_kiss = True

    # KISS over TCP: how often to retry while the server (Direwolf)
    # isn't accepting connections yet -- e.g. it starts a few seconds
    # after us at boot, or is restarting. None = keep trying.
    TCP_RETRY_SECONDS = 5.0
    TCP_MAX_ATTEMPTS: Optional[int] = None

    def _connect_tcp(self, host: str, port: int) -> "TCPTransport":
        attempt = 0
        last_logged = 0.0

        while True:
            attempt += 1
            try:
                transport = TCPTransport(host, port, timeout=self.timeout)
            except OSError as exc:
                if self.TCP_MAX_ATTEMPTS and attempt >= self.TCP_MAX_ATTEMPTS:
                    raise TNCError(
                        f"KISS TCP server at {host}:{port} isn't accepting "
                        f"connections ({exc}). Is Direwolf running, with "
                        f"KISSPORT {port}?"
                    ) from exc

                now = time.monotonic()
                if attempt == 1:
                    logger.warning(
                        "KISS TCP server at %s:%d isn't accepting connections "
                        "(%s) -- is Direwolf running, with KISSPORT %d? "
                        "Retrying every %gs.",
                        host, port, exc, port, self.TCP_RETRY_SECONDS,
                    )
                    last_logged = now
                elif now - last_logged >= 60:
                    logger.warning("Still waiting for KISS TCP server at %s:%d (%s)",
                                   host, port, exc)
                    last_logged = now

                time.sleep(self.TCP_RETRY_SECONDS)
                continue

            if attempt > 1:
                logger.info("Connected to KISS TCP server at %s:%d", host, port)
            return transport

    def disconnect(self) -> None:
        if not self.is_connected:
            return

        try:
            if self._entered_kiss:
                self.leave_kiss_mode()
        except Exception:
            logger.exception("%s: leaving KISS mode failed", self.MODEL)
        finally:
            self._entered_kiss = False
            self.serial.close()

    def enter_kiss_mode(self) -> None:
        """Put the TNC into KISS mode. Generic: it already is."""

    def leave_kiss_mode(self) -> None:
        """Undo enter_kiss_mode() at shutdown. Generic: nothing to undo."""

    def _write(self, data: bytes) -> None:
        self._require_connection()
        written = self.serial.write(data)

        if written is not None and written != len(data):
            raise TNCError(f"Short write to {self.MODEL}: {written} of {len(data)} bytes")

        self.serial.flush()

    def _settle(self, seconds: float) -> bytes:
        """Wait, collecting (and returning) whatever the TNC says meanwhile."""
        data = bytearray()
        deadline = time.monotonic() + seconds

        while time.monotonic() < deadline:
            chunk = self.serial.read(self.serial.in_waiting or 1)
            if chunk:
                data.extend(chunk)

        return bytes(data)

    # -- receive ----------------------------------------------------------

    def read_available(self) -> str:
        """Raw received bytes (KISS frames) as latin-1 text -- see KISSPacketParser."""
        self._require_connection()

        waiting = self.serial.in_waiting
        if not waiting:
            return ""

        return self.serial.read(waiting).decode("latin-1")

    def make_packet_parser(self) -> KISSPacketParser:
        return KISSPacketParser(self.PORT_MAP)

    # -- transmit ---------------------------------------------------------

    def send_ui(
        self,
        info: bytes,
        destination: str = "APRS",
        path: Sequence[str] = (),
        source: Optional[str] = None,
        channel: Optional[int] = None,
    ) -> bytes:
        """
        Transmit one AX.25 UI frame. ``source`` defaults to mycall (the
        station callsign the daemon sets); in KISS mode the TNC adds
        nothing, so there's no MYCALL to fall back on. Returns the KISS
        bytes written.
        """
        source = source or self.mycall

        if not source:
            raise TNCError("No station callsign set for KISS transmit (mycall)")

        frame = kiss_frame(
            encode_ui_frame(source, destination, list(path), info),
            self.APRS_TX_CHANNEL if channel is None else channel,
        )
        self._write(frame)
        return frame

    # -- not in KISS mode -------------------------------------------------

    def exit_kiss_mode(self, timeout: float = 5) -> bytes:
        raise TNCError(
            f"The {self.MODEL} is managed by the KISS driver; it leaves KISS "
            f"mode itself when the daemon stops."
        )

    for _name in (
        "send_command", "get", "set", "get_typed", "set_typed",
        "get_configuration", "get_bool", "set_bool", "get_choice",
        "set_choice", "get_multiport", "connect_station", "send_connected",
        "send_connected_bytes", "read_connected", "read_connected_bytes",
        "disconnect_station", "enter_command_mode", "list_pbbs_messages",
        "read_pbbs_message", "check_winlink_mail", "send_winlink_message",
        "listen",
    ):
        locals()[_name] = _terminal_only(_name)
    del _name


class GenericKISS(KISSTNC):
    """
    A TNC that's already in KISS mode, or a KISS server such as
    Direwolf (tcp://localhost:8001). Nothing is sent to enter or leave
    KISS. One radio channel, reported as the APRS port.
    """

    MODEL = "KISS TNC"
    NAME = "kiss"
