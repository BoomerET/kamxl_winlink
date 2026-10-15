"""Tests for background reception-history persistence."""

import tempfile
import unittest
from pathlib import Path

from packet import Packet
from reception_history import ReceptionHistory
from reception_writer import ReceptionWriter
from unittest.mock import patch


class ReceptionWriterTests(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = str(
            Path(self.tempdir.name) / "history.sqlite3"
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def make_packet(self, source="N5BA"):
        return Packet(
            source=source,
            destination="APRS",
            digipeaters=("WIDE1-1", "W5NGU-3*"),
            port=2,
            payload="Test packet",
            raw="Test raw packet",
            frame_type="UI",
        )

    def test_writer_persists_packets(self):
        writer = ReceptionWriter(self.database_path)

        writer.start()

        for _ in range(10):
            self.assertTrue(writer.submit(self.make_packet()))

        writer.stop()

        history = ReceptionHistory(self.database_path)

        try:
            self.assertEqual(history.total_packets(), 10)
        finally:
            history.close()

    def test_writer_survives_restart(self):
        writer = ReceptionWriter(self.database_path)
        writer.start()
        writer.submit(self.make_packet())
        writer.stop()

        writer2 = ReceptionWriter(self.database_path)
        writer2.start()
        writer2.submit(self.make_packet("K5TEST"))
        writer2.stop()

        history = ReceptionHistory(self.database_path)

        try:
            self.assertEqual(history.total_packets(), 2)
        finally:
            history.close()

    def test_submit_after_stop(self):
        writer = ReceptionWriter(self.database_path)
        writer.start()
        writer.stop()

        self.assertFalse(writer.submit(self.make_packet()))

    def test_start_after_stop(self):
        writer = ReceptionWriter(self.database_path)
        writer.start()
        writer.stop()

        with self.assertRaises(RuntimeError):
            writer.start()

    def test_database_initialization_failure(self):
        from unittest.mock import patch

        writer = ReceptionWriter(self.database_path)

        with patch(
            "reception_writer.ReceptionHistory",
            side_effect=OSError("Simulated database failure"),
        ):
            writer.start()
            writer._thread.join(timeout=5)

        self.assertFalse(writer._thread.is_alive())
        self.assertTrue(writer._failed)
        self.assertEqual(writer.write_errors, 1)
        self.assertFalse(writer.submit(self.make_packet()))

        writer.stop()

    def test_shutdown_during_initialization_failure(self):
        import threading
        from unittest.mock import patch

        entered = threading.Event()
        release = threading.Event()

        def fail_initialization(path):
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("Test initialization timed out")
            raise OSError("Simulated database failure")

        writer = ReceptionWriter(self.database_path)

        with patch(
            "reception_writer.ReceptionHistory",
            side_effect=fail_initialization,
        ):
            writer.start()
            self.assertTrue(entered.wait(timeout=5))

            shutdown_thread = threading.Thread(target=writer.stop)
            shutdown_thread.start()

            release.set()
            shutdown_thread.join(timeout=5)

            self.assertFalse(
                shutdown_thread.is_alive(),
                "Writer shutdown deadlocked",
            )

        self.assertTrue(writer._failed)
        self.assertEqual(writer.write_errors, 1)

    def test_writer_becomes_ready(self):
        """Writer reports ready after SQLite initializes."""
        with tempfile.TemporaryDirectory() as tmp:
            database_path = str(Path(tmp) / "history.sqlite3")
            writer = ReceptionWriter(database_path)
            writer.start()

            try:
                self.assertTrue(writer.wait_ready(timeout=5.0))
            finally:
                writer.stop()

    def test_writer_reports_initialization_failure(self):
        """Writer reports not ready when SQLite initialization fails."""
        with tempfile.TemporaryDirectory() as tmp:
            database_path = str(Path(tmp) / "history.sqlite3")
            writer = ReceptionWriter(database_path)

            with patch(
                "reception_writer.ReceptionHistory",
                side_effect=OSError("Simulated initialization failure"),
            ):
                writer.start()

                try:
                    self.assertFalse(writer.wait_ready(timeout=5.0))
                finally:
                    writer.stop()
