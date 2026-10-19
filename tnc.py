"""
Generic driver layer for TNC-2-style packet TNCs in Terminal Mode.

Everything the daemon, REST API and web pages need from a TNC lives
here, in ``TerminalTNC``: opening the serial port, sending commands and
waiting for the ``cmd:`` prompt, typed parameter get/set, AX.25
connected mode, MONITOR traffic, Winlink over a connected session, and
the KISS exit sequence. A specific TNC model is a small subclass that
overrides the class attributes describing how *that* firmware talks:

    PROMPT               command prompt bytes (b"cmd:")
    CONNECT_MARKERS      banners seen while a CONNECT is in progress
    DISCONNECT_MARKERS   banners seen while a DISCONNECT completes
    ERROR_MARKERS        text meaning "command rejected" (e.g. "EH?")
    DISCONNECT_COMMAND   what to send to drop the AX.25 link
    COMMANDS             typed-parameter table (see CommandInfo)
    HEADER_RE            regex matching one MONITOR header line
    DEFAULT_RADIO_PORT   port number for monitor lines that carry none
    APRS_TX_SUPPORTED    whether aprs_tx.py's transmit path is verified
    HAS_PBBS             built-in mailbox (the KAM-XL's PBBS)
    HAS_WINLINK          Winlink client verified with this TNC
                         against this model's STATUS/UNPROTO output

Drivers:

    kamxl.KAMXL       Kantronics KAM-XL (verified on real hardware)
    opentnc.OpenTNC   NA6D / AE6EO OpenTNC (TNC-2 style, unverified)

``create_tnc(model, port)`` picks one by name; see TNC_MODELS.

The defaults below are the KAM-XL's, because that's the firmware this
code was written and debugged against over the air. Per the project's
design philosophy, a new model's overrides should come from captured
real-hardware transcripts (see opentnc_capture.py), not from a manual.
"""

import logging
import re
import os
import serial
import time

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

import lzhuf

from packet import HEADER_RE, Packet, PacketParser
from winlink import (
    B2Proposal,
    OutgoingMessage,
    Proposal,
    WinlinkMessage,
    WinlinkProtocolError,
    build_b2_block,
    build_b2_proposal_line,
    build_encapsulated_message,
    build_fs_line,
    build_handshake_response,
    build_proposal_block,
    generate_mid,
    has_end_of_block_marker,
    has_fq_marker,
    parse_any_proposals,
    parse_b2_blocks,
    parse_disconnect_reason,
    parse_encapsulated_message,
    parse_fs_response,
    parse_message_block,
    parse_secure_challenge,
    split_message_blocks,
    winlink_message_from_encapsulated,
)


# Same logger name as before the driver split, so existing -v/--verbose
# output and any logging configuration keyed on "kamxl" keep working.
logger = logging.getLogger("kamxl")
logger.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# Command metadata
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandInfo:
    type: str
    writable: bool = True
    # Allowed values for "choice" / "multiport_choice" types, e.g.
    # DIGIPEAT's ("ON", "UIONLY", "OFF") or FULLDUP's ("ON", "OFF",
    # "LOOPBACK"). Left as None when every value is legal (or validation
    # isn't practical, e.g. free-form strings).
    choices: Optional[Tuple[str, ...]] = None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
#
# The class names stay KAM*, from before the driver split, because the
# daemon reports ``type(exc).__name__`` as ``error.type`` on its socket
# and REST API (see docs/daemon.md) -- renaming them would break every
# client that checks for "KAMError". The TNC* names are aliases for new
# code; both spellings are the same classes, whichever driver raised.

class KAMError(Exception):
    """Base exception for TNC errors (any driver)."""
    pass


class KAMCommandError(KAMError):
    """The TNC rejected or did not understand a command."""
    pass


class KAMTimeoutError(KAMError):
    """The TNC did not respond within the expected time."""
    pass


class KAMConnectionError(KAMError):
    """An AX.25 connected-mode connection failed."""
    pass


TNCError = KAMError
TNCCommandError = KAMCommandError
TNCTimeoutError = KAMTimeoutError
TNCConnectionError = KAMConnectionError


# ---------------------------------------------------------------------------
# Generic Terminal Mode TNC
# ---------------------------------------------------------------------------

