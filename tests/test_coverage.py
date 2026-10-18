"""
Offline tests for coverage tracking: which digipeaters hear us, how
well we hear others, broken down by station setup (radio/antenna).
"""

import os
import tempfile
import time
import unittest

from aprs_coverage import distance_miles, first_hop, normalize_setup
from fakes import ScriptedSerial, make_kam
from kamxl_daemon import KAMDaemon
from packet import Packet
from reception_history import ReceptionHistory
from test_rest import RestTestCase


HOME = (33.2390, -96.8450)  # N5BA
W5NGU = "!3313.65N/09707.64W#W5NGU digi"  # ~16 mi west
FAR = "!3330.00N/09730.00W-far station"   # ~46 mi


def _packet(source, payload, digis=(), port=2):
    return Packet(source=source, destination="APRS", digipeaters=tuple(digis),
                  port=port, frame_type="UI", payload=payload, raw="")


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class HelperTests(unittest.TestCase):

    def test_first_hop(self):
        cases = [
            (("W5NGU-3*",), "W5NGU-3"),
            # KAM-XL style: only the last repeater is starred.
            (("W5NGU-3", "WIDE1*", "WIDE2-1"), "W5NGU-3"),
            (("K5AAA", "K5BBB*"), "K5AAA"),
            (("w5ngu-3*", "WIDE2-1"), "W5NGU-3"),
            # Digipeated by a station that didn't insert its callsign.
            (("WIDE1*", "WIDE2-1"), ""),
            # Not digipeated at all.
            (("WIDE1-1", "WIDE2-1"), None),
            ((), None),
        ]
        for digis, expected in cases:
            with self.subTest(digis=digis):
                self.assertEqual(first_hop(digis), expected)

    def test_normalize_setup(self):
        self.assertEqual(normalize_setup("  J-pole   16 ft "), "J-pole 16 ft")
        self.assertEqual(normalize_setup(None), "")
        for bad in ("x" * 41, "tab\x07", 5):
            with self.assertRaises(ValueError):
                normalize_setup(bad)

    def test_distance(self):
        # Dallas to Fort Worth, about 30 miles.
        self.assertAlmostEqual(
            distance_miles(32.7767, -96.7970, 32.7555, -97.3308), 31.0,
            delta=1.5,
        )


