"""Offline tests for longer station tracks on the map (stations.history)."""

import os
import tempfile
import time
import unittest

import kamxl_web
from fakes import ScriptedSerial, make_kam
from kamxl_daemon import KAMDaemon
from packet import Packet
from reception_history import ReceptionHistory
from test_rest import RestTestCase


def _packet(source, payload, port=2, frame_type="UI"):
    return Packet(source=source, destination="APRS", digipeaters=(), port=port,
                  frame_type=frame_type, payload=payload, raw="")


def _pos(lat_min, lon_min, symbol=">", comment=""):
    return "!33%05.2fN/097%05.2fW%s%s" % (lat_min, lon_min, symbol, comment)


class HistoryTrackTests(unittest.TestCase):

    def setUp(self):
        self.h = ReceptionHistory(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.h.close)

    def record(self, source, payload, at, **kw):
        self.h.record(_packet(source, payload, **kw), received_at=at)

    def test_track_latest_position_and_order(self):
        self.record("K5ABC-9", _pos(10, 10), 100)
        self.record("K5ABC-9", _pos(10, 10), 110)        # same spot: no new point
        self.record("K5ABC-9", _pos(11, 12), 120)
        self.record("K5ABC-9", ">status text", 125)      # not a position
        self.record("K5ABC-9", _pos(12, 14, ">", "Kenwood"), 130)
        self.record("W5NGU-3", _pos(13.65, 7.64, "#"), 115)

        stations = self.h.station_tracks(0)
        self.assertEqual([s["callsign"] for s in stations], ["K5ABC-9", "W5NGU-3"])
        mobile = stations[0]
        self.assertEqual(mobile["reports"], 4)
        self.assertEqual(len(mobile["track"]), 3)
        self.assertEqual([p[2] for p in mobile["track"]], [100, 120, 130])
        self.assertAlmostEqual(mobile["latitude"], 33.2, places=3)
        self.assertEqual((mobile["first_heard"], mobile["last_heard"]), (100, 130))
        self.assertEqual(mobile["comment"], "Kenwood")
        self.assertEqual((mobile["symbol_table"], mobile["symbol_code"]), ("/", ">"))

    def test_same_rules_as_live_map(self):
        # Objects, gated copies, port 1 and connected frames don't count.
        self.record("W5NGU-3", ";146.920TX*181230z3313.65N/09707.64Wr", 100)
        self.record("W5CGC-10", "}K5IS>APRS,TCPIP,W5CGC-10*:" + _pos(1, 1), 100)
        self.record("K5HF", _pos(1, 1), 100, port=1)
        self.record("K5CON", _pos(1, 1), 100, frame_type="I00")
        self.assertEqual(self.h.station_tracks(0), [])

    def test_period(self):
        self.record("K5OLD", _pos(1, 1), 100)
        self.record("K5NEW", _pos(2, 2), 1000)
        self.assertEqual([s["callsign"] for s in self.h.station_tracks(500)], ["K5NEW"])

    def test_long_tracks_are_thinned(self):
        for i in range(50):
            self.record("K5ABC-9", _pos(10 + i * 0.1, 10), 100 + i)
        (s,) = self.h.station_tracks(0, max_points=10)
        self.assertEqual(len(s["track"]), 10)
        self.assertEqual(s["track"][0][2], 100)
        self.assertEqual(s["track"][-1][2], 149)  # newest always kept
        self.assertEqual(s["reports"], 50)


class DaemonTrackTests(unittest.TestCase):

    def test_rpc(self):
        path = os.path.join(tempfile.mkdtemp(), "h.db")
        h = ReceptionHistory(path)
        now = int(time.time())
        h.record(_packet("K5ABC-9", _pos(10, 10)), received_at=now - 5 * 3600)
        h.record(_packet("K5ABC-9", _pos(11, 11)), received_at=now - 3 * 3600)
        h.record(_packet("K5OLD", _pos(1, 1)), received_at=now - 30 * 3600)
        h.close()
        daemon = KAMDaemon(make_kam(ScriptedSerial({})), "/tmp/kamxl-tracks-test.sock",
                           station_callsign="N5BA", history_db=path)
        self.addCleanup(daemon.shutdown)

        day = daemon.dispatch("stations.history", {})
        self.assertEqual(day["hours"], 24)
        self.assertEqual([s["callsign"] for s in day["stations"]], ["K5ABC-9"])
        self.assertEqual(len(day["stations"][0]["track"]), 2)
        week = daemon.dispatch("stations.history", {"hours": 168})
        self.assertEqual(len(week["stations"]), 2)
        # Neither was heard in the last hour: the live map stays empty.
        self.assertEqual(daemon.dispatch("stations.list", {}), [])
        for bad in (0, 721, "24", True):
            with self.assertRaises(ValueError):
                daemon.dispatch("stations.history", {"hours": bad})

    def test_needs_history_db(self):
        daemon = KAMDaemon(make_kam(ScriptedSerial({})), "/tmp/kamxl-tracks-test2.sock",
                           station_callsign="N5BA")
        self.addCleanup(daemon.shutdown)
        with self.assertRaises(ValueError):
            daemon.dispatch("stations.history", {})


class TrackRestTests(RestTestCase):

    def test_route_and_page(self):
        _, port = self.start_stack(ScriptedSerial({}), history_db=True)
        status, payload = self.request(port, "GET", "/stations/history?hours=72")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"], {"hours": 72, "stations": []})
        status, _ = self.request(port, "GET", "/stations/history?hours=soon")
        self.assertEqual(status, 400)
        page = kamxl_web.load_page("map")
        for marker in ('id="trackPeriod"', "refreshHistory", "/stations/history"):
            self.assertIn(marker, page)


if __name__ == "__main__":
    unittest.main()
