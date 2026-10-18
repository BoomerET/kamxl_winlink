"""Background SQLite writer for AX.25 reception history."""

import logging
import queue
import threading
import time

from packet import Packet
from reception_history import ReceptionHistory


logger = logging.getLogger(__name__)


class ReceptionWriter:
    """Persist received packets without blocking the monitor thread."""

    def __init__(
        self,
        database_path: str,
        queue_size: int = 10000,
        retention_days: float = 0,
        prune_interval: float = 24 * 3600,
        clock=time.time,
    ) -> None:
        self.database_path = database_path
        # Packet rows older than this many days are deleted at startup
        # and then once per prune_interval. 0 = keep everything.
        self.retention_days = float(retention_days or 0)
        self._prune_interval = float(prune_interval)
        self._clock = clock
        self._next_prune_at = 0.0  # first pass right after startup
        self.last_pruned_at = None
        self.last_pruned_rows = 0
        self._queue = queue.Queue(maxsize=queue_size)
        self._thread = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._stopped = False
        self._failed = False
        self.dropped_packets = 0
        self.write_errors = 0
        # Current station setup label; stamped on each packet as it is
        # queued (see aprs_coverage.py).
        self.setup = None

    def start(self) -> None:
        """Start the background SQLite writer."""

        with self._lock:
            if self._stopped:
                raise RuntimeError("ReceptionWriter has been stopped")

            if self._thread is not None:
                return

            self._thread = threading.Thread(
                target=self._run,
                name="reception-sqlite-writer",
                daemon=False,
            )
            self._thread.start()

    def submit(self, packet: Packet) -> bool:
        """Queue a packet without waiting for SQLite."""

        with self._lock:
            if self._stopped or self._failed or self._thread is None:
                return False

            try:
                self._queue.put_nowait(
                    (packet, int(time.time()), self.setup)
                )
                return True
            except queue.Full:
                self.dropped_packets += 1
                return False

    def _run(self) -> None:
        """Initialize SQLite and process queued packets."""

        try:
            history = ReceptionHistory(self.database_path)

        except Exception:
            logger.exception("Failed to initialize reception history")

            with self._lock:
                self._failed = True
                self.write_errors += 1

            # Initialization failed. Wake any waiting readers.
            self._ready_event.set()

            self._discard_pending()
            return

        # SQLite is initialized and ready for queries.
        self._ready_event.set()

        try:
            while (
                not self._stop_event.is_set()
                or not self._queue.empty()
            ):
                self._maybe_prune(history)

                try:
                    packet, received_at, setup = self._queue.get(
                        timeout=0.1
                    )

                except queue.Empty:
                    continue

                try:
                    history.record(
                        packet,
                        received_at=received_at,
                        setup=setup,
                    )

                except Exception:
                    logger.exception("Failed to persist received packet")

                    with self._lock:
                        self.write_errors += 1

                finally:
                    self._queue.task_done()

        finally:
            history.close()

    def _maybe_prune(self, history) -> None:
        """Delete old packet rows if retention is on and a pass is due.

        Runs on the writer thread, which owns the inserting connection,
        so pruning and recording never compete for the database.
        """
        if not self.retention_days:
            return

        now = self._clock()

        if now < self._next_prune_at:
            return

        self._next_prune_at = now + self._prune_interval
        cutoff = int(now - self.retention_days * 86400)

        try:
            deleted = history.prune_packets(cutoff)
            deleted_bulletins = history.prune_bulletins(cutoff)
        except Exception:
            logger.exception("Failed to prune reception history")
            with self._lock:
                self.write_errors += 1
            return

        with self._lock:
            self.last_pruned_at = now
            self.last_pruned_rows = deleted

        if deleted or deleted_bulletins:
            logger.info(
                "Reception history: deleted %d packet record(s) and %d "
                "bulletin copy(ies) older than %g days",
                deleted, deleted_bulletins, self.retention_days,
            )

    def _discard_pending(self) -> None:
        """Discard pending records after initialization failure."""

        while True:
            try:
                self._queue.get_nowait()

            except queue.Empty:
                break

            else:
                self._queue.task_done()

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Wait for SQLite initialization to finish successfully."""

        if not self._ready_event.wait(timeout=timeout):
            return False

        with self._lock:
            return not self._failed

    def stop(self) -> None:
        """Stop accepting packets and flush pending records."""

        with self._lock:
            if self._stopped:
                return

            self._stopped = True
            self._stop_event.set()
            thread = self._thread

        if thread is not None:
            thread.join()

    def list_aprs_messages(self, addressee, limit=100):
        """Read received APRS messages using a separate SQLite connection."""

        if not self.wait_ready():
            raise RuntimeError(
                "Reception history database is not ready"
            )

        history = ReceptionHistory(self.database_path)

        try:
            return history.list_aprs_messages(
                addressee=addressee,
                limit=limit,
            )
        finally:
            history.close()

    def clear_aprs_messages(self, addressee):
        """Delete stored APRS messages using a separate SQLite connection."""

        if not self.wait_ready():
            raise RuntimeError(
                "Reception history database is not ready"
            )

        history = ReceptionHistory(self.database_path)

        try:
            return history.clear_aprs_messages(addressee)
        finally:
            history.close()

    def _with_history(self, operation):
        """Run ``operation(history)`` on a separate SQLite connection."""

        if not self.wait_ready():
            raise RuntimeError(
                "Reception history database is not ready"
            )

        history = ReceptionHistory(self.database_path)

        try:
            return operation(history)
        finally:
            history.close()

    def save_outbound(self, record):
        """Persist one sent-message record (insert or update)."""
        return self._with_history(lambda h: h.save_outbound(record))

    def packets_since(self, since, limit=20000):
        return self._with_history(lambda h: h.packets_since(since, limit))

    def get_setting(self, key):
        return self._with_history(lambda h: h.get_setting(key))

    def set_setting(self, key, value):
        return self._with_history(lambda h: h.set_setting(key, value))

    def log_own_tx(self, sent_at, kind, ref, path, setup):
        return self._with_history(
            lambda h: h.log_own_tx(sent_at, kind, ref, path, setup)
        )

    def record_own_heard(self, heard_at, kind, ref, via, station):
        return self._with_history(
            lambda h: h.record_own_heard(heard_at, kind, ref, via, station)
        )

    def coverage_report(self, since, home=None, is_own=None):
        return self._with_history(
            lambda h: h.coverage_report(since, home, is_own)
        )

    def load_outbound(self, limit=200):
        return self._with_history(lambda h: h.list_outbound(limit))

    def list_bulletins(self, since=None, limit=200):
        return self._with_history(lambda h: h.list_bulletins(since, limit))

    def clear_bulletins(self):
        return self._with_history(lambda h: h.clear_bulletins())

    def clear_outbound(self):
        return self._with_history(lambda h: h.clear_outbound())

    def snapshot(self, since=None):
        """Read persisted history using a separate SQLite connection."""

        if not self.wait_ready():
            raise RuntimeError(
                "Reception history database is not ready"
            )

        history = ReceptionHistory(self.database_path)

        try:
            with self._lock:
                last_pruned_at = self.last_pruned_at
                last_pruned_rows = self.last_pruned_rows

            return {
                "scope": "persistent",
                "total_packets": history.total_packets(),
                "hourly_counts": history.hourly_counts(since=since),
                "retention_days": self.retention_days,
                "oldest_packet_at": history.oldest_packet_at(),
                "last_pruned_at": last_pruned_at,
                "last_pruned_rows": last_pruned_rows,
            }

        finally:
            history.close()
