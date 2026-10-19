"""
Offline tests for the driver layer (tnc.py) and the OpenTNC driver
(opentnc.py). No hardware: the same scripted fake serial connections
the KAM-XL tests use.

The OpenTNC's output formats used here are TNC-2 convention, not yet
captured from a real unit (see opentnc.py's module docstring). When
opentnc_capture.py records the real thing, replace these fixtures with
it, the way packet.py's HEADER_RE was corrected for the KAM-XL.
"""

import unittest

from fakes import CannedSerial, ChunkSerial, ScriptedSerial

from aprs_tx import APRSTransmitError, check_aprs_tx_preflight, recover_aprs_command_mode
from kamxl import KAMXL, KAMCommandError, KAMConnectionError, KAMError
from tncstation_daemon import KAMDaemon
from opentnc import OpenTNC
from packet import APRS_RADIO_PORT, PacketParser, TNC2_HEADER_RE
from tnc import (
    TerminalTNC,
    TNCCommandError,
    TNCError,
    create_tnc,
    normalize_tnc_model,
    tnc_class,
)


def make_opentnc(serial):
    tnc = OpenTNC("COM_FAKE")
    tnc.serial = serial
    return tnc


_CONFIRM = {
    "confirm_transmit": True,
    "confirm_vhf_port2_radio_ready": True,
    "confirm_hf_port1_disconnected": True,
}


class ModelSelectionTests(unittest.TestCase):
    def test_names_and_aliases(self):
        for name in ("opentnc", "OpenTNC", "open-tnc", "na6d"):
            self.assertEqual(normalize_tnc_model(name), "opentnc")

        for name in ("kamxl", "KAM-XL", "kam", "kantronics"):
            self.assertEqual(normalize_tnc_model(name), "kamxl")

    def test_empty_defaults_to_kamxl(self):
        self.assertEqual(normalize_tnc_model(None), "kamxl")
        self.assertEqual(normalize_tnc_model(""), "kamxl")

    def test_unknown_model_raises(self):
        with self.assertRaises(ValueError):
            normalize_tnc_model("tnc-x")

    def test_create_tnc_builds_without_opening(self):
        tnc = create_tnc("opentnc", "/dev/ttyACM0")
        self.assertIsInstance(tnc, OpenTNC)
        self.assertEqual(tnc.port, "/dev/ttyACM0")
        self.assertFalse(tnc.is_connected)

        kam = create_tnc("kamxl", "COM8", baudrate=9600)
        self.assertIsInstance(kam, KAMXL)
        self.assertEqual(kam.baudrate, 9600)

    def test_both_drivers_share_the_base_class(self):
        self.assertTrue(issubclass(tnc_class("kamxl"), TerminalTNC))
        self.assertTrue(issubclass(tnc_class("opentnc"), TerminalTNC))


class ExceptionCompatibilityTests(unittest.TestCase):
    """The daemon sends type(exc).__name__ as error.type -- keep KAM* names."""

    def test_tnc_names_alias_the_original_classes(self):
        self.assertIs(TNCError, KAMError)
        self.assertIs(TNCCommandError, KAMCommandError)
        self.assertEqual(TNCError.__name__, "KAMError")

    def test_opentnc_errors_caught_as_kamerror(self):
        tnc = make_opentnc(ScriptedSerial({"BOGUS": "EH?"}))

        with self.assertRaises(KAMError):
            tnc.send_command("BOGUS")


