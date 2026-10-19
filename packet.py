import re

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# Matches a KAM-XL MONITOR header line. Format observed on real
# hardware, with MCOM and MRESP at their factory defaults (both
# ON/ON):
#
#   SOURCE>DESTINATION[,DIGI1,DIGI2*]/PORT: <TAG>:
#
# e.g.:
#
#   K5LRK>BEACON/2: <UI>:
#   WB5NZV>KD5EOC-10,RSSTN*/2: <<C>>:
#   KD5EOC-10>WB5NZV,RSSTN/2: <<I00>>:
#
# The bracketed tag is the KAM-XL's own frame-type annotation --
# single "<...>" for an AX.25 v1 packet, double "<<...>>" for v2 (see
# MCOM/MRESP in the manual). It's controlled by MCOM/MRESP, not by
# MONITOR itself, and both default to ON -- so on a stock
# configuration, *every* monitored header carries one of these, not
# just connect/control frames. An earlier version of this regex had
# no allowance for it at all, which meant HEADER_RE never matched a
# single real-hardware header line -- every line silently failed to
# parse (dropped, since _process_line() only appends non-matching
# lines to an already-pending packet -- with nothing ever matching,
# nothing ever became pending either), and the live monitor pane
# stayed empty no matter how much real traffic the KAM-XL was
# actually receiving. Found this way, on real hardware, rather than
# from the manual -- the manual quote above about the tag format was
# only actually confirmed against a live log once this bug surfaced.
#
# Digipeaters that have already repeated the packet are suffixed with
# "*". This is scoped to MONITOR/unsolicited traffic only -- plain
# Convers-mode session text (e.g. a Winlink handshake or a node chat
# session) never matches this shape and is intentionally left alone.
# The radio port this application treats as VHF APRS. On the KAM-XL
# that's its real Port 2 (Port 1 is HF). A single-port TNC's one radio
# port is reported as this port too (see opentnc.py), so the daemon's
# APRS handling, reception history and statistics work unchanged.
APRS_RADIO_PORT = 2


HEADER_RE: re.Pattern = re.compile(
    r"^(?P<source>[A-Za-z0-9\-]+)"
    r">(?P<destination>[A-Za-z0-9\-]+)"
    r"(?:,(?P<digipeaters>[A-Za-z0-9\-\*,]+))?"
    r"/(?P<port>\d+):"
    r"(?:\s*<{1,2}(?P<frame_type>[^<>]*)>{1,2}:)?"
    r"(?P<inline_payload>.*)$"
)


# Matches a TNC-2-style MONITOR header line, the format TAPR TNC-2
# clones print (and, by its author's description, what the OpenTNC's
# TNC-2-compatible command set should print -- UNVERIFIED against real
# OpenTNC output; check with opentnc_capture.py):
#
#   SOURCE>DEST[,DIGI1,DIGI2*][ <TAG>]:payload
#
# e.g.:
#
#   K5LRK>APRS,WIDE1-1*,WIDE2-1:!3313.65N/09707.64W-PHG2360
#   WB5NZV>KD5EOC-10 <C>:
#   KD5EOC-10>WB5NZV <I R0 S1>:Welcome
#
# Differences from the KAM-XL's HEADER_RE: no "/PORT" (single radio
# port), the frame tag (if MCOM/MRPT-style annotation is on) comes
# before the colon rather than after it, and the payload normally
# follows on the same line. Also tolerated: an optional leading
# timestamp (TNC-2 MSTAMP, e.g. "10/18/26 22:41:05 ") and a
# Direwolf-style "[0] " channel prefix. ``frame_type`` captures only
# the tag's first word ("UI" from "<UI R>", "I" from "<I R0 S1>"), the
# same shape the daemon already compares against ("UI"); the full tag
# stays in Packet.raw.
TNC2_HEADER_RE: re.Pattern = re.compile(
    r"^(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s+\d{1,2}:\d{2}(?::\d{2})?\s+)?"
    r"(?:\[\d+\]\s*)?"
    r"(?P<source>[A-Za-z0-9\-]+)"
    r">(?P<destination>[A-Za-z0-9\-]+)"
    r"(?:,(?P<digipeaters>[A-Za-z0-9\-\*,]+))?"
    r"(?:\s*<{1,2}(?P<frame_type>[A-Za-z]+)[^<>]*>{1,2})?"
    r":(?P<inline_payload>.*)$"
)


