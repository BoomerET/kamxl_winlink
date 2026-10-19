"""Offline tests for APRS weather decoding and its use on the map."""

import re
import shutil
import subprocess
import unittest

from aprs import parse_position, parse_weather
from packet import Packet
from stations import StationTracker

import tncstation_web


# Captured on the air at N5BA, 2026-10-18 (KC5TIL-3 via W5NGU-3).
KC5TIL = "_10172218c051s000g000t055r000p004P000h36b10278tRSW"


def _wx(payload):
    return parse_weather(payload, parse_position(payload))


class PositionlessWeatherTests(unittest.TestCase):

    def test_real_packet(self):
        w = parse_weather(KC5TIL)
        self.assertEqual(w.timestamp, "10172218")
        self.assertEqual(w.wind_direction, 51)
        self.assertEqual(w.wind_speed_mph, 0)
        self.assertEqual(w.wind_gust_mph, 0)
        self.assertEqual(w.temperature_f, 55)
        self.assertEqual(w.rain_1h_in, 0.0)
        self.assertEqual(w.rain_24h_in, 0.04)
        self.assertEqual(w.rain_since_midnight_in, 0.0)
        self.assertEqual(w.humidity_pct, 36)
        self.assertEqual(w.pressure_mb, 1027.8)
        # "tRSW" is a unit tag, not a second temperature.
        self.assertEqual(w.comment, "tRSW")

    def test_missing_values(self):
        w = parse_weather("_10180930c...s...g...t...r...p...P...h..b.....")
        self.assertIsNone(w)  # nothing actually measured

        w = parse_weather("_10180930c...s...g...t061h..b.....")
        self.assertEqual(w.temperature_f, 61)
        self.assertIsNone(w.wind_direction)
        self.assertIsNone(w.humidity_pct)
        self.assertIsNone(w.pressure_mb)


class PositionWeatherTests(unittest.TestCase):

    def test_spec_example(self):
        w = _wx("@092345z4903.50N/07201.75W_220/004g005t077r000p000P000h50b09900wRSW")
        self.assertEqual(
            (w.wind_direction, w.wind_speed_mph, w.wind_gust_mph, w.temperature_f),
            (220, 4, 5, 77),
        )
        self.assertEqual(w.humidity_pct, 50)
        self.assertEqual(w.pressure_mb, 990.0)
        self.assertIsNone(w.timestamp)
        self.assertEqual(w.comment, "wRSW")

    def test_negative_temperature_and_h00(self):
        w = _wx("!3313.65N/09707.64W_.../...g...t-05h00b.....")
        self.assertEqual(w.temperature_f, -5)
        self.assertEqual(w.humidity_pct, 100)
        self.assertIsNone(w.wind_direction)

    def test_luminosity_and_snow(self):
        w = _wx("!3313.65N/09707.64W_090/003t050b10100l123s005")
        self.assertEqual(w.luminosity_wm2, 1123)
        self.assertEqual(w.snow_24h_in, 0.5)

    def test_non_weather_stations(self):
        self.assertIsNone(_wx("!3313.65N/09707.64W-house"))
        self.assertIsNone(_wx("!3313.65N/09707.64W_just a comment"))
        self.assertIsNone(_wx("!3302.60N/09647.29W_PHG2360"))
        self.assertIsNone(parse_weather(":N5BA     :hello"))
        self.assertIsNone(parse_weather(None))

    def test_implausible_values_dropped(self):
        w = _wx("!3313.65N/09707.64W_999/005t250h99")
        self.assertIsNone(w.wind_direction)
        self.assertIsNone(w.temperature_f)
        self.assertEqual(w.humidity_pct, 99)


class TrackerWeatherTests(unittest.TestCase):

    def _p(self, payload, source="KC5TIL-3"):
        return Packet(source=source, destination="APTW01", digipeaters=(),
                      port=2, frame_type="UI", payload=payload, raw="")

    def test_positionless_weather_waits_for_position(self):
        t = StationTracker()
        self.assertIsNone(t.update(self._p(KC5TIL), now=100))

        s = t.update(self._p("!3302.60N/09647.29W_PHG2360"), now=200)
        self.assertEqual(s.weather["temperature_f"], 55)
        self.assertEqual(s.weather_at, 100)

    def test_stale_pending_weather_discarded(self):
        t = StationTracker()
        t.update(self._p(KC5TIL), now=100)
        s = t.update(self._p("!3302.60N/09647.29W_PHG2360"), now=100 + 7200)
        self.assertIsNone(s.weather)

    def test_weather_updates_and_survives_position_beacons(self):
        t = StationTracker()
        t.update(self._p("!3302.60N/09647.29W_PHG2360"), now=100)
        s = t.update(self._p(KC5TIL.replace("t055", "t060")), now=200)
        self.assertEqual(s.weather["temperature_f"], 60)
        self.assertEqual(s.last_heard, 100)  # position age unchanged

        s = t.update(self._p("!3302.60N/09647.29W_PHG2360"), now=300)
        self.assertEqual(s.weather["temperature_f"], 60)
        self.assertEqual(s.weather_at, 200)

    def test_position_weather_report(self):
        t = StationTracker()
        s = t.update(self._p(
            "!3310.00N/09700.00W_220/012g018t068r012p045P020h82b10132wRSW",
            source="W5WX",
        ), now=50)
        self.assertEqual(s.weather["wind_gust_mph"], 18)
        self.assertEqual(s.weather_at, 50)

    def test_ordinary_station_has_no_weather(self):
        t = StationTracker()
        s = t.update(self._p("!3311.31N/09730.18Wy194/000", source="N5JAT"))
        self.assertIsNone(s.weather)


class MapWeatherJsTests(unittest.TestCase):

    def _function(self, src, name):
        m = re.search(r"  function " + name + r"\((.*?)\) \{(.*?)\n  \}\n", src, re.S)
        self.assertIsNotNone(m, name)
        return "function %s(%s) {%s\n}\n" % (name, m.group(1), m.group(2))

    def test_weather_lines(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        src = (tncstation_web.PAGES_DIR / "map" / "app.js").read_text()
        harness = "\n".join([
            'const assert = require("node:assert/strict");',
            'var COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",'
            '"S","SSW","SW","WSW","W","WNW","NW","NNW"];',
            self._function(src, "compass"),
            self._function(src, "hasValue"),
            self._function(src, "weatherLines"),
            """
assert.equal(compass(51), "NE");
assert.equal(compass(220), "SW");
assert.equal(compass(359), "N");
var lines = weatherLines({temperature_f: 55, humidity_pct: 36,
  pressure_mb: 1027.8, wind_direction: 51, wind_speed_mph: 0,
  wind_gust_mph: 0, rain_1h_in: 0, rain_since_midnight_in: 0,
  rain_24h_in: 0.04});
assert.equal(lines[0], "55\\u00b0F (13\\u00b0C), humidity 36%, 1027.8 mb (30.35 inHg)");
assert.equal(lines[1], "Wind calm");
assert.equal(lines[2], "Rain 0.00 in last hour, 0.00 in since midnight, 0.04 in 24 h");
assert.deepEqual(weatherLines({wind_direction: 220, wind_speed_mph: 12,
  wind_gust_mph: 18}), ["Wind from SW 12 mph, gusts 18 mph"]);
assert.deepEqual(weatherLines({}), []);
""",
        ])
        r = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