class OpenTNCCommandTests(unittest.TestCase):
    def test_eh_rejection_names_the_model(self):
        tnc = make_opentnc(ScriptedSerial({"BOGUS": "EH?"}))

        with self.assertRaises(KAMCommandError) as ctx:
            tnc.send_command("BOGUS")

        self.assertIn("OpenTNC", str(ctx.exception))

    def test_question_eh_variant_is_also_a_rejection(self):
        tnc = make_opentnc(ScriptedSerial({"BOGUS": "?EH"}))

        with self.assertRaises(KAMCommandError):
            tnc.send_command("BOGUS")

    def test_get_accepts_several_reply_shapes(self):
        for reply in ("MYCALL NA6D", "MYCALL: NA6D", "MYCALL = NA6D",
                      "MYCALL is NA6D", "mycall NA6D"):
            tnc = make_opentnc(ScriptedSerial({"MYCALL": reply}))
            self.assertEqual(tnc.get("MYCALL"), "NA6D", reply)

    def test_get_skips_an_unrelated_line_before_the_value(self):
        tnc = make_opentnc(ScriptedSerial({
            "MYCALL": "Settings not saved\r\nMYCALL NA6D-1",
        }))

        self.assertEqual(tnc.get("MYCALL"), "NA6D-1")

    def test_get_works_with_echo_off(self):
        tnc = make_opentnc(ScriptedSerial({"PACLEN": "PACLEN 128"}, echo=False))
        self.assertEqual(tnc.get_typed("PACLEN"), 128)

    def test_monitor_is_a_single_bool(self):
        tnc = make_opentnc(ScriptedSerial({"MONITOR": "MONITOR ON"}))
        self.assertIs(tnc.get_typed("MONITOR"), True)

    def test_set_bool_writes_on_off(self):
        serial = ScriptedSerial({"DIGIPEAT": "DIGIPEAT ON"})
        tnc = make_opentnc(serial)

        self.assertIs(tnc.set_typed("DIGIPEAT", True), True)
        self.assertIn(b"DIGIPEAT ON\r", serial.written)

    def test_fx25_choices_are_validated_before_writing(self):
        serial = ScriptedSerial()
        tnc = make_opentnc(serial)

        with self.assertRaises(ValueError):
            tnc.set_fx25(connection="SOMETIMES")

        self.assertEqual(serial.written, [])

    def test_set_fx25_writes_and_reads_back(self):
        serial = ScriptedSerial({
            "FX25CONNECTION": "FX25CONNECTION OPPORTUNISTIC",
            "FX25ENCODING": "FX25ENCODING AUTOMATIC",
        })
        tnc = make_opentnc(serial)

        result = tnc.set_fx25(connection="opportunistic", encoding="AUTOMATIC")

        self.assertEqual(result, ("OPPORTUNISTIC", "AUTOMATIC"))
        self.assertIn(b"FX25CONNECTION OPPORTUNISTIC\r", serial.written)
        self.assertIn(b"FX25ENCODING AUTOMATIC\r", serial.written)

    def test_carrier(self):
        serial = ScriptedSerial({"CARRIER": "CARRIER SQUELCH"})
        tnc = make_opentnc(serial)

        with self.assertRaises(ValueError):
            tnc.set_carrier("DCD")

        self.assertEqual(tnc.set_carrier("squelch"), "SQUELCH")
        self.assertIn(b"CARRIER SQUELCH\r", serial.written)

    def test_save_settings_sends_save_bang(self):
        serial = ScriptedSerial()
        tnc = make_opentnc(serial)

        tnc.save_settings()

        self.assertEqual(serial.written, [b"SAVE!\r"])

    def test_version_is_read_only(self):
        tnc = make_opentnc(ScriptedSerial())

        with self.assertRaises(KAMError):
            tnc.set_typed("VERSION", "9.9")

    def test_interleaved_monitor_line_does_not_eat_the_reply(self):
        # TNC-2 monitor lines carry their payload inline with no blank
        # line after -- the KAM-XL's "through the next blank line"
        # stripping rule would delete "MYCALL NA6D" along with it.
        tnc = make_opentnc(ScriptedSerial({
            "MYCALL": "K5LRK>APRS,WIDE1-1*:>On the air\r\nMYCALL NA6D",
        }))

        self.assertEqual(tnc.get("MYCALL"), "NA6D")

    def test_pbbs_is_not_available(self):
        tnc = make_opentnc(ScriptedSerial())

        with self.assertRaises(KAMError):
            tnc.list_pbbs_messages()

        with self.assertRaises(KAMError):
            tnc.read_pbbs_message(1)


