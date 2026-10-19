"""
Offline tests for the configurable station callsign (TNCSTATION_CALLSIGN /
--callsign) and automatic ACKs for messages gated from APRS-IS.
"""

import io
import os
from contextlib import redirect_stderr
import time
import unittest
from unittest.mock import patch

import tncstation_daemon
from tncstation_daemon import KAMDaemon, normalize_station_callsign
from packet import Packet
from fakes import make_kam
from test_aprs_serial import APRSTransactionFake
from test_daemon import DaemonTestCase


_CONFIRM = {
    "confirm_transmit": True,
    "confirm_vhf_port2_radio_ready": True,
    "confirm_hf_port1_disconnected": True,
}


def _packet(source, payload, port=2):
    return Packet(
        source=source, destination="APRS", digipeaters=(), port=port,
        frame_type="UI", payload=payload, raw="",
    )


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class NormalizeCallsignTests(unittest.TestCase):

    def test_valid(self):
        self.assertEqual(normalize_station_callsign(" n5ba "), "N5BA")
        self.assertEqual(normalize_station_callsign("n5ba-10"), "N5BA-10")

    def test_invalid(self):
        for bad in ("", None, "N5BA-100", "TOOLONGX", "N5 BA", "N5BA-X"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                normalize_station_callsign(bad)


class CommandLineTests(unittest.TestCase):

    def _main(self, argv, env):
        with patch.dict(os.environ, env, clear=True), \
             patch.object(tncstation_daemon, "_load_dotenv"), \
             patch.object(tncstation_daemon, "KAMXL") as kamxl_cls, \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                tncstation_daemon.main(argv)
        return kamxl_cls

    def test_callsign_required(self):
        kamxl_cls = self._main(["--port", "/dev/null"], {})
        # Fails before the serial port is ever opened.
        kamxl_cls.assert_not_called()

    def test_invalid_callsign_rejected_before_serial(self):
        kamxl_cls = self._main(
            ["--port", "/dev/null"], {"TNCSTATION_CALLSIGN": "not a call"}
        )
        kamxl_cls.assert_not_called()


class StationCallsignTests(unittest.TestCase):

    def _daemon(self, callsign):
        # No serve_forever(); just the object, for pure-logic checks.
        daemon = KAMDaemon(
            make_kam(APRSTransactionFake()),
            "/tmp/kamxl-callsign-test.sock",
            station_callsign=callsign,
        )
        self.addCleanup(daemon.shutdown)
        return daemon

    def test_ssid_callsign_used_for_ack_candidates(self):
        daemon = self._daemon("n5ba-10")
        self.assertEqual(daemon.station_callsign, "N5BA-10")

        self.assertEqual(
            daemon._aprs_ack_candidate(
                _packet("N5BA-7", ":N5BA-10  :hi{12")
            ),
            ("N5BA-7", "12"),
        )
        # Plain N5BA is a different station.
        self.assertIsNone(daemon._aprs_ack_candidate(
            _packet("N5BA-7", ":N5BA     :hi{12")
        ))

    def test_no_callsign_means_no_acks(self):
        daemon = self._daemon(None)
        self.assertIsNone(daemon._aprs_ack_candidate(
            _packet("N5BA-7", ":N5BA     :hi{12")
        ))

    def test_third_party_candidate_uses_originator(self):
        daemon = self._daemon("N5BA")
        self.assertEqual(
            daemon._aprs_ack_candidate(_packet(
                "K5IGT-10",
                "}w1abc-9>APDR16,TCPIP,K5IGT-10*::N5BA     :hi there{AB",
            )),
            ("W1ABC-9", "AB"),
        )

    def test_own_gated_packet_not_acked(self):
        daemon = self._daemon("N5BA")
        self.assertIsNone(daemon._aprs_ack_candidate(_packet(
            "K5IGT-10", "}N5BA>APRS,TCPIP,K5IGT-10*::N5BA     :loop{1",
        )))

    def test_third_party_ack_not_acked(self):
        daemon = self._daemon("N5BA")
        self.assertIsNone(daemon._aprs_ack_candidate(_packet(
            "K5IGT-10", "}W1ABC>APRS,TCPIP,K5IGT-10*::N5BA     :ack12",
        )))


class IgateAutoAckTests(DaemonTestCase):

    def setUp(self):
        patcher = patch("aprs_tx.time")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_igated_message_gets_acked_over_rf(self):
        serial = APRSTransactionFake()
        serial.exclusive = True
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)
        self.assertTrue(client.call("aprs.ack_enable", **_CONFIRM)["ok"])

        daemon._handle_packet(_packet(
            "K5IGT-10",
            "}W1ABC-9>APDR16,TCPIP,K5IGT-10*::N5BA     :hi from IS{AB",
        ))

        # ACK goes to the originator, not the iGate.
        ack = b":W1ABC-9  :ackAB\r"
        self.assertTrue(_wait_for(lambda: ack in serial.written))
        self.assertNotIn(b":K5IGT-10 :ackAB\r", serial.written)

    def test_duplicate_igated_copy_acked_once(self):
        serial = APRSTransactionFake()
        serial.exclusive = True
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)
        client.call("aprs.ack_enable", **_CONFIRM)

        payload = "}W1ABC-9>APDR16,TCPIP,K5IGT-10*::N5BA     :dup{CD"
        daemon._handle_packet(_packet("K5IGT-10", payload))
        daemon._handle_packet(_packet("WIDE1-1", payload))

        ack = b":W1ABC-9  :ackCD\r"
        self.assertTrue(_wait_for(lambda: ack in serial.written))
        time.sleep(0.3)
        self.assertEqual(serial.written.count(ack), 1)


if __name__ == "__main__":
    unittest.main()
