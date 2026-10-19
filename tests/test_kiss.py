"""
KISS mode (kiss.py): framing, AX.25 encode/decode, the drivers, APRS
transmit as UI frames, KISS over TCP (Direwolf), and the daemon running
a whole station over KISS. No hardware: a fake TNC that speaks KISS
bytes, and a real local TCP server for the Direwolf case.
"""

import os
import socket
import tempfile
import threading
import time
import unittest

from unittest.mock import patch

import fakes  # noqa: F401  (puts the repo root on sys.path)

import kiss
from aprs_tx import (
    APRSTransmitError,
    _execute_aprs_tx_transaction,
    check_aprs_tx_preflight,
    recover_aprs_command_mode,
)
from kamxl import KAMXL, KAMXLKISS
from kiss import (
    FEND,
    KISS_EXIT,
    GenericKISS,
    KISSDeframer,
    KISSPacketParser,
    KISSTNC,
    decode_ax25,
    encode_ui_frame,
    kiss_frame,
)
from opentnc import OpenTNCKISS
from tnc import TNCError, create_tnc, normalize_tnc_mode, tnc_class
from tncstation_daemon import KAMDaemon


# A UI frame worked out by hand from the AX.25 2.2 spec (section 3.12):
# N0CALL>APRS,WIDE1-1:>hi -- each callsign character shifted left one
# bit; SSID bytes 0b011SSSS0, destination's top (command) bit set, the
# last address's bottom (extension) bit set; control 0x03, PID 0xF0.
KNOWN_UI = bytes.fromhex(
    "82 A0 A4 A6 40 40 E0"      # APRS     (C bit set)
    "9C 60 86 82 98 98 60"      # N0CALL
    "AE 92 88 8A 62 40 63"      # WIDE1-1  (last address)
    "03 F0"
) + b">hi"


def ui(source, dest, path, info, repeated=()):
    """Build a frame, then set has-been-repeated bits on chosen digis."""
    frame = bytearray(encode_ui_frame(source, dest, path, info))
    for index in repeated:
        frame[14 + 7 * index + 6] |= 0x80
    return bytes(frame)


class FakeKISSSerial:
    """A TNC on a serial port: queued incoming bytes, recorded writes."""

    exclusive = True

    def __init__(self, incoming=(), replies=None):
        self.is_open = True
        self.written = []
        self.replies = dict(replies or {})
        self._chunks = [bytes(c) for c in incoming]
        self._out = bytearray()
        self._lock = threading.Lock()

    def push(self, data):
        with self._lock:
            self._chunks.append(bytes(data))

    def write(self, data):
        with self._lock:
            self.written.append(bytes(data))
            self._out.extend(self.replies.get(bytes(data), b""))
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False

    @property
    def in_waiting(self):
        with self._lock:
            if not self._out and self._chunks:
                self._out.extend(self._chunks.pop(0))
            return len(self._out)

    def read(self, n=1):
        with self._lock:
            data = bytes(self._out[:n])
            del self._out[:len(data)]
            return data


def fast_settle(self, seconds):
    # The real _settle() waits out its full time collecting replies;
    # the fake answers instantly, so just take what's there.
    return self.serial.read(self.serial.in_waiting or 0)


def driver(cls=GenericKISS, serial=None, mycall="N5BA"):
    tnc = cls("FAKE", mycall=mycall)
    tnc.serial = serial if serial is not None else FakeKISSSerial()
    return tnc


def frames_written(serial):
    """Decode every KISS frame the driver wrote."""
    deframer = KISSDeframer()
    out = []
    for chunk in serial.written:
        out += deframer.feed(chunk)
    return [(channel, decode_ax25(ax25)) for channel, ax25 in out]


# ---------------------------------------------------------------------------

