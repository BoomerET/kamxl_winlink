"""
Offline, end-to-end tests for kamxl_daemon.py: a real KAMDaemon,
wired to the same scripted fake serial connections used elsewhere in
this suite, listening on a real (throwaway) Unix socket, talked to by
a real socket client -- no KAM-XL hardware, no mocking of the socket
layer itself.
"""

import json
import os
import socket
import tempfile
import threading
import time
import unittest

import winlink_api

from fakes import CannedSerial, ChunkSerial, ScriptedSerial, make_kam

from kamxl_daemon import KAMDaemon, _load_dotenv
from pbbs import PBBSMessage, PBBSMessageSummary
from pathlib import Path
from packet import Packet
from reception_history import ReceptionHistory
from test_aprs_serial import APRSTransactionFake
from winlink import OutgoingMessage, Proposal, WinlinkMessage
from unittest.mock import patch

class _Client:
    """
    Minimal newline-delimited JSON client for exercising a KAMDaemon
    over its Unix socket.
    """

    def __init__(self, socket_path, timeout=2):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(socket_path)
        self.rfile = self.sock.makefile("r")
        self._next_id = 0

    def call(self, method, **params):
        self._next_id += 1
        request_id = str(self._next_id)

        self.sock.sendall((json.dumps({
            "id": request_id,
            "method": method,
            "params": params,
        }) + "\n").encode("ascii"))

        # Skip past any monitor "packet" events (no "id") that might
        # be interleaved before our matching response arrives.
        while True:
            response = json.loads(self._readline())

            if response.get("id") == request_id:
                return response

    def read_event(self, timeout=2):
        self.sock.settimeout(timeout)
        return json.loads(self._readline())

    def _readline(self):
        line = self.rfile.readline()

        if not line:
            raise ConnectionError("Daemon closed the connection")

        return line

    def close(self):
        # socket.makefile() keeps its own reference to the underlying
        # fd -- closing just self.sock without also closing self.rfile
        # leaves the connection technically still open at the OS
        # level (refcounted), so the server never sees EOF/reset.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        self.rfile.close()
        self.sock.close()