@dataclass(frozen=True)
class Packet:
    """
    A single unsolicited packet, as reported by KAM-XL MONITOR output.

    ``digipeaters`` preserves the "*" suffix on any that have already
    repeated the packet (e.g. "K5LRK*") so callers can tell where in
    the path the packet actually is.

    ``payload`` is the packet's info text with the trailing newlines
    from each physical line stripped and rejoined with "\\n" -- i.e.
    it doesn't include the header line itself. ``raw`` keeps the full
    original text (header + payload) for debugging or in case the
    parsed fields are ever wrong.

    ``frame_type`` is the KAM-XL's own bracketed annotation from the
    header line (e.g. "UI", "C", "UA", "D", "DM", "I00", "rr1"), when
    MCOM/MRESP were on and it included one -- None otherwise. "UI" is
    an ordinary unconnected/beacon frame; most other values are AX.25
    connect-session control/supervisory traffic (connect and
    disconnect requests and acknowledgments, numbered info frames,
    receiver-ready acks, ...) that generally carries no payload of
    its own -- see the manual's MCOM/MRESP entries for the full list.
    """

    source: str
    destination: str
    digipeaters: Tuple[str, ...]
    port: int
    payload: str
    raw: str
    frame_type: Optional[str] = None

    @property
    def digipeated(self) -> bool:
        """
        True if at least one digipeater in the path has already
        repeated this packet.
        """
        return any(
            digi.endswith("*")
            for digi in self.digipeaters
        )


