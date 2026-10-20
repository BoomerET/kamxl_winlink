"""
Offline tests for the reception watchdog: warn when the receiver hears
nothing, or only this station's own callsigns, for too long.
"""

import io
import os
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import tncstation_daemon
from fakes import ScriptedSerial, make_kam
from tncstation_daemon import KAMDaemon
from packet import Packet
from test_rest import RestTestCase


def _packet(source, port=2):
    return Packet(
        source=source, destination="APRS", digipeaters=(), port=port,
        frame_type="UI", payload="!3313.65N/09707.64W-", raw="",
    )


class WatchdogStateTests(unittest.TestCase):

    def _daemon(self, minutes=30, callsign="N5BA"):
        daemon = KAMDaemon(
            make_kam(ScriptedSerial({})), "/tmp/kamxl-watchdog-test.sock",
            station_callsign=callsign, watchdog_minutes=minutes,
        )
        self.addCleanup(daemon.shutdown)
        return daemon

    def test_starting_then_silent(self):
        daemon = self._daemon()
        t0 = daemon._started_at

        self.assertEqual(daemon._watchdog_snapshot(t0 + 60)["state"], "starting")

        snap = daemon._watchdog_snapshot(t0 + 31 * 60)
        self.assertEqual(snap["state"], "silent")
        self.assertIn("Nothing heard", snap["message"])

    def test_ok_when_other_stations_heard(self):
        daemon = self._daemon()
        daemon._handle_packet(_packet("W5NGU-3"))

        snap = daemon._watchdog_snapshot()
        self.assertEqual(snap["state"], "ok")
        self.assertEqual(snap["last_other_source"], "W5NGU-3")

    def test_only_own_stations(self):
        # Last night's amplifier problem: the D75 (N5BA-7) still heard,
        # nothing else for over half an hour.
        daemon = self._daemon()
        daemon._handle_packet(_packet("W5NGU-3"))
        heard_other = daemon._last_other_packet_at

        with daemon._stats_lock:
            daemon._last_own_packet_at = heard_other + 40 * 60

        snap = daemon._watchdog_snapshot(heard_other + 41 * 60)
        self.assertEqual(snap["state"], "only_own")
        self.assertIn("Only your own stations", snap["message"])
        self.assertEqual(snap["minutes_since_other"], 41.0)

    def test_own_ssids_never_count_as_other(self):
        daemon = self._daemon()
        for source in ("N5BA", "N5BA-7", "n5ba-10"):
            daemon._handle_packet(_packet(source))

        self.assertIsNone(daemon._last_other_packet_at)
        self.assertIsNotNone(daemon._last_own_packet_at)

    def test_similar_callsign_is_other(self):
        daemon = self._daemon()
        daemon._handle_packet(_packet("N5BAX"))
        self.assertEqual(daemon._last_other_source, "N5BAX")

    def test_port1_ignored(self):
        daemon = self._daemon()
        daemon._handle_packet(_packet("W1ABC", port=1))
        self.assertIsNone(daemon._last_other_packet_at)

    def test_disabled(self):
        daemon = self._daemon(minutes=0)
        snap = daemon._watchdog_snapshot(daemon._started_at + 999 * 60)
        self.assertEqual(snap["state"], "disabled")

    def test_threshold_validation(self):
        with self.assertRaises(ValueError):
            self._daemon(minutes=2)

    def test_status_includes_watchdog(self):
        daemon = self._daemon()
        status = daemon.dispatch("status", {})
        self.assertIn("watchdog", status["diagnostics"])
        self.assertEqual(status["diagnostics"]["watchdog"]["state"], "starting")

    def test_tick_logs_alert_and_recovery_once(self):
        daemon = self._daemon()
        states = iter(["silent", "silent", "ok", "ok"])

        def fake_snapshot(now=None):
            return {"state": next(states), "message": "nothing heard",
                    "last_other_source": "W5NGU-3"}

        with patch.object(daemon, "_watchdog_snapshot", fake_snapshot), \
             self.assertLogs("tncstation_daemon", level="INFO") as logs:
            for _ in range(4):
                daemon._watchdog_tick()

        messages = [r.getMessage() for r in logs.records]
        self.assertEqual(messages, [
            "Reception watchdog: nothing heard",
            "Reception watchdog: recovered (heard W5NGU-3)",
        ])


class WatchdogCommandLineTests(unittest.TestCase):

    def test_too_small_threshold_rejected_before_serial(self):
        with patch.dict(os.environ, {"TNCSTATION_CALLSIGN": "N5BA"}, clear=True), \
             patch.object(tncstation_daemon, "_load_dotenv"), \
             patch.object(tncstation_daemon, "KAMXL") as kamxl_cls, \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                tncstation_daemon.main(
                    ["--port", "/dev/null", "--watchdog-minutes", "2"]
                )
        kamxl_cls.assert_not_called()


class WatchdogDashboardTests(RestTestCase):

    def test_status_endpoint_and_banner(self):
        import http.client
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/status")
        self.assertEqual(status, 200)
        self.assertIn("watchdog", payload["result"]["diagnostics"])

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/?token=test-token")
        html = conn.getresponse().read().decode("utf-8")
        self.assertIn('id="receptionAlert"', html)
        self.assertIn("showReceptionAlert", html)


if __name__ == "__main__":
    unittest.main()
