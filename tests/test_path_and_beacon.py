"""
Offline tests for the APRS digipeater path (KAMXL_APRS_PATH) and the
position beacon (KAMXL_LATITUDE / KAMXL_LONGITUDE / KAMXL_BEACON).
"""

import io
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import kamxl_daemon
from aprs import format_aprs_position, parse_position
from aprs_tx import (
    APRSTransmitError,
    execute_aprs_tx_transaction,
    validate_aprs_path,
    validate_unproto,
)
from fakes import make_kam
from kamxl_daemon import KAMDaemon
from packet import Packet
from test_aprs_serial import APRSTransactionFake
from test_daemon import _Client


_CONFIRM = {
    "confirm_transmit": True,
    "confirm_vhf_port2_radio_ready": True,
    "confirm_hf_port1_disconnected": True,
}

_BEACON = {
    "latitude": 33.2275,
    "longitude": -97.1273,
    "symbol": "/-",
    "comment": "N5BA KAM-XL",
}

_BEACON_PAYLOAD = "=3313.65N/09707.64W-N5BA KAM-XL"


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ---------------------------------------------------------------------------
# Pure formatting / validation
# ---------------------------------------------------------------------------

class PositionFormatTests(unittest.TestCase):

    def test_basic_report(self):
        self.assertEqual(
            format_aprs_position(33.2275, -97.1273, comment="N5BA KAM-XL"),
            _BEACON_PAYLOAD,
        )

    def test_round_trips_through_parser(self):
        pos = parse_position(_BEACON_PAYLOAD)
        self.assertAlmostEqual(pos.latitude, 33.2275, places=3)
        self.assertAlmostEqual(pos.longitude, -97.1273, places=3)
        self.assertEqual(pos.symbol_code, "-")

    def test_minutes_round_up_into_next_degree(self):
        self.assertEqual(
            format_aprs_position(-0.99999, 179.99999, "\\", "k"),
            "=0100.00S\\18000.00Ek",
        )

    def test_no_messaging_flag(self):
        self.assertTrue(
            format_aprs_position(1, 1, messaging=False).startswith("!")
        )

    def test_invalid_inputs(self):
        for kwargs in (
            {"latitude": 91, "longitude": 0},
            {"latitude": 0, "longitude": -181},
            {"latitude": 0, "longitude": 0, "comment": "x" * 44},
            {"latitude": 0, "longitude": 0, "comment": "bad|pipe"},
            {"latitude": 0, "longitude": 0, "symbol_code": ""},
            {"latitude": 0, "longitude": 0, "symbol_table": "x"},
            {"latitude": float("nan"), "longitude": 0},
        ):
            with self.assertRaises(ValueError, msg=kwargs):
                format_aprs_position(**kwargs)


class PathValidationTests(unittest.TestCase):

    def test_normalizes(self):
        self.assertEqual(
            validate_aprs_path(" wide1-1, wide2-1 "), "WIDE1-1,WIDE2-1"
        )

    def test_direct_spellings(self):
        for value in ("", "DIRECT", "none", None):
            self.assertEqual(validate_aprs_path(value), "")

    def test_digi_without_ssid_allowed(self):
        self.assertEqual(validate_aprs_path("W5NGU,WIDE2"), "W5NGU,WIDE2")

    def test_rejects_injection_and_too_long(self):
        for bad in ("WIDE1-1\rRESET", "WIDE1-1;X", ",".join(["WIDE1-1"] * 9)):
            with self.assertRaises(APRSTransmitError, msg=repr(bad)):
                validate_aprs_path(bad)

    def test_unproto_readback_with_via_on_both_ports(self):
        self.assertEqual(
            validate_unproto(
                "APRS VIA WIDE1-1,WIDE2-1/APRS VIA WIDE1-1,WIDE2-1"
            ),
            "APRS VIA WIDE1-1,WIDE2-1/APRS VIA WIDE1-1,WIDE2-1",
        )
        self.assertEqual(
            validate_unproto("APRS  VIA WIDE1-1, WIDE2-1/CQ"),
            "APRS VIA WIDE1-1,WIDE2-1/CQ",
        )


