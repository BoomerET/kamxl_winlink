"""
Kantronics KAM-XL driver.

The generic Terminal Mode machinery (serial I/O, commands, typed
parameters, AX.25 connected mode, MONITOR traffic, Winlink) lives in
tnc.py's TerminalTNC; this module adds what only the KAM-XL has: its
two-radio-port parameter table, the PORT default-port setting, and its
built-in PBBS.

Every name this module exported before the driver split is still
importable from here (KAMXL, KAMError and friends, COMMANDS,
CommandInfo, ...), so existing scripts and the daemon keep working.
The KAM* exceptions are the same classes as tnc.py's TNC* names -- an
``except KAMError`` catches errors from any driver.
"""

# os, serial and time aren't used directly here any more, but they're
# kept importable as kamxl.os / kamxl.serial / kamxl.time: existing
# tests patch e.g. "kamxl.serial.Serial", which reaches the same module
# objects tnc.py uses.
import os  # noqa: F401
import serial  # noqa: F401
import time  # noqa: F401

from typing import Any, Dict, List, Optional, Set

# Re-exported for code that imported these from kamxl before the split.
from packet import HEADER_RE, Packet, PacketParser  # noqa: F401
from pbbs import PBBSMessage, PBBSMessageSummary, parse_message, parse_message_list
from tnc import (  # noqa: F401
    CommandInfo,
    TerminalTNC,
    TNCCommandError,
    TNCConnectionError,
    TNCError,
    TNCTimeoutError,
    create_tnc,
    logger,
)


# ---------------------------------------------------------------------------
# Command metadata
# ---------------------------------------------------------------------------
COMMANDS: Dict[str, CommandInfo] = {
    "MONITOR": CommandInfo("multiport_bool"),
    # DIGIPEAT is a *single* value (not Multi-Port per the manual), with
    # three legal states rather than a simple ON/OFF.
    "DIGIPEAT": CommandInfo(
        "choice",
        choices=("ON", "UIONLY", "OFF")
    ),
    "MCON": CommandInfo("multiport_bool"),
    # FULLDUP is Multi-Port, but each port has three legal states
    # (ON/OFF/LOOPBACK), not just ON/OFF.
    "FULLDUP": CommandInfo(
        "multiport_choice",
        choices=("ON", "OFF", "LOOPBACK")
    ),

    "MYCALL": CommandInfo("multiport_string"),
    "HBAUD": CommandInfo("multiport_int"),

    "PORT": CommandInfo("int"),

    "VERSION": CommandInfo("string", writable=False),
}

# ---------------------------------------------------------------------------
# Exceptions -- defined in tnc.py, re-exported here under their
# original names (see module docstring)
# ---------------------------------------------------------------------------

KAMError = TNCError
KAMCommandError = TNCCommandError
KAMTimeoutError = TNCTimeoutError
KAMConnectionError = TNCConnectionError


# ---------------------------------------------------------------------------
# KAM-XL
# ---------------------------------------------------------------------------

