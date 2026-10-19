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

from typing import Dict, List, Optional

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