class FramingTests(unittest.TestCase):

    def test_escape_round_trip(self):
        payload = bytes([0x01, FEND, 0xDB, 0xDC, 0xDD, FEND, 0x02])
        frame = kiss_frame(payload, channel=3)

        self.assertEqual(frame[0], FEND)
        self.assertEqual(frame[1], 0x30)
        self.assertEqual(frame[-1], FEND)
        self.assertNotIn(FEND, frame[1:-1])
        self.assertEqual(KISSDeframer().feed(frame), [(3, payload)])

    def test_frames_split_across_reads_and_noise_ignored(self):
        a, b = kiss_frame(b"first"), kiss_frame(b"second", channel=1)
        # Text between frames (a banner, a stray prompt) isn't a frame.
        stream = b"cmd: banner text\r\n" + a + b"\r\ncmd:" + b
        deframer = KISSDeframer()
        got = []
        for i in range(0, len(stream), 3):
            got += deframer.feed(stream[i:i + 3])
        self.assertEqual(got, [(0, b"first"), (1, b"second")])

    def test_non_data_commands_ignored(self):
        txdelay = bytes([FEND, 0x01, 50, FEND])
        self.assertEqual(KISSDeframer().feed(txdelay + kiss_frame(b"x")), [(0, b"x")])

    def test_channel_range(self):
        with self.assertRaises(ValueError):
            kiss_frame(b"x", channel=16)


class AX25Tests(unittest.TestCase):

    def test_known_answer_encode(self):
        self.assertEqual(encode_ui_frame("N0CALL", "APRS", ["WIDE1-1"], b">hi"), KNOWN_UI)

    def test_known_answer_decode(self):
        frame = decode_ax25(KNOWN_UI)
        self.assertEqual(
            (frame.source, frame.destination, frame.digipeaters, frame.frame_type, frame.info),
            ("N0CALL", "APRS", ("WIDE1-1",), "UI", b">hi"),
        )

    def test_ssids_and_no_path(self):
        frame = decode_ax25(encode_ui_frame("KD5EOC-10", "APN391", [], b"!x"))
        self.assertEqual((frame.source, frame.destination, frame.digipeaters),
                         ("KD5EOC-10", "APN391", ()))

    def test_only_last_repeater_is_starred(self):
        # TNC-2 / KAM-XL display convention, which aprs_coverage relies on.
        frame = decode_ax25(ui("K5LRK", "APRS", ["KD5EOC-10", "WIDE1", "WIDE2-1"], b"x",
                               repeated=(0, 1)))
        self.assertEqual(frame.digipeaters, ("KD5EOC-10", "WIDE1*", "WIDE2-1"))

    def test_bad_addresses_rejected(self):
        for call in ("TOOLONGX", "N5BA-16", "N5 BA", ""):
            with self.subTest(call=call), self.assertRaises(ValueError):
                encode_ui_frame(call, "APRS", [], b"")
        with self.assertRaises(ValueError):
            encode_ui_frame("N5BA", "APRS", ["WIDE1-1"] * 9, b"")

    def test_malformed_frames_rejected(self):
        for junk in (b"", b"\x82" * 6, KNOWN_UI[:14], bytes(16)):
            with self.subTest(junk=junk), self.assertRaises(ValueError):
                decode_ax25(junk)

    def test_frame_types(self):
        base = bytearray(KNOWN_UI[:21])
        for control, name in ((0x3F, "SABM"), (0x53, "DISC"), (0x73, "UA"),
                              (0x01, "RR"), (0x00, "I"), (0x03, "UI")):
            with self.subTest(name=name):
                self.assertEqual(decode_ax25(bytes(base) + bytes([control, 0xF0])).frame_type, name)


class ParserTests(unittest.TestCase):

    def test_packets_match_the_terminal_parser_shape(self):
        parser = KISSPacketParser({0: 2})
        frame = ui("K5LRK", "APRS", ["KD5EOC-10", "WIDE2-1"],
                   b"!3313.65N/09707.64W-PHG2360 Sanger\r", repeated=(0,))
        [packet] = parser.feed(kiss_frame(frame).decode("latin-1"))

        self.assertEqual(packet.source, "K5LRK")
        self.assertEqual(packet.destination, "APRS")
        self.assertEqual(packet.digipeaters, ("KD5EOC-10*", "WIDE2-1"))
        self.assertTrue(packet.digipeated)
        self.assertEqual(packet.port, 2)
        self.assertEqual(packet.frame_type, "UI")
        self.assertEqual(packet.payload, "!3313.65N/09707.64W-PHG2360 Sanger")
        self.assertEqual(parser.flush(), [])

    def test_mic_e_bytes_survive(self):
        info = b"`}aCo+H>/'\"5i}\x9f\xe0|!w%Q!|3"
        [packet] = KISSPacketParser({0: 2}).feed(
            kiss_frame(ui("KE7A-9", "S2UX9U", [], info)).decode("latin-1"))
        self.assertEqual(packet.payload.encode("latin-1"), info)

    def test_channel_mapping(self):
        parser = KISSPacketParser({0: 1, 1: 2})
        frames = kiss_frame(ui("A1A", "APRS", [], b"hf"), 0) + \
            kiss_frame(ui("B2B", "APRS", [], b"vhf"), 1) + \
            kiss_frame(ui("C3C", "APRS", [], b"x"), 5)
        self.assertEqual([p.port for p in parser.feed(frames.decode("latin-1"))], [1, 2, 15])

    def test_garbage_frame_dropped_not_fatal(self):
        parser = KISSPacketParser({0: 2})
        data = kiss_frame(b"\x01\x02\x03") + kiss_frame(KNOWN_UI)
        self.assertEqual([p.source for p in parser.feed(data.decode("latin-1"))], ["N0CALL"])
        self.assertEqual(parser.dropped, 1)


