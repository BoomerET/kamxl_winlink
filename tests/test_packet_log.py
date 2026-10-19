"""Offline tests for the searchable packet log (packets.search, /log)."""

import http.client
import os
import tempfile
import time
import unittest

from fakes import ScriptedSerial, make_kam
from tncstation_daemon import KAMDaemon
from packet import Packet
from reception_history import ReceptionHistory
from test_rest import RestTestCase


def _packet(source, payload, digis=(), port=2):
    return Packet(source=source, destination="APRS", digipeaters=tuple(digis),
                  port=port, frame_type="UI", payload=payload, raw="")


T = 1_800_000_000


def _seed(history):
    rows = [
        (_packet("W5NGU-3", "!3313.65N/09707.64W#W5NGU Denton digi"), T - 500, "J-pole"),
        (_packet("W5NGU", "!3313.65N/09707.64W-W5NGU home"), T - 400, "J-pole"),
        (_packet("W5NGX", ">status W5NGX"), T - 350, "J-pole"),
        (_packet("K5ABC-9", "=3314.00N/09650.00W>Kenwood 146.520",
                 ["W5NGU-3", "WIDE1*", "WIDE2-1"]), T - 300, "Mag"),
        (_packet("K5ABC-9", ">100% on time_ok", ["K5DNT-4*"]), T - 200, "Mag"),
        (_packet("W5CGC-10", "}W1ABC-9>APDR16,TCPIP,W5CGC-10*::N5BA     :hi from IS{12"),
         T - 100, "J-pole"),
        (_packet("K5OLD", "!old position report"), T - 30 * 86400, None),
    ]
    for packet, at, setup in rows:
        history.record(packet, received_at=at, setup=setup)


class HistorySearchTests(unittest.TestCase):

    def setUp(self):
        self.h = ReceptionHistory(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.h.close)
        _seed(self.h)

    def sources(self, **filters):
        rows, _ = self.h.search_packets(**filters)
        return [r["source"] for r in rows]

    def test_everything_newest_first(self):
        rows, more = self.h.search_packets(since=T - 86400)
        self.assertFalse(more)
        self.assertEqual(rows[0]["source"], "W5CGC-10")
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[-1]["digipeaters"], [])
        self.assertEqual(rows[2]["digipeaters"], ["W5NGU-3", "WIDE1*", "WIDE2-1"])
        self.assertEqual(rows[2]["setup"], "Mag")

    def test_callsign_forms(self):
        self.assertEqual(self.sources(callsign="W5NGU-3"), ["W5NGU-3"])
        # No SSID: every SSID of that call, but not W5NGX.
        self.assertEqual(self.sources(callsign="W5NGU"), ["W5NGU", "W5NGU-3"])
        self.assertEqual(self.sources(callsign="w5ng*"), ["W5NGX", "W5NGU", "W5NGU-3"])

    def test_callsign_finds_gated_originator(self):
        self.assertEqual(self.sources(callsign="W1ABC"), ["W5CGC-10"])
        self.assertEqual(self.sources(callsign="W1ABC-9"), ["W5CGC-10"])

    def test_via(self):
        # First hop (not starred on KAM-XL) and starred hops both match.
        self.assertEqual(self.sources(via="W5NGU-3"), ["K5ABC-9"])
        self.assertEqual(self.sources(via="K5DNT"), ["K5ABC-9"])
        self.assertEqual(self.sources(via="WIDE1"), ["K5ABC-9"])
        self.assertEqual(self.sources(via="W5NGU-3", callsign="W5NGU"), [])

    def test_text_is_case_insensitive_and_literal(self):
        self.assertEqual(self.sources(text="kenwood"), ["K5ABC-9"])
        self.assertEqual(self.sources(text="100%"), ["K5ABC-9"])
        self.assertEqual(self.sources(text="time_ok"), ["K5ABC-9"])
        self.assertEqual(self.sources(text="time%ok"), [])
        self.assertEqual(self.sources(text="hi from IS"), ["W5CGC-10"])

    def test_period_and_setup(self):
        self.assertIn("K5OLD", self.sources())
        self.assertNotIn("K5OLD", self.sources(since=T - 86400))
        self.assertEqual(self.sources(setup="Mag"), ["K5ABC-9", "K5ABC-9"])
        self.assertEqual(self.sources(setup=""), ["K5OLD"])

    def test_paging(self):
        first, more = self.h.search_packets(limit=3)
        self.assertTrue(more)
        second, more = self.h.search_packets(limit=3, before_id=first[-1]["id"])
        third, more = self.h.search_packets(limit=3, before_id=second[-1]["id"])
        self.assertFalse(more)
        ids = [r["id"] for r in first + second + third]
        self.assertEqual(len(ids), 7)
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_rows_without_payload(self):
        # Rows stored before payloads were saved: found by callsign,
        # never by text.
        self.h._connection.execute(
            "INSERT INTO received_packets (received_at, source, destination,"
            " frame_type, port, digipeaters) VALUES (?, 'K5PRE', 'APRS', 'UI',"
            " 2, '[]')", (T,))
        self.h._connection.commit()
        rows, _ = self.h.search_packets(callsign="K5PRE")
        self.assertEqual(rows[0]["payload"], None)
        self.assertEqual(self.sources(text="K5PRE"), [])

    def test_station_summary(self):
        s = self.h.station_summary("K5ABC")
        self.assertEqual(s["packets"], 2)
        self.assertEqual(s["first_heard"], T - 300)
        self.assertEqual(s["last_heard"], T - 200)
        self.assertEqual(s["direct"], 0)
        # "Heard through" = the last repeater in each path.
        # W5NGU-3,WIDE1*,WIDE2-1: W5NGU-3 used up WIDE1, so it sent it.
        self.assertEqual([v["callsign"] for v in s["via"]], ["K5DNT-4", "W5NGU-3"])
        self.assertEqual(s["sources"], [{"callsign": "K5ABC-9", "packets": 2}])

        s = self.h.station_summary("W5NGU")
        self.assertEqual((s["packets"], s["direct"]), (2, 2))
        self.assertEqual(len(s["sources"]), 2)

        s = self.h.station_summary("K5NONE")
        self.assertEqual((s["packets"], s["last_heard"]), (0, None))


