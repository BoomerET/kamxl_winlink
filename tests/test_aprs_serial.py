"""Offline tests for KAM-XL APRS conversational-mode behavior."""

import unittest

from tests.fakes import make_kam
from kamxl import KAMTimeoutError
from aprs_tx import (
    APRSTransmitError,
    transmit_aprs_message,
    execute_aprs_tx_transaction,
    validate_aprs_paclen,
    validate_aprs_cpactime,
    validate_aprs_sendpac,
    validate_aprs_command_character,
    validate_aprs_recovery_status,
    recover_aprs_command_mode,
    check_aprs_tx_preflight,
)

class APRSSerialFake:
    """
    Simulate the command/conversational mode transition.

    This fake never accesses a real serial port.
    """

    def __init__(self):
        self.is_open = True
        self.mode = "command"
        self.written = []
        self._out = bytearray()

    def close(self):
        self.is_open = False

    def write(self, data):
        self.written.append(data)

        if self.mode == "command":
            if data == b"CONVERS\r":
                self.mode = "convers"
            else:
                self._out.extend(b"cmd:")

        elif self.mode == "convers":
            if data == b"\x03":
                self.mode = "command"
                self._out.extend(b"cmd:")

        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self._out.clear()

    @property
    def in_waiting(self):
        return len(self._out)

    def read(self, n):
        if not self._out:
            return b""

        count = max(1, min(n, len(self._out)))
        data = bytes(self._out[:count])
        del self._out[:count]
        return data

class KISSExitTests(unittest.TestCase):
    def test_exit_kiss_sends_exact_sequence(self):
        serial = APRSSerialFake()
        kam = make_kam(serial)

        banner = b"\r\nKANTRONICS KAM XL\r\ncmd:"

        original_write = serial.write

        def fake_write(data):
            if data == bytes([0xC0, 0xFF, 0xC0]):
                serial.written.append(data)
                serial._out.extend(banner)
                return len(data)

            return original_write(data)

        serial.write = fake_write

        response = kam.exit_kiss_mode(timeout=0.01)

        self.assertEqual(
            serial.written,
            [bytes([0xC0, 0xFF, 0xC0])],
        )
        self.assertEqual(response, banner)

class APRSTransactionFake(APRSSerialFake):
    """
    Simulate KAM-XL configuration and conversational mode.

    All writes remain entirely in memory.
    """

    def __init__(self):
        super().__init__()
        self._aprs_offline_simulation = True
        self.unproto = "CQ/CQ"
        self.fail_command = None

    def write(self, data):
        if self.mode == "convers":
            return super().write(data)

        self.written.append(data)
        command = data.decode("ascii").strip("\r\n")

        if command == self.fail_command:
            self._out.extend(b"EH?\r\ncmd:")
            return len(data)

        if command == "UNPROTO":
            self._out.extend(
                f"UNPROTO  {self.unproto}\r\ncmd:".encode("ascii")
            )

        elif command.startswith("UNPROTO "):
            value = command[len("UNPROTO "):]
            self.unproto = f"{value}/{value}" if "/" not in value else value
            self._out.extend(b"cmd:")

        elif command == "STATUS":
            self._out.extend(
                b"FREE BYTES 2685\r\n"
                b"A/2  stream - IO DISCONNECTED\r\ncmd:"
            )

        elif command == "PORT":
            self._out.extend(b"PORT 2\r\ncmd:")

        elif command == "PACLEN":
            self._out.extend(b"PACLEN 64/128\r\ncmd:")

        elif command == "CPACTIME":
            self._out.extend(b"CPACTIME OFF\r\ncmd:")

        elif command == "SENDPAC":
            self._out.extend(b"SENDPAC $0D (CTRL-M)\r\ncmd:")

        elif command == "COMMAND":
            self._out.extend(b"COMMAND $03 (CTRL-C)\r\ncmd:")

        elif command == "CONVERS":
            self.mode = "convers"

        else:
            self._out.extend(b"cmd:")

        return len(data)