class KAMXL(TerminalTNC):
    MODEL = "KAM-XL"
    NAME = "kamxl"

    # Verified over the air: aprs_tx.py's STATUS / PORT / UNPROTO
    # parsing was written against this firmware's real output.
    APRS_TX_SUPPORTED = True

    RADIO_PORTS = 2  # 1 = HF, 2 = VHF

    # The KAM-XL's MONITOR headers always carry "/PORT:" (see packet.py),
    # so DEFAULT_RADIO_PORT is never actually used for it.
    HEADER_RE = HEADER_RE

    COMMANDS = COMMANDS

    # PROMPT, CONNECT_MARKERS, DISCONNECT_MARKERS, ERROR_MARKERS and
    # DISCONNECT_COMMAND are TerminalTNC's defaults, which are the
    # KAM-XL's own values.

    def get_default_port(self) -> int:
        """
        Return the configured default radio port.

        Note:
            PORT is the default radio port setting, not necessarily the
            currently active I/O stream.
        """
        return int(
            self.get("PORT")
        )

    def set_default_port(self, port: int) -> int:
        """
        Change the KAM-XL default radio port setting.
        """
        if port not in (1, 2):
            raise ValueError(
                "KAM-XL default port must be 1 or 2"
            )

        self.set(
            "PORT",
            port
        )

        actual = self.get_default_port()

        if actual != port:
            raise KAMError(
                f"Requested default port {port}, "
                f"but KAM-XL reports {actual}"
            )

        return actual
    # -----------------------------------------------------------------------
    # PBBS (Milestone 6)
    # -----------------------------------------------------------------------
    #
    # Not a BBS this project implements -- the KAM-XL's own firmware
    # PBBS already handles message storage, forwarding, and SYSOP
    # access. These two methods just drive it through the same
    # connected-mode primitives above (connect_station() /
    # send_connected() / read_connected() / disconnect_station()) and
    # hand the raw text off to pbbs.py for parsing. Per the manual, a
    # connect from the local serial terminal gets automatic SYSOP
    # privilege -- no password exchange needed.

    def _collect_pbbs_response(self, timeout: float) -> str:
        """
        Collect connected-mode text from a PBBS command, stopping as
        soon as its "ENTER COMMAND:" prompt reappears (meaning it's
        finished and waiting for the next command). See _poll_until().
        """
        return self._poll_until(
            lambda text: "ENTER COMMAND" in text.upper(),
            timeout
        )

    def list_pbbs_messages(
        self,
        mypbbs: Optional[str] = None,
        connect_timeout: float = 15,
        read_timeout: float = 10
    ) -> List[PBBSMessageSummary]:
        """
        Connect to the KAM-XL's own PBBS and list its messages.

        ``mypbbs`` defaults to whatever MYPBBS is currently set to on
        the KAM-XL. ``read_timeout`` is a worst-case ceiling -- see
        _collect_pbbs_response(), which normally returns as soon as
        PBBS's prompt reappears, well before this elapses. Disconnects
        with disconnect_station() when done -- deliberately not PBBS's
        own "B" (bye) command, so ending the session reuses the
        already-hardened disconnect path instead of adding a second,
        PBBS-specific way to do the same thing.
        """
        if mypbbs is None:
            mypbbs = self.get("MYPBBS")

        self.connect_station(mypbbs, timeout=connect_timeout)

        try:
            self.send_connected("L")
            text = self._collect_pbbs_response(read_timeout)
        finally:
            self.disconnect_station()

        # parse_message_list() silently skips lines it doesn't
        # recognize -- by design, so a real-hardware format surprise
        # degrades gracefully instead of raising. The tradeoff: a
        # genuinely empty mailbox and a total parsing mismatch both
        # come back as an empty list, indistinguishable from the
        # return value alone. Logging the raw text here is what makes
        # them distinguishable, for anyone running with -v.
        logger.debug("pbbs list raw: %r", text)

        return parse_message_list(text)

    def read_pbbs_message(
        self,
        number: int,
        mypbbs: Optional[str] = None,
        connect_timeout: float = 15,
        read_timeout: float = 10
    ) -> Optional[PBBSMessage]:
        """
        Connect to the KAM-XL's own PBBS and read one message.

        ``read_timeout`` is a worst-case ceiling -- see
        _collect_pbbs_response(). Returns None if the response didn't
        look like a message (e.g. the number doesn't exist) rather
        than raising -- see pbbs.parse_message()'s docstring.
        """
        if mypbbs is None:
            mypbbs = self.get("MYPBBS")

        self.connect_station(mypbbs, timeout=connect_timeout)

        try:
            self.send_connected(f"R {number}")
            text = self._collect_pbbs_response(read_timeout)
        finally:
            self.disconnect_station()

        logger.debug("pbbs read raw: %r", text)

        return parse_message(text)

    def kill_pbbs_message(
        self,
        number: int,
        mypbbs: Optional[str] = None,
        connect_timeout: float = 15,
        read_timeout: float = 10
    ) -> Dict[str, Any]:
        """
        Connect to the KAM-XL's own PBBS and delete one message ("K <n>").

        The PBBS's reply to K isn't known from the real unit yet (the
        manual has been wrong about this PBBS's wording before), so it
        isn't parsed. Instead the mailbox is listed ("L") before and
        after, in the same session: the message counts as deleted only if
        it was listed before and isn't after. Returns {"number", "found",
        "deleted", "reply"}, where ``reply`` is the PBBS's own answer to
        K, prompt stripped -- shown to the operator when ``deleted`` is
        False (not allowed, ...). ``found`` False means no such message;
        K isn't sent then.
        """
        if mypbbs is None:
            mypbbs = self.get("MYPBBS")

        self.connect_station(mypbbs, timeout=connect_timeout)

        try:
            self.send_connected("L")
            before = self._collect_pbbs_response(read_timeout)
            found = number in _listed_numbers(before)
            reply = after = ""
            if found:
                self.send_connected(f"K {number}")
                reply = self._collect_pbbs_response(read_timeout)
                self.send_connected("L")
                after = self._collect_pbbs_response(read_timeout)
        finally:
            self.disconnect_station()

        logger.debug("pbbs list before kill raw: %r", before)
        if not found:
            return {"number": number, "found": False, "deleted": False, "reply": ""}

        logger.info("pbbs kill %d raw: %r", number, reply)
        logger.debug("pbbs list after kill raw: %r", after)

        remaining = _listed_numbers(after)
        # An empty parse is only trusted when the PBBS says so; a
        # listing that didn't parse must not look like a deletion.
        listed_ok = bool(remaining) or "NO MESSAGES" in after.upper()
        reply_lines = [
            line.strip() for line in reply.splitlines()
            if line.strip() and "ENTER COMMAND" not in line.upper()
        ]

        return {
            "number": number,
            "found": True,
            "deleted": listed_ok and number not in remaining,
            "reply": "\n".join(reply_lines),
        }


