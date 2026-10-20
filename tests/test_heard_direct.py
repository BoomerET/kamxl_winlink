"""Stations heard direct -- no digipeater in between (map ring, filter)."""

import os
import tempfile
import unittest

from fakes import ScriptedSerial, make_kam  # noqa: F401 (puts the repo on sys.path)

import tncstation_web
from packet import Packet
from reception_history import ReceptionHistory
from stations import StationTracker, heard_direct
from tncstation_daemon import KAMDaemon

POS = "!3313.65N/09707.64W-"


def _packet(source, digipeaters=(), payload=POS):
    return Packet(source=source, destination="APRS", digipeaters=tuple(digipeaters),
                  port=2, frame_type="UI", payload=payload, raw="")


class HeardDirectTests(unittest.TestCase):
    def test_definition(self):
        self.assertTrue(heard_direct(()))
        self.assertTrue(heard_direct(("WIDE1-1", "WIDE2-1")))      # unused path
        self.assertFalse(heard_direct(("KD5EOC-10*", "WIDE2-1")))  # repeated
        self.assertFalse(heard_direct(("WIDE1*", "WIDE2-1")))


class TrackerTests(unittest.TestCase):
    def test_last_direct_follows_direct_packets(self):
        tracker = StationTracker()

        station = tracker.update(_packet("K5ABC", ("KD5EOC-10*",)), now=100)
        self.assertIsNone(station.last_direct)

        station = tracker.update(_packet("K5ABC", ("WIDE1-1", "WIDE2-1")), now=200)
        self.assertEqual(station.last_direct, 200)

        # A later digipeated copy doesn't erase it.
        station = tracker.update(_packet("K5ABC", ("KD5EOC-10*",)), now=300)
        self.assertEqual(station.last_direct, 200)

    def test_non_position_packets_count_too(self):
        tracker = StationTracker()
        tracker.update(_packet("K5ABC", ("KD5EOC-10*",)), now=100)

        station = tracker.update(_packet("K5ABC", payload=">status"), now=150)

        self.assertEqual(station.last_direct, 150)


class HistoryTracksTests(unittest.TestCase):
    def setUp(self):
        self.h = ReceptionHistory(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.h.close)

    def test_last_direct_in_tracks(self):
        self.h.record(_packet("DIRECT", ("WIDE1-1",)), received_at=100)
        self.h.record(_packet("DIRECT", ("KD5EOC-10*",)), received_at=200)
        self.h.record(_packet("VIADIGI", ("KD5EOC-10*", "WIDE2*")), received_at=150)

        tracks = {s["callsign"]: s for s in self.h.station_tracks(0)}

        self.assertEqual(tracks["DIRECT"]["last_direct"], 100)
        self.assertIsNone(tracks["VIADIGI"]["last_direct"])

    def test_gated_packet_does_not_make_the_sender_direct(self):
        # An iGate's third-party copy: the gateway was heard, not K5ABC.
        self.h.record(_packet("K5ABC", ("KD5EOC-10*",)), received_at=100)
        self.h.record(_packet("W5IGT", (), payload="}K5ABC>APRS,TCPIP,W5IGT*:" + POS),
                      received_at=110)

        tracks = {s["callsign"]: s for s in self.h.station_tracks(0)}

        self.assertIsNone(tracks["K5ABC"]["last_direct"])


class DaemonTests(unittest.TestCase):
    def test_own_station_is_never_direct(self):
        # The OpenTNC hands our own transmissions back over KISS.
        daemon = KAMDaemon(make_kam(ScriptedSerial({})),
                           "/tmp/tncstation-direct-test.sock",
                           station_callsign="N5BA-1")
        self.addCleanup(daemon.shutdown)
        with daemon._stations_lock:
            daemon._stations.update(_packet("N5BA-1"))
            daemon._stations.update(_packet("K5ABC"))

        stations = {s["callsign"]: s for s in daemon.dispatch("stations.list", {})}

        self.assertIsNone(stations["N5BA-1"]["last_direct"])
        self.assertIsNotNone(stations["K5ABC"]["last_direct"])
        self.assertIsNone(daemon.dispatch("stations.get", {"callsign": "N5BA-1"})["last_direct"])


class MapPageTests(unittest.TestCase):
    def test_controls_present(self):
        page = tncstation_web.load_page("map")
        for marker in ('id="directOnly"', "directRing", "isDirect(", "Heard direct",
                       "path requested"):
            self.assertIn(marker, page)

    def test_log_labels_unused_path_as_direct(self):
        self.assertIn("direct (path ", tncstation_web.load_page("log"))