class HistoryCoverageTests(unittest.TestCase):

    def setUp(self):
        self.h = ReceptionHistory(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.h.close)
        self.now = 1_800_000_000.0

    def test_heard_matches_latest_tx_in_window(self):
        t = self.now
        first = self.h.log_own_tx(t - 1800, "beacon", "", "WIDE1-1", "Mag")
        second = self.h.log_own_tx(t - 10, "beacon", "", "WIDE1-1", "Mag")

        self.assertTrue(self.h.record_own_heard(t, "beacon", "", "digi", "W5NGU-3"))
        # Same digipeater heard again for the same beacon: ignored.
        self.assertFalse(self.h.record_own_heard(t + 1, "beacon", "", "digi", "W5NGU-3"))
        # Another digipeater for the same beacon counts.
        self.assertTrue(self.h.record_own_heard(t + 2, "beacon", "", "digi", "K5XYZ"))
        # Nothing sent in the last 15 minutes: not credited.
        self.assertFalse(self.h.record_own_heard(t + 3600, "beacon", "", "digi", "W5NGU-3"))
        # Message IDs must match.
        self.assertFalse(self.h.record_own_heard(t, "message", "AB12", "digi", "W5NGU-3"))

        rows = self.h._connection.execute(
            "SELECT tx_id FROM own_heard").fetchall()
        self.assertEqual({r[0] for r in rows}, {second})
        self.assertNotIn(first, {r[0] for r in rows})

    def test_report_per_setup(self):
        t = self.now
        # Digipeater position known from its own beacon.
        self.h.record(_packet("W5NGU-3", W5NGU), received_at=int(t - 100), setup="J-pole")

        # Mag-mount: 4 beacons, W5NGU-3 heard 1 of them.
        for i in range(4):
            self.h.log_own_tx(t - 86400 - i * 1800, "beacon", "", "", "Mag")
        self.h.record_own_heard(t - 86400 + 5, "beacon", "", "digi", "W5NGU-3")

        # J-pole: 2 beacons + 1 message, all heard by W5NGU-3, one gated.
        self.h.log_own_tx(t - 3600, "beacon", "", "", "J-pole")
        self.h.log_own_tx(t - 1800, "beacon", "", "", "J-pole")
        self.h.log_own_tx(t - 600, "message", "AB12", "", "J-pole")
        self.h.record_own_heard(t - 3595, "beacon", "", "digi", "W5NGU-3")
        self.h.record_own_heard(t - 1795, "beacon", "", "digi", "W5NGU-3")
        self.h.record_own_heard(t - 595, "message", "AB12", "digi", "W5NGU-3")
        self.h.record_own_heard(t - 590, "message", "AB12", "igate", "W5CGC-10")

        # Receive side: J-pole hears a far station directly, and a
        # digipeated one; our own stations don't count.
        self.h.record(_packet("K5FAR", FAR), received_at=int(t - 50), setup="J-pole")
        self.h.record(_packet("K5VIA", FAR, ["W5NGU-3*"]), received_at=int(t - 40), setup="J-pole")
        self.h.record(_packet("N5BA-7", FAR), received_at=int(t - 30), setup="J-pole")

        report = self.h.coverage_report(
            t - 30 * 86400, home=HOME,
            is_own=lambda c: c.split("-")[0] == "N5BA",
        )
        by = {e["setup"]: e for e in report}
        self.assertEqual([e["setup"] for e in report], ["J-pole", "Mag"])

        jpole, mag = by["J-pole"], by["Mag"]
        self.assertEqual(jpole["tx"], {"total": 3, "beacons": 2, "messages": 1,
                                       "heard": 3, "digipeated": 3, "igated": 1})
        self.assertEqual(mag["tx"]["total"], 4)
        self.assertEqual(mag["tx"]["digipeated"], 1)

        digi = jpole["digipeaters"][0]
        self.assertEqual((digi["callsign"], digi["heard"], digi["pct"]),
                         ("W5NGU-3", 3, 100.0))
        self.assertAlmostEqual(digi["latitude"], 33.2275, places=3)
        self.assertGreater(digi["distance_mi"], 10)
        self.assertEqual(mag["digipeaters"][0]["pct"], 25.0)
        self.assertEqual(jpole["igates"][0]["callsign"], "W5CGC-10")

        rx = jpole["rx"]
        self.assertEqual(rx["packets"], 3)  # W5NGU-3, K5FAR, K5VIA
        self.assertEqual(rx["stations"], 3)
        self.assertEqual(rx["direct_stations"], 2)  # not K5VIA
        far = rx["farthest_direct"]
        self.assertEqual([f["callsign"] for f in far], ["K5FAR", "W5NGU-3"])
        self.assertGreater(far[0]["distance_mi"], far[1]["distance_mi"])
        self.assertEqual(mag["rx"]["packets"], 0)

    def test_report_without_home_has_no_distances(self):
        self.h.record(_packet("K5FAR", FAR), received_at=int(self.now), setup="")
        report = self.h.coverage_report(self.now - 3600)
        self.assertEqual(report[0]["setup"], "")
        self.assertEqual(report[0]["rx"]["direct_stations"], 1)
        self.assertEqual(report[0]["rx"]["farthest_direct"], [])

    def test_old_rows_without_setup_group_as_unlabeled(self):
        self.h.record(_packet("K5OLD", FAR), received_at=int(self.now))
        report = self.h.coverage_report(self.now - 3600, home=HOME)
        self.assertEqual([e["setup"] for e in report], [""])