def _listed_numbers(text: str) -> Set[int]:
    """Message numbers in a PBBS "L" listing."""
    return {message.number for message in parse_message_list(text)}


# ---------------------------------------------------------------------------
# KISS mode
# ---------------------------------------------------------------------------

from kiss import KISS_EXIT, KISSTNC  # noqa: E402


class KAMXLKISS(KISSTNC):
    """
    The KAM-XL in KISS mode -- UNVERIFIED against the real unit.

    Entering, per the manual: ``INTFACE KISS`` at cmd:, then ``RESET``
    (see tools/enterKissMode.py). INTFACE is a *saved* setting, so leaving
    has to put it back, or the KAM-XL boots into KISS from then on and
    every Terminal Mode tool stops working: at shutdown this sends the
    KISS exit (C0 FF C0, see tools/exitKissMode.py), then ``INTFACE
    TERMINAL`` and ``RESET``. If the daemon dies without a clean
    shutdown, run tools/exitKissMode.py and do that by hand.

    Port mapping is the KAM-XL's two-radio layout as KISS channels --
    channel 0 = Port 1 (HF), channel 1 = Port 2 (VHF) -- assumed from
    the manual, not yet seen on the air. Until it has been, APRS
    transmit stays off here: a wrong guess would key the HF port. The
    KAM-XL's Terminal Mode driver (KAMXL) transmits APRS normally.
    """

    MODEL = "KAM-XL"
    NAME = "kamxl"

    PORT_MAP = {0: 1, 1: 2}
    RADIO_PORTS = 2
    APRS_TX_CHANNEL = 1
    APRS_TX_SUPPORTED = False

    def enter_kiss_mode(self) -> None:
        self._write(b"\r")
        reply = self._settle(0.5)

        if b"cmd:" not in reply:
            # No prompt: most likely already in KISS (INTFACE persists).
            logger.info("KAM-XL: no cmd: prompt; assuming it's already in KISS mode")
            return

        self._write(b"INTFACE KISS\r")
        reply = self._settle(1.0)

        if b"EH?" in reply:
            raise KAMCommandError(f"KAM-XL rejected INTFACE KISS: {reply!r}")

        self._write(b"RESET\r")
        self._settle(2.0)
        logger.info("KAM-XL: INTFACE KISS + RESET sent")

    def leave_kiss_mode(self) -> None:
        self._write(KISS_EXIT)
        self._settle(1.5)
        self._write(b"\rINTFACE TERMINAL\r")
        self._settle(1.0)
        self._write(b"RESET\r")
        self._settle(2.0)
        logger.info("KAM-XL: left KISS mode, INTFACE TERMINAL restored")