class PacketParser:
    """
    Incrementally reassembles KAM-XL MONITOR output into Packet
    objects.

    MONITOR text arrives from listen()/read_available() in arbitrary,
    not-line-aligned chunks -- observed on real hardware: a single
    line like "Welcome to the Denton County Texas EOC" split across
    reads as "Welcome to the D" / "enton County Texas EOC". Packets
    aren't explicitly delimited either: a packet is only known to be
    complete once the *next* packet's header line arrives (or the
    session ends), so this has to buffer across an unknown number of
    feed() calls.

    Usage:

        parser = PacketParser()

        def on_chunk(text):
            for packet in parser.feed(text):
                handle(packet)

        kam.listen(seconds=600, callback=on_chunk)

        # At the end of the session, flush() to emit anything left
        # over that never saw a following header line.
        for packet in parser.flush():
            handle(packet)
    """

    def __init__(
        self,
        header_re: Optional[re.Pattern] = None,
        default_port: int = APRS_RADIO_PORT,
    ) -> None:
        """
        ``header_re`` recognizes one MONITOR header line; it defaults to
        the KAM-XL's HEADER_RE. A TNC driver supplies its own (see
        tnc.TerminalTNC.make_packet_parser()). The regex must define
        the named groups ``source``, ``destination`` and
        ``inline_payload``; ``digipeaters``, ``port`` and
        ``frame_type`` are optional.

        ``default_port`` is the radio port recorded for a header that
        names none -- a single-port TNC's monitor lines don't.
        """
        self._header_re: re.Pattern = header_re or HEADER_RE
        self._default_port: int = default_port
        self._buffer: str = ""
        self._pending: Optional[Dict[str, Any]] = None

    def feed(self, text: str) -> List[Packet]:
        """
        Feed serial text, accepting CR, LF, and CRLF line endings.
        """
        self._buffer += text
        completed: List[Packet] = []

        while True:
            cr = self._buffer.find("\r")
            lf = self._buffer.find("\n")

            positions = [p for p in (cr, lf) if p >= 0]
            if not positions:
                break

            index = min(positions)
            line = self._buffer[:index]

            consume = 1
            if (
                self._buffer[index] == "\r"
                and len(self._buffer) > index + 1
                and self._buffer[index + 1] == "\n"
            ):
                consume = 2

            self._buffer = self._buffer[index + consume:]

            packet = self._process_line(line)
            if packet is not None:
                completed.append(packet)

        return completed

    def flush(self) -> List[Packet]:
        """
        Finalize whatever is still buffered -- e.g. because the
        session ended before a trailing newline or another header
        line arrived.

        Returns a list of any Packet objects this produces (usually
        0 or 1).
        """
        completed: List[Packet] = []

        if self._buffer:
            line = self._buffer.rstrip("\r")
            self._buffer = ""

            packet = self._process_line(line)

            if packet is not None:
                completed.append(packet)

        packet = self._finalize_pending()

        if packet is not None:
            completed.append(packet)

        return completed

    def flush_pending(self) -> List[Packet]:
        """
        Finish the packet currently being assembled, if its lines are
        all complete (nothing partial left in the buffer).

        For callers that know the line has gone quiet: TNC-2-style
        monitor output gives no end-of-packet marker, so without this
        the last packet of a burst waits for the *next* packet to
        arrive -- possibly minutes on a quiet channel, which matters
        for an incoming APRS message awaiting an ACK. Unlike flush(),
        a half-received line is left alone to finish arriving.
        """
        if self._buffer:
            return []

        packet = self._finalize_pending()

        return [packet] if packet is not None else []

    def _process_line(self, line: str) -> Optional[Packet]:
        """
        Handle one complete, decoded line.

        If it's a header line, whatever was previously pending is
        now complete and gets returned. Otherwise, the line is folded
        into the currently pending packet's payload (or silently
        dropped if nothing is pending -- e.g. leading noise before
        the first header, or non-MONITOR text like Convers-mode
        session traffic).
        """
        match = self._header_re.match(line)

        if match is None:
            if self._pending is not None:
                # A blank line terminates a complete APRS message.
                # Leave other packet types and multiline payloads
                # under the existing next-header/flush behavior.
                if line == "":
                    pending = self._pending
                    if (
                        pending["port"] == APRS_RADIO_PORT
                        and pending["frame_type"] in ("UI", None)
                        and len(pending["payload_lines"]) == 1
                    ):
                        from aprs import parse_aprs_message

                        message = parse_aprs_message(
                            pending["payload_lines"][0]
                        )
                        if message is not None:
                            return self._finalize_pending()

                self._pending["payload_lines"].append(line)

            return None

        completed = self._finalize_pending()

        groups = match.groupdict()
        digipeaters = groups.get("digipeaters")
        port = groups.get("port")

        self._pending = {
            "source": match.group("source"),
            "destination": match.group("destination"),
            "digipeaters": (
                tuple(digipeaters.split(","))
                if digipeaters
                else ()
            ),
            "port": int(port) if port else self._default_port,
            "frame_type": groups.get("frame_type") or None,
            "header_line": line,
            "payload_lines": (
                [match.group("inline_payload")]
                if match.group("inline_payload")
                else []
            ),
        }

        return completed

    def _finalize_pending(self) -> Optional[Packet]:
        if self._pending is None:
            return None

        payload_lines = self._pending["payload_lines"].copy()

        # Inline APRS packets can end with CR CR. Remove the
        # resulting empty line, but preserve trailing newlines
        # in the original multiline KAM-XL format.
        if (
            self._pending["header_line"].endswith(
                self._pending["payload_lines"][0]
            )
            if self._pending["payload_lines"]
            else False
        ):
            while payload_lines and payload_lines[-1] == "":
                payload_lines.pop()

        payload = "\n".join(payload_lines)

        raw = "\n".join(
            [self._pending["header_line"]]
            + (
                self._pending["payload_lines"][1:]
                if self._pending["payload_lines"]
                and self._pending["header_line"].endswith(
                    self._pending["payload_lines"][0]
                )
                else self._pending["payload_lines"]
            )
        )

        packet = Packet(
            source=self._pending["source"],
            destination=self._pending["destination"],
            digipeaters=self._pending["digipeaters"],
            port=self._pending["port"],
            payload=payload,
            raw=raw,
            frame_type=self._pending["frame_type"],
        )

        self._pending = None

        return packet
