"""0 N, 0 E (a tracker without a GPS fix) is not a position."""

import os
import tempfile
import unittest

from fakes import ScriptedSerial  # noqa: F401 (puts the repo on sys.path)

from aprs import parse_object, parse_position
from packet import Packet
from reception_history import ReceptionHistory
from stations import StationTracker


def _base91(value):
    chars = ""
    for _ in range(4):
        chars = chr(33 + value % 91) + chars
        value //= 91
    return chars


COMPRESSED_ZERO = "!/" + _base91(90 * 380926) + _base91(180 * 190463) + ">  T"


def _packet(source, payload):
    return Packet(source=source, destination="APRS", digipeaters=(), port=2,
                  frame_type="UI", payload=payload, raw="")


class ParseTests(unittest.TestCase):
    def test_zero_zero_is_no_position(self):
        for payload in ("!0000.00N/00000.00W>no fix",
                        "=0000.00S\\00000.00E>",
                        "@201845z0000.00N/00000.00E>",
                        COMPRESSED_ZERO):
            with self.subTest(payload=payload):
                self.assertIsNone(parse_position(payload))

    def test_real_positions_near_zero_are_kept(self):
        position = parse_position("!0030.00N/00000.00E>")
        self.assertAlmostEqual(position.latitude, 0.5)
        self.assertIsNotNone(parse_position("!0000.00N/00030.00W>"))

    def test_object_at_zero_zero_has_no_position(self):
        self.assertIsNone(parse_object(";TESTOBJ  *201845z0000.00N/00000.00W>"))
        self.assertIsNotNone(parse_object(";TESTOBJ  *201845z3313.65N/09707.64W>"))


class TrackerTests(unittest.TestCase):
    def test_no_fix_report_keeps_the_last_real_position(self):
        tracker = StationTracker()
        tracker.update(_packet("K5CAR-9", "!3313.65N/09707.64W>"), now=100)

        station = tracker.update(_packet("K5CAR-9", "!0000.00N/00000.00W>"), now=200)

        self.assertAlmostEqual(station.latitude, 33.2275, places=3)
        self.assertEqual(len(station.history), 1)
        self.assertEqual(station.recent_packets[-1].timestamp, 200)

    def test_no_fix_report_alone_makes_no_station(self):
        tracker = StationTracker()
        self.assertIsNone(tracker.update(_packet("K5CAR-9", "!0000.00N/00000.00W>")))


class HistoryTests(unittest.TestCase):
    def test_tracks_skip_zero_zero(self):
        h = ReceptionHistory(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(h.close)
        h.record(_packet("K5CAR-9", "!0000.00N/00000.00W>"), received_at=100)

        callsigns = [s["callsign"] for s in h.station_tracks(0)]

        self.assertNotIn("K5CAR-9", callsigns)


if __name__ == "__main__":
    unittest.main()