class TerminalTNC:
    # Human-readable model name, used in error messages and logs.
    MODEL: str = "TNC"

    # Short name accepted by create_tnc() / --tnc / TNCSTATION_TNC.
    NAME: str = "generic"

    # Text meaning the TNC rejected a command.
    ERROR_MARKERS: Tuple[str, ...] = ("EH?",)

    # Sent (followed by CR) from Command mode to drop the AX.25 link.
    DISCONNECT_COMMAND: str = "DISCONNE"

    # Typed-parameter table used by get_typed()/set_typed().
    COMMANDS: Dict[str, CommandInfo] = {}

    # One MONITOR header line; see packet.py.
    HEADER_RE: re.Pattern = HEADER_RE

    # Radio port reported for monitor lines that don't name one.
    DEFAULT_RADIO_PORT: int = 1

    # Seconds of serial silence after which a monitored packet still
    # being assembled is treated as complete (PacketParser.flush_pending).
    # None: wait for the next header line, as the KAM-XL always has --
    # its APRS messages end with a blank line, so they don't need this.
    MONITOR_IDLE_FLUSH: Optional[float] = None

    # aprs_tx.py's live transmit path parses STATUS/UNPROTO output in a
    # model-specific way. Leave False until that parsing has been
    # verified against the real firmware.
    APRS_TX_SUPPORTED: bool = False

    # Built-in mailbox (PBBS) and the Winlink client (winlink.py), both
    # written against the KAM-XL. The web pages hide what a station's TNC
    # can't do (daemon status "features").
    HAS_PBBS: bool = True
    HAS_WINLINK: bool = True

    def make_packet_parser(self) -> PacketParser:
        """
        Return a PacketParser that understands this model's MONITOR
        output. The daemon's monitor thread and monitor() both use it.
        """
        return PacketParser(
            header_re=self.HEADER_RE,
            default_port=self.DEFAULT_RADIO_PORT,
        )

    def _is_error_response(self, text: str) -> bool:
        return any(marker in text for marker in self.ERROR_MARKERS)

    PROMPT: bytes = b"cmd:"

    # Markers used while attempting an AX.25 CONNECT.
    #
    # The manual's own sample transcripts are inconsistent about the exact
    # text the KAM-XL prints: page 31 shows "*** CONNECTED TO callsign"
    # (upper-case "TO"), but the live examples on page 37 and in the
    # message reference on page 196 show "*** CONNECTED to callsign" /
    # "***CONNECTED to call" (lower-case "to", sometimes no space after
    # "***"). These patterns are matched case-insensitively and tolerate
    # a missing space so a real unit isn't mistaken for a timeout.
    #
    # The busy message is "***(callsign) busy" -- the callsign sits
    # between "***" and "busy", so it can't be matched as a fixed string.
    CONNECT_MARKERS: Dict[str, Union[bytes, re.Pattern]] = {
        "connected": re.compile(
            rb"\*\*\*\s*connected\s+to",
            re.IGNORECASE
        ),
        "retry_exceeded": re.compile(
            rb"\*\*\*\s*retry count exceeded",
            re.IGNORECASE
        ),
        "disconnected": re.compile(
            rb"\*\*\*\s*disconnected",
            re.IGNORECASE
        ),
        "busy": re.compile(
            rb"\*\*\*\S*\s*busy",
            re.IGNORECASE
        ),
        "eh": b"EH?",
    }

    # Markers used while waiting for a DISCONNECT to complete.
    DISCONNECT_MARKERS: Dict[str, Union[bytes, re.Pattern]] = {
        "disconnected": re.compile(
            rb"\*\*\*\s*disconnected",
            re.IGNORECASE
        ),
        "cant_disconnect": re.compile(
            rb"can't\s+disconnect",
            re.IGNORECASE
        ),
    }

    def __init__(
        self,
        port: str,
        baudrate: int = 19200,
        timeout: float = 0.25
    ) -> None:
        self.port: str = port
        self.baudrate: int = baudrate
        self.timeout: float = timeout
        self.serial: Optional[serial.Serial] = None

    # -----------------------------------------------------------------------
    # Serial connection
    # -----------------------------------------------------------------------

    def connect(self) -> None:
        """
        Open the serial connection to the TNC.
        """
        if self.serial and self.serial.is_open:
            return

        serial_options = {
            "port": self.port,
            "baudrate": self.baudrate,
            "bytesize": 8,
            "parity": "N",
            "stopbits": 1,
            "timeout": self.timeout,
        }

        # PySerial exclusive mode is supported on POSIX systems.
        # On Windows, omit this argument.
        if os.name == "posix":
            serial_options["exclusive"] = True

        self.serial = serial.Serial(**serial_options)

        time.sleep(0.1)

    def disconnect(self) -> None:
        """
        Close the serial connection to the TNC.
        """
        if self.serial and self.serial.is_open:
            self.serial.close()

    @property
    def is_connected(self) -> bool:
        return bool(
            self.serial
            and self.serial.is_open
        )

    def _require_connection(self) -> None:
        if not self.is_connected:
            raise TNCError(f"{self.MODEL} is not connected")

    def _drain_input(self) -> None:
        """
        Discard anything sitting in the receive buffer, including
        bytes still in flight.

        A plain reset_input_buffer() can race with a trailing "cmd:"
        prompt that hasn't fully arrived yet (observed right before
        issuing CONNECT), leaving a stray fragment for the next read
        to pick up. Clear, give the KAM-XL a moment to finish
        transmitting, then clear again.
        """
        self.serial.reset_input_buffer()
        time.sleep(0.05)
        self.serial.reset_input_buffer()

    def _strip_leading_prompt(self, text: str) -> str:
        """
        Remove a stray leading "cmd:" prompt glued to the front of a
        read, left over from a race with _drain_input(). It carries
        no useful content and otherwise leaks into things like
        connect_station()'s returned banner.
        """
        prompt = self.PROMPT.decode("ascii")

        if text.startswith(prompt):
            return text[len(prompt):]

        return text

    # -----------------------------------------------------------------------
    # Low-level serial reading
    # -----------------------------------------------------------------------

    def _read_until_prompt(self, timeout: float = 10) -> bytes:
        """
        Read until the KAM-XL command prompt is received.
        """
        self._require_connection()

        data = bytearray()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            chunk = self.serial.read(
                self.serial.in_waiting or 1
            )

            if chunk:
                data.extend(chunk)

                if self.PROMPT in data:
                    return bytes(data)

        raise TNCTimeoutError(
            f"Timed out waiting for {self.MODEL} command prompt"
        )

    def _read_until_any(
        self,
        markers: Dict[str, Union[bytes, re.Pattern]],
        timeout: float = 30,
        require_line_end: bool = False,
        line_end_grace: float = 0.5
    ) -> Tuple[str, Optional[str]]:
        """
        Read until one of the supplied markers is received.

        ``markers`` is a dict mapping a name to either a bytes literal or
        a compiled bytes regular expression. Regular expressions are
        useful when the text to match includes variable content, such as
        a callsign embedded between "***" and "busy".

        Some markers only need to see the *start* of a line to match
        (e.g. CONNECT_MARKERS' "connected" regex, which fires on
        "*** connected to" alone). Without ``require_line_end``, the
        rest of that line can still be in flight and gets returned
        truncated -- observed on real hardware with a VIA digipeat
        banner: "*** CONNECTED to KD5EOC-10 VIA RS" returned, with
        "STN\\r\\n" trickling in a moment later as if it were ordinary
        post-connect traffic. When ``require_line_end`` is True, once a
        marker matches, reading continues for up to ``line_end_grace``
        seconds (extended each time new bytes arrive) until the
        buffered data ends with a newline.

        Returns:
            tuple:
                decoded_text,
                matched_name

        If the timeout expires before any marker matches, matched_name
        will be None.
        """
        self._require_connection()

        data = bytearray()
        deadline = time.monotonic() + timeout
        matched_name: Optional[str] = None

        while time.monotonic() < deadline:
            chunk = self.serial.read(
                self.serial.in_waiting or 1
            )

            if chunk:
                data.extend(chunk)

                if matched_name is None:
                    for name, marker in markers.items():
                        if isinstance(marker, re.Pattern):
                            found = marker.search(data)
                        else:
                            found = marker in data

                        if found:
                            matched_name = name
                            break

                if matched_name is not None:
                    if not require_line_end:
                        break

                    if data.endswith((b"\r\n", b"\n")):
                        break

                    # Give the rest of the line a short grace period
                    # to arrive rather than returning it truncated.
                    deadline = time.monotonic() + line_end_grace

        return (
            bytes(data).decode(
                "ascii",
                errors="replace"
            ),
            matched_name
        )

    def read_available(self) -> str:
        """
        Return any bytes currently waiting from the KAM-XL.

        Useful for monitored packet traffic.
        """
        self._require_connection()

        if not self.serial.in_waiting:
            return ""

        data = self.serial.read(
            self.serial.in_waiting
        )

        # Preserve all 256 byte values in monitored AX.25 payloads.
        # Mic-E position reports can contain bytes above 0x7F.
        return data.decode("latin-1")

    def listen(
        self,
        seconds: float = 60,
        callback: Optional[Callable[[str], None]] = None
    ) -> str:
        """
        Listen to unsolicited KAM-XL output for a period of time.

        If callback is supplied, each chunk of decoded text is passed to it.

        Otherwise received text is returned as one string.
        """
        self._require_connection()

        deadline = time.monotonic() + seconds
        received: List[str] = []

        while time.monotonic() < deadline:
            text = self.read_available()

            if text:
                if callback:
                    callback(text)
                else:
                    received.append(text)

            time.sleep(0.05)

        return "".join(received)

    def monitor(
        self,
        seconds: Optional[float] = None,
        callback: Optional[Callable[[Packet], None]] = None
    ) -> Optional[Iterator[Packet]]:
        """
        Monitor unsolicited KAM-XL traffic, decoded into Packet
        objects instead of raw text.

        Unlike listen(), which captures a fixed window of raw text,
        monitor() defaults to running indefinitely (seconds=None) --
        it's meant for continuous use, with the caller deciding when
        to stop.

        If callback is supplied, each Packet is passed to it as soon
        as it's parsed, and this call blocks for up to seconds (or
        forever, until interrupted, if seconds is None):

            kam.monitor(callback=my_function)

        Otherwise, monitor() returns a generator yielding Packets as
        they arrive:

            for packet in kam.monitor():
                ...

        Note: a packet still being assembled when the generator stops
        (deadline reached, or the caller breaks out of the loop
        early) is only flushed and yielded if it was the deadline
        that ended things -- Python doesn't allow a generator to
        yield once more after being closed early via break/.close().
        """
        self._require_connection()

        parser = self.make_packet_parser()

        deadline: Optional[float] = (
            None
            if seconds is None
            else time.monotonic() + seconds
        )

        idle_flush = self.MONITOR_IDLE_FLUSH

        def packets() -> Iterator[Packet]:
            last_data = time.monotonic()

            while (
                deadline is None
                or time.monotonic() < deadline
            ):
                text = self.read_available()

                if text:
                    last_data = time.monotonic()

                    for packet in parser.feed(text):
                        yield packet
                elif (
                    idle_flush is not None
                    and time.monotonic() - last_data >= idle_flush
                ):
                    for packet in parser.flush_pending():
                        yield packet

                time.sleep(0.05)

            for packet in parser.flush():
                yield packet

        if callback is None:
            return packets()

        for packet in packets():
            callback(packet)

        return None

    # -----------------------------------------------------------------------
    # Terminal command handling
    # -----------------------------------------------------------------------

    def _strip_monitor_lines(self, text: str) -> str:
        """
        Remove any KAM-XL MONITOR-format lines embedded in a command's
        response text.

        Real bug found live (Dave's daemon log, a plain "VERSION"
        command sent through tncstation_daemon.py/tncstation_rest.py's web
        terminal): unsolicited MONITOR traffic and a command's response
        share one physical serial stream on real hardware (see
        docs/daemon.md's "Known limitation" -- previously flagged as
        "not yet hit in testing," now confirmed live). When a packet
        from another station arrives in the gap between sending a
        command and its response completing, it gets read in as if it
        were part of that response -- no client-side locking can
        prevent it, since it's the KAM-XL's own firmware interleaving
        them on the wire. Observed: three genuine monitored packets
        (from KD5EOC-10 and KC5GOI-1) landed ahead of the KAM-XL's own
        real reply to "VERSION", which came back as "EH?" -- the
        KAM-XL's own command parser apparently choked while busy
        emitting all that monitor traffic. A bare retry immediately
        succeeded, confirming this was transient interference from
        real RF traffic, not a lasting problem with the command itself.

        Recognizes a monitored packet the same way packet.py's
        PacketParser does (HEADER_RE), but with a narrower, more
        conservative rule for where each one ends: a header line,
        everything up to and including the next blank line (or the
        next header line, or the end of text if neither ever appears).
        This matches the blank-line-separated shape seen in the real
        capture above, and is deliberately simpler than PacketParser's
        own "a packet ends only when the next header line arrives"
        rule -- that rule would have swallowed the real capture's own
        "EH?" tail too, since nothing after it looked like another
        header. Good enough to clean up a command's response; not
        meant to replace PacketParser for genuine monitor decoding
        (which still runs separately, in tncstation_daemon.py's background
        thread, whenever it isn't locked out by an in-flight command).

        Known limitation of this heuristic: a monitored packet with no
        blank line after it (rare in what's been observed so far, but
        not something the KAM-XL is documented to guarantee) would
        swallow whatever genuine response text follows it too, same as
        packet.py's HEADER_RE itself needed a real-hardware correction
        after its first live test -- expect the same kind of scrutiny
        here if a case like that ever turns up.

        This does NOT recover the lost monitor data for stations.py's
        passive station database -- see docs/daemon.md's "Known
        limitation" for why that's a separate, harder problem left
        alone for now (a deliberate scope choice, not an oversight).
        This only stops embedded MONITOR noise from corrupting what
        send_command() returns or raises.
        """
        lines = text.splitlines()
        kept: List[str] = []
        index = 0

        while index < len(lines):
            line = lines[index]

            if self.HEADER_RE.match(line.strip()):
                index += 1

                while index < len(lines) and lines[index].strip():
                    index += 1

                if index < len(lines):
                    index += 1  # consume the trailing blank line too

                continue

            kept.append(line)
            index += 1

        return "\r\n".join(kept)

    def _remove_command_echo(self, text: str, command: str) -> str:
        """
        Remove the first line if it is simply the KAM echoing the command.

        This makes the library work with either ECHO ON or ECHO OFF.
        """
        lines = text.splitlines()

        if (
            lines
            and lines[0].strip().upper()
            == command.strip().upper()
        ):
            lines = lines[1:]

        return "\r\n".join(lines)

    def send_command(
        self,
        command: str,
        command_timeout: float = 10
    ) -> str:
        """
        Send a Terminal Mode command and wait for cmd:.

        Returns the command response without the trailing prompt.
        """
        self._require_connection()

        command = str(command).strip()

        self.serial.reset_input_buffer()

        self.serial.write(
            command.encode("ascii") + b"\r"
        )
        self.serial.flush()

        raw = self._read_until_prompt(
            timeout=command_timeout
        )

        response, _, _ = raw.partition(
            self.PROMPT
        )

        text = response.decode(
            "ascii",
            errors="replace"
        )

        text = text.strip("\r\n")

        # Strip any embedded MONITOR traffic (a real packet from
        # another station, arriving in the gap between this command
        # and its response -- see _strip_monitor_lines()'s docstring)
        # before anything else touches the text -- both the echo
        # check below (which only looks at the first line) and the
        # EH? check depend on this happening first.
        filtered = self._strip_monitor_lines(text)

        if filtered != text:
            logger.debug(
                "send_command(%r): removed embedded MONITOR traffic "
                "from response: %r -> %r",
                command, text, filtered
            )

        text = filtered

        # Account for ECHO ON.
        text = self._remove_command_echo(
            text,
            command
        )

        text = text.strip("\r\n")

        if self._is_error_response(text):
            raise TNCCommandError(
                f"{self.MODEL} rejected command "
                f"{command!r}: {text!r}"
            )

        return text

    # -----------------------------------------------------------------------
    # Generic get/set
    # -----------------------------------------------------------------------

    def get(self, command: str) -> str:
        """
        Query a KAM-XL parameter and return its raw value.
        """
        command = command.upper()

        response = self.send_command(command)

        lines = response.splitlines()

        if not lines:
            return ""

        # Most parameter queries return:
        #
        # COMMAND   VALUE
        #
        # Use the first meaningful line.
        line = lines[0].strip()

        parts = line.split(maxsplit=1)

        if len(parts) == 2:
            return parts[1]

        return ""

    def set(self, command: str, value: Any) -> str:
        """
        Set a raw KAM-XL parameter value.
        """
        command = command.upper()

        return self.send_command(
            f"{command} {value}"
        )

    # -----------------------------------------------------------------------
    # DISPLAY / configuration
    # -----------------------------------------------------------------------

    def get_configuration(self) -> Dict[str, str]:
        """
        Return the common one-line DISPLAY settings as a dictionary.

        Some KAM-XL DISPLAY entries are multiline and require specialized
        parsers; this method intentionally handles the normal one-line
        configuration entries.
        """
        response = self.send_command(
            "DISPLAY",
            command_timeout=20
        )

        config: Dict[str, str] = {}

        for line in response.splitlines():
            line = line.strip()

            if not line:
                continue

            parts = line.split(maxsplit=1)

            if len(parts) == 2:
                key, value = parts
                config[key] = value
            elif len(parts) == 1:
                # Some commands display with no trailing value at all
                # when they're unset (e.g. a blank MYCALL). Record them
                # with an empty value instead of silently dropping them.
                config[parts[0]] = ""

        return config

    # -----------------------------------------------------------------------
    # Default radio port
    # -----------------------------------------------------------------------


    # -----------------------------------------------------------------------
    # Boolean conversion
    # -----------------------------------------------------------------------

    @staticmethod
    def _parse_on_off(value: str) -> bool:
        value = value.strip().upper()

        if value == "ON":
            return True

        if value == "OFF":
            return False

        raise ValueError(
            f"Expected ON or OFF, got {value!r}"
        )

    @staticmethod
    def _format_on_off(value: bool) -> str:
        if not isinstance(value, bool):
            raise TypeError(
                "Value must be True or False"
            )

        return "ON" if value else "OFF"

    def get_bool(self, command: str) -> bool:
        return self._parse_on_off(
            self.get(command)
        )

    def set_bool(self, command: str, value: bool) -> str:
        return self.set(
            command,
            self._format_on_off(value)
        )

    # -----------------------------------------------------------------------
    # Restricted-choice parameters (e.g. DIGIPEAT: ON/UIONLY/OFF)
    # -----------------------------------------------------------------------

    def get_choice(self, command: str) -> str:
        """
        Query a single-value, restricted-choice parameter.
        """
        return self.get(command).strip().upper()

    def set_choice(
        self,
        command: str,
        value: Any,
        choices: Optional[Tuple[str, ...]] = None
    ) -> str:
        """
        Set a single-value, restricted-choice parameter.

        If choices is supplied, value is validated against it before being
        sent to the KAM-XL.
        """
        value = str(value).strip().upper()

        if choices and value not in choices:
            raise ValueError(
                f"{command} must be one of {choices}, got {value!r}"
            )

        self.set(command, value)

        return self.get_choice(command)

    # -----------------------------------------------------------------------
    # Multi-port parameters
    # -----------------------------------------------------------------------

    def get_multiport(self, command: str) -> Tuple[str, str]:
        """
        Return a two-port value as:

            (port1_value, port2_value)
        """
        value = self.get(command)

        parts = value.split(
            "/",
            maxsplit=1
        )

        if len(parts) != 2:
            raise TNCError(
                f"{command} did not return "
                f"a multi-port value: {value!r}"
            )

        return (
            parts[0].strip(),
            parts[1].strip()
        )

    def get_multiport_bool(self, command: str) -> Tuple[bool, bool]:
        port1, port2 = self.get_multiport(
            command
        )

        return (
            self._parse_on_off(port1),
            self._parse_on_off(port2),
        )

    def set_multiport_bool(
        self,
        command: str,
        port: int,
        value: bool
    ) -> Tuple[bool, bool]:
        """
        Change a boolean setting for only one radio port.

        Port 1:
            ON/
            OFF/

        Port 2:
            /ON
            /OFF
        """
        if port not in (1, 2):
            raise ValueError(
                "KAM-XL port must be 1 or 2"
            )

        formatted = self._format_on_off(
            value
        )

        if port == 1:
            parameter = f"{formatted}/"
        else:
            parameter = f"/{formatted}"

        self.set(
            command,
            parameter
        )

        return self.get_multiport_bool(
            command
        )

    def get_multiport_choice(self, command: str) -> Tuple[str, str]:
        """
        Return a two-port restricted-choice value as:

            (port1_value, port2_value)

        Values are returned upper-cased and stripped (e.g. FULLDUP's
        ON / OFF / LOOPBACK).
        """
        port1, port2 = self.get_multiport(
            command
        )

        return (
            port1.strip().upper(),
            port2.strip().upper(),
        )

    def set_multiport_choice(
        self,
        command: str,
        port: int,
        value: Any,
        choices: Optional[Tuple[str, ...]] = None
    ) -> Tuple[str, str]:
        """
        Change a restricted-choice setting for only one radio port.

        If choices is supplied, value is validated against it before being
        sent to the KAM-XL.
        """
        if port not in (1, 2):
            raise ValueError(
                "KAM-XL port must be 1 or 2"
            )

        value = str(value).strip().upper()

        if choices and value not in choices:
            raise ValueError(
                f"{command} must be one of {choices}, got {value!r}"
            )

        if port == 1:
            parameter = f"{value}/"
        else:
            parameter = f"/{value}"

        self.set(
            command,
            parameter
        )

        return self.get_multiport_choice(
            command
        )

    # -----------------------------------------------------------------------
    # Typed parameters
    # -----------------------------------------------------------------------

    def get_typed(self, command: str) -> Any:
        """
        Query a known KAM-XL parameter and convert it into an appropriate
        Python type.
        """
        command = command.upper()

        metadata = self.COMMANDS.get(
            command
        )

        if metadata is None:
            return self.get(command)

        value_type = metadata.type

        if value_type == "multiport_bool":
            return self.get_multiport_bool(
                command
            )

        if value_type == "multiport_choice":
            return self.get_multiport_choice(
                command
            )

        if value_type == "multiport_int":
            left, right = self.get_multiport(
                command
            )

            return (
                int(left),
                int(right)
            )

        if value_type == "multiport_string":
            return self.get_multiport(
                command
            )

        if value_type == "choice":
            return self.get_choice(
                command
            )

        if value_type == "bool":
            return self.get_bool(
                command
            )

        if value_type == "int":
            return int(
                self.get(command)
            )

        return self.get(command)

    def set_typed(self, command: str, value: Any) -> Any:
        """
        Set a known parameter using normal Python values.

        The new value is read back after the write.
        """
        command = command.upper()

        metadata = self.COMMANDS.get(
            command
        )

        # Unknown commands can still use the raw API.
        if metadata is None:
            return self.set(
                command,
                value
            )

        if not metadata.writable:
            raise TNCError(
                f"{command} is read-only"
            )

        value_type = metadata.type

        if value_type == "multiport_bool":
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or not all(
                    isinstance(v, bool)
                    for v in value
                )
            ):
                raise TypeError(
                    f"{command} requires "
                    f"a tuple of two booleans"
                )

            formatted = (
                f"{self._format_on_off(value[0])}/"
                f"{self._format_on_off(value[1])}"
            )

            self.set(
                command,
                formatted
            )

            return self.get_typed(
                command
            )

        if value_type == "multiport_choice":
            if (
                not isinstance(value, tuple)
                or len(value) != 2
            ):
                raise TypeError(
                    f"{command} requires "
                    f"a tuple of two values"
                )

            formatted_values = []

            for v in value:
                v = str(v).strip().upper()

                if (
                    metadata.choices
                    and v not in metadata.choices
                ):
                    raise ValueError(
                        f"{command} must be one of "
                        f"{metadata.choices}, got {v!r}"
                    )

                formatted_values.append(v)

            self.set(
                command,
                f"{formatted_values[0]}/{formatted_values[1]}"
            )

            return self.get_typed(
                command
            )

        if value_type == "multiport_int":
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or not all(
                    isinstance(v, int)
                    for v in value
                )
            ):
                raise TypeError(
                    f"{command} requires "
                    f"a tuple of two integers"
                )

            self.set(
                command,
                f"{value[0]}/{value[1]}"
            )

            return self.get_typed(
                command
            )

        if value_type == "multiport_string":
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or not all(
                    isinstance(v, str)
                    for v in value
                )
            ):
                raise TypeError(
                    f"{command} requires "
                    f"a tuple of two strings"
                )

            self.set(
                command,
                f"{value[0]}/{value[1]}"
            )

            return self.get_typed(
                command
            )

        if value_type == "choice":
            return self.set_choice(
                command,
                value,
                choices=metadata.choices
            )

        if value_type == "bool":
            self.set_bool(
                command,
                value
            )

            return self.get_typed(
                command
            )

        if value_type == "int":
            if not isinstance(value, int):
                raise TypeError(
                    f"{command} requires an integer"
                )

            self.set(
                command,
                value
            )

            return self.get_typed(
                command
            )

        self.set(
            command,
            value
        )

        return self.get_typed(
            command
        )

    # -----------------------------------------------------------------------
    # AX.25 connected mode
    # -----------------------------------------------------------------------

    def connect_station(
        self,
        callsign: str,
        via: Optional[Union[str, List[str]]] = None,
        timeout: float = 60
    ) -> str:
        """
        Attempt an AX.25 connected-mode connection.

        Examples:

            kam.connect_station("KD5EOC-10")

            kam.connect_station(
                "KD5EOC-10",
                via="RSSTN"
            )

            kam.connect_station(
                "TARGET",
                via=["DIGI1", "DIGI2"]
            )

        A successful CONNECT may put the KAM-XL into Convers mode.
        """
        self._require_connection()

        callsign = callsign.strip().upper()

        command = f"CONNECT {callsign}"

        if via:
            if isinstance(via, str):
                via = [via]

            path = ",".join(
                str(digi).strip().upper()
                for digi in via
            )

            command += f" VIA {path}"

        self._drain_input()

        self.serial.write(
            command.encode("ascii") + b"\r"
        )
        self.serial.flush()

        text, marker = self._read_until_any(
            self.CONNECT_MARKERS,
            timeout=timeout,
            require_line_end=True
        )

        text = self._strip_leading_prompt(text)

        if marker == "connected":
            return text

        if marker is None:
            raise TNCTimeoutError(
                f"Timed out attempting connection "
                f"to {callsign}"
            )

        raise TNCConnectionError(
            f"Could not connect to {callsign}: "
            f"{text.strip()}"
        )

    def send_connected(
        self,
        text: Any,
        add_cr: bool = True
    ) -> None:
        """
        Send text while the KAM-XL is in Convers mode.
        """
        self._require_connection()

        data = str(text).encode("ascii")

        if add_cr:
            data += b"\r"

        self.serial.write(data)
        self.serial.flush()

    def send_connected_bytes(self, data: bytes) -> None:
        """
        Send raw bytes while the KAM-XL is in Convers mode -- unlike
        send_connected(), does NOT encode through ASCII first.

        Needed for Winlink's B2 binary message-body transfer (mirrors
        read_connected_bytes()'s reasoning exactly, just for the write
        direction): send_connected()'s ``str(text).encode("ascii")``
        would raise ``UnicodeEncodeError`` outright for any byte >=
        0x80, and LZHUF-compressed bytes (see winlink.build_b2_block())
        span the full 0-255 range. Everything in a Winlink exchange
        BEFORE the actual compressed message bytes (the handshake, our
        own proposal lines, even our own "FC ..." B2 proposal line) is
        still plain ASCII text and continues to use send_connected()
        unchanged -- only the binary block itself, built by
        winlink.build_b2_block(), needs this.
        """
        self._require_connection()

        self.serial.write(data)
        self.serial.flush()

    def read_connected(
        self,
        timeout: float = 5
    ) -> str:
        """
        Collect connected-mode data for up to timeout seconds.
        """
        self._require_connection()

        data = bytearray()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            chunk = self.serial.read(
                self.serial.in_waiting or 1
            )

            if chunk:
                data.extend(chunk)

        return bytes(data).decode(
            "ascii",
            errors="replace"
        )

    def read_connected_bytes(
        self,
        timeout: float = 5
    ) -> bytes:
        """
        Collect connected-mode data for up to timeout seconds, as raw
        bytes -- unlike read_connected(), does NOT decode through
        ASCII.

        Needed for Winlink's B2 binary message-body transfer (milestone
        8 extension): LZHUF-compressed bytes span the full 0-255 range,
        and read_connected()'s ``errors="replace"`` ASCII decode would
        silently replace every byte >= 0x80 (roughly half of all
        possible compressed byte values) with U+FFFD, irreversibly
        corrupting the data. This was found while designing B2 support,
        not from a hardware test -- a real gap in the existing
        connected-mode read path, which was built for genuinely
        text-only protocols (PBBS commands, Winlink's own ASCII
        handshake/proposal lines) and was never exercised with a real
        binary payload until B2 needed one. Everything in a Winlink
        exchange BEFORE the actual compressed message bytes (the
        handshake, the proposal lines themselves, even a "FC ..." B2
        proposal line) is still plain ASCII and continues to use
        read_connected() unchanged -- only the binary block framing
        after an "FS" accept needs this.
        """
        self._require_connection()

        data = bytearray()
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            chunk = self.serial.read(
                self.serial.in_waiting or 1
            )

            if chunk:
                data.extend(chunk)

        return bytes(data)

    def exit_kiss_mode(self, timeout: float = 5) -> bytes:
        """
        Send the standard KISS exit sequence (C0 FF C0).

        Documented for both the KAM-XL and the OpenTNC.

        Caller must hold exclusive serial access and confirm
        that all radio interfaces are physically disconnected.

        Returns raw response bytes for verification by caller.
        Does not reset or reconfigure the TNC.
        """
        self._require_connection()

        self._drain_input()

        self.serial.write(bytes([0xC0, 0xFF, 0xC0]))
        self.serial.flush()

        return self.read_connected_bytes(timeout=timeout)

    def enter_command_mode(
        self,
        timeout: float = 5
    ) -> str:
        """
        Send Ctrl-C to return from Convers mode to Command mode.
        """
        self._require_connection()

        self._drain_input()

        self.serial.write(
            b"\x03"
        )
        self.serial.flush()

        text, marker = self._read_until_any(
            {"prompt": self.PROMPT},
            timeout=timeout
        )

        if marker is None:
            # KAM-XL manual: Ctrl-C may produce no prompt when
            # the TNC is already in Command mode. A carriage
            # return should produce cmd: in that mode.
            # Transparent mode will not produce the prompt.
            self._drain_input()
            self.serial.write(b"\r")
            self.serial.flush()

            text, marker = self._read_until_any(
                {"prompt": self.PROMPT},
                timeout=timeout,
            )

            if marker is None:
                raise TNCTimeoutError(
                    "Timed out verifying Command mode after "
                    "Ctrl-C and carriage return; "
                    f"received={text[:200]!r}"
                )

        return text

    def disconnect_station(
        self,
        timeout: float = 30,
        command_mode_timeout: float = 5
    ) -> str:
        """
        Return to Command mode and disconnect the current AX.25 link.

        ``timeout`` bounds the DISCONNECT confirmation step;
        ``command_mode_timeout`` separately bounds the initial Ctrl-C
        step that gets back to Command mode first (previously always
        hardcoded to enter_command_mode()'s own 5s default, regardless
        of what a caller passed for ``timeout`` -- surprising, since
        the two steps run sequentially and a caller asking for a more
        patient disconnect overall would reasonably expect that to
        cover both).
        """
        self._require_connection()

        self.enter_command_mode(timeout=command_mode_timeout)

        self._drain_input()

        self.serial.write(
            self.DISCONNECT_COMMAND.encode("ascii") + b"\r"
        )
        self.serial.flush()

        text, marker = self._read_until_any(
            self.DISCONNECT_MARKERS,
            timeout=timeout,
            require_line_end=True
        )

        text = self._strip_leading_prompt(text)

        if marker is None:
            raise TNCTimeoutError(
                "Timed out waiting for AX.25 disconnect"
            )

        return text

    def _poll_until(
        self,
        done: Callable[[str], bool],
        timeout: float
    ) -> str:
        """
        Collect connected-mode text in short slices, stopping as soon
        as ``done(accumulated_text)`` returns True rather than always
        waiting out one fixed-duration read_connected() call.

        Generalized from a PBBS-specific helper (originally
        _collect_pbbs_response(), now a thin wrapper around this) after
        a real bug: a single read_connected(timeout=N) call just
        returns whatever arrived in N seconds, whether or not the
        far end was actually done sending -- a real PBBS message
        (found live, on hardware) had its last line silently truncated
        because it took slightly longer than the old fixed 5s window
        to fully arrive. Polling in short slices and checking a
        predicate means the common case returns as soon as it's
        actually finished, and ``timeout`` becomes a true worst-case
        ceiling instead of "the length of every single call, whether
        needed or not." Milestone 8 (Winlink) reuses this directly,
        since its multi-stage handshake/proposal/message exchange has
        several different "are we done yet" conditions, not just one
        fixed prompt string the way PBBS has.
        """
        deadline = time.monotonic() + timeout
        text = ""

        while True:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                break

            text += self.read_connected(timeout=min(1.0, remaining))

            if done(text):
                break

        return text

    def _poll_until_bytes(
        self,
        done: Callable[[bytes], bool],
        timeout: float
    ) -> bytes:
        """
        Bytes-returning analog of _poll_until(), for the one part of a
        Winlink exchange that isn't plain text: the binary-framed B2
        message body, after an "FS" accept for one or more B2Proposals
        (milestone 8 extension -- see read_connected_bytes()'s
        docstring for why this can't reuse the str-based _poll_until()
        without corrupting data).
        """
        deadline = time.monotonic() + timeout
        data = b""

        while True:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                break

            data += self.read_connected_bytes(timeout=min(1.0, remaining))

            if done(data):
                break

        return data

    # -----------------------------------------------------------------------
    # Winlink (Milestone 8, extended with real B2 support)
    # -----------------------------------------------------------------------
    #
    # Connects to a real Winlink RMS Packet gateway over AX.25 and
    # downloads whatever mail is waiting -- receive-only for this
    # first pass (see winlink.py's module docstring for the full scope
    # writeup: no outbound send yet, single message block per call).
    # Now speaks both the plain-ASCII FBB tier (legacy Proposal/"FB")
    # AND Winlink's own B2 extension (B2Proposal/"FC", LZHUF-compressed,
    # binary-framed -- see lzhuf.py and winlink.py's B2 section) --
    # added after a real gateway (KD5EOC-10) was found to require B2
    # and disconnect rather than fall back to ASCII (see
    # winlink.parse_disconnect_reason()'s docstring). Built from the
    # public B2F/FBB specs and two independently-authored reference
    # implementations cross-checked against each other (see lzhuf.py's
    # module docstring) -- still UNVERIFIED against a real populated
    # mailbox, expect adjustment once actually tested, the same way
    # pbbs.py's parsing was before Dave's real-hardware pass confirmed
    # (and partially corrected) it.

    def check_winlink_mail(
        self,
        gateway: str,
        password: str,
        mycall: Optional[str] = None,
        connect_timeout: float = 60,
        read_timeout: float = 30,
    ) -> List[WinlinkMessage]:
        """
        Connect to a Winlink RMS Packet gateway, complete the
        secure-login handshake, and download whatever mail is waiting
        -- up to one proposal block (5 messages) per call; see
        winlink.py's module docstring for why. Never proposes an
        outbound message of our own (receive-only MVP scope) -- this
        can't be used to send mail yet.

        Handles both plain-ASCII (legacy "FB") and B2 ("FC",
        LZHUF-compressed) proposals -- whichever the gateway actually
        sends is reflected in each returned WinlinkMessage's richness:
        a B2 message carries the real structured header (subject,
        from, to, cc, attachment metadata); a legacy ASCII message only
        ever has a plain title and body (see winlink.py's module
        docstring). Raises WinlinkProtocolError if a single block ever
        mixes both kinds (not supported -- see winlink.py's "MIXED-
        BATCH SCOPE LIMIT" note) or if a B2 message fails to
        decompress/checksum-verify.

        ``mycall`` defaults to the KAM-XL's own MYCALL (its first port
        value, if MYCALL is a multi-port setting) -- this needs to
        match your registered Winlink account callsign, or the
        gateway's secure login will reject it. ``password`` is your
        real Winlink account password, sent as an 8-digit challenge
        response, never in the clear (see winlink.secure_login_response()) --
        still, don't log it; this method deliberately never does.

        Returns an empty list if there's no mail waiting (the gateway
        replies "FQ" instead of proposing anything) -- not an error.

        Raises ``TNCConnectionError`` if the gateway hangs up on us
        mid-exchange (detected via the KAM-XL's own "*** DISCONNECTED"
        banner -- see winlink.parse_disconnect_reason()'s docstring).
        The exception message quotes whatever reason the gateway gave,
        verbatim -- see winlink.py's module docstring's "KNOWN
        DISCONNECT REASONS" note for the real ones found so far; more
        than one unrelated cause has already turned up in testing, so
        this deliberately doesn't guess which applies.
        """
        if mycall is None:
            # MYCALL can be a multi-port value like "N5BA-10/N5BA-10"
            # -- Winlink account identity isn't tied to a specific
            # radio port, so only the first one is used.
            mycall = self.get("MYCALL").split("/")[0].strip()

        self.connect_station(gateway, timeout=connect_timeout)

        # Set the instant a "*** DISCONNECTED" banner is spotted in any
        # polled text -- see _raise_if_gateway_hung_up() below. Read by
        # the finally block: once the KAM-XL has already printed that
        # banner and returned to Command mode on its own, a further
        # disconnect_station() call has no fresh "cmd:" prompt left to
        # wait for (the one that arrived is already consumed) and
        # reliably times out instead of doing anything useful.
        gateway_already_disconnected = False

        def _raise_if_gateway_hung_up(text: str) -> None:
            nonlocal gateway_already_disconnected

            reason = parse_disconnect_reason(text)

            if reason is None:
                return

            gateway_already_disconnected = True

            detail = f" (\"{reason}\")" if reason else ""

            # Deliberately doesn't guess WHY the gateway hung up in the
            # message text below -- it used to always claim "this can
            # happen if the gateway requires B2 protocol support,"
            # which was accurate for the first real disconnect this was
            # built from (KD5EOC-10 demanding B2) but went stale and
            # actively misleading the moment a second, unrelated real
            # disconnect reason showed up in testing ("Unknown client
            # types are not allowed on production servers -- use
            # cms-z.winlink.org", i.e. the production CMS rejecting
            # this client's own identification, nothing to do with B2
            # at all -- see winlink.py's module docstring's "KNOWN
            # DISCONNECT REASONS" note). The gateway's own stated
            # reason is already quoted verbatim below -- that's more
            # trustworthy than any canned guess this code could add.
            raise TNCConnectionError(
                f"{gateway} disconnected before completing the "
                f"Winlink exchange{detail}. See winlink.py's module "
                f"docstring for known real-world reasons this has "
                f"happened before."
            )

        try:
            handshake_text = self._poll_until(
                lambda text: text.rstrip().endswith(">"),
                read_timeout
            )

            logger.debug("winlink handshake raw: %r", handshake_text)

            _raise_if_gateway_hung_up(handshake_text)

            challenge = None

            for line in handshake_text.splitlines():
                challenge = parse_secure_challenge(line)

                if challenge is not None:
                    break

            response = build_handshake_response(
                mycall,
                secure_challenge=challenge,
                password=password if challenge else None,
            )

            # "FF" tells the gateway we have nothing to propose --
            # receive-only MVP, see this method's docstring.
            self.send_connected(response + "\rFF")

            proposal_text = self._poll_until(
                lambda text: (
                    has_end_of_block_marker(text) or has_fq_marker(text)
                ),
                read_timeout
            )

            logger.debug("winlink proposals raw: %r", proposal_text)

            _raise_if_gateway_hung_up(proposal_text)

            if has_fq_marker(proposal_text):
                return []

            proposals = parse_any_proposals(proposal_text)

            if not proposals:
                return []

            is_b2 = any(isinstance(p, B2Proposal) for p in proposals)
            is_legacy = any(isinstance(p, Proposal) for p in proposals)

            if is_b2 and is_legacy:
                # See winlink.py's module docstring, "MIXED-BATCH SCOPE
                # LIMIT" -- not expected in practice against a real
                # Winlink gateway, and not worth the complexity of
                # interleaving two different wire formats to read back
                # a scenario that can't currently be tested against
                # anything real.
                raise WinlinkProtocolError(
                    "Gateway proposed a mix of legacy-ASCII (FB) and "
                    "B2 (FC) messages in the same block -- not "
                    "supported yet (see winlink.py's module docstring)"
                )

            self.send_connected(build_fs_line(len(proposals)))

            if is_b2:
                messages_bytes = self._poll_until_bytes(
                    lambda data: len(parse_b2_blocks(data, len(proposals)))
                    >= len(proposals),
                    read_timeout
                )

                logger.debug("winlink b2 messages raw: %r", messages_bytes)

                # Lossy on purpose -- this is only ever used to spot a
                # plain-ASCII marker substring (the KAM-XL's own
                # disconnect banner), never to recover real data, so
                # losing information on non-ASCII compressed bytes
                # doesn't matter here (see read_connected_bytes()'s
                # docstring for why that lossy decode is NOT
                # acceptable for the actual message bytes themselves).
                _raise_if_gateway_hung_up(
                    messages_bytes.decode("ascii", errors="replace")
                )

                b2_blocks = parse_b2_blocks(messages_bytes, len(proposals))
            else:
                messages_text = self._poll_until(
                    lambda text: text.count("\x1a") >= len(proposals),
                    read_timeout
                )

                logger.debug("winlink messages raw: %r", messages_text)

                _raise_if_gateway_hung_up(messages_text)
        finally:
            if not gateway_already_disconnected:
                self.disconnect_station()

        if is_b2:
            messages = []

            for block, proposal in zip(b2_blocks, proposals):
                try:
                    raw = lzhuf.decompress_b2(block.compressed_data)
                    encapsulated = parse_encapsulated_message(raw)
                except lzhuf.LZHUFError as exc:
                    raise WinlinkProtocolError(
                        f"Failed to decompress B2 message {proposal.mid!r}: {exc}"
                    ) from exc

                messages.append(
                    winlink_message_from_encapsulated(proposal, encapsulated)
                )

            return messages

        blocks = split_message_blocks(messages_text, len(proposals))

        return [
            parse_message_block(block, proposal)
            for block, proposal in zip(blocks, proposals)
        ]

    def send_winlink_message(
        self,
        gateway: str,
        password: str,
        messages: List[OutgoingMessage],
        mycall: Optional[str] = None,
        connect_timeout: float = 60,
        read_timeout: float = 30,
    ) -> List[str]:
        """
        Connect to a Winlink RMS Packet gateway, complete secure login,
        and send one to five outgoing messages -- see winlink.py's
        module docstring's "SEND SUPPORT" note for the full scope:
        send-only (never downloads -- that's check_winlink_mail()'s
        job, called separately if wanted), text-body-only (no
        attachments), B2/FC only, no persistent outbound queue or
        partial-resume support.

        Returns the MID of each message the gateway actually accepted,
        in ``messages`` order -- a message the gateway rejected,
        deferred, or erred on (see winlink.parse_fs_response()) is
        simply left out of the returned list. An empty return means
        nothing was accepted (but the call still completed normally --
        this is not itself an error).

        Whatever the gateway proposes back to us (the protocol's
        implicit acknowledgment step, reversing transfer direction) is
        always declined -- see the module docstring for why this
        method deliberately doesn't also download in the same call.

        ``mycall``/``password`` behave exactly as in
        check_winlink_mail() -- see that method's docstring. Raises
        ``TNCConnectionError`` the same way if the gateway hangs up
        mid-exchange, and ``winlink.WinlinkProtocolError`` if the
        gateway's "FS" answer to our proposal is malformed (wrong
        count, unrecognized code) or never arrives.

        Raises ``ValueError`` if ``messages`` is empty or has more
        than 5 entries -- the FBB protocol's own per-block limit (see
        winlink.py's module docstring's "SINGLE BLOCK ONLY" note,
        which applies here exactly as it does to check_winlink_mail(),
        just in the opposite direction).

        UNVERIFIED AGAINST A REAL GATEWAY -- see winlink.py's module
        docstring's "SEND SUPPORT" section. Built and offline-tested
        against the same two cross-checked sources as the rest of B2,
        but no account with permission to actually deliver mail through
        a real RMS gateway has confirmed this round-trips over the air
        yet.
        """
        if not messages:
            raise ValueError(
                "messages must contain at least one OutgoingMessage"
            )

        if len(messages) > 5:
            raise ValueError(
                f"Got {len(messages)} messages, but the FBB protocol "
                f"allows at most 5 proposals per block"
            )

        if mycall is None:
            # MYCALL can be a multi-port value like "N5BA-10/N5BA-10"
            # -- Winlink account identity isn't tied to a specific
            # radio port, so only the first one is used.
            mycall = self.get("MYCALL").split("/")[0].strip()

        self.connect_station(gateway, timeout=connect_timeout)

        # Same pattern as check_winlink_mail() -- see that method for
        # the full story of why this matters (a gateway that hangs up
        # mid-exchange leaves the KAM-XL already back in Command mode,
        # which a naive disconnect_station() call would then hang
        # waiting on).
        gateway_already_disconnected = False

        def _raise_if_gateway_hung_up(text: str) -> None:
            nonlocal gateway_already_disconnected

            reason = parse_disconnect_reason(text)

            if reason is None:
                return

            gateway_already_disconnected = True

            detail = f" (\"{reason}\")" if reason else ""

            raise TNCConnectionError(
                f"{gateway} disconnected before completing the "
                f"Winlink exchange{detail}. See winlink.py's module "
                f"docstring for known real-world reasons this has "
                f"happened before."
            )

        accepted_mids: List[str] = []

        try:
            handshake_text = self._poll_until(
                lambda text: text.rstrip().endswith(">"),
                read_timeout
            )

            logger.debug("winlink send handshake raw: %r", handshake_text)

            _raise_if_gateway_hung_up(handshake_text)

            challenge = None

            for line in handshake_text.splitlines():
                challenge = parse_secure_challenge(line)

                if challenge is not None:
                    break

            response = build_handshake_response(
                mycall,
                secure_challenge=challenge,
                password=password if challenge else None,
            )

            # Resolve each message's MID exactly once up front (see
            # generate_mid()'s docstring for why) and build everything
            # needed to both propose and, if accepted, transmit it --
            # each message's raw bytes are built exactly once, so the
            # proposal line's declared size is guaranteed to match
            # what actually gets compressed and sent (rebuilding it a
            # second time for the size alone would risk a mismatch,
            # since build_encapsulated_message() stamps the current
            # time into the Date: header).
            prepared = []

            for msg in messages:
                mid = msg.mid or generate_mid(mycall)
                raw = build_encapsulated_message(mid, msg, mycall)
                compressed = lzhuf.compress_b2(raw)

                prepared.append((mid, msg, raw, compressed))

            proposal_lines = [
                build_b2_proposal_line(mid, len(raw), len(compressed))
                for mid, _msg, raw, compressed in prepared
            ]

            proposal_block = build_proposal_block(proposal_lines)

            self.send_connected(response + "\r" + proposal_block)

            fs_text = self._poll_until(
                lambda text: any(
                    line.strip() == "FS" or line.strip().startswith("FS ")
                    for line in text.splitlines()
                ),
                read_timeout
            )

            logger.debug("winlink send FS raw: %r", fs_text)

            _raise_if_gateway_hung_up(fs_text)

            fs_line = next(
                (
                    line.strip() for line in fs_text.splitlines()
                    if line.strip() == "FS" or line.strip().startswith("FS ")
                ),
                None
            )

            if fs_line is None:
                raise WinlinkProtocolError(
                    f"Gateway never answered our proposal with an FS "
                    f"line: {fs_text!r}"
                )

            answers = parse_fs_response(fs_line, len(messages))

            for (mid, msg, _raw, compressed), answer in zip(prepared, answers):
                if answer != "accept":
                    continue

                title = msg.subject or "No title"
                block = build_b2_block(title, compressed)
                self.send_connected_bytes(block)
                accepted_mids.append(mid)

            # Yield the turn: per the FBB protocol, the gateway now
            # either proposes its own waiting mail back (an implicit
            # ack that it received our block) or says it has nothing
            # ("FF"). This method never downloads (see docstring) --
            # whatever the gateway offers here is always declined.
            #
            # Checking for a bare "FF" here (unlike
            # has_end_of_block_marker()'s own documented caution
            # against doing exactly that) is safe in this specific
            # spot: the transmission immediately preceding this poll
            # always ends in either "F> XX" (our proposal block's
            # checksum trailer, from send_connected() above) or raw
            # binary EOT bytes (from send_connected_bytes() above, if
            # any message was accepted) -- never a literal "FF" the
            # way check_winlink_mail()'s own transmission does, so
            # there's no echo of our own text to mistake for the
            # gateway's genuine reply here.
            reciprocal_text = self._poll_until(
                lambda text: (
                    has_end_of_block_marker(text)
                    or has_fq_marker(text)
                    or any(line.strip() == "FF" for line in text.splitlines())
                ),
                read_timeout
            )

            logger.debug("winlink send reciprocal raw: %r", reciprocal_text)

            _raise_if_gateway_hung_up(reciprocal_text)

            if has_end_of_block_marker(reciprocal_text):
                their_proposals = parse_any_proposals(reciprocal_text)

                if their_proposals:
                    # Send-only scope (see winlink.py's module
                    # docstring) -- always decline whatever the
                    # gateway offers back. Downloading it is a
                    # separate call (check_winlink_mail()), not this
                    # method's job. No further text needs to follow
                    # this "FS" reject line -- disconnect_station() in
                    # the finally block below ends the session the
                    # same way check_winlink_mail() already does,
                    # without a separate explicit "FQ"/"FF" first.
                    self.send_connected(
                        build_fs_line(len(their_proposals), accept=False)
                    )
        finally:
            if not gateway_already_disconnected:
                self.disconnect_station()

        return accepted_mids


