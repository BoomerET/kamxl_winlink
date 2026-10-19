"""Offline tests for packet-history retention (TNCSTATION_HISTORY_DAYS)."""

import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import tncstation_daemon
from packet import Packet
from reception_history import ReceptionHistory
from reception_writer import ReceptionWriter


DAY = 86400


def _packet(source="W5NGU-3", payload="!3313.65N/09707.64W#"):
    return Packet(
        source=source, destination="APRS", digipeaters=(), port=2,
        frame_type="UI", payload=payload, raw="",
    )


class PrunePacketsTests(unittest.TestCase):

    def setUp(self):
        self.history = ReceptionHistory(
            os.path.join(tempfile.mkdtemp(), "h.db")
        )
        self.addCleanup(self.history.close)

    def test_deletes_only_old_packets_in_batches(self):
        now = 1_800_000_000
        for age_days in (200, 120, 91, 89, 10, 0):
            self.history.record(_packet(), received_at=now - age_days * DAY)

        deleted = self.history.prune_packets(now - 90 * DAY, batch_size=2)

        self.assertEqual(deleted, 3)
        self.assertEqual(self.history.total_packets(), 3)
        self.assertEqual(self.history.oldest_packet_at(), now - 89 * DAY)

    def test_messages_are_never_pruned(self):
        now = 1_800_000_000
        self.history.record(
            _packet("N5BA-7", ":N5BA     :old message{1"),
            received_at=now - 400 * DAY,
        )
        self.history.save_outbound(
            {"message_id": "AB12", "sent_at": now - 400 * DAY}
        )

        self.history.prune_packets(now - 90 * DAY)

        self.assertEqual(self.history.total_packets(), 0)
        self.assertEqual(len(self.history.list_aprs_messages("N5BA")), 1)
        self.assertEqual(len(self.history.list_outbound()), 1)

    def test_empty_database(self):
        self.assertEqual(self.history.prune_packets(int(time.time())), 0)
        self.assertIsNone(self.history.oldest_packet_at())


class WriterRetentionTests(unittest.TestCase):

    def _seed(self, path, now, ages):
        history = ReceptionHistory(path)
        for age in ages:
            history.record(_packet(), received_at=int(now - age * DAY))
        history.close()

    def _writer(self, path, **kwargs):
        writer = ReceptionWriter(path, **kwargs)
        writer.start()
        self.addCleanup(writer.stop)
        self.assertTrue(writer.wait_ready())
        return writer

    def _wait(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_prunes_at_startup_and_reports(self):
        path = os.path.join(tempfile.mkdtemp(), "h.db")
        now = time.time()
        self._seed(path, now, ages=(100, 95, 5))

        writer = self._writer(path, retention_days=90)

        self.assertTrue(self._wait(lambda: writer.last_pruned_at is not None))
        snap = writer.snapshot()
        self.assertEqual(snap["total_packets"], 1)
        self.assertEqual(snap["retention_days"], 90)
        self.assertEqual(snap["last_pruned_rows"], 2)

    def test_zero_keeps_everything(self):
        path = os.path.join(tempfile.mkdtemp(), "h.db")
        now = time.time()
        self._seed(path, now, ages=(1000, 5))

        writer = self._writer(path, retention_days=0)
        time.sleep(0.3)

        self.assertEqual(writer.snapshot()["total_packets"], 2)
        self.assertIsNone(writer.last_pruned_at)

    def test_prunes_again_after_interval(self):
        path = os.path.join(tempfile.mkdtemp(), "h.db")
        clock = {"now": time.time()}
        writer = self._writer(
            path, retention_days=90, prune_interval=3600,
            clock=lambda: clock["now"],
        )
        self.assertTrue(self._wait(lambda: writer.last_pruned_at is not None))
        first = writer.last_pruned_at

        # A packet recorded "now" ages past the cutoff a day later.
        writer.submit(_packet())
        self.assertTrue(self._wait(lambda: writer.snapshot()["total_packets"] == 1))
        clock["now"] += 91 * DAY

        self.assertTrue(self._wait(lambda: writer.last_pruned_at != first))
        self.assertTrue(self._wait(lambda: writer.snapshot()["total_packets"] == 0))


class HistoryDaysCommandLineTests(unittest.TestCase):

    def test_below_31_days_rejected_before_serial(self):
        with patch.dict(os.environ, {"TNCSTATION_CALLSIGN": "N5BA"}, clear=True), \
             patch.object(tncstation_daemon, "_load_dotenv"), \
             patch.object(tncstation_daemon, "KAMXL") as kamxl_cls, \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                tncstation_daemon.main(["--port", "/dev/null", "--history-days", "7"])
        kamxl_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
