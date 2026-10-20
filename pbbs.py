"""
Parsing helpers for the KAM-XL's built-in PBBS (Personal Mailbox)
firmware feature.

This is NOT a BBS implemented by this project -- the KAM-XL's own
firmware already handles all of the AX.25 session mechanics, message
storage, forwarding, and SYSOP access. This module just turns its
plain-text PBBS command output into structured Python objects, the
same relationship packet.py has to raw MONITOR text.

Reached via an ordinary local AX.25 CONNECT to MYPBBS -- per the
manual, a connect from the local serial terminal gets automatic SYSOP
privilege, no password exchange needed -- followed by single-letter
commands typed at its own "ENTER COMMAND:" prompt: "L" to list
messages, "R <n>" to read one. kamxl.py's KAMXL.list_pbbs_messages()
and KAMXL.read_pbbs_message() drive that exchange using the same
connect_station()/send_connected()/read_connected()/
disconnect_station() primitives used for any other connected-mode
session -- nothing PBBS-specific at the serial protocol level.

IMPORTANT: unlike most of this project, the parsing here has NOT yet
been validated against a real KAM-XL -- it's built from the manual's
documented output format (message list and message header examples),
not from a captured live session. Per this project's own design
philosophy (see docs/history.md), that makes it a best-effort first draft,
not a confirmed-correct implementation -- expect it to need
adjustment the same way packet.py's HEADER_RE did once tested for
real. Lines that don't match the expected shape are skipped rather
than raising, specifically so a format surprise degrades gracefully
(fewer parsed messages) instead of blowing up the whole call.
"""

import re

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class PBBSMessageSummary:
    """
    One row of a PBBS "L" (list) response.

    ``msg_type`` is 'B' (bulletin), 'T' (traffic/NTS), or 'P'
    (private). ``status`` is the KAM-XL's one-letter status code --
    meaning depends on ``msg_type`` (e.g. 'F'/'H' for a bulletin,
    'H'/'N'/'Y' for a private message) -- see the manual's PBBS
    message-status section. ``pages`` is the KAM-XL's own page count
    for the message, when it printed one (not every list line
    includes it).
    """

    number: int
    msg_type: str
    status: Optional[str]
    size: int
    to: str
    from_call: str
    date: str
    pages: Optional[int]
    subject: str


@dataclass(frozen=True)
class PBBSMessage:
    """
    A single message body, from a PBBS "R <n>" response.

    ``routing`` is the "@..." hierarchical routing address on the
    header line, when present (e.g. "@WA4EWV.#STX.TX.USA.NOAM") --
    None for a message with no forwarding routing attached.
    """

    number: int
    date: str
    from_call: str
    to: str
    routing: Optional[str]
    body: str


# Manual example (message-list section):
#
#   MSG# ST SIZE TO      FROM   DATE                SUBJECT
#   6    B  45   KEPS    W3IWI  10/19/01 09:37:11 2  Line Element set
#   4    B  26   HELP    WB5BBW 10/19/01 09:34:05    Xerox 820
#
# "ST" is a type character (B/T/P) optionally followed by a status
# character -- shown here as a single letter in both examples, so the
# status half is treated as optional. The trailing digit after the
# timestamp on the first example row (but not the second) appears to
# be a page count; also treated as optional. Whitespace-tolerant
# rather than fixed-column, since the manual's own text formatting
# may not preserve exact real-hardware column widths.
_LIST_LINE_RE = re.compile(
    r"^\s*(?P<number>\d+)\s+"
    r"(?P<type>[A-Za-z])(?P<status>[A-Za-z])?\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<to>\S+)\s+"
    r"(?P<from>\S+)\s+"
    r"(?P<date>\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}:\d{2})"
    r"(?:\s+(?P<pages>\d+))?"
    r"(?:\s+(?P<subject>.*\S))?\s*$"
)

# Manual example (message-read header line):
#
#   MSG#2 02/10/92 10:30:58 FROM KBØNYK TO HELP @WA4EWV.#STX.TX.USA.NOAM
_READ_HEADER_RE = re.compile(
    r"^MSG#(?P<number>\d+)\s+"
    r"(?P<date>\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2}:\d{2})\s+"
    r"FROM\s+(?P<from>\S+)\s+"
    r"TO\s+(?P<to>\S+)"
    r"(?:\s+(?P<routing>@\S+))?"
    r"\s*$"
)


def parse_message_list(text: str) -> List[PBBSMessageSummary]:
    """
    Parse a PBBS "L" response into a list of PBBSMessageSummary.

    Non-matching lines (the column header, "NNN BYTES AVAILABLE",
    "NEXT MESSAGE NUMBER N", the "ENTER COMMAND:" prompt, blank
    lines, ...) are silently skipped rather than raising.
    """
    messages = []

    for line in text.splitlines():
        match = _LIST_LINE_RE.match(line)

        if match is None:
            continue

        pages = match.group("pages")

        messages.append(PBBSMessageSummary(
            number=int(match.group("number")),
            msg_type=match.group("type").upper(),
            status=(
                match.group("status").upper()
                if match.group("status")
                else None
            ),
            size=int(match.group("size")),
            to=match.group("to"),
            from_call=match.group("from"),
            date=match.group("date"),
            pages=int(pages) if pages else None,
            subject=match.group("subject") or "",
        ))

    return messages


def parse_message(text: str) -> Optional[PBBSMessage]:
    """
    Parse a PBBS "R <n>" response into a PBBSMessage.

    Returns None if no line matched the expected "MSG#..." header
    shape at all (e.g. the message number didn't exist and the KAM-XL
    printed an error instead) -- callers should treat that as "message
    not found/not parseable" rather than assuming an empty message.
    """
    lines = text.splitlines()

    header_index = None
    header_match = None

    for index, line in enumerate(lines):
        match = _READ_HEADER_RE.match(line.strip())

        if match is not None:
            header_index = index
            header_match = match
            break

    if header_match is None:
        return None

    # Whatever follows the header line, up to (but not including) the
    # KAM-XL's next command prompt, is the message body.
    body_lines = lines[header_index + 1:]

    while body_lines and body_lines[-1].strip().upper().startswith(
        "ENTER COMMAND"
    ):
        body_lines.pop()

    body = "\n".join(body_lines).strip("\r\n")

    return PBBSMessage(
        number=int(header_match.group("number")),
        date=header_match.group("date"),
        from_call=header_match.group("from"),
        to=header_match.group("to"),
        routing=header_match.group("routing"),
        body=body,
    )
