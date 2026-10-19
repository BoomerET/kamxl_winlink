"""
The configurable APRS tocall (AX.25 destination), waiting for a KISS
TCP server (Direwolf) that isn't up yet, and KISS-specific wording in
the APRS session check.
"""

import os
import socket
import tempfile
import threading
import time
import unittest

from unittest.mock import patch

from fakes import make_kam

import tncstation_daemon
from aprs_tx import (
    DEFAULT_TOCALL,
    APRSTransmitError,
    build_aprs_tx_plan,
    execute_aprs_tx_transaction,
    validate_tocall,
)
from kiss import GenericKISS, KISSDeframer, decode_ax25
from test_aprs_serial import APRSTransactionFake
from test_kiss import FakeKISSSerial, driver, frames_written
from tnc import TNCError
from tncstation_daemon import KAMDaemon


class TocallTests(unittest.TestCase):

    def test_default_is_an_experimental_range_identifier(self):
        self.assertEqual(DEFAULT_TOCALL, "APZTNC")
        self.assertTrue(DEFAULT_TOCALL.startswith("APZ"))

    def test_validation(self):
        self.assertEqual(validate_tocall(" apztnc "), "APZTNC")
        self.assertEqual(validate_tocall("APRS"), "APRS")
        for bad in ("", "APZTNC1", "APZ-1", "AP ZT", None):
            with self.subTest(bad=bad), self.assertRaises(APRSTransmitError):
                validate_tocall(bad)

    def test_plan_uses_the_tocall(self):
        plan = build_aprs_tx_plan(":W5ABC    :hi", "CQ/CQ", "WIDE1-1", "APZTNC")
        self.assertEqual(plan[0], "UNPROTO APZTNC VIA WIDE1-1")
        self.assertEqual(build_aprs_tx_plan(":W5ABC    :hi", "CQ/CQ", "", "APZTNC")[0],
                         "UNPROTO APZTNC")
        # Old callers get the old destination.
        self.assertEqual(build_aprs_tx_plan(":W5ABC    :hi", "CQ/CQ")[0], "UNPROTO APRS")

    def test_terminal_mode_sets_and_restores_unproto_with_the_tocall(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)
        kam.aprs_tocall = "APZTNC"

        execute_aprs_tx_transaction(kam, ":W5ABC    :hi{1", "WIDE1-1,WIDE2-1")

        written = [w.decode("ascii").strip("\r") for w in serial.written]
        self.assertIn("UNPROTO APZTNC VIA WIDE1-1,WIDE2-1", written)
        self.assertEqual(serial.unproto, "CQ/CQ")      # restored afterwards

    def test_kiss_frame_destination_is_the_tocall(self):
        tnc = driver()
        tnc.aprs_tocall = "APZTNC"
        from aprs_tx import _execute_aprs_tx_transaction
        _execute_aprs_tx_transaction(tnc, ":W5ABC    :hi", "")
        [(_, frame)] = frames_written(tnc.serial)
        self.assertEqual(frame.destination, "APZTNC")

    def test_daemon_passes_it_to_the_driver_and_reports_it(self):
        kam = make_kam(APRSTransactionFake())
        daemon = KAMDaemon(kam, "/tmp/tocall-test.sock", station_callsign="N5BA",
                           aprs_tocall="apztnc")
        self.addCleanup(daemon.shutdown)
        self.assertEqual(kam.aprs_tocall, "APZTNC")
        self.assertEqual(daemon.dispatch("status", {})["aprs_tocall"], "APZTNC")

    def test_constructed_directly_the_daemon_keeps_aprs(self):
        kam = make_kam(APRSTransactionFake())
        daemon = KAMDaemon(kam, "/tmp/tocall-test2.sock", station_callsign="N5BA")
        self.addCleanup(daemon.shutdown)
        self.assertEqual(kam.aprs_tocall, "APRS")

    def run_main(self, env):
        captured = {}

        class Stop(Exception):
            pass

        def fake_daemon(*args, **kwargs):
            captured.update(kwargs)
            raise Stop

        class FakeTNC:
            MODEL = "KAM-XL"

            def connect(self):
                pass

        base = {"TNCSTATION_PORT": "/dev/null", "TNCSTATION_CALLSIGN": "N5BA"}
        with patch.dict("os.environ", dict(base, **env), clear=True), \
                patch.object(tncstation_daemon, "_load_dotenv"), \
                patch.object(tncstation_daemon, "create_tnc", lambda *a, **k: FakeTNC()), \
                patch.object(tncstation_daemon, "KAMDaemon", fake_daemon), \
                patch("logging.basicConfig"):
            with self.assertRaises(Stop):
                tncstation_daemon.main([])
        return captured["aprs_tocall"]

    def test_command_line_default_and_setting(self):
        self.assertEqual(self.run_main({}), "APZTNC")
        self.assertEqual(self.run_main({"TNCSTATION_TOCALL": "aprs"}), "APRS")

    def test_bad_setting_is_refused_at_startup(self):
        with self.assertRaises(SystemExit):
            with patch("sys.stderr"):
                self.run_main({"TNCSTATION_TOCALL": "TOOLONG1"})


