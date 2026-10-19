"""Offline tests for APRS telemetry decoding, storage queries and page."""

import http.client
import os
import tempfile
import time
import unittest

from aprs_telemetry import (
    TelemetryDefinitions, build_series, is_definition, parse_telemetry,
)
from fakes import ScriptedSerial, make_kam
from kamxl_daemon import KAMDaemon
from packet import Packet
from reception_history import ReceptionHistory
from test_rest import RestTestCase


def _packet(source, payload, port=2):
    return Packet(source=source, destination="APRS", digipeaters=(), port=port,
                  frame_type="UI", payload=payload, raw="")


class ParseTests(unittest.TestCase):

    def test_spec_example(self):
        r = parse_telemetry("T#005,199,000,255,073,123,01101001")
        self.assertEqual(r.sequence, "005")
        self.assertEqual(r.analog, (199.0, 0.0, 255.0, 73.0, 123.0))
        self.assertEqual(r.bits, "01101001")

    def test_variants(self):
        self.assertEqual(parse_telemetry("T#MIC199,000,255,073,123,01101001").sequence, "MIC")
        # Modern senders: decimals, negatives, fewer channels, no bits.
        r = parse_telemetry("T#123,13.6,72.5,-3")
        self.assertEqual(r.analog, (13.6, 72.5, -3.0, None, None))
        self.assertIsNone(r.bits)
        # Empty channels and a trailing comment after the bits.
        r = parse_telemetry("T#7,12,,,,,10000000 solar ok")
        self.assertEqual(r.analog, (12.0, None, None, None, None))
        self.assertEqual(r.bits, "10000000")

    def test_not_telemetry(self):
        for payload in ("T#", "T#abc", "T#1,x,y", "!3313.65N/09707.64W#digi",
                        ">status |ab|", ":N5BA     :T#1,2,3", None):
            with self.subTest(payload=payload):
                self.assertIsNone(parse_telemetry(payload))

    def test_compressed_in_position_comment(self):
        # Sequence 1, channels 100 and 3000, then bits 0b00000101.
        def b91(n):
            return chr(n // 91 + 33) + chr(n % 91 + 33)
        body = b91(1) + b91(100) + b91(3000) + b91(0) + b91(0) + b91(0) + b91(5)
        r = parse_telemetry("!3313.65N/09707.64W#digi |" + body + "|")
        self.assertEqual(r.sequence, "1")
        self.assertEqual(r.analog, (100.0, 3000.0, 0.0, 0.0, 0.0))
        self.assertEqual(r.bits, "10100000")  # bit 1 is the LSB
        r = parse_telemetry("=3313.65N/09707.64W- |" + b91(9) + b91(42) + "|")
        self.assertEqual(r.analog, (42.0, None, None, None, None))

    def test_definitions(self):
        d = TelemetryDefinitions()
        for text in ("PARM.Battery,Temp,,,,Door,Fan", "UNIT.Volts,deg.F",
                     "EQNS.0,0.1,0,0,1,-40,0,1,0,0,1,0,0,1,0",
                     "BITS.10111111,W5NGU Digi"):
            self.assertTrue(is_definition(text))
            self.assertTrue(d.apply(text))
        self.assertFalse(d.apply("hello there"))
        self.assertAlmostEqual(d.value(0, 136), 13.6)
        self.assertEqual(d.value(1, 112), 72.0)
        self.assertEqual(d.project, "W5NGU Digi")
        # BITS sense: bit 2 is "on" when 0.
        self.assertTrue(d.bit_on(0, "10000000"))
        self.assertTrue(d.bit_on(1, "10000000"))
        self.assertFalse(d.bit_on(1, "11000000"))

    def test_bad_eqns_ignored(self):
        d = TelemetryDefinitions()
        self.assertFalse(d.apply("EQNS.0,one,0"))
        self.assertEqual(d.value(0, 5), 5.0)

    def test_build_series(self):
        d = TelemetryDefinitions()
        d.apply("PARM.Battery,,,,,Door")
        d.apply("EQNS.0,0.1,0")
        reports = [
            (100, parse_telemetry("T#1,130,5,,,,10000000")),
            (200, parse_telemetry("T#2,128,6,,,,00000000")),
        ]
        out = build_series("W5NGU-3", reports, d)
        self.assertTrue(out["defined"])
        self.assertEqual(out["reports"], 2)
        # Channel 2 carries data but PARM left it blank: unused, hidden.
        (battery,) = out["analog"]
        self.assertEqual(battery["name"], "Battery")
        self.assertEqual([round(v, 2) for _, v in battery["points"]], [13.0, 12.8])
        self.assertEqual(battery["latest"], 12.8)
        # Without any names, every channel with data is shown.
        raw = build_series("K5X", reports, TelemetryDefinitions())
        self.assertEqual([c["name"] for c in raw["analog"]], ["Channel 1", "Channel 2"])
        # Named bit kept; unnamed bits that never change are dropped.
        self.assertEqual([b["name"] for b in out["bits"]], ["Door"])
        self.assertEqual(out["bits"][0]["points"], [[100, True], [200, False]])


class HistoryTelemetryTests(unittest.TestCase):

    def setUp(self):
        self.h = ReceptionHistory(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.h.close)
        rows = [
            ("W5NGU-3", ":W5NGU-3  :PARM.Battery,Temp", 50),
            ("W5NGU-3", ":W5NGU-3  :UNIT.Volts,deg.F", 51),
            ("W5NGU-3", ":W5NGU-3  :EQNS.0,0.1,0,0,1,-40", 52),
            ("W5NGU-3", ":W5NGU-3  :BITS.11111111,Denton Digi", 53),
            ("W5NGU-3", "T#001,136,112,,,,", 1000),
            ("W5NGU-3", "!3313.65N/09707.64W#ordinary beacon", 1100),
            ("W5NGU-3", "T#002,132,110,,,,", 1200),
            ("K5DNT-4", "T#9,50", 1300),
            ("W5CGC-10", "}K5GATE>APRS,TCPIP,W5CGC-10*:T#3,1,2", 1400),
            ("W5NGU-3", "T#000,140,120,,,,", 10),   # too old for since=500
        ]
        for source, payload, at in rows:
            self.h.record(_packet(source, payload), received_at=at)

    def test_stations(self):
        stations = self.h.telemetry_stations(500)
        self.assertEqual([s["callsign"] for s in stations],
                         ["K5GATE", "K5DNT-4", "W5NGU-3"])
        ngu = stations[2]
        self.assertEqual(ngu["reports"], 2)
        self.assertEqual(ngu["project"], "Denton Digi")
        self.assertEqual(ngu["channels"], ["Battery", "Temp"])

    def test_series_uses_definitions(self):
        out = self.h.telemetry_series("W5NGU-3", 500)
        self.assertEqual(out["reports"], 2)
        battery, temp = out["analog"]
        self.assertEqual((battery["name"], battery["unit"]), ("Battery", "Volts"))
        self.assertEqual([round(v, 1) for _, v in battery["points"]], [13.6, 13.2])
        self.assertEqual([v for _, v in temp["points"]], [72.0, 70.0])
        self.assertEqual(out["project"], "Denton Digi")

    def test_gated_telemetry_counts_for_originator(self):
        out = self.h.telemetry_series("K5GATE", 500)
        self.assertEqual(out["reports"], 1)
        self.assertFalse(out["defined"])

    def test_unknown_station(self):
        out = self.h.telemetry_series("K5NONE", 0)
        self.assertEqual((out["reports"], out["analog"], out["last_heard"]),
                         (0, [], None))


class DaemonTelemetryTests(unittest.TestCase):

    def test_rpcs(self):
        path = os.path.join(tempfile.mkdtemp(), "h.db")
        h = ReceptionHistory(path)
        h.record(_packet("W5NGU-3", "T#001,136,112"), received_at=int(time.time()) - 60)
        h.close()
        daemon = KAMDaemon(make_kam(ScriptedSerial({})), "/tmp/kamxl-tlm-test.sock",
                           station_callsign="N5BA", history_db=path)
        self.addCleanup(daemon.shutdown)

        stations = daemon.dispatch("telemetry.stations", {})
        self.assertEqual([s["callsign"] for s in stations["stations"]], ["W5NGU-3"])
        out = daemon.dispatch("telemetry.get", {"callsign": "w5ngu-3", "days": 1})
        self.assertEqual(out["reports"], 1)
        self.assertEqual(out["days"], 1)
        for bad in ({"callsign": "x y"}, {"callsign": "W5NGU-3", "days": 0}):
            with self.assertRaises(ValueError):
                daemon.dispatch("telemetry.get", bad)

    def test_needs_history_db(self):
        daemon = KAMDaemon(make_kam(ScriptedSerial({})), "/tmp/kamxl-tlm-test2.sock",
                           station_callsign="N5BA")
        self.addCleanup(daemon.shutdown)
        with self.assertRaises(ValueError):
            daemon.dispatch("telemetry.stations", {})


class TelemetryRestTests(RestTestCase):

    def test_page_and_routes(self):
        _, port = self.start_stack(ScriptedSerial({}), history_db=True)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/telemetry?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn('id="stationList"', html)

        status, payload = self.request(port, "GET", "/telemetry/stations?days=30")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["stations"], [])
        status, payload = self.request(port, "GET", "/telemetry/W5NGU-3?days=7")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["callsign"], "W5NGU-3")
        status, _ = self.request(port, "GET", "/telemetry/stations?days=x")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