class OpenTNCConnectedModeTests(unittest.TestCase):
    def test_connect_banner(self):
        tnc = make_opentnc(CannedSerial(["cmd:*** CONNECTED to KD5EOC-10\r\n"]))

        banner = tnc.connect_station("kd5eoc-10")

        self.assertIn("CONNECTED to KD5EOC-10", banner)
        self.assertEqual(tnc.serial.written[-1], b"CONNECT KD5EOC-10\r")

    def test_connect_rejection_variant(self):
        tnc = make_opentnc(CannedSerial(["?EH\r\ncmd:"]))

        with self.assertRaises(KAMConnectionError):
            tnc.connect_station("KD5EOC-10", timeout=1)

    def test_disconnect_spells_out_the_command(self):
        tnc = make_opentnc(CannedSerial([
            "cmd:",
            "*** DISCONNECTED\r\n",
        ]))

        tnc.disconnect_station(timeout=1, command_mode_timeout=1)

        self.assertIn(b"DISCONNECT\r", tnc.serial.written)

    def test_enter_kiss_mode_does_not_wait_for_a_prompt(self):
        tnc = make_opentnc(CannedSerial([]))

        tnc.enter_kiss_mode(settle=0.05)

        self.assertEqual(tnc.serial.written, [b"KISS ON\r"])


class TNC2MonitorParsingTests(unittest.TestCase):
    def test_header_shapes(self):
        cases = {
            "K5LRK>APRS,WIDE1-1*,WIDE2-1:!3313.65N/09707.64W-":
                ("K5LRK", "APRS", "WIDE1-1*,WIDE2-1", None, "!3313.65N/09707.64W-"),
            "WB5NZV>KD5EOC-10 <C>:":
                ("WB5NZV", "KD5EOC-10", None, "C", ""),
            "KD5EOC-10>WB5NZV <I R0 S1>:Welcome":
                ("KD5EOC-10", "WB5NZV", None, "I", "Welcome"),
            "10/18/26 22:41:05 N0CALL>BEACON <UI R>:hello":
                ("N0CALL", "BEACON", None, "UI", "hello"),
            "[0] N0CALL>APRS:>status":
                ("N0CALL", "APRS", None, None, ">status"),
        }

        for line, expected in cases.items():
            match = TNC2_HEADER_RE.match(line)
            self.assertIsNotNone(match, line)
            self.assertEqual(
                (match.group("source"), match.group("destination"),
                 match.group("digipeaters"), match.group("frame_type"),
                 match.group("inline_payload")),
                expected,
                line,
            )

    def test_non_headers_are_ignored(self):
        for line in ("cmd:", "MYCALL NA6D", "*** CONNECTED to KD5EOC-10", ""):
            self.assertIsNone(TNC2_HEADER_RE.match(line), line)

    def test_opentnc_monitor_reports_the_aprs_port(self):
        tnc = make_opentnc(ChunkSerial([
            "K5LRK>APRS,WIDE1-1*,WI",
            "DE2-1:>Denton EOC\r\nN0CALL>APRS:=3313.",
            "65N/09707.64W-\r\n",
        ]))

        packets = list(tnc.monitor(seconds=0.5))

        self.assertEqual([p.source for p in packets], ["K5LRK", "N0CALL"])
        self.assertEqual(packets[0].digipeaters, ("WIDE1-1*", "WIDE2-1"))
        self.assertTrue(packets[0].digipeated)
        self.assertEqual(packets[0].payload, ">Denton EOC")
        self.assertEqual(packets[1].payload, "=3313.65N/09707.64W-")
        self.assertTrue(all(p.port == APRS_RADIO_PORT for p in packets))

    def test_kamxl_parser_is_unchanged(self):
        parser = KAMXL("COM_FAKE").make_packet_parser()

        packets = parser.feed("K5LRK>BEACON/2: <UI>:\r\nhello\r\n")
        packets += parser.flush()

        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].port, 2)
        self.assertEqual(packets[0].frame_type, "UI")
        self.assertEqual(packets[0].payload, "hello")

    def test_default_parser_still_reads_kamxl_format(self):
        # Nothing is complete until the next header or flush.
        parser = PacketParser()
        parser.feed("K5LRK>BEACON/1: <UI>:\r\nhf\r\n")
        self.assertEqual(parser.flush()[0].port, 1)