# ---------------------------------------------------------------------------
# Driver selection
# ---------------------------------------------------------------------------

# Short name -> (module, class) per mode. Imported lazily so tnc.py
# never imports a driver module at load time (each imports tnc.py).
TNC_MODELS: Dict[str, Dict[str, Tuple[str, str]]] = {
    "kamxl": {
        "terminal": ("kamxl", "KAMXL"),
        "kiss": ("kamxl", "KAMXLKISS"),
    },
    "opentnc": {
        "terminal": ("opentnc", "OpenTNC"),
        "kiss": ("opentnc", "OpenTNCKISS"),
    },
    # Already in KISS, or a KISS server such as Direwolf (tcp://host:8001).
    "kiss": {
        "kiss": ("kiss", "GenericKISS"),
    },
}

# Accepted spellings, e.g. TNCSTATION_TNC=kam-xl or --tnc OpenTNC.
_MODEL_ALIASES: Dict[str, str] = {
    "kam": "kamxl",
    "kam-xl": "kamxl",
    "kam_xl": "kamxl",
    "kantronics": "kamxl",
    "open-tnc": "opentnc",
    "open_tnc": "opentnc",
    "na6d": "opentnc",
    "direwolf": "kiss",
    "generic": "kiss",
}

DEFAULT_TNC_MODEL = "kamxl"

