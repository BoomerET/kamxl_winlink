"""
Outbound APRS message tracking: message-ID allocation and matching
incoming ``ackID`` / ``rejID`` replies to the messages we sent.

Pure bookkeeping -- no serial I/O, no RF, no threads of its own. The
daemon calls :meth:`OutboundAprsTracker.register` after a message has
been submitted to the TNC, and :meth:`OutboundAprsTracker.observe`
for every decoded APRS message the monitor thread sees.

An ack is only matched when all three of these line up, so a stray
ack between two other stations can't be mistaken for ours:

* it is addressed to our station callsign,
* it was sent *by* the station we addressed the original message to,
* it carries the message ID we assigned.

State is in memory only (like ``stations.StationTracker``): the sent-
message list starts empty after a daemon restart. IDs are seeded from
the clock so a restart doesn't immediately reuse recent IDs, which
would let a late ack for an old message "confirm" a new one.
"""

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional

from aprs import AprsMessage


_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
# Four base-36 characters: 1.68M distinct IDs before wrapping, while
# staying well inside APRS's 5-character limit (some older radios
# truncate longer IDs on display).
_ID_WIDTH = 4
_ID_SPACE = len(_ID_ALPHABET) ** _ID_WIDTH

STATUS_PENDING = "pending"
STATUS_ACKED = "acked"
STATUS_REJECTED = "rejected"


def _encode_id(value: int) -> str:
    chars = []

    for _ in range(_ID_WIDTH):
        value, digit = divmod(value, len(_ID_ALPHABET))
        chars.append(_ID_ALPHABET[digit])

    return "".join(reversed(chars))


@dataclass
class OutboundAprsMessage:
    """One message we submitted to the TNC, and what came back."""

    message_id: str
    addressee: str
    text: str
    payload: str
    sent_at: float
    status: str = STATUS_PENDING
    attempts: int = 1
    last_sent_at: Optional[float] = None
    acked_at: Optional[float] = None
    # Station that actually answered (normally == addressee).
    acked_by: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class OutboundAprsTracker:
    """Thread-safe record of sent APRS messages and their ack state."""

    station_callsign: str
    max_messages: int = 200
    clock: Callable[[], float] = time.time
    _messages: Dict[str, OutboundAprsMessage] = field(default_factory=dict)
    _next_id: Optional[int] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.station_callsign = self.station_callsign.strip().upper()

        if self._next_id is None:
            self._next_id = int(self.clock()) % _ID_SPACE

    # -- sending -------------------------------------------------------

    def allocate_id(self) -> str:
        """Reserve the next message ID (never one still being tracked)."""
        with self._lock:
            for _ in range(_ID_SPACE):
                candidate = _encode_id(self._next_id)
                self._next_id = (self._next_id + 1) % _ID_SPACE

                if candidate not in self._messages:
                    return candidate

        raise RuntimeError("No free APRS message IDs")  # pragma: no cover

    def register(
        self,
        message_id: str,
        addressee: str,
        text: str,
        payload: str,
    ) -> OutboundAprsMessage:
        """Record a message after it was successfully submitted."""
        now = self.clock()

        with self._lock:
            existing = self._messages.get(message_id)

            if existing is not None:
                # Manual resend of the same message ID.
                existing.attempts += 1
                existing.last_sent_at = now
                return OutboundAprsMessage(**existing.to_dict())

            record = OutboundAprsMessage(
                message_id=message_id,
                addressee=addressee.strip().upper(),
                text=text,
                payload=payload,
                sent_at=now,
                last_sent_at=now,
            )
            self._messages[message_id] = record
            self._trim()

            return OutboundAprsMessage(**record.to_dict())

    def get(self, message_id: str) -> Optional[OutboundAprsMessage]:
        with self._lock:
            record = self._messages.get(message_id)
            return None if record is None else OutboundAprsMessage(
                **record.to_dict()
            )

    def list(self) -> List[Dict[str, object]]:
        """Newest first, as plain dicts (JSON-ready)."""
        with self._lock:
            records = sorted(
                self._messages.values(),
                key=lambda r: r.sent_at,
                reverse=True,
            )
            return [r.to_dict() for r in records]

    # -- receiving -----------------------------------------------------

    def observe(
        self, source: str, message: AprsMessage
    ) -> Optional[OutboundAprsMessage]:
        """
        Apply an incoming decoded APRS message.

        Returns the updated record if it acknowledged (or rejected) one
        of our pending messages, else None. Repeated acks -- e.g. the
        same ack heard again via a digipeater -- are ignored.
        """
        if message.addressee != self.station_callsign:
            return None

        if message.kind == "ack":
            acked_id, status = message.message_id, STATUS_ACKED
        elif message.kind == "rej":
            acked_id, status = message.message_id, STATUS_REJECTED
        elif message.kind == "message" and message.reply_ack:
            acked_id, status = message.reply_ack, STATUS_ACKED
        else:
            return None

        source = source.strip().upper()

        with self._lock:
            record = self._messages.get(acked_id)

            if record is None or record.status != STATUS_PENDING:
                return None

            if record.addressee != source:
                return None

            record.status = status
            record.acked_at = self.clock()
            record.acked_by = source

            return OutboundAprsMessage(**record.to_dict())

    # -- internals -----------------------------------------------------

    def _trim(self) -> None:
        """Drop the oldest records beyond max_messages. Caller holds lock."""
        excess = len(self._messages) - self.max_messages

        if excess <= 0:
            return

        oldest = sorted(self._messages.values(), key=lambda r: r.sent_at)

        for record in oldest[:excess]:
            del self._messages[record.message_id]
