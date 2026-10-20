"""Offline tests for APRS objects/items on the map and "Message" links."""

import http.client
import re
import shutil
import subprocess
import unittest

from aprs import parse_frequency_info, parse_object, parse_position
from packet import Packet
from stations import StationTracker
from test_aprs_serial import APRSTransactionFake
from test_rest import RestTestCase

import tncstation_web


REPEATER = ";147.000-T*111111z3312.34N/09707.12Wr T110 -060 R30m DCARA"


def _p(source, payload):
    return Packet(source=source, destination="APRS", digipeaters=(), port=2,
                  frame_type="UI", payload=payload, raw="")


class ParseObjectTests(unittest.TestCase):

    def test_object(self):
        obj = parse_object(REPEATER)
        self.assertEqual((obj.name, obj.kind, obj.live), ("147.000-T", "object", True))
        self.assertAlmostEqual(obj.position.latitude, 33.20567, places=4)
        self.assertEqual(obj.position.symbol_code, "r")
        self.assertEqual(obj.position.timestamp, "111111z")

    def test_killed_object(self):
        obj = parse_object(REPEATER.replace("-T*", "-T_"))
        self.assertFalse(obj.live)

    def test_item(self):
        obj = parse_object(")HAMFEST!3311.00N/09708.00W/Sat 8am")
        self.assertEqual((obj.name, obj.kind, obj.live), ("HAMFEST", "item", True))
        self.assertEqual(obj.position.comment, "Sat 8am")
        self.assertFalse(parse_object(")HAMFEST_3311.00N/09708.00W/").live)

    def test_not_objects(self):
        for payload in ("!3311.00N/09708.00W-house", ":N5BA     :hi",
                        ";SHORT*111111z", ")X!garbage", None):
            self.assertIsNone(parse_object(payload), payload)

    def test_object_is_not_a_station_position(self):
        self.assertIsNone(parse_position(REPEATER))


class FrequencyInfoTests(unittest.TestCase):

    def test_full_repeater(self):
        self.assertEqual(
            parse_frequency_info("147.000-T", " T110 -060 R30m DCARA"),
            {"frequency_mhz": 147.0, "offset_khz": -600, "tone_hz": 110.9,
             "dcs_code": None, "range_miles": 30},
        )

    def test_variants(self):
        f = parse_frequency_info("444.500+", "")
        self.assertEqual((f["frequency_mhz"], f["offset_khz"]), (444.5, 5000))
        f = parse_frequency_info("146.94MHz", "C100 D023 R40k")
        self.assertEqual((f["tone_hz"], f["dcs_code"], f["range_miles"]),
                         (100.0, "023", 25))

    def test_not_a_frequency(self):
        self.assertIsNone(parse_frequency_info("HAMFEST", "Sat 8am"))
        self.assertIsNone(parse_frequency_info("NET", "updated 12.30 today"))


class TrackerObjectTests(unittest.TestCase):

    def test_object_tracked_without_moving_owner(self):
        t = StationTracker()
        t.update(_p("W5NGU-3", "!3313.65NI09707.64W#Digi"), now=0)
        t.update(_p("W5NGU-3", REPEATER), now=10)

        objs = t.list_objects()
        self.assertEqual([o.name for o in objs], ["147.000-T"])
        self.assertEqual(objs[0].owner, "W5NGU-3")
        self.assertEqual(objs[0].frequency["tone_hz"], 110.9)
        station = t.get_station("W5NGU-3")
        self.assertAlmostEqual(station.latitude, 33.2275, places=3)
        # Still listed in the sender's recent packets.
        self.assertEqual(station.recent_packets[-1].payload, REPEATER)

    def test_object_from_unknown_station_and_updates(self):
        t = StationTracker()
        t.update(_p("K5DEN", ")HAMFEST!3311.00N/09708.00W/Sat"), now=0)
        t.update(_p("K5XYZ", ")HAMFEST!3312.00N/09708.00W/Sat 9am"), now=5)
        obj = t.list_objects()[0]
        self.assertEqual((obj.owner, obj.report_count, obj.comment),
                         ("K5XYZ", 2, "Sat 9am"))
        self.assertIsNone(t.get_station("K5DEN"))

    def test_kill_and_expiry(self):
        t = StationTracker()
        t.update(_p("W5NGU-3", REPEATER), now=0)
        t.update(_p("W5NGU-3", REPEATER.replace("-T*", "-T_")), now=1)
        self.assertEqual(t.list_objects(), [])

        t.update(_p("W5NGU-3", REPEATER), now=0)
        t.expire_stale(max_age_seconds=3600, now=4000)
        self.assertEqual(t.list_objects(), [])


class ObjectsEndpointTests(RestTestCase):

    def test_objects_endpoint(self):
        daemon, port = self.start_stack(APRSTransactionFake())
        daemon._handle_packet(_p("W5NGU-3", REPEATER))

        status, payload = self.request(port, "GET", "/objects")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"][0]["name"], "147.000-T")
        self.assertEqual(payload["result"][0]["frequency"]["offset_khz"], -600)
        # Stations list is unchanged by objects.
        self.assertEqual(self.request(port, "GET", "/stations")[1]["result"], [])

    def test_pages_have_controls(self):
        _, port = self.start_stack(APRSTransactionFake())
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/map?token=test-token")
        html = conn.getresponse().read().decode("utf-8")
        self.assertIn('id="showObjects"', html)
        self.assertIn('authedUrl("/objects")', html)
        self.assertIn("messageLinkHtml", html)

        conn.request("GET", "/?token=test-token")
        html = conn.getresponse().read().decode("utf-8")
        self.assertIn("prefillRecipientFromUrl", html)


class FrequencySummaryJsTests(unittest.TestCase):

    def test_summary(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        src = (tncstation_web.PAGES_DIR / "map" / "app.js").read_text()

        def fn(name):
            m = re.search(r"  function " + name + r"\((.*?)\) \{(.*?)\n  \}\n", src, re.S)
            self.assertIsNotNone(m, name)
            return "function %s(%s) {%s\n}\n" % (name, m.group(1), m.group(2))

        harness = ('const assert = require("node:assert/strict");\n'
                   + fn("formatOffset") + fn("frequencySummary") + r"""
assert.equal(frequencySummary({frequency_mhz: 147, offset_khz: -600, tone_hz: 110.9,
  dcs_code: null, range_miles: 30}),
  "147.000 MHz, −600 kHz, tone 110.9, range 30 mi");
assert.equal(frequencySummary({frequency_mhz: 444.5, offset_khz: 5000, tone_hz: null,
  dcs_code: "023", range_miles: null}), "444.500 MHz, +5 MHz, DCS 023");
assert.equal(frequencySummary(null), "");
""")
        r = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