class DaemonCoverageTests(unittest.TestCase):

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "h.db")

    def _daemon(self):
        daemon = KAMDaemon(
            make_kam(ScriptedSerial({})), "/tmp/kamxl-coverage-test.sock",
            station_callsign="N5BA", history_db=self.db,
            beacon={"latitude": HOME[0], "longitude": HOME[1],
                    "comment": "N5BA KAM-XL"},
        )
        self.addCleanup(daemon.shutdown)
        return daemon

    def test_setup_persists_across_restart(self):
        daemon = self._daemon()
        self.assertEqual(daemon.dispatch("coverage.status", {}),
                         {"enabled": True, "setup": ""})
        daemon.dispatch("coverage.set_setup", {"setup": " Mag-mount  garage "})
        daemon.shutdown()

        again = self._daemon()
        self.assertEqual(again.dispatch("coverage.status", {})["setup"],
                         "Mag-mount garage")

    def test_bad_setup_rejected(self):
        daemon = self._daemon()
        with self.assertRaises(ValueError):
            daemon.dispatch("coverage.set_setup", {"setup": "x" * 50})

    def test_beacon_and_message_heard_back_counted(self):
        daemon = self._daemon()
        daemon.dispatch("coverage.set_setup", {"setup": "J-pole 16 ft"})

        daemon._log_own_tx("beacon", "", "WIDE1-1,WIDE2-1")
        daemon._handle_packet(_packet(
            "N5BA", daemon._beacon_payload, ["W5NGU-3", "WIDE1*", "WIDE2-1"]
        ))
        # A second-hop copy credits the first hop again: not double counted.
        daemon._handle_packet(_packet(
            "N5BA", daemon._beacon_payload, ["W5NGU-3", "K5XYZ*"]
        ))

        daemon._log_own_tx("message", "AB12", "WIDE1-1,WIDE2-1")
        daemon._handle_packet(_packet(
            "N5BA", ":N5BA-7   :hi{AB12", ["W5NGU-3*", "WIDE2-1"]
        ))
        daemon._handle_packet(_packet(
            "W5CGC-10", "}N5BA>APRS,TCPIP,W5CGC-10*::N5BA-7   :hi{AB12"
        ))

        report = daemon.dispatch("coverage.report", {"days": 30})
        self.assertEqual(report["home"]["latitude"], HOME[0])
        entry = report["setups"][0]
        self.assertEqual(entry["setup"], "J-pole 16 ft")
        self.assertEqual(entry["tx"]["total"], 2)
        self.assertEqual(entry["tx"]["digipeated"], 2)
        self.assertEqual(entry["tx"]["igated"], 1)
        self.assertEqual(
            [(d["callsign"], d["heard"]) for d in entry["digipeaters"]],
            [("W5NGU-3", 2)],
        )
        # The existing heard-back display still shows the last repeater.
        self.assertEqual(
            daemon.dispatch("aprs.beacon_status", {})["heard_back"]["digipeated_by"],
            "K5XYZ",
        )

    def test_received_packets_tagged_with_setup(self):
        daemon = self._daemon()
        daemon.dispatch("coverage.set_setup", {"setup": "Mag"})
        daemon._handle_packet(_packet("K5FAR", FAR))
        daemon.dispatch("coverage.set_setup", {"setup": "J-pole"})
        daemon._handle_packet(_packet("K5FAR", FAR))

        def setups():
            report = daemon.dispatch("coverage.report", {"days": 1})
            return {e["setup"]: e["rx"]["packets"] for e in report["setups"]}

        self.assertTrue(_wait_for(lambda: setups() == {"Mag": 1, "J-pole": 1}),
                        setups())

    def test_without_history_db(self):
        daemon = KAMDaemon(
            make_kam(ScriptedSerial({})), "/tmp/kamxl-coverage-test2.sock",
            station_callsign="N5BA",
        )
        self.addCleanup(daemon.shutdown)
        report = daemon.dispatch("coverage.report", {})
        self.assertEqual((report["enabled"], report["setups"]), (False, []))
        with self.assertRaises(ValueError):
            daemon.dispatch("coverage.set_setup", {"setup": "Mag"})
        # Transmissions/heard-back are simply not recorded.
        daemon._log_own_tx("beacon", "", "")


class CoverageRestTests(RestTestCase):

    def test_routes(self):
        _, port = self.start_stack(ScriptedSerial({}), history_db=True)

        status, payload = self.request(port, "POST", "/coverage/setup",
                                       body={"setup": "J-pole 16 ft"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["setup"], "J-pole 16 ft")

        status, payload = self.request(port, "GET", "/coverage?days=7")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["days"], 7)
        self.assertEqual(payload["result"]["setup"], "J-pole 16 ft")

        status, _ = self.request(port, "GET", "/coverage?days=0")
        self.assertEqual(status, 400)
        status, _ = self.request(port, "POST", "/coverage/setup",
                                 body={"setup": 5})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