TNC_MODES = ("terminal", "kiss")


def normalize_tnc_model(model: Optional[str]) -> str:
    """
    Return the canonical short name for ``model`` ("kamxl", "opentnc",
    "kiss"), defaulting to the KAM-XL when ``model`` is empty. Raises
    ValueError for anything unrecognized.
    """
    value = (model or "").strip().lower() or DEFAULT_TNC_MODEL
    value = _MODEL_ALIASES.get(value, value)

    if value not in TNC_MODELS:
        raise ValueError(
            f"Unknown TNC model {model!r} "
            f"(expected one of: {', '.join(sorted(TNC_MODELS))})"
        )

    return value


def normalize_tnc_mode(model: Optional[str], mode: Optional[str]) -> str:
    """
    Return "terminal" or "kiss" for ``model``. Empty means the model's
    default: Terminal Mode where it has one, else KISS. Raises
    ValueError for an unknown mode or one the model doesn't have.
    """
    name = normalize_tnc_model(model)
    modes = TNC_MODELS[name]
    value = (mode or "").strip().lower()

    if not value:
        return "terminal" if "terminal" in modes else "kiss"

    if value not in TNC_MODES:
        raise ValueError(
            f"Unknown TNC mode {mode!r} (expected one of: {', '.join(TNC_MODES)})"
        )

    if value not in modes:
        raise ValueError(f"TNC model {name!r} has no {value} mode")

    return value


def tnc_class(model: Optional[str], mode: Optional[str] = None) -> type:
    """Return the driver class for ``model`` in ``mode`` (see normalize_tnc_mode)."""
    import importlib

    name = normalize_tnc_model(model)
    module_name, class_name = TNC_MODELS[name][normalize_tnc_mode(name, mode)]

    return getattr(importlib.import_module(module_name), class_name)


def create_tnc(
    model: Optional[str],
    port: str,
    baudrate: Optional[int] = None,
    mode: Optional[str] = None,
    **kwargs: Any
) -> TerminalTNC:
    """
    Build (but don't open) a driver for ``model`` on ``port``.

        tnc = create_tnc("opentnc", "/dev/ttyACM0")
        tnc = create_tnc("opentnc", "/dev/ttyACM0", mode="kiss")
        tnc = create_tnc("kiss", "tcp://localhost:8001")    # Direwolf
        tnc.connect()

    ``baudrate`` of None keeps the driver's own default.
    """
    cls = tnc_class(model, mode)

    if baudrate is not None:
        kwargs["baudrate"] = baudrate

    return cls(port, **kwargs)