class DaemonTestCase(unittest.TestCase):
    """
    Starts a KAMDaemon wired to a caller-supplied fake serial
    connection, on a throwaway Unix socket, in a background thread.
    """

    def start_daemon(self, serial):
        kam = make_kam(serial)

        tmp_dir = tempfile.mkdtemp()
        socket_path = os.path.join(tmp_dir, "kamxl.sock")

        daemon = KAMDaemon(kam, socket_path, station_callsign="N5BA")

        thread = threading.Thread(
            target=daemon.serve_forever,
            daemon=True
        )
        thread.start()

        deadline = time.monotonic() + 2

        while (
            not os.path.exists(socket_path)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        # addCleanup runs LIFO -- register thread.join() *before*
        # shutdown() so shutdown() (which unblocks serve_forever()'s
        # loop) actually runs first. Registered the other way around,
        # join(timeout) just burns its whole timeout waiting on a
        # thread nothing has told to stop yet.
        self.addCleanup(thread.join, 2)
        self.addCleanup(daemon.shutdown)

        return daemon, socket_path

    def connect(self, socket_path):
        # The socket file existing (checked in start_daemon) doesn't
        # strictly guarantee the server is done with bind()+listen()
        # by the time a client tries to connect -- under load (this
        # suite spins up a fresh daemon thread per test), an early
        # connect() attempt can still occasionally see
        # ConnectionRefusedError. Retry briefly rather than assume
        # the first attempt lands.
        deadline = time.monotonic() + 2
        last_error = None

        while time.monotonic() < deadline:
            try:
                client = _Client(socket_path)
                break
            except ConnectionRefusedError as exc:
                last_error = exc
                time.sleep(0.02)
        else:
            raise last_error

        self.addCleanup(client.close)

        return client

class APRSAutoACKTests(DaemonTestCase):
    """Automatic APRS ACK safety tests using fake serial hardware."""

    def test_auto_ack_disabled_by_default(self):
        serial = APRSTransactionFake()
        daemon, socket_path = self.start_daemon(serial)

        # The worker may run, but RF authorization must be absent.
        self.assertTrue(daemon._aprs_ack_thread.is_alive())
        self.assertFalse(daemon._aprs_auto_ack_enabled)
        self.assertFalse(daemon._aprs_ack_is_authorized())
        self.assertTrue(daemon._aprs_session_uncertain)

        # An incoming APRS message must not queue an ACK.
        packet = Packet(
            source="N5BA-7",
            destination="APRS",
            digipeaters=(),
            port=2,
            frame_type="UI",
            payload=":N5BA     :Test message{123",
            raw="",
        )

        daemon._handle_packet(packet)

        self.assertTrue(daemon._aprs_ack_queue.empty())
        self.assertEqual(serial.written, [])

    def test_auto_ack_enable_requires_explicit_confirmations(self):
        serial = APRSTransactionFake()
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        response = client.call("aprs.ack_enable")

        self.assertFalse(response["ok"])
        self.assertEqual(
            response["error"]["type"],
            "APRSTransmitError",
        )

        self.assertFalse(daemon._aprs_auto_ack_enabled)
        self.assertFalse(daemon._aprs_ack_is_authorized())

        # Rejection must not access the TNC.
        self.assertEqual(serial.written, [])

class APRSPreviewTests(DaemonTestCase):
    """APRS preview must validate input without accessing the TNC."""

    def test_aprs_get_unproto_reads_without_modifying_configuration(self):
        serial = APRSTransactionFake()
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        # Offline simulation of a previously verified session.
        # Never clear this latch directly in production.
        with daemon._kam_lock:
            daemon._aprs_session_uncertain = False

        response = client.call("aprs.tx_get_unproto")

        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["unproto"], "CQ/CQ")
        self.assertFalse(response["result"]["rf_enabled"])
        self.assertFalse(response["result"]["transmitted"])

        # Exactly one read-only query; no configuration changes.
        self.assertEqual(serial.written, [b"UNPROTO\r"])
        self.assertEqual(serial.unproto, "CQ/CQ")

        # The diagnostic must not unlock APRS transmission.
        safety = client.call("aprs.tx_safety")
        self.assertEqual(safety["result"]["state"], "unknown")
        self.assertFalse(safety["result"]["rf_enabled"])

    def test_aprs_get_unproto_rejects_uncertain_session(self):
        serial = APRSTransactionFake()
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        # A fresh daemon starts with an uncertain APRS session.
        self.assertTrue(daemon._aprs_session_uncertain)

        response = client.call("aprs.tx_get_unproto")

        self.assertFalse(response["ok"])
        self.assertEqual(
            response["error"]["type"],
            "APRSTransmitError",
        )

        # Rejection must occur before any TNC access.
        self.assertEqual(serial.written, [])
        self.assertEqual(serial.unproto, "CQ/CQ")

        safety = client.call("aprs.tx_safety")
        self.assertFalse(safety["result"]["rf_enabled"])

    def test_aprs_get_unproto_rejects_unsafe_state(self):
        serial = APRSTransactionFake()
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        # Simulate a session with a cleared uncertainty latch
        # but an explicitly unsafe APRS safety state.
        with daemon._kam_lock:
            daemon._aprs_session_uncertain = False
            daemon._invalidate_aprs_safety(
                "Simulated unsafe APRS state"
            )

        response = client.call("aprs.tx_get_unproto")

        self.assertFalse(response["ok"])
        self.assertEqual(
            response["error"]["type"],
            "APRSTransmitError",
        )

        # No serial access or configuration changes permitted.
        self.assertEqual(serial.written, [])
        self.assertEqual(serial.unproto, "CQ/CQ")

        safety = client.call("aprs.tx_safety")
        self.assertEqual(safety["result"]["state"], "unsafe")
        self.assertFalse(safety["result"]["rf_enabled"])

    def test_aprs_get_unproto_failure_latches_unsafe(self):
        serial = APRSTransactionFake()
        serial.fail_command = "UNPROTO"

        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        # Offline simulation of a previously verified session.
        with daemon._kam_lock:
            daemon._aprs_session_uncertain = False

        response = client.call("aprs.tx_get_unproto")

        self.assertFalse(response["ok"])

        # The failed query must latch session uncertainty.
        with daemon._kam_lock:
            self.assertTrue(daemon._aprs_session_uncertain)

        safety = client.call("aprs.tx_safety")
        self.assertEqual(safety["result"]["state"], "unsafe")
        self.assertFalse(safety["result"]["rf_enabled"])

        # Exactly one query, with no configuration modification.
        self.assertEqual(serial.written, [b"UNPROTO\r"])
        self.assertEqual(serial.unproto, "CQ/CQ")

    def test_valid_message(self):
        serial = ScriptedSerial({})
        _, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        response = client.call(
            "aprs.preview_message",
            addressee="ANSRVR",
            message="CQ #APRSTHURSDAY Howdy from Prosper TX",
        )

        self.assertTrue(response["ok"], response)
        self.assertEqual(
            response["result"]["payload"],
            ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX",
        )
        self.assertEqual(response["result"]["length"], 49)
        self.assertEqual(response["result"]["paclen"], 128)
        self.assertEqual(response["result"]["port"], 2)
        self.assertFalse(response["result"]["transmitted"])

    def test_invalid_message(self):
        serial = ScriptedSerial({})
        _, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        response = client.call(
            "aprs.preview_message",
            addressee="INVALID-CALLSIGN",
            message="Hello",
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "ValueError")

    def test_preview_does_not_write_to_serial(self):
        serial = ScriptedSerial({})
        _, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        response = client.call(
            "aprs.preview_message",
            addressee="ANSRVR",
            message="Hello",
        )

        self.assertTrue(response["ok"], response)
        self.assertEqual(serial.written, [])

    def test_aprs_send_requires_all_confirmations(self):
        serial = ScriptedSerial({})
        _, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        base = {
            "addressee": "N5BA-7",
            "message": "Hello from KAM-XL! 73 de N5BA",
        }

        confirmations = {
            "confirm_transmit": True,
            "confirm_vhf_port2_radio_ready": True,
            "confirm_hf_port1_disconnected": True,
        }

        for missing in confirmations:
            with self.subTest(missing=missing):
                params = {
                    **base,
                    **{
                        key: value
                        for key, value in confirmations.items()
                        if key != missing
                    },
                }

                response = client.call("aprs.send_message", **params)

                self.assertFalse(response["ok"], response)
                self.assertIn("error", response)

        self.assertEqual(serial.written, [])

    def test_aprs_send_is_disabled(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)

        client = self.connect(socket_path)
        response = client.call(
            "aprs.send_message",
            addressee="ANSRVR",
            message="CQ #APRSTHURSDAY Howdy from Prosper TX",
        )

        self.assertIsNotNone(response.get("error"))
        self.assertIn(
            "confirm_transmit",
            str(response["error"]).lower(),
        )

        self.assertEqual(serial.written, [])

    def test_aprs_send_cannot_be_enabled_by_request(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)

        client = self.connect(socket_path)
        response = client.call(
            "aprs.send_message",
            addressee="ANSRVR",
            message="CQ #APRSTHURSDAY Howdy from Prosper TX",
            enable_rf=True,
            dry_run=False,
        )

        self.assertIsNotNone(response.get("error"))
        self.assertEqual(serial.written, [])

    def test_aprs_safety_starts_unknown(self):
        serial = ScriptedSerial({})
        _, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        response = client.call("aprs.tx_safety")

        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["state"], "unknown")
        self.assertFalse(response["result"]["rf_enabled"])
        self.assertEqual(serial.written, [])

    def test_raw_terminal_command_marks_aprs_unsafe(self):
        serial = ScriptedSerial({})
        _, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        client.call("send_command", command="STATUS")

        response = client.call("aprs.tx_safety")

        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["state"], "unsafe")
        self.assertIn(
            "Raw terminal command",
            response["result"]["reason"],
        )
        self.assertFalse(response["result"]["rf_enabled"])

    def test_set_invalidates_aprs_safety(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)
        daemon._aprs_session_uncertain = False

        client.call(
            "set",
            command="MYCALL",
            value="N5BA",
        )
        self.assertTrue(daemon._aprs_session_uncertain)

        response = client.call("aprs.tx_safety")

        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["state"], "unsafe")
        self.assertEqual(
            response["result"]["reason"],
            "TNC configuration changed",
        )

    def test_invalidated_safety_cannot_enable_rf(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        client.call("send_command", command="STATUS")

        response = client.call(
            "aprs.send_message",
            addressee="ANSRVR",
            message="CQ #APRSTHURSDAY Howdy from Prosper TX",
            enable_rf=True,
        )

        self.assertFalse(response["ok"])

        safety = client.call("aprs.tx_safety")
        self.assertEqual(safety["result"]["state"], "unsafe")
        self.assertFalse(safety["result"]["rf_enabled"])

        # Only the STATUS command should have reached the TNC.
        self.assertEqual(serial.written, [b"STATUS\r"])

    def test_preflight_rejects_unsafe_state_without_serial_io(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        with daemon._kam_lock:
            daemon._invalidate_aprs_safety(
                "Simulated uncertain TNC state"
            )

        response = client.call("aprs.tx_preflight")

        self.assertTrue(response["ok"], response)
        self.assertFalse(response["result"]["passed"])
        self.assertEqual(response["result"]["state"], "unsafe")
        self.assertFalse(response["result"]["rf_enabled"])
        self.assertEqual(serial.written, [])

    def test_preflight_blocked_on_uncertain_session(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        self.assertTrue(daemon._aprs_session_uncertain)

        response = client.call("aprs.tx_preflight")

        self.assertTrue(response["ok"], response)
        self.assertFalse(response["result"]["passed"])
        self.assertEqual(response["result"]["state"], "unknown")
        self.assertFalse(response["result"]["rf_enabled"])
        self.assertFalse(response["result"]["transmitted"])
        self.assertEqual(response["result"]["checks"], [])

        self.assertIn(
            "session state",
            response["result"]["reason"].lower(),
        )

        # No STATUS or other TNC commands are permitted.
        self.assertEqual(serial.written, [])

        safety = client.call("aprs.tx_safety")
        self.assertEqual(safety["result"]["state"], "unknown")
        self.assertFalse(safety["result"]["rf_enabled"])

    def test_preflight_failure_latches_unsafe(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)
        # Offline simulation: the TNC session has already been
        # independently verified. Never do this in production.
        with daemon._kam_lock:
            daemon._aprs_session_uncertain = False

        response = client.call("aprs.tx_preflight")

        self.assertFalse(response["ok"])

        # A failed preflight must also latch session uncertainty.
        with daemon._kam_lock:
            self.assertTrue(daemon._aprs_session_uncertain)

        safety = client.call("aprs.tx_safety")

        self.assertTrue(safety["ok"], safety)
        self.assertEqual(safety["result"]["state"], "unsafe")
        self.assertFalse(safety["result"]["rf_enabled"])

    def test_aprs_recovery_requires_confirmation(self):
        serial = ScriptedSerial({})
        _, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        response = client.call("aprs.tx_recover")

        self.assertFalse(response["ok"])
        self.assertEqual(serial.written, [])

    def test_aprs_recovery_failure_remains_unsafe(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        # Simulate a previously verified session-clear state.
        # This is offline test setup only, not a production
        # procedure for clearing the uncertainty latch.
        with daemon._kam_lock:
            daemon._aprs_session_uncertain = False

        response = client.call(
            "aprs.tx_recover",
            confirm_recovery=True,
        )

        # The fake cannot complete a valid recovery.
        self.assertFalse(response["ok"])
        # Failed recovery must restore the session uncertainty latch.
        with daemon._kam_lock:
            self.assertTrue(daemon._aprs_session_uncertain)

        safety = client.call("aprs.tx_safety")

        self.assertTrue(safety["ok"], safety)
        self.assertEqual(safety["result"]["state"], "unsafe")
        self.assertFalse(safety["result"]["rf_enabled"])

    def test_aprs_recovery_does_not_enable_transmission(self):
        serial = ScriptedSerial({})
        _, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        client.call(
            "aprs.tx_recover",
            confirm_recovery=True,
        )

        response = client.call(
            "aprs.send_message",
            addressee="ANSRVR",
            message="CQ #APRSTHURSDAY Howdy from Prosper TX",
            enable_rf=True,
        )

        self.assertFalse(response["ok"])
        self.assertIn(
            "confirm_transmit",
            str(response["error"]).lower(),
        )

    def test_aprs_recovery_blocked_after_connection_attempt(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        # Simulate a previous connection attempt whose final
        # AX.25 state is not independently verified.
        with daemon._kam_lock:
            daemon._aprs_session_uncertain = True

        response = client.call(
            "aprs.tx_recover",
            confirm_recovery=True,
        )

        self.assertFalse(response["ok"])
        self.assertIn(
            "recovery blocked",
            str(response["error"]).lower(),
        )

        # No Ctrl-C or STATUS command may reach the TNC.
        self.assertEqual(serial.written, [])

        safety = client.call("aprs.tx_safety")
        self.assertFalse(safety["result"]["rf_enabled"])

    def test_aprs_recovery_blocked_after_raw_command(self):
        serial = ScriptedSerial({})
        _, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        # Even a read-only raw command must conservatively
        # invalidate our knowledge of the TNC's session state.
        client.call("send_command", command="STATUS")

        written_before_recovery = list(serial.written)

        response = client.call(
            "aprs.tx_recover",
            confirm_recovery=True,
        )

        self.assertFalse(response["ok"])
        self.assertIn(
            "recovery blocked",
            str(response["error"]).lower(),
        )

        # Recovery must not add Ctrl-C or any other serial writes.
        self.assertEqual(serial.written, written_before_recovery)

        safety = client.call("aprs.tx_safety")
        self.assertTrue(safety["ok"])
        self.assertFalse(safety["result"]["rf_enabled"])

    def test_aprs_recovery_blocked_on_fresh_daemon(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        # A fresh daemon cannot know the previous TNC state.
        self.assertTrue(daemon._aprs_session_uncertain)

        response = client.call(
            "aprs.tx_recover",
            confirm_recovery=True,
        )

        self.assertFalse(response["ok"])
        self.assertIn(
            "recovery blocked",
            str(response["error"]).lower(),
        )

        # Recovery must not send Ctrl-C or STATUS.
        self.assertEqual(serial.written, [])

        safety = client.call("aprs.tx_safety")
        self.assertTrue(safety["ok"])
        self.assertFalse(safety["result"]["rf_enabled"])

    def test_aprs_diagnostics_startup_is_read_only(self):
        serial = ScriptedSerial({})
        _, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        response = client.call("aprs.tx_diagnostics")

        self.assertTrue(response["ok"], response)

        result = response["result"]

        self.assertTrue(result["session_uncertain"])
        self.assertEqual(result["safety_state"], "unknown")
        self.assertFalse(result["command_mode_verified"])
        self.assertFalse(result["rf_enabled"])
        self.assertFalse(result["recovery_allowed"])

        # Diagnostics must never write to the TNC.
        self.assertEqual(serial.written, [])

    def test_aprs_exit_kiss_requires_confirmation(self):
        serial = ScriptedSerial({})
        _, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        response = client.call("aprs.tx_exit_kiss")

        self.assertFalse(response["ok"])
        self.assertIn(
            "confirmation",
            str(response["error"]).lower(),
        )
        self.assertEqual(serial.written, [])

    def test_aprs_initialize_no_longer_requires_radio_disconnect(self):
        # Initialization writes only Ctrl-C and STATUS, so radios may
        # stay connected; no confirm_radio_disconnected needed.
        serial = APRSTransactionFake()
        serial.mode = "convers"
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        response = client.call("aprs.tx_initialize")

        self.assertTrue(response["ok"], response)
        self.assertFalse(response["result"]["rf_enabled"])
        self.assertEqual(serial.written, [b"\x03", b"STATUS\r"])
        self.assertFalse(daemon._aprs_session_uncertain)

    def test_aprs_exit_kiss_failure_remains_unsafe(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        def fail_exit_kiss(timeout=5):
            raise OSError("Simulated serial failure")

        daemon.kam.exit_kiss_mode = fail_exit_kiss

        response = client.call(
            "aprs.tx_exit_kiss",
            confirm_radio_disconnected=True,
        )

        self.assertFalse(response["ok"])
        self.assertTrue(daemon._aprs_session_uncertain)

        safety = client.call("aprs.tx_safety")
        self.assertEqual(safety["result"]["state"], "unsafe")
        self.assertFalse(safety["result"]["rf_enabled"])

        self.assertEqual(serial.written, [])

    def test_aprs_exit_kiss_response_remains_unverified(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        banner = b"\r\nKANTRONICS KAM XL\r\ncmd:"

        def fake_exit_kiss(timeout=5):
            return banner

        daemon.kam.exit_kiss_mode = fake_exit_kiss

        response = client.call(
            "aprs.tx_exit_kiss",
            confirm_radio_disconnected=True,
        )

        self.assertTrue(response["ok"], response)

        result = response["result"]
        self.assertTrue(result["exit_sequence_sent"])
        self.assertEqual(result["response_hex"], banner.hex())
        self.assertIn("KANTRONICS", result["response_text"])

        self.assertFalse(result["terminal_mode_verified"])
        self.assertFalse(result["initialized"])
        self.assertFalse(result["rf_enabled"])
        self.assertFalse(result["transmitted"])

        self.assertTrue(daemon._aprs_session_uncertain)

        safety = client.call("aprs.tx_safety")
        self.assertEqual(safety["result"]["state"], "unsafe")
        self.assertFalse(safety["result"]["rf_enabled"])

        self.assertEqual(serial.written, [])

    def test_aprs_exit_kiss_rejects_stopped_monitor(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        # Simulate a monitor that is shutting down.
        daemon._monitor_stop.set()

        response = client.call(
            "aprs.tx_exit_kiss",
            confirm_radio_disconnected=True,
        )

        self.assertFalse(response["ok"])
        self.assertIn(
            "monitor thread",
            str(response["error"]).lower(),
        )

        self.assertEqual(serial.written, [])
        self.assertTrue(daemon._aprs_session_uncertain)

        safety = client.call("aprs.tx_safety")
        self.assertEqual(safety["result"]["state"], "unsafe")
        self.assertFalse(safety["result"]["rf_enabled"])

    def test_aprs_initialize_failure_remains_unsafe(self):
        serial = ScriptedSerial({})
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        # Fake serial cannot complete command-mode recovery.
        response = client.call(
            "aprs.tx_initialize",
            confirm_radio_disconnected=True,
        )

        self.assertFalse(response["ok"])
        self.assertTrue(daemon._aprs_session_uncertain)

        safety = client.call("aprs.tx_safety")
        self.assertEqual(safety["result"]["state"], "unsafe")
        self.assertFalse(safety["result"]["rf_enabled"])

    def test_aprs_initialize_failure_cannot_enable_rf(self):
        serial = ScriptedSerial({})
        _, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        client.call(
            "aprs.tx_initialize",
            confirm_radio_disconnected=True,
        )

        response = client.call(
            "aprs.send_message",
            addressee="ANSRVR",
            message="CQ #APRSTHURSDAY Howdy from Prosper TX",
            enable_rf=True,
        )

        self.assertFalse(response["ok"])
        self.assertIn(
            "confirm_transmit",
            str(response["error"]).lower(),
        )

    def test_aprs_initialize_success_does_not_enable_rf(self):
        serial = APRSTransactionFake()
        serial.mode = "convers"

        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        self.assertTrue(daemon._aprs_session_uncertain)

        response = client.call(
            "aprs.tx_initialize",
            confirm_radio_disconnected=True,
        )

        self.assertTrue(response["ok"], response)

        result = response["result"]
        self.assertTrue(result["initialized"])
        self.assertTrue(result["recovered"])
        self.assertFalse(result["rf_enabled"])
        self.assertFalse(result["transmitted"])
        self.assertEqual(result["active_stream"], "A/2")
        self.assertFalse(result["ax25_connected"])

        self.assertFalse(daemon._aprs_session_uncertain)

        self.assertEqual(
            serial.written,
            [b"\x03", b"STATUS\r"],
        )

        safety = client.call("aprs.tx_safety")
        self.assertEqual(safety["result"]["state"], "unknown")
        self.assertFalse(safety["result"]["rf_enabled"])

        # Successful initialization must not unlock transmission.
        send = client.call(
            "aprs.send_message",
            addressee="ANSRVR",
            message="CQ #APRSTHURSDAY Howdy from Prosper TX",
            enable_rf=True,
        )

        self.assertFalse(send["ok"])
        self.assertIn(
            "confirm_transmit",
            str(send["error"]).lower(),
        )
        # The rejected send request must not touch the TNC.
        self.assertEqual(
            serial.written,
            [b"\x03", b"STATUS\r"],
        )

    def test_aprs_initialize_rejects_stopped_monitor(self):
        serial = APRSTransactionFake()
        serial.mode = "convers"

        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        # Stop monitoring without shutting down the RPC server.
        daemon._monitor_stop.set()
        daemon._monitor_thread.join(timeout=2)

        self.assertFalse(daemon._monitor_thread.is_alive())

        # Initialization must fail without touching the TNC.
        response = client.call(
            "aprs.tx_initialize",
            confirm_radio_disconnected=True,
        )

        self.assertFalse(response["ok"])
        self.assertIn(
            "monitor thread",
            str(response["error"]).lower(),
        )

        self.assertEqual(serial.written, [])
        self.assertTrue(daemon._aprs_session_uncertain)
        self.assertEqual(daemon._aprs_safety_state, "unsafe")

    def test_aprs_initialize_rejects_connected_stream(self):
        class ConnectedStreamFake(APRSTransactionFake):
            def write(self, data):
                if data == b"STATUS\r":
                    self.written.append(data)
                    self._out.extend(
                        b"FREE BYTES 2685\r\n"
                        b"A/2  stream - CONNECTED to N5BA-1\r\n"
                        b"cmd:"
                    )
                    return len(data)

                return super().write(data)

        serial = ConnectedStreamFake()
        serial.mode = "convers"

        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        response = client.call(
            "aprs.tx_initialize",
            confirm_radio_disconnected=True,
        )

        self.assertFalse(response["ok"])
        self.assertTrue(daemon._aprs_session_uncertain)

        self.assertEqual(
            serial.written,
            [b"\x03", b"STATUS\r"],
        )

        diagnostics = client.call("aprs.tx_diagnostics")

        self.assertTrue(
            diagnostics["result"]["session_uncertain"]
        )
        self.assertEqual(
            diagnostics["result"]["safety_state"],
            "unsafe",
        )
        self.assertFalse(
            diagnostics["result"]["rf_enabled"]
        )

    def test_tnc_queries_invalidate_aprs_session(self):
        operations = [
            ("get", {"command": "MYCALL"}),
            ("get_typed", {"command": "MYCALL"}),
            ("get_configuration", {}),
            ("read_connected", {"timeout": 0.01}),
        ]

        for method, params in operations:
            with self.subTest(method=method):
                serial = ScriptedSerial({})
                daemon, socket_path = self.start_daemon(serial)
                client = self.connect(socket_path)

                # Simulate previously verified session state.
                daemon._aprs_session_uncertain = False
                daemon._aprs_safety_state = "unknown"

                # The operation may succeed or fail with this fake.
                # Either way, it must invalidate session certainty.
                client.call(method, **params)

                self.assertTrue(
                    daemon._aprs_session_uncertain,
                    f"{method} did not invalidate session certainty",
                )
                self.assertEqual(
                    daemon._aprs_safety_state,
                    "unsafe",
                    f"{method} did not invalidate APRS safety",
                )

class PingStatusTests(DaemonTestCase):
    def test_ping(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("ping")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], "pong")

    def test_status(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("status")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["connected"], True)
        self.assertEqual(response["result"]["port"], "COM_FAKE")
        self.assertEqual(response["result"]["monitor_subscribers"], 0)

    def test_packet_diagnostics(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        before = client.call("status")["result"]["diagnostics"]

        self.assertEqual(before["packets_received"], 0)
        self.assertEqual(before["packets_by_port"], {})
        self.assertIsNone(before["last_packet_at"])
        self.assertGreaterEqual(before["uptime_seconds"], 0)

        from packet import Packet

        packet = Packet(
            source="N5BA",
            destination="APRS",
            digipeaters=(),
            port=2,
            payload=">Test packet",
            raw="N5BA>APRS/2: <UI>:\n>Test packet",
            frame_type="UI",
        )

        daemon._handle_packet(packet)

        after = client.call("status")["result"]["diagnostics"]

        self.assertEqual(after["packets_received"], 1)
        self.assertEqual(after["packets_by_port"], {"2": 1})
        self.assertIsNotNone(after["last_packet_at"])
        self.assertGreaterEqual(after["last_packet_at"], before["started_at"])
        self.assertGreaterEqual(
            after["uptime_seconds"],
            before["uptime_seconds"],
        )

    def test_aprs_reception_stats(self):
        from packet import Packet

        daemon, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        before = client.call("aprs.stats")

        self.assertTrue(before["ok"])
        self.assertEqual(before["result"]["total_packets"], 0)
        self.assertEqual(before["result"]["unique_sources"], 0)
        self.assertEqual(before["result"]["top_sources"], [])

        def make_packet(source, digipeaters=()):
            return Packet(
                source=source,
                destination="APRS",
                digipeaters=digipeaters,
                port=2,
                payload=">Reception statistics test",
                raw=f"{source}>APRS:>Reception statistics test",
                frame_type="UI",
            )

        daemon._handle_packet(
            make_packet("N5BA", ("W5NGU-3*", "WIDE2-1"))
        )
        daemon._handle_packet(
            make_packet("N5BA", ("W5NGU-3*",))
        )
        daemon._handle_packet(
            make_packet("K5TEST", ("W5NGU-3", "WIDE2-1"))
        )

        response = client.call("aprs.stats")

        self.assertTrue(response["ok"])

        stats = response["result"]

        self.assertEqual(stats["scope"], "since_daemon_start")
        self.assertEqual(stats["total_packets"], 3)
        self.assertEqual(stats["unique_sources"], 2)

        self.assertEqual(
            stats["top_sources"][0],
            {"callsign": "N5BA", "packets": 2},
        )

        self.assertEqual(
            stats["top_digipeaters"],
            [{"callsign": "W5NGU-3", "packets": 2}],
        )

        self.assertEqual(stats["used_digipeater_packets"], 2)
        self.assertEqual(
            stats["no_identified_digipeater_packets"],
            1,
        )

        # Existing diagnostics and new statistics must agree.
        status = client.call("status")["result"]["diagnostics"]

        self.assertEqual(
            status["packets_received"],
            stats["total_packets"],
        )

    def test_packet_receive_rates(self):
        from unittest.mock import patch

        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        # Test the rolling-window calculation without relying on
        # real time or waiting five minutes.
        with daemon._stats_lock:
            daemon._recent_packet_times.clear()
            daemon._recent_packet_times.extend([
                700.0,   # Older than five minutes
                750.0,   # Inside five minutes, outside one minute
                950.0,   # Inside one minute
                990.0,   # Inside one minute
            ])

        with patch("kamxl_daemon.time.monotonic", return_value=1000.0):
            status = daemon._m_status({})

        diag = status["diagnostics"]

        self.assertEqual(diag["packets_last_1m"], 2)
        self.assertEqual(diag["packets_last_5m"], 3)
        self.assertEqual(diag["rate_1m"], 2.0)
        self.assertEqual(diag["rate_5m"], 0.6)

        # Verify old timestamps are pruned.
        with daemon._stats_lock:
            self.assertEqual(
                list(daemon._recent_packet_times),
                [750.0, 950.0, 990.0],
            )
    def test_activity_history(self):
        from unittest.mock import patch
    
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))
    
        with daemon._stats_lock:
            daemon._activity_minutes.clear()
            daemon._activity_minutes.update({
                40: 9,   # Outside the 60-minute window
                41: 2,   # Oldest included minute
                75: 4,
                99: 3,
                100: 5,  # Current minute
            })
    
        # Minute 100 corresponds to monotonic time 6000.
        with patch("kamxl_daemon.time.monotonic", return_value=6000.0):
            diagnostics = daemon._m_status({})["diagnostics"]
    
        history = diagnostics["activity_history"]
    
        self.assertEqual(len(history), 60)
        self.assertEqual(history[0], 2)
        self.assertEqual(history[75 - 41], 4)
        self.assertEqual(history[99 - 41], 3)
        self.assertEqual(history[-1], 5)
        self.assertEqual(history[1], 0)
        self.assertEqual(sum(history), 14)

class TypedCommandTests(DaemonTestCase):
    def test_get_typed_multiport_int(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "HBAUD": "HBAUD    0/1200",
        }))
        client = self.connect(socket_path)

        response = client.call("get_typed", command="HBAUD")

        self.assertTrue(response["ok"])
        # Tuples cross the wire as JSON arrays.
        self.assertEqual(response["result"], [0, 1200])

    def test_set_typed_multiport_bool_round_trip(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({
            "MONITOR": "MONITOR  ON/OFF",
        }))
        client = self.connect(socket_path)
        daemon._aprs_session_uncertain = False

        # A JSON array in -> converted back to a tuple before it
        # reaches kamxl.py's isinstance(value, tuple) check.
        response = client.call(
            "set_typed",
            command="MONITOR",
            value=[True, False]
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], [True, False])
        self.assertTrue(daemon._aprs_session_uncertain)
        self.assertEqual(daemon._aprs_safety_state, "unsafe")

    def test_get_configuration(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "DISPLAY": "MYCALL   N5BA-10/N5BA-10\r\nHBAUD    0/1200",
        }))
        client = self.connect(socket_path)

        response = client.call("get_configuration")

        self.assertTrue(response["ok"])
        self.assertEqual(
            response["result"],
            {"MYCALL": "N5BA-10/N5BA-10", "HBAUD": "0/1200"}
        )


class SendCommandTests(DaemonTestCase):
    """
    The raw send_command passthrough added for the web terminal
    (milestone 4) -- unlike get/get_typed, it doesn't assume a
    "COMMAND value" response shape, so it works for commands kamxl.py
    has no typed metadata for at all.
    """

    def test_send_arbitrary_command_returns_raw_text(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "BEACON": "BEACON EVERY 0",
        }))
        client = self.connect(socket_path)

        response = client.call("send_command", command="BEACON")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], "BEACON EVERY 0")

    def test_unknown_command_still_returns_whatever_came_back(self):
        # No typed metadata, no scripted response -- just the echo
        # and prompt, same as an unrecognized command would look on
        # real hardware.
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("send_command", command="NOTAREALCOMMAND")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], "")


