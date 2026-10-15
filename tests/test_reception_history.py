"""Tests for persistent SQLite reception history."""

import tempfile
import unittest
from pathlib import Path

from packet import Packet
from reception_history import ReceptionHistory


class ReceptionHistoryTests(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_path = str(
            Path(self.tempdir.name) / "history.sqlite3"
        )
        self.history = ReceptionHistory(self.database_path)

    def tearDown(self):
        self.history.close()
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

    def test_empty_database(self):
        self.assertEqual(self.history.total_packets(), 0)
        self.assertEqual(self.history.hourly_counts(), [])

    def test_record_packet(self):
        self.history.record(
            self.make_packet(),
            received_at=1760000000,
        )

        self.assertEqual(self.history.total_packets(), 1)

    def test_history_survives_restart(self):
        self.history.record(
            self.make_packet(),
            received_at=1760000000,
        )

        self.history.close()

        self.history = ReceptionHistory(self.database_path)

        self.assertEqual(self.history.total_packets(), 1)

    def test_hourly_counts(self):
        base = 1760000400

        self.history.record(self.make_packet(), received_at=base)
        self.history.record(self.make_packet(), received_at=base + 30)
        self.history.record(
            self.make_packet("K5TEST"),
            received_at=base + 3600,
        )

        counts = self.history.hourly_counts()

        self.assertEqual(
            sum(entry["packet_count"] for entry in counts),
            3,
        )
        self.assertEqual(len(counts), 2)

    def test_hourly_counts_since(self):
        base = 1760000400

        self.history.record(self.make_packet(), received_at=base)
        self.history.record(
            self.make_packet(),
            received_at=base + 3600,
        )

        counts = self.history.hourly_counts(since=base + 3600)

        self.assertEqual(len(counts), 1)
        self.assertEqual(counts[0]["packet_count"], 1)