class DriverTests(unittest.TestCase):

    def test_model_and_mode_selection(self):
        self.assertIs(tnc_class("opentnc", "kiss"), OpenTNCKISS)
        self.assertIs(tnc_class("kamxl", "kiss"), KAMXLKISS)
        self.assertIs(tnc_class("direwolf"), GenericKISS)
        self.assertIs(tnc_class("kamxl"), KAMXL)
        self.assertEqual(normalize_tnc_mode("kiss", None), "kiss")
        self.assertEqual(normalize_tnc_mode("opentnc", ""), "terminal")
        with self.assertRaises(ValueError):
            normalize_tnc_mode("kiss", "terminal")
        with self.assertRaises(ValueError):
            normalize_tnc_mode("kamxl", "hostmode")
        tnc = create_tnc("kiss", "tcp://localhost:8001")
        self.assertIsInstance(tnc, GenericKISS)
        self.assertFalse(tnc.is_connected)

    def test_monitor_yields_packets(self):
        serial = FakeKISSSerial([kiss_frame(KNOWN_UI)[:9], kiss_frame(KNOWN_UI)[9:]])
        packets = list(driver(serial=serial).monitor(seconds=0.3))
        self.assertEqual([p.source for p in packets], ["N0CALL"])

    def test_send_ui(self):
        tnc = driver()
        tnc.send_ui(b">test", path=["WIDE1-1"])
        [(channel, frame)] = frames_written(tnc.serial)
        self.assertEqual(channel, 0)
        self.assertEqual((frame.source, frame.destination, frame.digipeaters, frame.info),
                         ("N5BA", "APRS", ("WIDE1-1",), b">test"))

    def test_send_ui_needs_a_callsign(self):
        tnc = driver(mycall=None)
        with self.assertRaises(TNCError):
            tnc.send_ui(b">x")
        self.assertEqual(tnc.serial.written, [])

    def test_terminal_only_methods_explain_themselves(self):
        tnc = driver()
        for call in (lambda: tnc.send_command("MYCALL"), lambda: tnc.get("MYCALL"),
                     lambda: tnc.connect_station("KD5EOC-10"),
                     lambda: tnc.check_winlink_mail("KD5EOC-10", "pw"),
                     lambda: tnc.list_pbbs_messages(), lambda: tnc.exit_kiss_mode()):
            with self.assertRaises(TNCError) as ctx:
                call()
            self.assertIn("KISS", str(ctx.exception))
        self.assertEqual(tnc.serial.written, [])


class EnterLeaveTests(unittest.TestCase):

    def setUp(self):
        patcher = patch.object(KISSTNC, "_settle", fast_settle)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_generic_sends_nothing(self):
        tnc = driver(GenericKISS)
        tnc.enter_kiss_mode()
        tnc._entered_kiss = True
        tnc.disconnect()
        self.assertEqual(tnc.serial.written, [])
        self.assertFalse(tnc.serial.is_open)

    def test_opentnc_kiss_on_and_exit(self):
        tnc = driver(OpenTNCKISS)
        tnc.enter_kiss_mode()
        self.assertEqual(tnc.serial.written, [b"\r", b"KISS ON\r"])
        tnc._entered_kiss = True
        tnc.disconnect()
        self.assertEqual(tnc.serial.written[-1], KISS_EXIT)
        self.assertFalse(tnc.serial.is_open)

    def test_kamxl_enters_from_terminal_mode(self):
        serial = FakeKISSSerial(replies={b"\r": b"\r\ncmd:"})
        tnc = driver(KAMXLKISS, serial)
        tnc.enter_kiss_mode()
        self.assertEqual(serial.written, [b"\r", b"INTFACE KISS\r", b"RESET\r"])

    def test_kamxl_already_in_kiss_is_left_alone(self):
        tnc = driver(KAMXLKISS)
        tnc.enter_kiss_mode()
        self.assertEqual(tnc.serial.written, [b"\r"])

    def test_kamxl_restores_terminal_mode(self):
        tnc = driver(KAMXLKISS)
        tnc._entered_kiss = True
        tnc.disconnect()
        self.assertEqual(tnc.serial.written,
                         [KISS_EXIT, b"\rINTFACE TERMINAL\r", b"RESET\r"])

    def test_kamxl_kiss_transmit_stays_off(self):
        self.assertFalse(KAMXLKISS.APRS_TX_SUPPORTED)
        self.assertEqual(KAMXLKISS.PORT_MAP, {0: 1, 1: 2})