class ErrorHandlingTests(DaemonTestCase):
    def test_unknown_method(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("not_a_real_method")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "KAMError")

    def test_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("get")  # missing "command"

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")

    def test_readonly_command_error_reaches_client(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "VERSION": "KAM-XL VERSION 1.24160",
        }))
        client = self.connect(socket_path)

        response = client.call(
            "set_typed",
            command="VERSION",
            value="TEST"
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "KAMError")
        self.assertIn("read-only", response["error"]["message"])


class ConnectStationTests(DaemonTestCase):
    def test_successful_connect(self):
        _, socket_path = self.start_daemon(
            CannedSerial(["*** CONNECTED to KD5EOC-10\r\n"])
        )
        client = self.connect(socket_path)

        response = client.call(
            "connect_station",
            callsign="KD5EOC-10",
            timeout=1
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], "*** CONNECTED to KD5EOC-10\r\n")

    def test_busy_raises_connection_error(self):
        _, socket_path = self.start_daemon(
            CannedSerial(["***(N0CALL) busy\r\n"])
        )
        client = self.connect(socket_path)

        response = client.call(
            "connect_station",
            callsign="N0CALL",
            timeout=1
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "KAMConnectionError")


class ConnectedModePassthroughTests(DaemonTestCase):
    def test_send_and_read_connected(self):
        daemon, socket_path = self.start_daemon(
            CannedSerial(["hello back!\r\n"])
        )
        client = self.connect(socket_path)
        daemon._aprs_session_uncertain = False

        send_response = client.call("send_connected", text="hi there")
        self.assertTrue(daemon._aprs_session_uncertain)
        self.assertEqual(daemon._aprs_safety_state, "unsafe")
        self.assertTrue(send_response["ok"])
        self.assertIsNone(send_response["result"])

        read_response = client.call("read_connected", timeout=0.3)
        self.assertTrue(read_response["ok"])
        self.assertEqual(read_response["result"], "hello back!\r\n")

    def test_disconnect_station(self):
        daemon, socket_path = self.start_daemon(
            CannedSerial(["cmd:", "cmd:*** DISCONNECTED\r\n"])
        )
        client = self.connect(socket_path)
        daemon._aprs_session_uncertain = False

        response = client.call("disconnect_station", timeout=1)
        self.assertTrue(daemon._aprs_session_uncertain)
        self.assertEqual(daemon._aprs_safety_state, "unsafe")

        self.assertTrue(response["ok"])
        self.assertIn("DISCONNECTED", response["result"])


