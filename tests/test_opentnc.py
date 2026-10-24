"""
Offline tests for the driver layer (tnc.py) and the OpenTNC driver
(opentnc.py). No hardware: the same scripted fake serial connections
the KAM-XL tests use.

The OpenTNC's output formats used here are TNC-2 convention, not yet
captured from a real unit (see opentnc.py's module docstring). When
tools/opentnc_capture.py records the real thing, replace these fixtures with
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
        serial = ScriptedSerial({"CARRIERDETECT": "is SQuelch"})
        tnc = make_opentnc(serial)

        with self.assertRaises(ValueError):
            tnc.set_carrier("DCD")

        self.assertEqual(tnc.set_carrier("squelch"), "SQUELCH")
        self.assertIn(b"CARRIERDETECT SQUELCH\r", serial.written)

    def test_save_settings_sends_save_bang(self):
        serial = ScriptedSerial()
        tnc = make_opentnc(serial)

        tnc.save_settings()

        self.assertEqual(serial.written, [b"SAVE!\r"])

    def test_mode_switching_settings_are_read_only(self):
        serial = ScriptedSerial()
        tnc = make_opentnc(serial)

        for name, value in (("KISS", "ON"), ("MODE", "EXPERT")):
            with self.assertRaises(KAMError):
                tnc.set_typed(name, value)
        self.assertEqual(serial.written, [])

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

        with self.assertRaises(KAMError):
            tnc.kill_pbbs_message(1)


# ---------------------------------------------------------------------------
# Replaying the real unit (tests/opentnc_capture_20261019.txt)
# ---------------------------------------------------------------------------

import ast
from pathlib import Path

CAPTURE = Path(__file__).resolve().parent / "opentnc_capture_20261019.txt"


def load_capture(path=CAPTURE):
    """sent-command bytes -> the exact bytes the OpenTNC answered."""
    lines = path.read_text().splitlines()
    replies, sent = {}, None
    for i, line in enumerate(lines):
        if "] sent " in line:
            sent = ast.literal_eval(lines[i + 1].strip())
        elif line.rstrip().endswith("] recv") and sent is not None:
            replies[sent] = ast.literal_eval(lines[i + 1].strip())
            sent = None
    return replies


class CaptureReplaySerial:
    """Answers each command with what the real OpenTNC sent back."""

    def __init__(self):
        self.is_open = True
        self.replies = load_capture()
        self.written = []
        self._out = bytearray()

    def write(self, data):
        self.written.append(bytes(data))
        self._out.extend(self.replies.get(
            bytes(data).upper(),
            bytes(data).strip() + b"\r\nUnknown command: X\r\nType 'help' "
            b"to list all commands\r\ncmd:"))
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self._out.clear()

    def close(self):
        self.is_open = False

    @property
    def in_waiting(self):
        return len(self._out)

    def read(self, n=1):
        data = bytes(self._out[:n])
        del self._out[:len(data)]
        return data


class RealUnitTests(unittest.TestCase):
    """The driver against the OpenTNC's own replies, byte for byte."""

    def setUp(self):
        self.serial = CaptureReplaySerial()
        self.tnc = make_opentnc(self.serial)

    def test_capture_fixture_loaded(self):
        self.assertIn(b"MYCALL\r", self.serial.replies)
        self.assertGreater(len(self.serial.replies), 30)

    def test_bare_query_is_reply(self):
        self.assertEqual(self.tnc.get("MYCALL"), "N5BA-11")
        self.assertEqual(self.tnc.get("COMMAND"), "0x3 (CTRL-C)")

    def test_typed_settings(self):
        expected = {
            "MYCALL": "N5BA-11",
            "MONITOR": True,
            "DIGIPEAT": False,
            "CPACTIME": False,
            "CR": True,
            "FX25CONNECTION": "OPPORTUNISTIC",
            "FX25ENCODING": "AUTOMATIC",
            "PACLEN": 256,
            "MAXFRAME": 2,
            "FRACK": 6,
            "RETRY": 9,
            "RESPTIME": 5,
            "CHECK": 30,
            "TXDELAY": 40,
            "SLOTTIME": 10,
            "PERSIST": 63,
            "UNPROTO": "CQ",
        }
        for name, value in expected.items():
            with self.subTest(setting=name):
                self.assertEqual(self.tnc.get_typed(name), value)

    def test_carrier_and_fx25_helpers(self):
        # The capture asked "CARRIER", which the OpenTNC accepts as an
        # abbreviation of CARRIERDETECT; replay it under the full name too.
        self.serial.replies[b"CARRIERDETECT\r"] = self.serial.replies[b"CARRIER\r"].replace(
            b"CARRIER\r\n", b"CARRIERDETECT\r\n")
        self.assertEqual(self.tnc.get_carrier(), "SQUELCH")
        self.assertEqual(self.tnc.get_fx25(), ("OPPORTUNISTIC", "AUTOMATIC"))

    def test_unknown_command_is_an_error(self):
        for command in ("XYZZYQ", "STATUS", "VERSION", "APRSDIGI"):
            with self.subTest(command=command):
                with self.assertRaises(KAMCommandError) as ctx:
                    self.tnc.send_command(command)
                self.assertIn("OpenTNC", str(ctx.exception))

    def test_display(self):
        config = self.tnc.get_configuration()
        self.assertEqual(config["MYCALL"], "N5BA-11")
        self.assertEqual(config["CARRIERDETECT"], "SQuelch")
        self.assertEqual(config["FX25CONNECTION"], "OPportunistic")
        self.assertEqual(config["TXLEVEL"], "256")
        self.assertEqual(config["KISS"], "off")
        self.assertNotIn("HEAP", " ".join(config))
        self.assertEqual(len(config), 43)

    def test_command_table_matches_the_firmware(self):
        # Every typed setting is one the real DISPLAY lists.
        displayed = set(self.tnc.get_configuration())
        missing = set(OpenTNC.COMMANDS) - displayed
        self.assertEqual(missing, set())


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


