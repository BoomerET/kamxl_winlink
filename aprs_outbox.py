"""
Outbound APRS message tracking: message-ID allocation, matching
incoming ``ackID`` / ``rejID`` replies to the messages we sent, and
(optionally) scheduling automatic resends of unacknowledged messages.

Pure bookkeeping -- no serial I/O, no RF, no threads of its own. The
daemon calls :meth:`OutboundAprsTracker.register` after a message has
been submitted to the TNC, and :meth:`OutboundAprsTracker.observe`
for every decoded APRS message the monitor thread sees. When the
daemon has a history database it passes ``on_change`` so every
change is persisted, and :meth:`load` restores the list at startup.

An ack is only matched when all three of these line up, so a stray
ack between two other stations can't be mistaken for ours:

* it is addressed to our station callsign,
* it was sent *by* the station we addressed the original message to,
* it carries the message ID we assigned.

IDs are seeded from the clock (and never reuse an ID still tracked)
so a restart doesn't immediately reuse recent ones, which would let a
late ack for an old message "confirm" a new one.
"""

import threading
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable, Dict, Iterable, List, Optional

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


def _decode_id(message_id: str) -> Optional[int]:
    if len(message_id) != _ID_WIDTH:
        return None
    value = 0
    for char in message_id:
        digit = _ID_ALPHABET.find(char)
        if digit < 0:
            return None
        value = value * len(_ID_ALPHABET) + digit
    return value


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
    # Digipeater path it was sent with ("" = direct).
    path: str = ""
    # Automatic resend schedule (see schedule_retries()). When
    # retries_left reaches 0, next_retry_at is the deadline after
    # which the message is marked gave_up.
    next_retry_at: Optional[float] = None
    retries_left: int = 0
    gave_up: bool = False
    # "Did it get out?": the digipeater we heard repeat it, and the
    # iGate we heard send it back from APRS-IS (proof it reached the
    # internet). Latest of each.
    digipeated_by: Optional[str] = None
    digipeated_at: Optional[float] = None
    igated_by: Optional[str] = None
    igated_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OutboundAprsMessage":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class OutboundAprsTracker:
    """Thread-safe record of sent APRS messages and their ack state."""

    station_callsign: str
    max_messages: int = 200
    clock: Callable[[], float] = time.time
    on_change: Optional[Callable[[Dict[str, Any]], None]] = None
    _messages: Dict[str, OutboundAprsMessage] = field(default_factory=dict)
    _next_id: Optional[int] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.station_callsign = self.station_callsign.strip().upper()

        if self._next_id is None:
            self._next_id = int(self.clock()) % _ID_SPACE

    # -- persistence -----------------------------------------------------

    def load(self, records: Iterable[Dict[str, Any]]) -> int:
        """
        Restore saved records (e.g. from the history database).

        Any automatic resend schedule is dropped: a restart must never
        lead to the radio transmitting on its own for old messages.
        Late acks for restored pending messages still match.
        """
        loaded = 0

        with self._lock:
            for data in records:
                record = OutboundAprsMessage.from_dict(data)
                record.next_retry_at = None
                record.retries_left = 0
                self._messages[record.message_id] = record
                loaded += 1

                # Continue numbering after the newest restored ID so a
                # clock-seeded start can't land on a recent one.
                value = _decode_id(record.message_id)
                if value is not None and record.sent_at >= max(
                    (r.sent_at for r in self._messages.values()), default=0
                ):
                    self._next_id = (value + 1) % _ID_SPACE

            self._trim()

        return loaded

    def _changed(self, record: OutboundAprsMessage) -> None:
        if self.on_change is not None:
            self.on_change(record.to_dict())

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
        path: str = "",
    ) -> OutboundAprsMessage:
        """Record a message after it was successfully submitted.

        Registering an ID that is already tracked counts as a resend.
        """
        now = self.clock()

        with self._lock:
            record = self._messages.get(message_id)

            if record is not None:
                record.attempts += 1
                record.last_sent_at = now
            else:
                record = OutboundAprsMessage(
                    message_id=message_id,
                    addressee=addressee.strip().upper(),
                    text=text,
                    payload=payload,
                    sent_at=now,
                    last_sent_at=now,
                    path=path,
                )
                self._messages[message_id] = record
                self._trim()

            snapshot = OutboundAprsMessage(**record.to_dict())

        self._changed(snapshot)
        return snapshot

    def schedule_retries(
        self, message_id: str, count: int, interval: float
    ) -> Optional[OutboundAprsMessage]:
        """Plan up to ``count`` automatic resends, first after ``interval``."""
        with self._lock:
            record = self._messages.get(message_id)
            if record is None or record.status != STATUS_PENDING:
                return None
            record.retries_left = max(0, int(count))
            record.next_retry_at = self.clock() + interval
            record.gave_up = False
            snapshot = OutboundAprsMessage(**record.to_dict())

        self._changed(snapshot)
        return snapshot

    def due_retries(self, now: Optional[float] = None) -> List[OutboundAprsMessage]:
        """Pending messages whose next automatic resend is due."""
        now = self.clock() if now is None else now
        with self._lock:
            return [
                OutboundAprsMessage(**r.to_dict())
                for r in self._messages.values()
                if r.status == STATUS_PENDING
                and r.next_retry_at is not None
                and r.next_retry_at <= now
                and r.retries_left > 0
            ]

    def expire_retries(self, now: Optional[float] = None) -> List[OutboundAprsMessage]:
        """Mark gave_up on messages whose last resend went unanswered."""
        now = self.clock() if now is None else now
        expired = []
        with self._lock:
            for r in self._messages.values():
                if (
                    r.status == STATUS_PENDING
                    and r.retries_left == 0
                    and r.next_retry_at is not None
                    and r.next_retry_at <= now
                ):
                    r.gave_up = True
                    r.next_retry_at = None
                    expired.append(OutboundAprsMessage(**r.to_dict()))

        for snapshot in expired:
            self._changed(snapshot)
        return expired

    def retry_sent(
        self, message_id: str, base_interval: float
    ) -> Optional[OutboundAprsMessage]:
        """Book-keep one automatic resend (call after register())."""
        with self._lock:
            record = self._messages.get(message_id)
            if record is None:
                return None
            record.retries_left = max(0, record.retries_left - 1)
            # Back off: interval, 2x, 4x, ... after each attempt. After
            # the last retry this is the wait before giving up.
            record.next_retry_at = self.clock() + base_interval * (
                2 ** max(0, record.attempts - 1)
            )
            snapshot = OutboundAprsMessage(**record.to_dict())

        self._changed(snapshot)
        return snapshot

    def cancel_retries(self, message_id: str) -> None:
        with self._lock:
            record = self._messages.get(message_id)
            if record is None:
                return
            record.next_retry_at = None
            record.retries_left = 0
            snapshot = OutboundAprsMessage(**record.to_dict())
        self._changed(snapshot)

    def get(self, message_id: str) -> Optional[OutboundAprsMessage]:
        with self._lock:
            record = self._messages.get(message_id)
            return None if record is None else OutboundAprsMessage(
                **record.to_dict()
            )

    def list(self) -> List[Dict[str, Any]]:
        """Newest first, as plain dicts (JSON-ready)."""
        with self._lock:
            records = sorted(
                self._messages.values(),
                key=lambda r: r.sent_at,
                reverse=True,
            )
            return [r.to_dict() for r in records]

    def clear(self) -> int:
        with self._lock:
            count = len(self._messages)
            self._messages.clear()
        return count

    def heard_back(
        self,
        message_id: str,
        addressee: str,
        digipeater: Optional[str] = None,
        igate: Optional[str] = None,
    ) -> Optional[OutboundAprsMessage]:
        """Record that a copy of our own message was heard on the air."""
        with self._lock:
            record = self._messages.get(message_id)
            if record is None or record.addressee != addressee.strip().upper():
                return None
            now = self.clock()
            if digipeater:
                record.digipeated_by, record.digipeated_at = digipeater, now
            if igate:
                record.igated_by, record.igated_at = igate, now
            snapshot = OutboundAprsMessage(**record.to_dict())

        self._changed(snapshot)
        return snapshot

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
            # Answered: stop any automatic resends.
            record.next_retry_at = None
            record.retries_left = 0
            record.gave_up = False

            snapshot = OutboundAprsMessage(**record.to_dict())

        self._changed(snapshot)
        return snapshot

    # -- internals -----------------------------------------------------

    def _trim(self) -> None:
        """Drop the oldest records beyond max_messages. Caller holds lock."""
        excess = len(self._messages) - self.max_messages

        if excess <= 0:
            return

        oldest = sorted(self._messages.values(), key=lambda r: r.sent_at)

        for record in oldest[:excess]:
            del self._messages[record.message_id]