class APRSTransmitTests(unittest.TestCase):

    def test_message_goes_out_as_one_ui_frame(self):
        tnc = driver()
        result = _execute_aprs_tx_transaction(tnc, ":W5ABC    :hello{12", "WIDE1-1,WIDE2-1")

        self.assertTrue(result["submitted_to_tnc"])
        [(channel, frame)] = frames_written(tnc.serial)
        self.assertEqual((frame.source, frame.destination), ("N5BA", "APRS"))
        self.assertEqual(frame.digipeaters, ("WIDE1-1", "WIDE2-1"))
        self.assertEqual(frame.info, b":W5ABC    :hello{12")

    def test_direct_beacon(self):
        tnc = driver()
        beacon = "=3314.34N/09650.70W-N5BA demo"
        _execute_aprs_tx_transaction(tnc, beacon, "")
        [(_, frame)] = frames_written(tnc.serial)
        self.assertEqual((frame.digipeaters, frame.info), ((), beacon.encode()))

    def test_bad_payload_or_path_rejected_before_anything_is_sent(self):
        for payload, path in ((":W5ABC    :line\rbreak", ""), ("hello", ""),
                              (":W5ABC    :hi", "NOT A PATH")):
            tnc = driver()
            with self.subTest(payload=payload, path=path), self.assertRaises(Exception):
                _execute_aprs_tx_transaction(tnc, payload, path)
            self.assertEqual(tnc.serial.written, [])

    def test_recovery_and_preflight_need_no_io(self):
        tnc = driver()
        self.assertEqual(recover_aprs_command_mode(tnc)["active_stream"], "KISS")
        self.assertEqual(check_aprs_tx_preflight(tnc)["checks"], ["KISS"])
        self.assertEqual(tnc.serial.written, [])

    def test_unsupported_model_refuses(self):
        tnc = driver(KAMXLKISS)
        with self.assertRaises(APRSTransmitError):
            _execute_aprs_tx_transaction(tnc, ":W5ABC    :hi", "")
        self.assertEqual(tnc.serial.written, [])


class FakeKISSServer:
    """A KISS TCP server like Direwolf's port 8001."""

    def __init__(self, frames=()):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.frames = list(frames)
        self.received = bytearray()
        self.conn = None
        self.ready = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self.conn, _ = self.sock.accept()
        for f in self.frames:
            self.conn.sendall(f)
        self.ready.set()
        self.conn.settimeout(0.2)
        while True:
            try:
                data = self.conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            self.received.extend(data)

    def close(self):
        if self.conn:
            self.conn.close()
        self.sock.close()


class TCPTests(unittest.TestCase):

    def test_direwolf_style_receive_and_transmit(self):
        server = FakeKISSServer([kiss_frame(KNOWN_UI)])
        self.addCleanup(server.close)

        tnc = create_tnc("direwolf", f"tcp://127.0.0.1:{server.port}", mycall="N5BA")
        tnc.connect()
        self.addCleanup(tnc.disconnect)
        server.ready.wait(2)

        packets = list(tnc.monitor(seconds=0.5))
        self.assertEqual([p.source for p in packets], ["N0CALL"])
        self.assertIs(tnc.serial.exclusive, True)

        tnc.send_ui(b">from tncstation")
        deadline = time.monotonic() + 2
        while not server.received and time.monotonic() < deadline:
            time.sleep(0.02)
        [(_, ax25)] = KISSDeframer().feed(bytes(server.received))
        self.assertEqual(decode_ax25(ax25).info, b">from tncstation")

    def test_server_going_away_is_an_error(self):
        server = FakeKISSServer()
        tnc = create_tnc("kiss", f"tcp://127.0.0.1:{server.port}")
        tnc.connect()
        server.ready.wait(2)
        server.close()
        time.sleep(0.1)
        with self.assertRaises(TNCError):
            for _ in range(20):
                tnc.read_available()
                time.sleep(0.02)

    def test_tcp_address_parsing(self):
        self.assertEqual(kiss.parse_tcp_address("tcp://localhost:8001"), ("localhost", 8001))
        self.assertEqual(kiss.parse_tcp_address("TCP://[::1]:8001/"), ("::1", 8001))
        self.assertIsNone(kiss.parse_tcp_address("/dev/ttyACM0"))
        self.assertIsNone(kiss.parse_tcp_address("COM9"))