class DaemonSearchTests(unittest.TestCase):

    def _daemon(self, db=True):
        path = os.path.join(tempfile.mkdtemp(), "h.db") if db else None
        if path:
            h = ReceptionHistory(path)
            for packet, at in (
                (_packet("W5NGU-3", "!3313.65N/09707.64W#digi"), time.time() - 60),
                (_packet("K5OLD", "!old"), time.time() - 20 * 86400),
            ):
                h.record(packet, received_at=int(at))
            h.close()
        daemon = KAMDaemon(
            make_kam(ScriptedSerial({})), "/tmp/kamxl-log-test.sock",
            station_callsign="N5BA", history_db=path,
        )
        self.addCleanup(daemon.shutdown)
        return daemon

    def test_search_and_summary(self):
        daemon = self._daemon()
        result = daemon.dispatch("packets.search", {"callsign": "w5ngu"})
        self.assertEqual([p["source"] for p in result["packets"]], ["W5NGU-3"])
        self.assertEqual(result["summary"]["packets"], 1)
        self.assertEqual(result["days"], 7)

        # Older pages don't repeat the summary.
        page = daemon.dispatch("packets.search",
                               {"callsign": "W5NGU", "before_id": 10**9})
        self.assertNotIn("summary", page)

    def test_days(self):
        daemon = self._daemon()
        week = daemon.dispatch("packets.search", {})
        everything = daemon.dispatch("packets.search", {"days": 0})
        self.assertEqual(len(week["packets"]), 1)
        self.assertEqual(len(everything["packets"]), 2)
        self.assertNotIn("summary", everything)

    def test_validation(self):
        daemon = self._daemon()
        for bad in ({"callsign": "W5NGU' OR 1=1"}, {"via": "TOOLONGCALL"},
                    {"text": "x" * 101}, {"days": -1}, {"days": "7"},
                    {"limit": 0}, {"before_id": "5"}):
            with self.subTest(params=bad), self.assertRaises(ValueError):
                daemon.dispatch("packets.search", bad)

    def test_needs_history_db(self):
        daemon = self._daemon(db=False)
        with self.assertRaises(ValueError):
            daemon.dispatch("packets.search", {})


class LogRestTests(RestTestCase):

    def test_page_and_search(self):
        _, port = self.start_stack(ScriptedSerial({}), history_db=True)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/log?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn('id="searchForm"', html)
        self.assertIn("/packets/search", html)

        status, payload = self.request(
            port, "GET", "/packets/search?callsign=W5NGU&text=digi&days=30&limit=50"
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["packets"], [])
        self.assertEqual(payload["result"]["summary"]["packets"], 0)

        status, _ = self.request(port, "GET", "/packets/search?days=soon")
        self.assertEqual(status, 400)
        status, _ = self.request(port, "GET", "/packets/search?callsign=%3Cx%3E")
        self.assertNotEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