class APRSSerialTests(unittest.TestCase):

    def test_convers_does_not_return_command_prompt(self):
        serial = APRSSerialFake()
        kam = make_kam(serial)

        serial.write(b"CONVERS\r")

        self.assertEqual(serial.mode, "convers")
        self.assertEqual(serial.in_waiting, 0)

    def test_ctrl_c_returns_to_command_mode(self):
        serial = APRSSerialFake()
        kam = make_kam(serial)

        serial.write(b"CONVERS\r")
        kam.enter_command_mode(timeout=0.1)

        self.assertEqual(serial.mode, "command")
        self.assertIn(b"\x03", serial.written)

    def test_payload_is_sent_once(self):
        serial = APRSSerialFake()
        kam = make_kam(serial)

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"

        serial.write(b"CONVERS\r")
        kam.send_connected(payload)

        self.assertEqual(
            serial.written.count(payload.encode("ascii") + b"\r"),
            1,
        )

        self.assertEqual(serial.mode, "convers")

    def test_dry_run_performs_no_serial_writes(self):
        serial = APRSSerialFake()
        kam = make_kam(serial)

        result = transmit_aprs_message(
            kam,
            ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX",
            "CQ/CQ",
        )

        self.assertTrue(result["dry_run"])
        self.assertFalse(result["transmitted"])
        self.assertEqual(result["serial_writes"], 0)
        self.assertEqual(serial.written, [])

    def test_live_transmission_is_disabled(self):
        serial = APRSSerialFake()
        kam = make_kam(serial)

        with self.assertRaises(APRSTransmitError):
            transmit_aprs_message(
                kam,
                ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX",
                "CQ/CQ",
                dry_run=False,
            )

        self.assertEqual(serial.written, [])

    def test_dry_run_includes_restore_command(self):
        serial = APRSSerialFake()
        kam = make_kam(serial)

        result = transmit_aprs_message(
            kam,
            ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX",
            "CQ/CQ",
            path="WIDE1-1,WIDE2-1",
        )

        self.assertEqual(
            result["commands"][-1],
            "UNPROTO CQ/CQ",
        )

    def test_transaction_fake_reads_unproto(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        self.assertEqual(kam.get("UNPROTO"), "CQ/CQ")

    def test_transaction_fake_changes_unproto(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        kam.set("UNPROTO", "APRS")

        self.assertEqual(serial.unproto, "APRS/APRS")
        self.assertEqual(kam.get("UNPROTO"), "APRS/APRS")

    def test_transaction_fake_reports_active_stream(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        response = kam.send_command("STATUS")

        self.assertIn("A/2  stream - IO DISCONNECTED", response)

    def test_transaction_fake_rejects_command(self):
        serial = APRSTransactionFake()
        serial.fail_command = "UNPROTO APRS"
        kam = make_kam(serial)

        with self.assertRaises(Exception):
            kam.set("UNPROTO", "APRS")

        self.assertEqual(serial.unproto, "CQ/CQ")

    def test_transaction_stops_after_initial_unproto_timeout(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        original_get = kam.get

        def failed_initial_read(command):
            if command == "UNPROTO":
                raise KAMTimeoutError("Simulated initial read timeout")
            return original_get(command)

        kam.get = failed_initial_read

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"

        with self.assertRaises((KAMTimeoutError, APRSTransmitError)):
            execute_aprs_tx_transaction(kam, payload)

        self.assertEqual(serial.unproto, "CQ/CQ")
        self.assertEqual(serial.mode, "command")
        self.assertNotIn(b"UNPROTO APRS\r", serial.written)
        self.assertNotIn(b"UNPROTO CQ/CQ\r", serial.written)
        self.assertNotIn(b"CONVERS\r", serial.written)
        self.assertNotIn(
            payload.encode("ascii") + b"\r",
            serial.written,
        )

    def test_transaction_rejects_wrong_port_before_changes(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        original_write = serial.write

        def wrong_port(data):
            if data == b"PORT\r":
                serial.written.append(data)
                serial._out.extend(b"PORT 1\r\ncmd:")
                return len(data)

            return original_write(data)

        serial.write = wrong_port

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"

        with self.assertRaises(APRSTransmitError):
            execute_aprs_tx_transaction(kam, payload)

        self.assertEqual(serial.unproto, "CQ/CQ")
        self.assertEqual(serial.mode, "command")
        self.assertEqual(
            serial.written,
            [b"STATUS\r", b"PORT\r"],
        )
        self.assertNotIn(
            payload.encode("ascii") + b"\r",
            serial.written,
        )

    def test_transaction_rejects_short_convers_write(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"
        payload_bytes = payload.encode("ascii") + b"\r"

        original_write = serial.write
        convers_write_count = 0

        def short_convers_write(data):
            nonlocal convers_write_count

            if data == b"CONVERS\r":
                convers_write_count += 1
                return len(data) - 1

            return original_write(data)

        serial.write = short_convers_write

        with self.assertRaises(APRSTransmitError):
            execute_aprs_tx_transaction(kam, payload)

        self.assertEqual(convers_write_count, 1)
        self.assertNotIn(payload_bytes, serial.written)

        # Recovery must succeed before restoring UNPROTO.
        self.assertIn(b"\x03", serial.written)
        self.assertEqual(serial.mode, "command")
        self.assertEqual(serial.unproto, "CQ/CQ")

    def test_transaction_recovers_after_convers_flush_failure(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        original_flush = serial.flush

        def fail_convers_flush():
            if serial.mode == "convers":
                raise OSError("Simulated CONVERS flush failure")
            return original_flush()

        serial.flush = fail_convers_flush

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"

        with self.assertRaises(OSError):
            execute_aprs_tx_transaction(kam, payload)

        # CONVERS was accepted, but the payload was never sent.
        self.assertIn(b"CONVERS\r", serial.written)
        self.assertNotIn(
            payload.encode("ascii") + b"\r",
            serial.written,
        )

        # Command mode must be recovered before restoration.
        self.assertIn(b"\x03", serial.written)
        self.assertEqual(serial.mode, "command")
        self.assertEqual(serial.unproto, "CQ/CQ")

    def test_transaction_reports_uncertain_payload_submission(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"
        payload_bytes = payload.encode("ascii") + b"\r"

        original_write = serial.write

        def fail_after_payload_write(data):
            result = original_write(data)

            if data == payload_bytes:
                raise OSError("Simulated error after payload write")

            return result

        serial.write = fail_after_payload_write

        with self.assertRaisesRegex(
            APRSTransmitError,
            "submission is uncertain",
        ):
            execute_aprs_tx_transaction(kam, payload)

        # The complete payload may have reached the TNC.
        # It must never be submitted a second time.
        self.assertEqual(serial.written.count(payload_bytes), 1)

        # Recover command mode and restore configuration.
        self.assertIn(b"\x03", serial.written)
        self.assertEqual(serial.mode, "command")
        self.assertEqual(serial.unproto, "CQ/CQ")

    def test_transaction_reports_uncertain_short_payload_write(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"
        payload_bytes = payload.encode("ascii") + b"\r"

        original_write = serial.write
        payload_write_count = 0

        def short_payload_write(data):
            nonlocal payload_write_count

            if data == payload_bytes:
                payload_write_count += 1
                return len(data) - 1

            return original_write(data)

        serial.write = short_payload_write

        with self.assertRaisesRegex(
            APRSTransmitError,
            "submission is uncertain",
        ):
            execute_aprs_tx_transaction(kam, payload)

        self.assertEqual(payload_write_count, 1)
        self.assertEqual(serial.mode, "command")
        self.assertEqual(serial.unproto, "CQ/CQ")

    def test_transaction_restoration_failure_warns_payload_submitted(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"
        payload_bytes = payload.encode("ascii") + b"\r"

        original_set = kam.set

        def fail_restoration(command, value):
            if command == "UNPROTO" and value == "CQ/CQ":
                raise OSError("Simulated restoration failure")
            return original_set(command, value)

        kam.set = fail_restoration

        with self.assertRaisesRegex(
            APRSTransmitError,
            "do not retry automatically",
        ):
            execute_aprs_tx_transaction(kam, payload)

        self.assertEqual(serial.written.count(payload_bytes), 1)
        self.assertEqual(serial.mode, "command")

        # Restoration failed; the modified configuration remains.
        self.assertEqual(serial.unproto, "APRS/APRS")

    def test_transaction_rejects_connected_status_after_submission(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"
        payload_bytes = payload.encode("ascii") + b"\r"

        original_write = serial.write
        status_count = 0

        def connected_after_submission(data):
            nonlocal status_count

            if data == b"STATUS\r":
                status_count += 1

                if status_count == 2:
                    serial.written.append(data)
                    serial._out.extend(
                        b"FREE BYTES 2685\r\n"
                        b"A/2  stream - IO CONNECTED to N5XYZ\r\n"
                        b"cmd:"
                    )
                    return len(data)

            return original_write(data)

        serial.write = connected_after_submission

        with self.assertRaisesRegex(
            APRSTransmitError,
            "do not retry automatically",
        ):
            execute_aprs_tx_transaction(kam, payload)

        self.assertEqual(status_count, 2)
        self.assertEqual(serial.written.count(payload_bytes), 1)
        self.assertEqual(serial.mode, "command")

        # Recovery STATUS was unsafe; do not restore UNPROTO.
        self.assertEqual(serial.unproto, "APRS/APRS")
        self.assertNotIn(b"UNPROTO CQ/CQ\r", serial.written)

    def test_transaction_submits_once_and_restores(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"

        result = execute_aprs_tx_transaction(kam, payload)

        self.assertTrue(result["submitted_to_tnc"])
        self.assertFalse(result["rf_confirmed"])
        self.assertFalse(result["delivered"])
        self.assertTrue(result["unproto_restored"])

        self.assertEqual(serial.unproto, "CQ/CQ")
        self.assertEqual(serial.mode, "command")

        self.assertEqual(
            serial.written.count(payload.encode("ascii") + b"\r"),
            1,
        )

    def test_transaction_rejects_wrong_stream_without_changes(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        original_write = serial.write

        def wrong_stream(data):
            if data == b"STATUS\r":
                serial.written.append(data)
                serial._out.extend(
                    b"FREE BYTES 2685\r\n"
                    b"A/1  stream - IO DISCONNECTED\r\ncmd:"
                )
                return len(data)

            return original_write(data)

        serial.write = wrong_stream

        with self.assertRaises(APRSTransmitError):
            execute_aprs_tx_transaction(
                kam,
                ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX",
            )

        self.assertEqual(serial.unproto, "CQ/CQ")
        self.assertNotIn(b"CONVERS\r", serial.written)

    def test_transaction_restores_after_rejected_configuration(self):
        serial = APRSTransactionFake()
        serial.fail_command = "UNPROTO APRS"
        kam = make_kam(serial)

        with self.assertRaises(Exception):
            execute_aprs_tx_transaction(
                kam,
                ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX",
            )

        self.assertEqual(serial.unproto, "CQ/CQ")
        self.assertNotIn(b"CONVERS\r", serial.written)

    def test_transaction_stops_if_command_mode_recovery_fails(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        def fail_command_mode(timeout=5):
            raise RuntimeError("Simulated command-mode failure")

        kam.enter_command_mode = fail_command_mode

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"

        with self.assertRaisesRegex(
            APRSTransmitError,
            "do not retry automatically",
        ):
            execute_aprs_tx_transaction(kam, payload)

        # The payload was submitted, but command-mode recovery failed.
        self.assertEqual(
            serial.written.count(payload.encode("ascii") + b"\r"),
            1,
        )

        # Critically, we must not send a configuration command
        # while the TNC may still be in conversational mode.
        self.assertNotIn(
            b"UNPROTO CQ/CQ\r",
            serial.written,
        )

    def test_transaction_stops_after_unproto_oserror(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        original_set = kam.set

        def failed_unproto_write(command, value):
            if command == "UNPROTO" and value == "APRS":
                # The TNC may have accepted the setting.
                serial.unproto = "APRS"
                raise OSError("Simulated serial write failure")

            return original_set(command, value)

        kam.set = failed_unproto_write

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"

        with self.assertRaisesRegex(
            APRSTransmitError,
            "TNC state is uncertain",
        ):
            execute_aprs_tx_transaction(kam, payload)

        self.assertEqual(serial.unproto, "APRS")

        # No automatic restoration when operating mode is uncertain.
        self.assertNotIn(b"UNPROTO CQ/CQ\r", serial.written)

        # Never proceed to conversational mode or submit the payload.
        self.assertNotIn(b"CONVERS\r", serial.written)
        self.assertNotIn(
            payload.encode("ascii") + b"\r",
            serial.written,
        )

    def test_transaction_restores_after_mismatched_unproto_readback(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        original_get = kam.get
        unproto_reads = 0

        def mismatched_readback(command):
            nonlocal unproto_reads

            if command == "UNPROTO":
                unproto_reads += 1

                if unproto_reads == 2:
                    return "APRS VIA WIDE1-1"

            return original_get(command)

        kam.get = mismatched_readback

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"

        with self.assertRaisesRegex(
            APRSTransmitError,
            "UNPROTO change verification failed",
        ):
            execute_aprs_tx_transaction(kam, payload)

        self.assertEqual(serial.unproto, "CQ/CQ")
        self.assertEqual(serial.mode, "command")
        self.assertIn(b"UNPROTO CQ/CQ\r", serial.written)
        self.assertNotIn(b"CONVERS\r", serial.written)
        self.assertNotIn(
            payload.encode("ascii") + b"\r",
            serial.written,
        )

    def test_transaction_stops_after_malformed_unproto_readback(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        original_get = kam.get
        unproto_reads = 0

        def malformed_readback(command):
            nonlocal unproto_reads

            if command == "UNPROTO":
                unproto_reads += 1

                if unproto_reads == 2:
                    return "APRS\r\nPORT 1"

            return original_get(command)

        kam.get = malformed_readback

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"

        with self.assertRaisesRegex(
            APRSTransmitError,
            "TNC state is uncertain",
        ):
            execute_aprs_tx_transaction(kam, payload)

        self.assertEqual(serial.unproto, "APRS/APRS")

        # Do not restore configuration after an untrusted response.
        self.assertNotIn(b"UNPROTO CQ/CQ\r", serial.written)

        # Never enter CONVERS or submit the APRS payload.
        self.assertNotIn(b"CONVERS\r", serial.written)
        self.assertNotIn(
            payload.encode("ascii") + b"\r",
            serial.written,
        )

    def test_transaction_stops_after_unproto_readback_timeout(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        original_get = kam.get
        unproto_reads = 0

        def timeout_on_readback(command):
            nonlocal unproto_reads

            if command == "UNPROTO":
                unproto_reads += 1

                if unproto_reads == 2:
                    raise TimeoutError(
                        "Simulated UNPROTO read-back timeout"
                    )

            return original_get(command)

        kam.get = timeout_on_readback

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"

        with self.assertRaisesRegex(
            APRSTransmitError,
            "TNC state is uncertain",
        ):
            execute_aprs_tx_transaction(kam, payload)

        # The configuration change succeeded before read-back failed.
        self.assertEqual(serial.unproto, "APRS/APRS")

        # Never attempt restoration while command mode is uncertain.
        self.assertNotIn(b"UNPROTO CQ/CQ\r", serial.written)

        # No conversational mode or APRS payload submission.
        self.assertNotIn(b"CONVERS\r", serial.written)
        self.assertNotIn(
            payload.encode("ascii") + b"\r",
            serial.written,
        )

    def test_transaction_stops_after_generic_unproto_timeout(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        original_set = kam.set

        def lost_ack(command, value):
            if command == "UNPROTO" and value == "APRS":
                # Simulate the TNC accepting the configuration
                # change but losing its acknowledgment.
                serial.unproto = "APRS"
                raise TimeoutError("Simulated lost acknowledgment")

            return original_set(command, value)

        kam.set = lost_ack

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"

        with self.assertRaisesRegex(
            APRSTransmitError,
            "TNC state is uncertain",
        ):
            execute_aprs_tx_transaction(kam, payload)

        # The configuration may have changed.
        self.assertEqual(serial.unproto, "APRS")

        # Do not attempt restoration while state is uncertain.
        self.assertNotIn(b"UNPROTO CQ/CQ\r", serial.written)

        # Never submit the APRS payload after a timeout.
        self.assertNotIn(b"CONVERS\r", serial.written)
        self.assertNotIn(
            payload.encode("ascii") + b"\r",
            serial.written,
        )

    def test_transaction_stops_after_unproto_timeout(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        original_set = kam.set

        def timeout_after_change(command, value):
            if command == "UNPROTO" and value == "APRS":
                serial.unproto = "APRS"

                raise KAMTimeoutError(
                    "Simulated lost command response"
                )

            return original_set(command, value)

        kam.set = timeout_after_change

        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"

        with self.assertRaisesRegex(
            APRSTransmitError,
            "TNC state is uncertain",
        ):
            execute_aprs_tx_transaction(kam, payload)

        # The TNC may have accepted the setting.
        self.assertEqual(serial.unproto, "APRS")

        # No automatic restoration was attempted.
        self.assertNotIn(
            b"UNPROTO CQ/CQ\r",
            serial.written,
        )

        # No APRS payload was submitted.
        self.assertNotIn(
            payload.encode("ascii") + b"\r",
            serial.written,
        )

    def test_aprs_preflight_accepts_known_configuration(self):
        validate_aprs_paclen("PACLEN   64/128")
        validate_aprs_cpactime("CPACTIME OFF")
        validate_aprs_sendpac("SENDPAC  $0D (CTRL-M)")
        validate_aprs_command_character("COMMAND  $03 (CTRL-C)")

    def test_aprs_preflight_rejects_wrong_paclen(self):
        with self.assertRaises(APRSTransmitError):
            validate_aprs_paclen("PACLEN 64/64")

    def test_aprs_preflight_rejects_cpactime_enabled(self):
        with self.assertRaises(APRSTransmitError):
            validate_aprs_cpactime("CPACTIME ON")

    def test_aprs_preflight_rejects_wrong_sendpac(self):
        with self.assertRaises(APRSTransmitError):
            validate_aprs_sendpac("SENDPAC $0A (CTRL-J)")

    def test_aprs_preflight_rejects_wrong_command_character(self):
        with self.assertRaises(APRSTransmitError):
            validate_aprs_command_character("COMMAND $04 (CTRL-D)")

    def test_preflight_passes_known_configuration(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        original_write = serial.write

        responses = {
            "PORT": "PORT 2",
            "PACLEN": "PACLEN 64/128",
            "CPACTIME": "CPACTIME OFF",
            "SENDPAC": "SENDPAC $0D (CTRL-M)",
            "COMMAND": "COMMAND $03 (CTRL-C)",
        }

        def preflight_write(data):
            command = data.decode("ascii").strip("\r\n")

            if command in responses:
                serial.written.append(data)
                serial._out.extend(
                    (responses[command] + "\r\ncmd:").encode("ascii")
                )
                return len(data)

            return original_write(data)

        serial.write = preflight_write

        result = check_aprs_tx_preflight(kam)

        self.assertTrue(result["passed"])
        self.assertFalse(result["rf_enabled"])
        self.assertFalse(result["transmitted"])
        self.assertEqual(serial.mode, "command")

        self.assertEqual(
            serial.written,
            [
                b"STATUS\r",
                b"PORT\r",
                b"PACLEN\r",
                b"CPACTIME\r",
                b"SENDPAC\r",
                b"COMMAND\r",
            ],
        )

    def test_preflight_rejects_wrong_port_without_transmitting(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        original_write = serial.write

        def wrong_port_write(data):
            if data == b"PORT\r":
                serial.written.append(data)
                serial._out.extend(b"PORT 1\r\ncmd:")
                return len(data)

            return original_write(data)

        serial.write = wrong_port_write

        with self.assertRaises(APRSTransmitError):
            check_aprs_tx_preflight(kam)

        self.assertEqual(serial.mode, "command")
        self.assertEqual(
            serial.written,
            [b"STATUS\r", b"PORT\r"],
        )
        self.assertNotIn(b"CONVERS\r", serial.written)

    def test_recovery_status_accepts_disconnected_a2(self):
        response = (
            "FREE BYTES 2685\r\n"
            "A/2  stream - IO DISCONNECTED"
        )

        result = validate_aprs_recovery_status(response)

        self.assertTrue(
            result["command_mode_response_valid"]
        )
        self.assertEqual(result["active_stream"], "A/2")
        self.assertFalse(result["ax25_connected"])
        self.assertFalse(result["rf_enabled"])

    def test_recovery_status_rejects_unexpected_line(self):
        response = (
            "FREE BYTES 2685\r\n"
            "A/2  stream - IO DISCONNECTED\r\n"
            "UNEXPECTED DATA"
        )

        with self.assertRaises(APRSTransmitError):
            validate_aprs_recovery_status(response)

    def test_recovery_status_rejects_duplicate_stream(self):
        response = (
            "FREE BYTES 2685\r\n"
            "A/2  stream - IO DISCONNECTED\r\n"
            "A/2  stream - IO DISCONNECTED"
        )

        with self.assertRaises(APRSTransmitError):
            validate_aprs_recovery_status(response)

    def test_recovery_status_rejects_connected_stream(self):
        response = (
            "FREE BYTES 2685\r\n"
            "A/2  stream - IO CONNECTED to N5XYZ"
        )

        with self.assertRaises(APRSTransmitError):
            validate_aprs_recovery_status(response)

    def test_recovery_status_rejects_wrong_stream(self):
        response = (
            "FREE BYTES 2685\r\n"
            "A/1  stream - IO DISCONNECTED"
        )

        with self.assertRaises(APRSTransmitError):
            validate_aprs_recovery_status(response)

    def test_recover_command_mode_from_convers(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        serial.mode = "convers"

        result = recover_aprs_command_mode(kam)

        self.assertTrue(result["recovered"])
        self.assertEqual(result["active_stream"], "A/2")
        self.assertFalse(result["rf_enabled"])
        self.assertFalse(result["transmitted"])

        self.assertEqual(serial.mode, "command")
        self.assertEqual(
            serial.written,
            [b"\x03", b"STATUS\r"],
        )

    def test_recover_command_mode_rejects_connected_status(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        original_write = serial.write

        def connected_status(data):
            if data == b"STATUS\r":
                serial.written.append(data)
                serial._out.extend(
                    b"FREE BYTES 2685\r\n"
                    b"A/2  stream - IO CONNECTED to N5XYZ\r\n"
                    b"cmd:"
                )
                return len(data)

            return original_write(data)

        serial.write = connected_status
        serial.mode = "convers"

        with self.assertRaisesRegex(
            APRSTransmitError,
            "recovery failed",
        ):
            recover_aprs_command_mode(kam)

        self.assertNotIn(b"CONVERS\r", serial.written)

    def test_recover_already_in_command_mode_with_return(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        # Simulate a TNC already in Command mode that
        # does not generate another prompt for Ctrl-C.
        original_write = serial.write

        def silent_ctrl_c(data):
            if data == b"\x03":
                serial.written.append(data)
                return len(data)

            if data == b"\r":
                serial.written.append(data)
                serial._out.extend(b"cmd:")
                return len(data)

            return original_write(data)

        serial.write = silent_ctrl_c

        result = recover_aprs_command_mode(kam)

        self.assertTrue(result["recovered"])
        self.assertEqual(result["active_stream"], "A/2")
        self.assertFalse(result["rf_enabled"])
        self.assertFalse(result["transmitted"])

        self.assertEqual(
            serial.written,
            [b"\x03", b"\r", b"STATUS\r"],
        )

    def test_recover_command_mode_stops_on_timeout(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)

        def fail_recovery(timeout=5):
            raise KAMTimeoutError("Simulated missing prompt")

        kam.enter_command_mode = fail_recovery

        with self.assertRaisesRegex(
            APRSTransmitError,
            "state remains uncertain",
        ):
            recover_aprs_command_mode(kam)

        # No STATUS query is allowed after recovery fails.
        self.assertEqual(serial.written, [])

    def test_transaction_rejects_unmarked_serial_interface(self):
        serial = APRSTransactionFake()
        del serial._aprs_offline_simulation
        kam = make_kam(serial)
        payload = ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX"

        with self.assertRaisesRegex(
            APRSTransmitError,
            "offline simulation",
        ):
            execute_aprs_tx_transaction(kam, payload)

        self.assertEqual(serial.written, [])

if __name__ == "__main__":
    unittest.main()