# Heard by the real unit, 2026-10-19: N5BA's KAM-XL beacon, printed
# right after the prompt.
REAL_MONITOR_LINE = (
    "cmd:0 (???): <[To APRS from N5BA via WIDE1-1 WIDE2-1: 03(UI) "
    "PID ^F0 =3314.34N/09650.70W-N5BA KAM-XL ^0D ]"
)


class RealMonitorLineTests(unittest.TestCase):
    def _parse(self, *chunks):
        parser = OpenTNC("COM_FAKE").make_packet_parser()
        packets = []
        for chunk in chunks:
            packets += parser.feed(chunk)
        return packets + parser.flush()

    def test_the_real_line(self):
        (packet,) = self._parse(REAL_MONITOR_LINE + "\r\n")

        self.assertEqual(packet.source, "N5BA")
        self.assertEqual(packet.destination, "APRS")
        self.assertEqual(packet.digipeaters, ("WIDE1-1", "WIDE2-1"))
        self.assertFalse(packet.digipeated)
        self.assertEqual(packet.frame_type, "UI")
        self.assertEqual(packet.port, APRS_RADIO_PORT)
        self.assertEqual(packet.payload, "=3314.34N/09650.70W-N5BA KAM-XL")
        self.assertTrue(packet.raw.startswith("<[To APRS from N5BA"))

    def test_emitted_as_soon_as_the_line_ends(self):
        parser = OpenTNC("COM_FAKE").make_packet_parser()

        self.assertEqual(parser.feed(REAL_MONITOR_LINE[:30]), [])
        packets = parser.feed(REAL_MONITOR_LINE[30:] + "\r\n")

        self.assertEqual([p.source for p in packets], ["N5BA"])

    def test_shapes(self):
        # Built the way DumpPacket() (firmware src/packetutil.c) prints.
        cases = {
            # Payload runs straight into "]"; "*" on DEST is the
            # command bit, not part of the callsign.
            "7 (2.0): <[To APZTNC* from K5LRK-9: 03(UI) PID ^F0 >status]":
                ("K5LRK-9", "APZTNC", (), "UI", ">status"),
            # "*" on a digipeater means it has repeated the packet.
            "8 (2.0): <[To APRS* from W5ABC via KD5EOC-10* WIDE2-1: "
            "03(UI) PID ^F0 !x]":
                ("W5ABC", "APRS", ("KD5EOC-10*", "WIDE2-1"), "UI", "!x"),
            # Mic-E: non-ASCII bytes escaped " ^XX " (a space each side).
            "9 (???): <[To S32U6T from N0CALL: 03(UI) PID ^F0 `| ^E2 l ^7F j>/]":
                ("N0CALL", "S32U6T", (), "UI", "`|\xe2l\x7fj>/"),
            # A real space next to an escape survives; CR LF in the middle.
            "10 (2.0): <[To APRS from N0CALL: 03(UI) PID ^F0 >a  ^0D  ^0A b]":
                ("N0CALL", "APRS", (), "UI", ">a \nb"),
            # Signal details in front of the version; [DAMA] flag.
            "11 A01234 J0.05 B1200 F1(2.0): <[To APRS*[DAMA] from N5BA: "
            "03(UI) PID ^F0 >fx25]":
                ("N5BA", "APRS", (), "UI", ">fx25"),
            # Connected-mode frames, extended (v2.2) control field.
            "12 (2.2): <[To N5BA* from KD5EOC-10: 0042(I n_r=1 P n_s=1) "
            "PID ^F0 Welcome]":
                ("KD5EOC-10", "N5BA", (), "I", "Welcome"),
            "13 (2.0): <[To N5BA* from KD5EOC-10: 3F(SABM) ]":
                ("KD5EOC-10", "N5BA", (), "SABM", ""),
        }

        for line, (source, dest, digis, frame_type, payload) in cases.items():
            (packet,) = self._parse(line + "\r\n")
            self.assertEqual(
                (packet.source, packet.destination, packet.digipeaters,
                 packet.frame_type, packet.payload),
                (source, dest, digis, frame_type, payload),
                line,
            )

    def test_packet_counter_is_not_a_port(self):
        packets = self._parse(
            "1 (???): <[To APRS from N5BA: 03(UI) PID ^F0 >one]\r\n"
            "42 (???): <[To APRS from N5BA: 03(UI) PID ^F0 >two]\r\n"
        )

        self.assertEqual([p.port for p in packets], [APRS_RADIO_PORT] * 2)

    def test_only_received_packets(self):
        self.assertEqual(self._parse(
            "3 (2.0): >[To APRS from N5BA-11: 03(UI) PID ^F0 >sent]\r\n"
        ), [])

    def test_line_split_by_the_firmware_is_joined(self):
        (packet,) = self._parse(
            "0 (???): <[To APRS from N5BA: 03(UI) PID ^F0 >one",
            "\r\n two]\r\n",
        )

        self.assertEqual(packet.payload, ">one two")

    def test_unknown_frame_part_is_still_recorded(self):
        (packet,) = self._parse("0 (???): <[To N5BA from KD5EOC-10: odd]\r\n")

        self.assertEqual(packet.source, "KD5EOC-10")
        self.assertIsNone(packet.frame_type)
        self.assertEqual(packet.payload, "odd")

    def test_tnc2_lines_still_parse(self):
        packets = self._parse(
            "K5LRK>APRS,WIDE1-1*:>status\r\n" + REAL_MONITOR_LINE + "\r\n"
        )

        self.assertEqual([p.source for p in packets], ["K5LRK", "N5BA"])

    def test_prompt_and_noise_are_not_packets(self):
        self.assertEqual(self._parse("cmd:\r\nMYCALL\r\nis N5BA-11\r\n"), [])

    def test_monitor_line_does_not_eat_a_reply(self):
        # Mid-reply there's no idle prompt in front of it.
        tnc = make_opentnc(ScriptedSerial({
            "MYCALL": REAL_MONITOR_LINE[len("cmd:"):] + "\r\nis N5BA-11",
        }))

        self.assertEqual(tnc.get("MYCALL"), "N5BA-11")

    def test_beacon_reaches_the_station_list(self):
        import os
        import tempfile
        import time

        tmp = tempfile.mkdtemp()
        tnc = make_opentnc(ChunkSerial([REAL_MONITOR_LINE + "\r\n"]))
        daemon = KAMDaemon(
            tnc,
            os.path.join(tmp, "t.sock"),
            station_callsign="N5BA-11",
            history_db=os.path.join(tmp, "history.sqlite3"),
        )
        self.addCleanup(daemon.shutdown)

        deadline = time.monotonic() + 5
        stations = []
        while time.monotonic() < deadline and not stations:
            time.sleep(0.1)
            stations = daemon.dispatch("stations.list", {})

        self.assertEqual([s["callsign"] for s in stations], ["N5BA"])
        self.assertAlmostEqual(stations[0]["latitude"], 33 + 14.34 / 60, 4)


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

    def test_status_features(self):
        # Terminal Mode, but no mailbox, and Winlink is untried.
        self.assertEqual(self._daemon().dispatch("status", {})["features"],
                         {"terminal": True, "pbbs": False, "winlink": False})

    def test_kamxl_has_every_feature(self):
        from fakes import make_kam

        daemon = KAMDaemon(
            make_kam(ScriptedSerial({})),
            "/tmp/kamxl-features-test.sock",
            station_callsign="N5BA",
        )
        self.addCleanup(daemon.shutdown)

        self.assertEqual(daemon.dispatch("status", {})["features"],
                         {"terminal": True, "pbbs": True, "winlink": True})

    def test_radio_ports_drive_the_transmit_wording(self):
        # The dashboard names ports ("VHF Port 2") only on a two-port TNC.
        from fakes import make_kam

        kamxl = KAMDaemon(make_kam(ScriptedSerial({})),
                          "/tmp/kamxl-ports-test.sock", station_callsign="N5BA")
        self.addCleanup(kamxl.shutdown)

        self.assertEqual(kamxl.dispatch("status", {})["radio_ports"], 2)
        self.assertEqual(self._daemon().dispatch("status", {})["radio_ports"], 1)

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