class DaemonOverKISSTests(unittest.TestCase):
    """A whole station over KISS: receive, inbox, send, auto-ACK."""

    def start(self, incoming=()):
        self.serial = FakeKISSSerial(incoming)
        tnc = GenericKISS("FAKE")
        tnc.serial = self.serial
        tmp = tempfile.mkdtemp()
        self.daemon = KAMDaemon(
            tnc,
            os.path.join(tmp, "kiss.sock"),
            history_db=os.path.join(tmp, "history.sqlite3"),
            station_callsign="N5BA",
            aprs_path="WIDE1-1,WIDE2-1",
        )
        self.addCleanup(self.daemon.shutdown)
        return tnc

    def wait_for(self, check, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = check()
            if result:
                return result
            time.sleep(0.05)
        self.fail("timed out")

    def test_station_callsign_becomes_the_source(self):
        self.assertEqual(self.start().mycall, "N5BA")

    def test_status_reports_kiss(self):
        self.start()
        status = self.daemon.dispatch("status", {})
        self.assertEqual((status["tnc_model"], status["tnc_mode"], status["aprs_tx_supported"]),
                         ("KISS TNC", "kiss", True))

    def test_received_message_reaches_the_inbox(self):
        self.start([kiss_frame(ui("W5ABC", "APRS", ["KD5EOC-10"],
                                  b":N5BA     :hello over KISS{17", repeated=(0,)))])
        messages = self.wait_for(lambda: self.daemon.dispatch("aprs.messages", {})["messages"])
        self.assertEqual((messages[0]["source"], messages[0]["message"]),
                         ("W5ABC", "hello over KISS"))

    def test_positions_reach_the_station_list(self):
        self.start([kiss_frame(ui("K5LRK", "APRS", [], b"!3313.65N/09707.64W-Sanger"))])
        stations = self.wait_for(lambda: self.daemon.dispatch("stations.list", {}))
        items = stations if isinstance(stations, list) else stations.get("stations", [])
        self.assertIn("K5LRK", [s["callsign"] for s in items])

    def test_send_message(self):
        self.start()
        result = self.daemon.dispatch("aprs.send_message", {
            "addressee": "W5ABC", "message": "hi from KISS",
            "confirm_transmit": True, "confirm_vhf_port2_radio_ready": True,
            "confirm_hf_port1_disconnected": True,
        })
        self.assertTrue(result["submitted_to_tnc"])
        [(_, frame)] = frames_written(self.serial)
        self.assertEqual((frame.source, frame.destination, frame.digipeaters),
                         ("N5BA", "APRS", ("WIDE1-1", "WIDE2-1")))
        self.assertTrue(frame.info.startswith(b":W5ABC    :hi from KISS{"))

    def test_auto_ack(self):
        self.start()
        self.daemon.dispatch("aprs.ack_enable", {
            "confirm_transmit": True, "confirm_vhf_port2_radio_ready": True,
            "confirm_hf_port1_disconnected": True,
        })
        self.serial.push(kiss_frame(ui("W5ABC", "APRS", [], b":N5BA     :ack me{42")))
        acks = self.wait_for(lambda: [f for _, f in frames_written(self.serial)
                                      if f.info.startswith(b":W5ABC    :ack")])
        self.assertEqual(acks[0].info, b":W5ABC    :ack42")
        self.assertEqual(acks[0].source, "N5BA")

    def test_terminal_features_report_kiss(self):
        self.start()
        with self.assertRaises(TNCError) as ctx:
            self.daemon.dispatch("send_command", {"command": "MYCALL"})
        self.assertIn("KISS", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