class PBBSTests(DaemonTestCase):
    """
    KAMXL.list_pbbs_messages()/read_pbbs_message() are already
    covered directly (call order, argument flow, error handling) in
    tests/test_pbbs.py using stubbed primitives -- chaining a real
    connect+command+disconnect exchange through one shared
    CannedSerial queue doesn't work cleanly (read_connected()'s
    "collect for N seconds" semantics drains every queued chunk in a
    single call, regardless of which logical step they were meant
    for). So here, daemon.kam.list_pbbs_messages/read_pbbs_message
    are stubbed directly too -- these tests are about what the daemon
    layer itself adds on top: lock acquisition, params passed
    through, and Packet-style dataclass -> JSON-safe dict conversion.
    """

    def test_list_messages_returns_json_safe_dicts(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        daemon.kam.list_pbbs_messages = lambda **kwargs: [
            PBBSMessageSummary(
                number=6, msg_type="B", status=None, size=45,
                to="KEPS", from_call="W3IWI", date="10/19/01 09:37:11",
                pages=2, subject="Line Element set",
            )
        ]

        client = self.connect(socket_path)
        response = client.call("pbbs.list_messages")

        self.assertTrue(response["ok"])
        self.assertEqual(len(response["result"]), 1)
        self.assertEqual(
            response["result"][0]["subject"], "Line Element set"
        )
        self.assertEqual(response["result"][0]["from_call"], "W3IWI")

    def test_list_messages_passes_params_through(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        captured = {}

        def fake_list(**kwargs):
            captured.update(kwargs)
            return []

        daemon.kam.list_pbbs_messages = fake_list

        client = self.connect(socket_path)
        client.call(
            "pbbs.list_messages",
            mypbbs="N5BA-2",
            connect_timeout=10,
            read_timeout=3
        )

        self.assertEqual(captured["mypbbs"], "N5BA-2")
        self.assertEqual(captured["connect_timeout"], 10)
        self.assertEqual(captured["read_timeout"], 3)

    def test_read_message_returns_dict(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        daemon.kam.read_pbbs_message = lambda number, **kwargs: PBBSMessage(
            number=number, date="02/10/92 10:30:58", from_call="KB0NYK",
            to="HELP", routing=None, body="hi",
        )

        client = self.connect(socket_path)
        response = client.call("pbbs.read_message", number=2)

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["from_call"], "KB0NYK")
        self.assertEqual(response["result"]["number"], 2)

    def test_read_message_not_found_returns_none(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        daemon.kam.read_pbbs_message = lambda number, **kwargs: None

        client = self.connect(socket_path)
        response = client.call("pbbs.read_message", number=999)

        self.assertTrue(response["ok"])
        self.assertIsNone(response["result"])

    def test_read_message_missing_number_is_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("pbbs.read_message")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")


class WinlinkTests(DaemonTestCase):
    """
    Milestone 8. Same reasoning as PBBSTests above:
    daemon.kam.check_winlink_mail is stubbed directly rather than
    driven through a real connect/handshake/proposal exchange (already
    covered in tests/test_winlink.py's KAMXLWinlinkIntegrationTests) --
    these are about what the daemon layer itself adds: lock
    acquisition, params passed through, dataclass -> JSON-safe dict
    conversion (including the nested Proposal dataclass).
    """

    def test_check_mail_returns_json_safe_dicts(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        proposal = Proposal(
            msg_type="P", sender="N0CALL", via="N5BA-10",
            recipient="N5BA-10", mid="12345_N0CALL", size=42,
            raw="FB P N0CALL N5BA-10 N5BA-10 12345_N0CALL 42",
        )

        daemon.kam.check_winlink_mail = lambda **kwargs: [
            WinlinkMessage(
                title="Test Subject", body="Hello there.",
                proposal=proposal, raw="Test Subject\r\nHello there.",
            )
        ]

        client = self.connect(socket_path)
        response = client.call(
            "winlink.check_mail", gateway="N5BA-10", password="FOOBAR"
        )

        self.assertTrue(response["ok"])
        self.assertEqual(len(response["result"]), 1)
        self.assertEqual(response["result"][0]["title"], "Test Subject")
        self.assertEqual(
            response["result"][0]["proposal"]["sender"], "N0CALL"
        )

    def test_check_mail_passes_params_through(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        captured = {}

        def fake_check(**kwargs):
            captured.update(kwargs)
            return []

        daemon.kam.check_winlink_mail = fake_check

        client = self.connect(socket_path)
        client.call(
            "winlink.check_mail",
            gateway="N5BA-10",
            password="FOOBAR",
            mycall="N5BA-2",
            connect_timeout=90,
            read_timeout=15,
        )

        self.assertEqual(captured["gateway"], "N5BA-10")
        self.assertEqual(captured["password"], "FOOBAR")
        self.assertEqual(captured["mycall"], "N5BA-2")
        self.assertEqual(captured["connect_timeout"], 90)
        self.assertEqual(captured["read_timeout"], 15)

    def test_missing_gateway_is_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.check_mail", password="FOOBAR")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")

    def test_missing_password_is_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.check_mail", gateway="N5BA-10")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")


class WinlinkSendMessageTests(DaemonTestCase):
    """
    Milestone 8's send-support extension. Same reasoning as
    WinlinkTests above: daemon.kam.send_winlink_message is stubbed
    directly (the real connect/handshake/proposal exchange is covered
    in tests/test_winlink.py's
    KAMXLSendWinlinkMessageIntegrationTests) -- these are about what
    the daemon layer itself adds: lock acquisition, JSON dict ->
    OutgoingMessage conversion, params passed through.
    """

    def test_send_message_returns_accepted_mids(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        daemon.kam.send_winlink_message = lambda **kwargs: ["12345_N5BA"]

        client = self.connect(socket_path)
        response = client.call(
            "winlink.send_message",
            gateway="N5BA-10",
            password="FOOBAR",
            messages=[{"to": ["N0CALL"], "subject": "Hi", "body": "Hello"}],
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], ["12345_N5BA"])

    def test_send_message_converts_dicts_to_outgoing_messages(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        captured = {}

        def fake_send(**kwargs):
            captured.update(kwargs)
            return []

        daemon.kam.send_winlink_message = fake_send

        client = self.connect(socket_path)
        client.call(
            "winlink.send_message",
            gateway="N5BA-10",
            password="FOOBAR",
            mycall="N5BA-2",
            connect_timeout=90,
            read_timeout=15,
            messages=[
                {
                    "to": ["N0CALL"], "cc": ["N1CALL"],
                    "subject": "Hi", "body": "Hello",
                },
            ],
        )

        self.assertEqual(captured["gateway"], "N5BA-10")
        self.assertEqual(captured["password"], "FOOBAR")
        self.assertEqual(captured["mycall"], "N5BA-2")
        self.assertEqual(captured["connect_timeout"], 90)
        self.assertEqual(captured["read_timeout"], 15)

        messages = captured["messages"]
        self.assertEqual(len(messages), 1)
        self.assertIsInstance(messages[0], OutgoingMessage)
        self.assertEqual(messages[0].to, ["N0CALL"])
        self.assertEqual(messages[0].cc, ["N1CALL"])
        self.assertEqual(messages[0].subject, "Hi")
        self.assertEqual(messages[0].body, "Hello")

    def test_missing_gateway_is_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call(
            "winlink.send_message",
            password="FOOBAR",
            messages=[{"to": ["N0CALL"], "subject": "Hi", "body": "Hello"}],
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")

    def test_missing_messages_is_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call(
            "winlink.send_message", gateway="N5BA-10", password="FOOBAR"
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")


class WinlinkApiTests(DaemonTestCase):
    """
    The Winlink HTTP web-service API wiring (account lookups, gateway
    listings -- winlink_api.py, unrelated to the B2F/RF winlink.py
    tests above). These never touch daemon.kam at all -- kamxl_daemon.py
    calls winlink_api.account_exists()/get_gateway_status() directly,
    with no serial port involved -- so the real network calls are
    swapped out here for direct module-level fakes (kamxl_daemon.py
    does `import winlink_api` rather than `from winlink_api import
    ...`, specifically so reassigning an attribute on the module
    object here is visible to it), the same "swap the real thing for
    a fake at the seam" discipline as daemon.kam.send_winlink_message
    above, just one level further out.
    """

    def setUp(self):
        self._old_key = os.environ.get("WINLINK_API_KEY")
        os.environ["WINLINK_API_KEY"] = "TEST_KEY"
        self.addCleanup(self._restore_key)

    def _restore_key(self):
        if self._old_key is None:
            os.environ.pop("WINLINK_API_KEY", None)
        else:
            os.environ["WINLINK_API_KEY"] = self._old_key

    def _patch(self, name, fake):
        original = getattr(winlink_api, name)
        setattr(winlink_api, name, fake)
        self.addCleanup(setattr, winlink_api, name, original)

    def test_account_exists_passes_callsign_and_key(self):
        calls = []

        self._patch(
            "account_exists",
            lambda callsign, api_key, **kwargs: calls.append(
                (callsign, api_key)
            ) or True
        )

        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.account_exists", callsign="N5BA")

        self.assertTrue(response["ok"])
        self.assertTrue(response["result"])
        self.assertEqual(calls, [("N5BA", "TEST_KEY")])

    def test_account_exists_missing_callsign_is_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.account_exists")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")

    def test_missing_api_key_raises_clear_error(self):
        os.environ.pop("WINLINK_API_KEY", None)

        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.account_exists", callsign="N5BA")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "KAMError")
        self.assertIn("WINLINK_API_KEY", response["error"]["message"])

    def test_gateway_status_returns_dicts(self):
        gateway = winlink_api.Gateway(
            callsign="KD5EOC-10", base_callsign="KD5EOC",
            requested_mode="Packet", comments="", last_status="",
            latitude=33.2, longitude=-97.1,
            channels=[
                winlink_api.GatewayChannel(
                    operating_hours="24/7", supported_modes="PACKET",
                    frequency=145.09, service_code="PUBLIC", baud="1200",
                    radio_range="25", mode=1, gridsquare="EM13ov",
                    antenna="Vertical",
                ),
            ],
        )
        self._patch(
            "get_gateway_status", lambda api_key, **kwargs: [gateway]
        )

        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.gateway_status")

        self.assertTrue(response["ok"])
        self.assertEqual(len(response["result"]), 1)
        self.assertEqual(response["result"][0]["callsign"], "KD5EOC-10")
        self.assertEqual(len(response["result"][0]["channels"]), 1)
        self.assertEqual(
            response["result"][0]["channels"][0]["gridsquare"], "EM13ov"
        )

    def test_nearby_gateways_returns_distance_km(self):
        near = winlink_api.Gateway(
            callsign="NEAR-10", base_callsign="NEAR",
            requested_mode="Packet", comments="", last_status="",
            latitude=33.05, longitude=-97.05, channels=[],
        )
        self._patch(
            "get_gateway_status", lambda api_key, **kwargs: [near]
        )

        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call(
            "winlink.nearby_gateways", latitude=33.0, longitude=-97.0
        )

        self.assertTrue(response["ok"])
        self.assertEqual(len(response["result"]), 1)
        self.assertEqual(response["result"][0]["gateway"]["callsign"], "NEAR-10")
        self.assertGreater(response["result"][0]["distance_km"], 0)

    def test_nearby_gateways_missing_latitude_is_missing_param(self):
        self._patch("get_gateway_status", lambda api_key, **kwargs: [])

        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.nearby_gateways", longitude=-97.0)

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")


class LoadDotenvTests(unittest.TestCase):
    """
    kamxl_daemon.py's minimal .env loader -- reads WINLINK_API_KEY (or
    anything else) out of a KEY=VALUE file so it doesn't have to be
    exported by hand every time the daemon starts. See _load_dotenv()'s
    own docstring for the full scope (no third-party dotenv dependency,
    no interpolation/line-continuation support).
    """

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._env_path = os.path.join(self._tmp_dir, ".env")

        self._saved_environ = dict(os.environ)
        self.addCleanup(self._restore_environ)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._saved_environ)

    def _write_env(self, content):
        with open(self._env_path, "w") as f:
            f.write(content)

    def test_missing_file_is_a_no_op(self):
        # Must not raise -- a .env file is a convenience, not a
        # requirement, and the daemon has to keep working without one.
        _load_dotenv(os.path.join(self._tmp_dir, "does-not-exist.env"))

    def test_sets_previously_unset_variable(self):
        os.environ.pop("WINLINK_API_KEY", None)
        self._write_env("WINLINK_API_KEY=ABC123\n")

        _load_dotenv(self._env_path)

        self.assertEqual(os.environ["WINLINK_API_KEY"], "ABC123")

    def test_real_environment_variable_takes_precedence(self):
        # Standard dotenv semantics -- the file only fills in gaps, it
        # never overrides something already exported for real.
        os.environ["WINLINK_API_KEY"] = "REAL_VALUE"
        self._write_env("WINLINK_API_KEY=FROM_DOTENV\n")

        _load_dotenv(self._env_path)

        self.assertEqual(os.environ["WINLINK_API_KEY"], "REAL_VALUE")

    def test_blank_lines_and_comments_skipped(self):
        self._write_env("\n# a comment\nFOO_TEST_VAR=bar\n")

        _load_dotenv(self._env_path)

        self.assertEqual(os.environ["FOO_TEST_VAR"], "bar")

    def test_quoted_value_is_unquoted(self):
        self._write_env('FOO_TEST_VAR="bar baz"\n')

        _load_dotenv(self._env_path)

        self.assertEqual(os.environ["FOO_TEST_VAR"], "bar baz")

    def test_line_without_equals_sign_skipped(self):
        self._write_env("not a valid line\nFOO_TEST_VAR=bar\n")

        _load_dotenv(self._env_path)

        self.assertEqual(os.environ["FOO_TEST_VAR"], "bar")


class MonitorSubscribeTests(DaemonTestCase):
    # A packet is only known to be "complete" once the *next* header
    # line arrives (see packet.py's PacketParser docstring), so this
    # includes a third header purely to complete the second packet --
    # otherwise it would just sit pending, waiting for the monitor
    # loop to stop and flush it (see test_shutdown_stops_monitor_thread
    # below for that path instead).
    CHUNKS = [
        "KD5EOC-10>BEACON/2:\r\n",
        "Winlink 2000 RMS Packet Server\r\n",
        "N5BA-4>BEACON/2:\r\n",
        "N5BA-4 Linux Node http://digipi.org/\r\n",
        "N0CALL>BEACON/2:\r\n",
    ]

    def test_subscribe_receives_packet_events(self):
        _, socket_path = self.start_daemon(ChunkSerial(self.CHUNKS))
        client = self.connect(socket_path)

        subscribe_response = client.call("monitor.subscribe")
        self.assertTrue(subscribe_response["ok"])

        first = client.read_event(timeout=3)
        self.assertEqual(first["event"], "packet")
        self.assertEqual(first["data"]["source"], "KD5EOC-10")
        self.assertEqual(first["data"]["destination"], "BEACON")
        self.assertEqual(first["data"]["port"], 2)

        second = client.read_event(timeout=3)
        self.assertEqual(second["data"]["source"], "N5BA-4")
        self.assertEqual(
            second["data"]["payload"],
            "N5BA-4 Linux Node http://digipi.org/"
        )

    def test_status_reflects_subscriber_count(self):
        _, socket_path = self.start_daemon(ChunkSerial([]))
        client = self.connect(socket_path)

        before = client.call("status")
        self.assertEqual(before["result"]["monitor_subscribers"], 0)

        client.call("monitor.subscribe")

        after = client.call("status")
        self.assertEqual(after["result"]["monitor_subscribers"], 1)

        client.call("monitor.unsubscribe")

        final = client.call("status")
        self.assertEqual(final["result"]["monitor_subscribers"], 0)

    def test_monitor_thread_starts_with_no_subscribers(self):
        # Milestone 7: the monitor thread is now always-on from the
        # moment a KAMDaemon is constructed, not started on-demand by
        # the first monitor.subscribe() -- needed so the station
        # database (stations.py) builds passively even if nobody ever
        # opens the map page or a live monitor pane. Confirm it's
        # already running with zero subscribers, rather than only
        # after the first one connects.
        daemon, _ = self.start_daemon(ChunkSerial([]))

        self.assertIsNotNone(daemon._monitor_thread)
        self.assertTrue(daemon._monitor_thread.is_alive())

        with daemon._subscribers_lock:
            self.assertEqual(len(daemon._subscribers), 0)

    def test_monitor_thread_outlives_last_subscriber(self):
        # The milestone 5/6 behavior stopped the monitor thread once
        # its last subscriber left (a background thread outliving
        # every subscriber was a real resource leak back then, since
        # broadcasting packets was the thread's only job). Milestone 7
        # deliberately changed that: the thread now also feeds the
        # always-on station tracker, so it has a reason to keep
        # running with zero subscribers -- confirm it does.
        daemon, socket_path = self.start_daemon(ChunkSerial([
            "N0CALL>BEACON/2:\r\n",
            "still going after the last subscriber leaves\r\n",
        ]))
        client = self.connect(socket_path)

        client.call("monitor.subscribe")
        time.sleep(0.3)  # let the monitor loop pick up the chunks

        client.close()
        time.sleep(0.3)  # give an (incorrect) stop a moment to happen

        self.assertTrue(daemon._monitor_thread.is_alive())

        with daemon._subscribers_lock:
            self.assertEqual(len(daemon._subscribers), 0)

    def test_shutdown_stops_monitor_thread(self):
        # The always-on thread still needs to stop *somewhere* --
        # confirm daemon.shutdown() does it (and flushes/discards
        # whatever was still pending, rather than raising), now that
        # "last subscriber leaves" no longer does.
        daemon, socket_path = self.start_daemon(ChunkSerial([
            "N0CALL>BEACON/2:\r\n",
            "final packet, never followed by another header\r\n",
        ]))
        self.connect(socket_path)

        deadline = time.monotonic() + 2
        while (
            not daemon._monitor_thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

        self.assertTrue(daemon._monitor_thread.is_alive())

        daemon.shutdown()

        deadline = time.monotonic() + 2
        while (
            daemon._monitor_thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

        self.assertFalse(daemon._monitor_thread.is_alive())

    def test_monitor_failure_invalidates_aprs_session(self):
        daemon, socket_path = self.start_daemon(ChunkSerial([]))
        self.connect(socket_path)

        # Simulate a previously verified APRS session.
        with daemon._kam_lock:
            daemon._aprs_session_uncertain = False
            daemon._aprs_safety_state = "unknown"

        # Arrange for the next monitor read to fail.
        def fail_read():
            raise RuntimeError("Simulated monitor failure")

        with patch.object(daemon.kam, "read_available", side_effect=fail_read):
            with patch("kamxl_daemon.os._exit") as mock_exit:
                deadline = time.monotonic() + 2

                while (
                    not mock_exit.called
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)

                self.assertTrue(
                    mock_exit.called,
                    "Monitor failure did not trigger process exit",
                )
                mock_exit.assert_called_once_with(1)

                with daemon._kam_lock:
                    self.assertTrue(daemon._aprs_session_uncertain)
                    self.assertEqual(
                        daemon._aprs_safety_state,
                        "unsafe",
                    )

class StationsTests(DaemonTestCase):
    """
    Milestone 7: the always-on monitor thread (see
    MonitorSubscribeTests above) decodes APRS position reports out of
    ordinary MONITOR traffic into stations.py's StationTracker --
    with no monitor.subscribe() call required, unlike packet events.
    """

    def _wait_for_stations(self, client, timeout=2):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            response = client.call("stations.list")
            stations = response["result"]

            if stations:
                return stations

            time.sleep(0.05)

        return []

    def test_stations_list_reflects_decoded_position(self):
        _, socket_path = self.start_daemon(ChunkSerial([
            "N5BA-9>APRS/1:\r\n",
            "!4903.50N/07201.75W-Test comment\r\n",
            "N0CALL>BEACON/1:\r\n",  # completes the N5BA-9 packet
        ]))
        client = self.connect(socket_path)

        stations = self._wait_for_stations(client)

        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0]["callsign"], "N5BA-9")
        self.assertAlmostEqual(
            stations[0]["latitude"], 49 + 3.50 / 60, places=6
        )
        self.assertEqual(stations[0]["comment"], "Test comment")

    def test_stations_get_known_callsign(self):
        _, socket_path = self.start_daemon(ChunkSerial([
            "N5BA-9>APRS/1:\r\n",
            "!4903.50N/07201.75W-Test comment\r\n",
            "N0CALL>BEACON/1:\r\n",
        ]))
        client = self.connect(socket_path)

        self._wait_for_stations(client)

        response = client.call("stations.get", callsign="N5BA-9")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["callsign"], "N5BA-9")

    def test_stations_get_unknown_callsign_returns_none(self):
        _, socket_path = self.start_daemon(ChunkSerial([]))
        client = self.connect(socket_path)

        response = client.call("stations.get", callsign="NOBODY")

        self.assertTrue(response["ok"])
        self.assertIsNone(response["result"])

    def test_stations_list_empty_when_no_position_traffic(self):
        _, socket_path = self.start_daemon(ChunkSerial([
            "N0CALL>BEACON/1:\r\n",
            "just chatter, no position report\r\n",
        ]))
        client = self.connect(socket_path)

        time.sleep(0.3)  # let the monitor loop pick it up either way

        response = client.call("stations.list")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], [])

class ReceptionHistoryIntegrationTests(DaemonTestCase):
    """Verify daemon packet reception persists to SQLite."""

    def test_packet_survives_daemon_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            database_path = str(Path(tmp) / "reception.sqlite3")

            # First daemon instance.
            kam1 = make_kam(ScriptedSerial({}))
            daemon1 = KAMDaemon(
                kam1,
                str(Path(tmp) / "daemon1.sock"),
                history_db=database_path,
                station_callsign="N5BA",
            )

            try:
                packet = Packet(
                    source="N5BA",
                    destination="APRS",
                    digipeaters=("WIDE1-1", "W5NGU-3*"),
                    port=2,
                    payload="SQLite integration test",
                    raw="N5BA>APRS,WIDE1-1,W5NGU-3*/2: <UI>:",
                    frame_type="UI",
                )

                daemon1._handle_packet(packet)
            finally:
                daemon1.shutdown()

            # Reopen the database after the daemon shuts down.
            history = ReceptionHistory(database_path)

            try:
                self.assertEqual(history.total_packets(), 1)
            finally:
                history.close()

            # Second daemon instance, using the same database.
            kam2 = make_kam(ScriptedSerial({}))
            daemon2 = KAMDaemon(
                kam2,
                str(Path(tmp) / "daemon2.sock"),
                history_db=database_path,
                station_callsign="N5BA",
            )

            try:
                daemon2._handle_packet(packet)
            finally:
                daemon2.shutdown()

            history = ReceptionHistory(database_path)

            try:
                self.assertEqual(history.total_packets(), 2)
            finally:
                history.close()

    def test_history_disabled(self):
        daemon = KAMDaemon(
            make_kam(ScriptedSerial({})),
            "/tmp/kamxl-history-disabled-test.sock",
            station_callsign="N5BA",
        )

        try:
            result = daemon._m_aprs_history({})
            self.assertFalse(result["enabled"])
            self.assertEqual(result["total_packets"], 0)
            self.assertEqual(result["hourly_counts"], [])
        finally:
            daemon.shutdown()

    def test_history_rpc_returns_persisted_packets(self):
        with tempfile.TemporaryDirectory() as tmp:
            database_path = str(Path(tmp) / "history.sqlite3")

            daemon = KAMDaemon(
                make_kam(ScriptedSerial({})),
                str(Path(tmp) / "daemon.sock"),
                history_db=database_path,
                station_callsign="N5BA",
            )

            packet = Packet(
                source="N5BA",
                destination="APRS",
                digipeaters=("W5NGU-3*",),
                port=2,
                payload="Test",
                raw="Test",
                frame_type="UI",
            )

            try:
                daemon._handle_packet(packet)

                # Stop the writer to guarantee the queued record
                # has been committed before querying history.
                daemon._reception_writer.stop()

                result = daemon._m_aprs_history({})

                self.assertTrue(result["enabled"])
                self.assertEqual(result["scope"], "persistent")
                self.assertEqual(result["total_packets"], 1)
                self.assertEqual(
                    sum(
                        entry["packet_count"]
                        for entry in result["hourly_counts"]
                    ),
                    1,
                )
            finally:
                daemon.shutdown()

    def test_history_rpc_rejects_invalid_since(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = KAMDaemon(
                make_kam(ScriptedSerial({})),
                str(Path(tmp) / "daemon.sock"),
                history_db=str(Path(tmp) / "history.sqlite3"),
                station_callsign="N5BA",
            )

            try:
                for invalid in ("yesterday", 1.5, True):
                    with self.subTest(invalid=invalid):
                        with self.assertRaises(ValueError):
                            daemon._m_aprs_history({
                                "since": invalid,
                            })
            finally:
                daemon.shutdown()

if __name__ == "__main__":
    unittest.main()
