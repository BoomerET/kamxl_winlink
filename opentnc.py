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

STATUS: UNVERIFIED AGAINST REAL HARDWARE. Everything in this file that
describes what the firmware *prints* -- the MONITOR header format,
the command-rejected text, the connect/disconnect banners, how a
parameter query is answered -- comes from TNC-2 convention and the
NA6D getting-started guide, not from captured OpenTNC output. Per this
project's design philosophy (README, "Design philosophy"), run
opentnc_capture.py against the real unit first and correct the class
attributes below from what it records. What IS documented by NA6D and
the firmware author, and relied on here:

  - ``cmd:`` prompt; ``HELP`` lists commands
  - ``MYCALL``, ``CARRIER SQUELCH|AUDIO``, ``SAVE!`` (write to flash)
  - ``KISS ON`` enters KISS; C0 FF C0 (or a power cycle) leaves it
  - ``DIGIPEAT ON``, ``APRSDIGI WIDE1-1``
  - ``FX25Connection OFF|REACTIVE|OPPORTUNISTIC|ON``
  - ``FX25Encoding OFF|MINIMUM|MAXIMUM|AUTOMATIC``
  - TNC-2 timing parameters (PACLEN, MAXFRAME, FRACK, RETRY, TXDELAY,
    SLOTTIME, PERSIST, ...) -- Outpost's documented OpenTNC setup
    sends them unchanged
  - one AX.25 connection at a time; no STREAMSW
  - flow control: none (USB handles it)

What this driver deliberately does NOT do yet:

  - APRS transmit through aprs_tx.py (APRS_TX_SUPPORTED = False). That
    path validates the TNC's STATUS and UNPROTO replies line by line
    before keying the radio, and those formats haven't been seen from
    an OpenTNC. It fails closed with a clear error until they have.
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

from packet import APRS_RADIO_PORT, TNC2_HEADER_RE
from tnc import (
    CommandInfo,
    TerminalTNC,
    TNCError,
    logger,
)


ON_OFF = ("ON", "OFF")

FX25_CONNECTION_CHOICES = ("OFF", "REACTIVE", "OPPORTUNISTIC", "ON")
FX25_ENCODING_CHOICES = ("OFF", "MINIMUM", "MAXIMUM", "AUTOMATIC")
CARRIER_CHOICES = ("SQUELCH", "AUDIO")


OPENTNC_COMMANDS: Dict[str, CommandInfo] = {
    # Identity / station
    "MYCALL": CommandInfo("string"),
    "VERSION": CommandInfo("string", writable=False),

    # Monitoring
    "MONITOR": CommandInfo("bool"),

    # Channel access. CARRIER's two documented values; other firmware
    # versions may add more, so it's validated only through set_carrier().
    "CARRIER": CommandInfo("string"),

    # Digipeating
    "DIGIPEAT": CommandInfo("bool"),
    "APRSDIGI": CommandInfo("string"),

    # FX.25 forward error correction (documented values)
    "FX25CONNECTION": CommandInfo("choice", choices=FX25_CONNECTION_CHOICES),
    "FX25ENCODING": CommandInfo("choice", choices=FX25_ENCODING_CHOICES),

    # TNC-2 link / timing parameters
    "PACLEN": CommandInfo("int"),
    "MAXFRAME": CommandInfo("int"),
    "FRACK": CommandInfo("int"),
    "RETRY": CommandInfo("int"),
    "RESPTIME": CommandInfo("int"),
    "CHECK": CommandInfo("int"),
    "TXDELAY": CommandInfo("int"),
    "SLOTTIME": CommandInfo("int"),
    "PERSIST": CommandInfo("int"),
    "PPERSIST": CommandInfo("bool"),
    "CPACTIME": CommandInfo("bool"),
    "CR": CommandInfo("bool"),
}


class OpenTNC(TerminalTNC):
    MODEL = "OpenTNC"
    NAME = "opentnc"

    COMMANDS = OPENTNC_COMMANDS

    HEADER_RE = TNC2_HEADER_RE

    # Single radio port, reported as the APRS port (see module docstring).
    DEFAULT_RADIO_PORT = APRS_RADIO_PORT

    # TNC-2 monitor output has no end-of-packet marker; a packet whose
    # line has been followed by half a second of silence is complete.
    # Otherwise the last packet of a burst -- maybe a message to us --
    # would wait for the next one to be heard.
    MONITOR_IDLE_FLUSH = 0.5

    # Not until aprs_tx.py has been checked against real STATUS/UNPROTO
    # output (see module docstring).
    APRS_TX_SUPPORTED = False

    # TNC-2 clones print "EH?" (some "?EH") for an unrecognized command.
    ERROR_MARKERS = ("EH?", "?EH")

    # Spelled out in full: a modern parser may not accept the TNC-2
    # eight-letter abbreviation the KAM-XL driver sends.
    DISCONNECT_COMMAND = "DISCONNECT"

    CONNECT_MARKERS: Dict[str, Union[bytes, re.Pattern]] = {
        **TerminalTNC.CONNECT_MARKERS,
        "eh": re.compile(rb"EH\?|\?EH"),
    }

    # A parameter query reply may be "KEY VALUE", "KEY: VALUE",
    # "KEY = VALUE" or "KEY is VALUE" -- tolerated until the real format
    # is confirmed.
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
            match = self._VALUE_LINE_RE.match(line)

            if match and match.group("key").upper() == command:
                return match.group("value")

        match = self._VALUE_LINE_RE.match(lines[0])

        return match.group("value") if match else ""

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
        return "\r\n".join(
            line for line in text.splitlines()
            if not self.HEADER_RE.match(line.strip())
        )

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
        return self.get("CARRIER").strip().upper()

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

        self.set("CARRIER", mode)

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
