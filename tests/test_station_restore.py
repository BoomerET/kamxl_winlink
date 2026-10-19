"""
Offline tests for rebuilding the map after a restart: packet payloads
are stored in the history database and the last hour is replayed into
the station tracker when the daemon starts.
"""

import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from fakes import ScriptedSerial, make_kam
from tncstation_daemon import STATION_MAX_AGE, KAMDaemon
from packet import Packet
from reception_history import ReceptionHistory


def _packet(source, payload, digis=(), port=2, frame_type="UI"):
    return Packet(
        source=source, destination="APRS", digipeaters=tuple(digis),
        port=port, frame_type=frame_type, payload=payload, raw="raw",
    )


POS_A = "!3313.65N/09707.64W#W5NGU digi"
POS_B = "!3314.00N/09708.00W>moving"
WX = "_10181230c220s004g010t072r000p000P000h55b10150"


class PayloadStorageTests(unittest.TestCase):

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "h.db")

    def test_payload_round_trip_oldest_first(self):
        history = ReceptionHistory(self.path)
        self.addCleanup(history.close)
        history.record(_packet("W5NGU-3", POS_A, ["WIDE1-1*"]), received_at=1000)
        history.record(_packet("K5ABC-9", POS_B), received_at=1010)
        history.record(_packet("K5OLD", POS_B), received_at=500)

        got = history.packets_since(900)

        self.assertEqual([(p.source, t) for p, t in got],
                         [("W5NGU-3", 1000), ("K5ABC-9", 1010)])
        packet = got[0][0]
        self.assertEqual(packet.payload, POS_A)
        self.assertEqual(packet.digipeaters, ("WIDE1-1*",))
        self.assertEqual(packet.port, 2)
        self.assertEqual(packet.frame_type, "UI")

    def test_limit_keeps_newest(self):
        history = ReceptionHistory(self.path)
        self.addCleanup(history.close)
        for i in range(5):
            history.record(_packet("W%d" % i, POS_A), received_at=1000 + i)

        got = history.packets_since(0, limit=2)
        self.assertEqual([p.source for p, _ in got], ["W3", "W4"])

    def test_old_database_is_migrated(self):
        # A database created before payloads were stored.
        conn = sqlite3.connect(self.path)
        conn.execute("""
            CREATE TABLE received_packets (
                id INTEGER PRIMARY KEY, received_at INTEGER NOT NULL,
                source TEXT NOT NULL, destination TEXT, frame_type TEXT,
                port INTEGER, digipeaters TEXT NOT NULL)
        """)
        conn.execute(
            "INSERT INTO received_packets (received_at, source, destination,"
            " frame_type, port, digipeaters) VALUES (1000, 'OLD', 'APRS',"
            " 'UI', 2, '[]')"
        )
        conn.commit()
        conn.close()

        history = ReceptionHistory(self.path)
        self.addCleanup(history.close)
        history.record(_packet("NEW", POS_A), received_at=1001)

        self.assertEqual(history.total_packets(), 2)
        # Old rows have no payload and are skipped, not crashed on.
        self.assertEqual([p.source for p, _ in history.packets_since(0)],
                         ["NEW"])

        # Reopening an already-migrated database is fine.
        ReceptionHistory(self.path).close()


class DaemonRestoreTests(unittest.TestCase):

    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "h.db")

    def _seed(self, rows):
        history = ReceptionHistory(self.db)
        for packet, age in rows:
            history.record(packet, received_at=int(time.time() - age))
        history.close()

    def _daemon(self, **kwargs):
        daemon = KAMDaemon(
            make_kam(ScriptedSerial({})), "/tmp/tncstation-restore-test.sock",
            station_callsign="N5BA", history_db=self.db, **kwargs,
        )
        self.addCleanup(daemon.shutdown)
        return daemon

    def test_map_restored_with_trail_weather_and_objects(self):
        self._seed([
            (_packet("K5ABC-9", "!3313.00N/09707.00W>"), 1800),
            (_packet("K5ABC-9", POS_B), 600),
            (_packet("W5NGU-3", POS_A, ["WIDE1-1*"]), 900),
            (_packet("KF5WX", "!3312.00N/09706.00W_"), 1200),
            (_packet("KF5WX", WX), 300),
            (_packet("W5NGU-3",
                     ";146.920TX*181230z3313.65N/09707.64Wr-060 T110"), 400),
            # Too old for the map: heard more than an hour ago.
            (_packet("K5GONE", POS_A), STATION_MAX_AGE + 120),
        ])

        daemon = self._daemon()
        stations = {s["callsign"]: s for s in daemon.dispatch("stations.list", {})}

        self.assertEqual(sorted(stations), ["K5ABC-9", "KF5WX", "W5NGU-3"])
        mobile = stations["K5ABC-9"]
        self.assertEqual(len(mobile["history"]), 2)  # trail survives
        self.assertEqual(mobile["packet_count"], 2)
        self.assertAlmostEqual(mobile["latitude"], 33.2333, places=3)
        # Original receive time, not the restart time.
        self.assertLess(mobile["last_heard"], time.time() - 500)

        self.assertIsNotNone(stations["KF5WX"]["weather"])
        self.assertEqual(
            list(stations["W5NGU-3"]["recent_packets"][0]["digipeaters"]),
            ["WIDE1-1*"],
        )
        objects = daemon.dispatch("objects.list", {})
        self.assertEqual([o["name"] for o in objects], ["146.920TX"])

    def test_replay_never_acks_or_notifies(self):
        # A message to us heard just before the restart must not be
        # acknowledged, counted, or sent to the phone again.
        self._seed([(_packet("N5BA-7", ":N5BA     :hello{12"), 60)])
        notifier = MagicMock()

        daemon = self._daemon(notifier=notifier)

        self.assertEqual(daemon._aprs_ack_queue.qsize(), 0)
        notifier.notify.assert_not_called()
        self.assertEqual(daemon._packets_received, 0)

    def test_restored_stations_still_expire(self):
        self._seed([(_packet("K5ABC-9", POS_B), STATION_MAX_AGE - 5)])
        daemon = self._daemon()
        self.assertEqual(len(daemon.dispatch("stations.list", {})), 1)

        with daemon._stations_lock:
            daemon._stations.expire_stale(
                STATION_MAX_AGE, now=time.time() + 60
            )
        self.assertEqual(daemon.dispatch("stations.list", {}), [])

    def test_no_history_db_starts_empty(self):
        daemon = KAMDaemon(
            make_kam(ScriptedSerial({})), "/tmp/tncstation-restore-test2.sock",
            station_callsign="N5BA",
        )
        self.addCleanup(daemon.shutdown)
        self.assertEqual(daemon.dispatch("stations.list", {}), [])

    def test_bad_database_does_not_stop_startup(self):
        daemon = self._daemon()
        daemon._reception_writer.packets_since = MagicMock(
            side_effect=RuntimeError("disk on fire")
        )
        with self.assertLogs("tncstation_daemon", level="ERROR"):
            daemon._restore_stations()
        self.assertEqual(daemon.dispatch("stations.list", {}), [])


if __name__ == "__main__":
    unittest.main()