class IdleFlushTests(unittest.TestCase):
    """TNC-2 output has no end-of-packet marker -- see MONITOR_IDLE_FLUSH."""

    def test_flush_pending_finishes_a_complete_packet(self):
        parser = OpenTNC("COM_FAKE").make_packet_parser()

        self.assertEqual(parser.feed("N0CALL>APRS:>status\r\n"), [])

        packets = parser.flush_pending()

        self.assertEqual([p.payload for p in packets], [">status"])
        self.assertEqual(parser.flush_pending(), [])

    def test_flush_pending_leaves_a_partial_line_alone(self):
        parser = OpenTNC("COM_FAKE").make_packet_parser()

        parser.feed("N0CALL>APRS:>one\r\nK5LRK>AP")

        # Half of the next line is still in flight, so it isn't quiet
        # yet: don't finish anything early.
        self.assertEqual(parser.flush_pending(), [])

        packets = parser.feed("RS:>two\r\n") + parser.flush_pending()

        self.assertEqual([p.payload for p in packets], [">one", ">two"])

    def test_only_opentnc_opts_in(self):
        self.assertIsNone(KAMXL.MONITOR_IDLE_FLUSH)
        self.assertIsNotNone(OpenTNC.MONITOR_IDLE_FLUSH)

    def test_last_message_of_a_burst_reaches_the_inbox(self):
        import os
        import tempfile
        import time

        tmp = tempfile.mkdtemp()
        tnc = make_opentnc(ChunkSerial([
            "K5LRK>APRS,WIDE1-1*:!3313.65N/09707.64W-\r\n",
            "W5ABC>APRS::N5BA     :hello via OpenTNC{12\r\n",
        ]))
        daemon = KAMDaemon(
            tnc,
            os.path.join(tmp, "kamxl.sock"),
            station_callsign="N5BA",
            history_db=os.path.join(tmp, "history.sqlite3"),
        )
        self.addCleanup(daemon.shutdown)

        deadline = time.monotonic() + 5
        messages = []

        while time.monotonic() < deadline and not messages:
            time.sleep(0.1)
            messages = daemon.dispatch("aprs.messages", {})["messages"]

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["source"], "W5ABC")
        self.assertEqual(messages[0]["message"], "hello via OpenTNC")


class APRSTransmitGateTests(unittest.TestCase):
    def test_kamxl_is_supported(self):
        self.assertTrue(KAMXL.APRS_TX_SUPPORTED)

    def test_preflight_refuses_opentnc_before_any_write(self):
        serial = ScriptedSerial()
        tnc = make_opentnc(serial)

        with self.assertRaises(APRSTransmitError) as ctx:
            check_aprs_tx_preflight(tnc)

        self.assertIn("OpenTNC", str(ctx.exception))
        self.assertEqual(serial.written, [])

    def test_recovery_refuses_opentnc_before_any_write(self):
        serial = ScriptedSerial()
        tnc = make_opentnc(serial)

        with self.assertRaises(APRSTransmitError):
            recover_aprs_command_mode(tnc)

        self.assertEqual(serial.written, [])


class DaemonWithOpenTNCTests(unittest.TestCase):
    def _daemon(self):
        daemon = KAMDaemon(
            make_opentnc(ScriptedSerial({})),
            "/tmp/kamxl-opentnc-test.sock",
            station_callsign="N5BA",
        )
        self.addCleanup(daemon.shutdown)
        return daemon

    def test_status_reports_model(self):
        status = self._daemon().dispatch("status", {})

        self.assertEqual(status["tnc_model"], "OpenTNC")
        self.assertIs(status["aprs_tx_supported"], False)

    def test_transmit_features_refuse_up_front(self):
        daemon = self._daemon()

        for method in ("aprs.beacon_enable", "aprs.ack_enable",
                       "aprs.retry_enable", "aprs.send_message"):
            with self.assertRaises(APRSTransmitError, msg=method):
                daemon.dispatch(method, dict(
                    _CONFIRM, addressee="N0CALL", message="hi"
                ))

    def test_tncstation_daemon_status_reports_model(self):
        from fakes import make_kam

        daemon = KAMDaemon(
            make_kam(ScriptedSerial({})),
            "/tmp/kamxl-model-test.sock",
            station_callsign="N5BA",
        )
        self.addCleanup(daemon.shutdown)

        status = daemon.dispatch("status", {})

        self.assertEqual(status["tnc_model"], "KAM-XL")
        self.assertIs(status["aprs_tx_supported"], True)


if __name__ == "__main__":
    unittest.main()