class TCPRetryTests(unittest.TestCase):

    def free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_waits_for_a_server_that_starts_late(self):
        port = self.free_port()
        server = socket.socket()
        accepted = threading.Event()

        def start_later():
            time.sleep(0.4)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", port))
            server.listen(1)
            conn, _ = server.accept()
            accepted.set()
            conn.close()

        threading.Thread(target=start_later, daemon=True).start()
        self.addCleanup(server.close)

        tnc = GenericKISS(f"tcp://127.0.0.1:{port}")
        with patch.object(GenericKISS, "TCP_RETRY_SECONDS", 0.1), \
                self.assertLogs("kamxl", "INFO") as logs:
            tnc.connect()
        self.addCleanup(tnc.disconnect)

        self.assertTrue(accepted.wait(2))
        output = "\n".join(logs.output)
        self.assertIn(f"127.0.0.1:{port} isn't accepting connections", output)
        self.assertIn("is Direwolf running", output)
        self.assertEqual(output.count("isn't accepting connections"), 1)   # said once
        self.assertIn("Connected to KISS TCP server", output)

    def test_gives_up_with_a_clear_error_when_told_to(self):
        port = self.free_port()
        tnc = GenericKISS(f"tcp://127.0.0.1:{port}")
        with patch.object(GenericKISS, "TCP_RETRY_SECONDS", 0.01), \
                patch.object(GenericKISS, "TCP_MAX_ATTEMPTS", 3), \
                self.assertLogs("kamxl", "WARNING"):
            with self.assertRaises(TNCError) as ctx:
                tnc.connect()
        self.assertIn(f"KISSPORT {port}", str(ctx.exception))


class KISSSessionWordingTests(unittest.TestCase):

    def test_kiss_session_check_says_kiss(self):
        tnc = GenericKISS("FAKE")
        tnc.serial = FakeKISSSerial()
        daemon = KAMDaemon(tnc, os.path.join(tempfile.mkdtemp(), "s.sock"),
                           station_callsign="N5BA")
        self.addCleanup(daemon.shutdown)

        with self.assertLogs("tncstation_daemon", "INFO") as logs:
            with daemon._kam_lock:
                daemon._verify_aprs_session_locked()

        output = "\n".join(logs.output)
        self.assertIn("KISS mode", output)
        self.assertNotIn("A/2", output)
        self.assertIn("KISS mode", daemon._aprs_safety_reason)

    def test_terminal_session_check_unchanged(self):
        daemon = KAMDaemon(make_kam(APRSTransactionFake()),
                           os.path.join(tempfile.mkdtemp(), "s.sock"),
                           station_callsign="N5BA")
        self.addCleanup(daemon.shutdown)

        with self.assertLogs("tncstation_daemon", "INFO") as logs:
            with daemon._kam_lock:
                daemon._verify_aprs_session_locked()

        self.assertIn("command mode, A/2 idle", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