class TransactionPathTests(unittest.TestCase):

    def setUp(self):
        patcher = patch("aprs_tx.time")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_message_with_path_sets_and_restores_unproto(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)
        payload = ":W1ABC    :Hi{0A1F"

        execute_aprs_tx_transaction(kam, payload, "WIDE1-1,WIDE2-1")

        self.assertIn(b"UNPROTO APRS VIA WIDE1-1,WIDE2-1\r", serial.written)
        self.assertEqual(serial.unproto, "CQ/CQ")
        self.assertIn(payload.encode() + b"\r", serial.written)

    def test_position_beacon_payload_accepted(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        result = execute_aprs_tx_transaction(
            kam, _BEACON_PAYLOAD, "WIDE1-1,WIDE2-1"
        )

        self.assertTrue(result["submitted_to_tnc"])
        self.assertIn(_BEACON_PAYLOAD.encode() + b"\r", serial.written)

    def test_malformed_position_rejected_before_io(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        with self.assertRaises(APRSTransmitError):
            execute_aprs_tx_transaction(kam, "=3313.65N/09707.64W-x|y")
        self.assertEqual(serial.written, [])

    def test_unexpected_readback_restores_and_does_not_transmit(self):
        # If the real KAM-XL ever shows a VIA path differently than we
        # expect, the send must stop cleanly with UNPROTO put back.
        class OnePortFake(APRSTransactionFake):
            def write(self, data):
                if self.mode == "command" and data.startswith(b"UNPROTO "):
                    value = data.decode().strip()[len("UNPROTO "):]
                    self.written.append(data)
                    self.unproto = (
                        value if "/" in value else f"{value}/CQ"
                    )
                    self._out.extend(b"cmd:")
                    return len(data)
                return super().write(data)

        serial = OnePortFake()
        kam = make_kam(serial)
        payload = ":W1ABC    :Hi{0A1F"

        with self.assertRaises(APRSTransmitError):
            execute_aprs_tx_transaction(kam, payload, "WIDE1-1,WIDE2-1")

        self.assertNotIn(payload.encode() + b"\r", serial.written)
        self.assertEqual(serial.unproto, "CQ/CQ")


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class _DaemonMixin:

    def start(self, aprs_path="WIDE1-1,WIDE2-1", beacon=_BEACON,
              serial=None, interval=30):
        patcher = patch("aprs_tx.time")
        patcher.start()
        self.addCleanup(patcher.stop)

        serial = serial or APRSTransactionFake()
        serial.exclusive = True
        socket_path = os.path.join(tempfile.mkdtemp(), "k.sock")
        daemon = KAMDaemon(
            make_kam(serial), socket_path, station_callsign="N5BA",
            aprs_path=aprs_path, beacon=beacon,
            beacon_interval_minutes=interval,
        )
        thread = threading.Thread(target=daemon.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(daemon.shutdown)

        deadline = time.monotonic() + 2
        client = None
        while not os.path.exists(socket_path) and time.monotonic() < deadline:
            time.sleep(0.01)
        while time.monotonic() < deadline:
            try:
                client = _Client(socket_path)
                break
            except (ConnectionRefusedError, FileNotFoundError):
                time.sleep(0.02)
        self.addCleanup(client.close)
        return serial, daemon, client


class DaemonPathTests(_DaemonMixin, unittest.TestCase):

    def test_send_uses_configured_path(self):
        serial, daemon, client = self.start()

        response = client.call(
            "aprs.send_message", addressee="N5BA-7", message="hi", **_CONFIRM
        )

        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["path"], "WIDE1-1,WIDE2-1")
        self.assertIn(b"UNPROTO APRS VIA WIDE1-1,WIDE2-1\r", serial.written)
        self.assertEqual(serial.unproto, "CQ/CQ")

    def test_request_can_send_direct(self):
        serial, daemon, client = self.start()

        response = client.call(
            "aprs.send_message", addressee="N5BA-7", message="hi",
            path="DIRECT", **_CONFIRM
        )

        self.assertTrue(response["ok"], response)
        self.assertIn(b"UNPROTO APRS\r", serial.written)

    def test_invalid_request_path_rejected_without_io(self):
        serial, daemon, client = self.start()

        response = client.call(
            "aprs.send_message", addressee="N5BA-7", message="hi",
            path="WIDE1-1\rRESET", **_CONFIRM
        )

        self.assertFalse(response["ok"])
        self.assertEqual(serial.written, [])

    def test_auto_ack_uses_path(self):
        serial, daemon, client = self.start()
        client.call("aprs.ack_enable", **_CONFIRM)

        daemon._handle_packet(Packet(
            source="N5BA-7", destination="APK005", digipeaters=(), port=2,
            frame_type="UI", payload=":N5BA     :hello{77", raw="",
        ))

        self.assertTrue(_wait_for(
            lambda: b":N5BA-7   :ack77\r" in serial.written
        ))
        self.assertIn(b"UNPROTO APRS VIA WIDE1-1,WIDE2-1\r", serial.written)

    def test_outbox_reports_path(self):
        _, _, client = self.start(aprs_path="DIRECT")
        self.assertEqual(client.call("aprs.outbox")["result"]["path"], "")


class DaemonBeaconTests(_DaemonMixin, unittest.TestCase):

    def test_unconfigured_beacon(self):
        serial, daemon, client = self.start(beacon=None)

        status = client.call("aprs.beacon_status")["result"]
        self.assertFalse(status["configured"])

        response = client.call("aprs.beacon_enable", **_CONFIRM)
        self.assertFalse(response["ok"])
        self.assertIn("KAMXL_LATITUDE", response["error"]["message"])

    def test_enable_requires_confirmations(self):
        serial, daemon, client = self.start()
        self.assertFalse(client.call("aprs.beacon_enable")["ok"])
        self.assertFalse(client.call("aprs.beacon_status")["result"]["enabled"])

    def test_scheduled_beacon_is_sent_and_rescheduled(self):
        serial, daemon, client = self.start()
        self.assertTrue(client.call("aprs.beacon_enable", **_CONFIRM)["ok"])
        with daemon._beacon_lock:
            daemon._beacon_next_at = time.time()

        beacon = _BEACON_PAYLOAD.encode() + b"\r"
        self.assertTrue(_wait_for(lambda: beacon in serial.written))
        self.assertIn(b"UNPROTO APRS VIA WIDE1-1,WIDE2-1\r", serial.written)

        self.assertTrue(_wait_for(
            lambda: client.call(
                "aprs.beacon_status"
            )["result"]["beacons_sent"] == 1
        ))
        status = client.call("aprs.beacon_status")["result"]
        self.assertEqual(status["last_result"], "sent")
        self.assertGreater(status["next_beacon_at"], time.time() + 29 * 60)

    def test_beacon_now(self):
        serial, daemon, client = self.start()

        response = client.call("aprs.beacon_now", **_CONFIRM)

        self.assertTrue(response["ok"], response)
        self.assertIn(_BEACON_PAYLOAD.encode() + b"\r", serial.written)
        # Beacon now doesn't turn the periodic beacon on.
        self.assertFalse(response["result"]["status"]["enabled"])

    def test_paused_after_terminal_use(self):
        serial, daemon, client = self.start()
        client.call("aprs.beacon_enable", **_CONFIRM)
        client.call("send_command", command="STATUS")
        written_before = len(serial.written)

        with daemon._beacon_lock:
            daemon._beacon_next_at = time.time()

        self.assertTrue(_wait_for(
            lambda: client.call(
                "aprs.beacon_status"
            )["result"]["last_result"] == "paused"
        ))
        self.assertEqual(serial.written[written_before:], [])
        status = client.call("aprs.beacon_status")["result"]
        self.assertTrue(status["enabled"])
        self.assertTrue(status["paused"])

        # Operator-initiated beacon re-verifies and goes out.
        response = client.call("aprs.beacon_now", **_CONFIRM)
        self.assertTrue(response["ok"], response)

    def test_failed_beacon_turns_beacon_off(self):
        serial = APRSTransactionFake()
        serial.fail_command = "PACLEN"
        serial, daemon, client = self.start(serial=serial)
        client.call("aprs.beacon_enable", **_CONFIRM)
        with daemon._beacon_lock:
            daemon._beacon_next_at = time.time()

        self.assertTrue(_wait_for(
            lambda: not client.call("aprs.beacon_status")["result"]["enabled"]
        ))
        status = client.call("aprs.beacon_status")["result"]
        self.assertIn("failed", status["disabled_reason"])

    def test_constructor_validation(self):
        kam = make_kam(APRSTransactionFake())
        with self.assertRaises(ValueError):
            KAMDaemon(kam, "/tmp/x.sock", beacon_interval_minutes=5)
        with self.assertRaises(ValueError):
            KAMDaemon(kam, "/tmp/x.sock", beacon={**_BEACON, "symbol": "-"})


class CommandLineTests(unittest.TestCase):

    def _main_exits(self, argv, env=None):
        base = {"KAMXL_CALLSIGN": "N5BA"}
        base.update(env or {})
        with patch.dict(os.environ, base, clear=True), \
             patch.object(kamxl_daemon, "_load_dotenv"), \
             patch.object(kamxl_daemon, "KAMXL") as kamxl_cls, \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                kamxl_daemon.main(["--port", "/dev/null"] + argv)
        kamxl_cls.assert_not_called()

    def test_latitude_without_longitude(self):
        self._main_exits([], {"KAMXL_LATITUDE": "33.2"})

    def test_interval_too_short(self):
        self._main_exits(["--beacon-interval", "5"])

    def test_bad_path(self):
        self._main_exits([], {"KAMXL_APRS_PATH": "WIDE1-1;RESET"})

    def test_bad_comment(self):
        self._main_exits(
            [], {"KAMXL_LATITUDE": "33", "KAMXL_LONGITUDE": "-97",
                 "KAMXL_BEACON_COMMENT": "x" * 50}
        )


if __name__ == "__main__":
    unittest.main()
