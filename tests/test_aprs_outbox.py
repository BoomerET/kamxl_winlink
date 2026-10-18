"""
Offline tests for APRS message acknowledgments on messages we send:
message-ID formatting/parsing (aprs.py), the OutboundAprsTracker
(aprs_outbox.py), and the daemon/REST wiring that matches incoming
ackID/rejID replies to sent messages.
"""

import http.client
import unittest
from unittest.mock import patch

from fakes import make_kam

from aprs import (
    format_aprs_message,
    parse_aprs_message,
    unwrap_third_party,
)
from aprs_outbox import OutboundAprsTracker
from aprs_tx import execute_aprs_tx_transaction
from packet import Packet
from test_aprs_serial import APRSTransactionFake
from test_daemon import DaemonTestCase
from test_rest import RestTestCase


def _packet(source, payload, port=2, frame_type="UI"):
    return Packet(
        source=source,
        destination="APRS",
        digipeaters=(),
        port=port,
        frame_type=frame_type,
        payload=payload,
        raw="",
    )


class _Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


# ---------------------------------------------------------------------------
# aprs.py
# ---------------------------------------------------------------------------

class MessageIdFormattingTests(unittest.TestCase):

    def test_message_id_appended(self):
        self.assertEqual(
            format_aprs_message("w1abc-7", "Hello", message_id="0A1F"),
            ":W1ABC-7  :Hello{0A1F",
        )

    def test_no_message_id_unchanged(self):
        self.assertEqual(
            format_aprs_message("W1ABC", "Hello"),
            ":W1ABC    :Hello",
        )

    def test_invalid_message_ids_rejected(self):
        for bad in ("", "TOOLONG", "a-b", "{1", 7):
            with self.assertRaises(ValueError, msg=repr(bad)):
                format_aprs_message("W1ABC", "Hi", message_id=bad)

    def test_brace_still_reserved_in_text(self):
        with self.assertRaises(ValueError):
            format_aprs_message("W1ABC", "Hi{1")

    def test_round_trip(self):
        payload = format_aprs_message("W1ABC", "Hi there", message_id="12")
        msg = parse_aprs_message(payload)
        self.assertEqual(msg.text, "Hi there")
        self.assertEqual(msg.message_id, "12")
        self.assertIsNone(msg.reply_ack)


class ReplyAckParsingTests(unittest.TestCase):

    def test_reply_ack_message(self):
        msg = parse_aprs_message(":N5BA     :Got it{AB}CD")
        self.assertEqual(msg.kind, "message")
        self.assertEqual(msg.text, "Got it")
        self.assertEqual(msg.message_id, "AB")
        self.assertEqual(msg.reply_ack, "CD")

    def test_reply_ack_capable_message_without_ack(self):
        msg = parse_aprs_message(":N5BA     :Hello{AB}")
        self.assertEqual(msg.message_id, "AB")
        self.assertIsNone(msg.reply_ack)

    def test_ack_with_reply_ack_suffix(self):
        msg = parse_aprs_message(":N5BA     :ack12}")
        self.assertEqual(msg.kind, "ack")
        self.assertEqual(msg.message_id, "12")

    def test_ack_with_trailing_space(self):
        msg = parse_aprs_message(":N5BA     :ack0A1F ")
        self.assertEqual(msg.kind, "ack")
        self.assertEqual(msg.message_id, "0A1F")

    def test_malformed_reply_ack_rejected(self):
        self.assertIsNone(parse_aprs_message(":N5BA     :Hi{AB}C-D"))


class ThirdPartyUnwrapTests(unittest.TestCase):

    def test_unwraps_igated_ack(self):
        self.assertEqual(
            unwrap_third_party(
                "K5IGT-10",
                "}ansrvr>APRS,TCPIP,K5IGT-10*::N5BA     :ack12",
            ),
            ("ANSRVR", ":N5BA     :ack12"),
        )

    def test_ordinary_packet_unchanged(self):
        self.assertEqual(
            unwrap_third_party("W1ABC", ":N5BA     :ack12"),
            ("W1ABC", ":N5BA     :ack12"),
        )

    def test_malformed_header_unchanged(self):
        self.assertEqual(
            unwrap_third_party("W1ABC", "}garbage"),
            ("W1ABC", "}garbage"),
        )


class TransactionAcceptsMessageIdTests(unittest.TestCase):

    def test_payload_with_id_is_canonical(self):
        serial = APRSTransactionFake()
        kam = make_kam(serial)
        payload = format_aprs_message("W1ABC", "Hello", message_id="0A1F")

        with patch("aprs_tx.time"):
            result = execute_aprs_tx_transaction(kam, payload)

        self.assertTrue(result["submitted_to_tnc"])
        self.assertEqual(
            serial.written.count(payload.encode("ascii") + b"\r"), 1
        )


# ---------------------------------------------------------------------------
# aprs_outbox.py
# ---------------------------------------------------------------------------

class OutboundTrackerTests(unittest.TestCase):

    def setUp(self):
        self.clock = _Clock()
        self.tracker = OutboundAprsTracker("n5ba", clock=self.clock)

    def _send(self, addressee="W1ABC-7", text="Hello"):
        message_id = self.tracker.allocate_id()
        payload = format_aprs_message(addressee, text, message_id=message_id)
        self.tracker.register(message_id, addressee, text, payload)
        return message_id

    def _ack(self, source, message_id, addressee="N5BA", kind="ack"):
        msg = parse_aprs_message(
            f":{addressee:<9}:{kind}{message_id}"
        )
        return self.tracker.observe(source, msg)

    def test_ids_are_unique_and_valid(self):
        ids = {self.tracker.allocate_id() for _ in range(500)}
        self.assertEqual(len(ids), 500)
        for message_id in ids:
            self.assertRegex(message_id, r"^[0-9A-Z]{4}$")

    def test_ids_seeded_from_clock(self):
        other = OutboundAprsTracker("N5BA", clock=_Clock(1_000_500.0))
        self.assertNotEqual(self.tracker.allocate_id(), other.allocate_id())

    def test_new_message_is_pending(self):
        message_id = self._send()
        record = self.tracker.get(message_id)
        self.assertEqual(record.status, "pending")
        self.assertEqual(record.attempts, 1)
        self.assertEqual(record.addressee, "W1ABC-7")

    def test_matching_ack_marks_acked(self):
        message_id = self._send()
        self.clock.now += 42

        updated = self._ack("w1abc-7", message_id)

        self.assertEqual(updated.status, "acked")
        self.assertEqual(updated.acked_by, "W1ABC-7")
        self.assertEqual(updated.acked_at, self.clock.now)
        self.assertEqual(self.tracker.get(message_id).status, "acked")

    def test_rej_marks_rejected(self):
        message_id = self._send()
        updated = self._ack("W1ABC-7", message_id, kind="rej")
        self.assertEqual(updated.status, "rejected")

    def test_reply_ack_marks_acked(self):
        message_id = self._send()
        msg = parse_aprs_message(":N5BA     :Sure thing{ZZ}" + message_id)
        updated = self.tracker.observe("W1ABC-7", msg)
        self.assertEqual(updated.status, "acked")

    def test_ack_from_wrong_station_ignored(self):
        message_id = self._send()
        self.assertIsNone(self._ack("K2XYZ", message_id))
        # Different SSID is a different station.
        self.assertIsNone(self._ack("W1ABC", message_id))
        self.assertEqual(self.tracker.get(message_id).status, "pending")

    def test_ack_to_other_station_ignored(self):
        message_id = self._send()
        self.assertIsNone(
            self._ack("W1ABC-7", message_id, addressee="K2XYZ")
        )
        self.assertEqual(self.tracker.get(message_id).status, "pending")

    def test_unknown_id_ignored(self):
        self._send()
        self.assertIsNone(self._ack("W1ABC-7", "ZZZZ"))

    def test_duplicate_ack_ignored(self):
        message_id = self._send()
        self.assertIsNotNone(self._ack("W1ABC-7", message_id))
        self.assertIsNone(self._ack("W1ABC-7", message_id))

    def test_plain_message_does_not_ack(self):
        message_id = self._send()
        msg = parse_aprs_message(f":N5BA     :hello{{{message_id}")
        self.assertIsNone(self.tracker.observe("W1ABC-7", msg))
        self.assertEqual(self.tracker.get(message_id).status, "pending")

    def test_resend_increments_attempts(self):
        message_id = self._send()
        record = self.tracker.get(message_id)
        self.clock.now += 60

        self.tracker.register(
            message_id, record.addressee, record.text, record.payload
        )

        record = self.tracker.get(message_id)
        self.assertEqual(record.attempts, 2)
        self.assertEqual(record.last_sent_at, self.clock.now)

    def test_list_is_newest_first(self):
        first = self._send(text="one")
        self.clock.now += 1
        second = self._send(text="two")

        ids = [m["message_id"] for m in self.tracker.list()]
        self.assertEqual(ids, [second, first])

    def test_trims_oldest(self):
        tracker = OutboundAprsTracker("N5BA", max_messages=3, clock=self.clock)
        ids = []
        for n in range(5):
            self.clock.now += 1
            message_id = tracker.allocate_id()
            tracker.register(message_id, "W1ABC", str(n), "x")
            ids.append(message_id)

        self.assertEqual(
            [m["message_id"] for m in tracker.list()],
            list(reversed(ids[2:])),
        )


# ---------------------------------------------------------------------------
# Daemon wiring
# ---------------------------------------------------------------------------

_CONFIRM = {
    "confirm_transmit": True,
    "confirm_vhf_port2_radio_ready": True,
    "confirm_hf_port1_disconnected": True,
}


def _ready_daemon(test, serial):
    """A daemon whose APRS session has been verified (offline only)."""
    serial.exclusive = True
    daemon, socket_path = test.start_daemon(serial)

    with daemon._kam_lock:
        daemon._aprs_session_uncertain = False
        daemon._aprs_safety_state = "unknown"

    return daemon, test.connect(socket_path)


class DaemonOutboundAckTests(DaemonTestCase):

    def setUp(self):
        patcher = patch("aprs_tx.time")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, client, **params):
        response = client.call("aprs.send_message", **_CONFIRM, **params)
        self.assertTrue(response["ok"], response)
        return response["result"]

    def test_send_appends_message_id(self):
        serial = APRSTransactionFake()
        daemon, client = _ready_daemon(self, serial)

        result = self._send(client, addressee="W1ABC-7", message="Hello")
        message_id = result["message_id"]

        self.assertEqual(
            result["payload"], f":W1ABC-7  :Hello{{{message_id}"
        )
        self.assertEqual(result["outbound"]["status"], "pending")
        self.assertIn(result["payload"].encode("ascii") + b"\r", serial.written)

    def test_incoming_ack_updates_outbox(self):
        serial = APRSTransactionFake()
        daemon, client = _ready_daemon(self, serial)
        message_id = self._send(
            client, addressee="W1ABC-7", message="Hello"
        )["message_id"]

        daemon._handle_packet(
            _packet("W1ABC-7", f":N5BA     :ack{message_id}")
        )

        outbox = client.call("aprs.outbox")["result"]
        self.assertEqual(outbox["station"], "N5BA")
        self.assertEqual(outbox["messages"][0]["status"], "acked")
        self.assertEqual(outbox["messages"][0]["acked_by"], "W1ABC-7")

    def test_third_party_ack_via_igate(self):
        serial = APRSTransactionFake()
        daemon, client = _ready_daemon(self, serial)
        message_id = self._send(
            client, addressee="ANSRVR", message="CQ HOTG hi"
        )["message_id"]

        daemon._handle_packet(_packet(
            "K5IGT-10",
            f"}}ANSRVR>APJIO4,TCPIP,K5IGT-10*::N5BA     :ack{message_id}",
        ))

        msg = client.call("aprs.outbox")["result"]["messages"][0]
        self.assertEqual(msg["status"], "acked")
        self.assertEqual(msg["acked_by"], "ANSRVR")

    def test_ack_on_hf_port_ignored(self):
        serial = APRSTransactionFake()
        daemon, client = _ready_daemon(self, serial)
        message_id = self._send(
            client, addressee="W1ABC-7", message="Hello"
        )["message_id"]

        daemon._handle_packet(
            _packet("W1ABC-7", f":N5BA     :ack{message_id}", port=1)
        )

        msg = client.call("aprs.outbox")["result"]["messages"][0]
        self.assertEqual(msg["status"], "pending")

    def test_ack_matching_never_writes_to_tnc(self):
        serial = APRSTransactionFake()
        daemon, client = _ready_daemon(self, serial)
        message_id = self._send(
            client, addressee="W1ABC-7", message="Hello"
        )["message_id"]
        written_before = list(serial.written)

        daemon._handle_packet(
            _packet("W1ABC-7", f":N5BA     :ack{message_id}")
        )
        client.call("aprs.outbox")

        self.assertEqual(serial.written, written_before)

    def test_resend_reuses_id(self):
        serial = APRSTransactionFake()
        daemon, client = _ready_daemon(self, serial)
        first = self._send(client, addressee="W1ABC-7", message="Hello")

        again = self._send(client, resend_id=first["message_id"])

        self.assertEqual(again["message_id"], first["message_id"])
        self.assertEqual(again["payload"], first["payload"])
        self.assertEqual(again["outbound"]["attempts"], 2)
        self.assertEqual(
            serial.written.count(first["payload"].encode("ascii") + b"\r"),
            2,
        )
        self.assertEqual(len(client.call("aprs.outbox")["result"]["messages"]), 1)

    def test_resend_refused_after_ack(self):
        serial = APRSTransactionFake()
        daemon, client = _ready_daemon(self, serial)
        message_id = self._send(
            client, addressee="W1ABC-7", message="Hello"
        )["message_id"]
        daemon._handle_packet(
            _packet("W1ABC-7", f":N5BA     :ack{message_id}")
        )
        written_before = list(serial.written)

        response = client.call(
            "aprs.send_message", resend_id=message_id, **_CONFIRM
        )

        self.assertFalse(response["ok"])
        self.assertIn("already acked", response["error"]["message"])
        self.assertEqual(serial.written, written_before)

    def test_resend_unknown_id_refused_without_tnc_access(self):
        serial = APRSTransactionFake()
        daemon, client = _ready_daemon(self, serial)

        response = client.call(
            "aprs.send_message", resend_id="ZZZZ", **_CONFIRM
        )

        self.assertFalse(response["ok"])
        self.assertEqual(serial.written, [])

    def test_resend_still_requires_confirmations(self):
        serial = APRSTransactionFake()
        daemon, client = _ready_daemon(self, serial)
        message_id = self._send(
            client, addressee="W1ABC-7", message="Hello"
        )["message_id"]
        written_before = list(serial.written)

        response = client.call("aprs.send_message", resend_id=message_id)

        self.assertFalse(response["ok"])
        self.assertEqual(serial.written, written_before)

    def test_send_auto_verifies_unverified_session(self):
        # Fresh daemon: session unverified (as after every restart).
        serial = APRSTransactionFake()
        serial.exclusive = True
        serial.mode = "convers"
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)
        self.assertTrue(daemon._aprs_session_uncertain)

        result = self._send(client, addressee="W1ABC-7", message="Hello")

        # Ctrl-C + STATUS verification came first, then the transaction.
        self.assertEqual(serial.written[:2], [b"\x03", b"STATUS\r"])
        self.assertIn(
            result["payload"].encode("ascii") + b"\r", serial.written
        )
        self.assertFalse(daemon._aprs_session_uncertain)

    def test_send_refused_when_stream_connected(self):
        class ConnectedFake(APRSTransactionFake):
            def write(self, data):
                if data == b"STATUS\r" and self.mode == "command":
                    self.written.append(data)
                    self._out.extend(
                        b"FREE BYTES 2685\r\n"
                        b"A/2  stream - CONNECTED to W1ABC-1\r\ncmd:"
                    )
                    return len(data)
                return super().write(data)

        serial = ConnectedFake()
        serial.exclusive = True
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)

        response = client.call(
            "aprs.send_message",
            addressee="W1ABC-7", message="Hello", **_CONFIRM
        )

        self.assertFalse(response["ok"])
        self.assertIn("Nothing was transmitted", response["error"]["message"])
        self.assertNotIn(b"CONVERS\r", serial.written)
        self.assertEqual(daemon._aprs_safety_state, "unsafe")
        self.assertEqual(client.call("aprs.outbox")["result"]["messages"], [])

    def test_failed_send_not_tracked(self):
        serial = APRSTransactionFake()
        serial.fail_command = "PACLEN"
        daemon, client = _ready_daemon(self, serial)

        response = client.call(
            "aprs.send_message",
            addressee="W1ABC-7", message="Hello", **_CONFIRM
        )

        self.assertFalse(response["ok"])
        self.assertEqual(client.call("aprs.outbox")["result"]["messages"], [])

    def test_ack_event_broadcast_to_subscribers(self):
        serial = APRSTransactionFake()
        daemon, client = _ready_daemon(self, serial)
        message_id = self._send(
            client, addressee="W1ABC-7", message="Hello"
        )["message_id"]

        listener = self.connect(daemon.socket_path)
        listener.call("monitor.subscribe")

        daemon._handle_packet(
            _packet("W1ABC-7", f":N5BA     :ack{message_id}")
        )

        events = []
        while True:
            event = listener.read_event()
            events.append(event.get("event"))
            if event.get("event") == "aprs_outbound":
                self.assertEqual(event["data"]["status"], "acked")
                break
            self.assertLess(len(events), 5, events)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

class RestOutboxTests(RestTestCase):

    def test_outbox_empty(self):
        _, port = self.start_stack(APRSTransactionFake())

        status, payload = self.request(port, "GET", "/aprs/outbox")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["messages"], [])

    def test_outbox_requires_auth(self):
        _, port = self.start_stack(APRSTransactionFake())

        status, _ = self.request(port, "GET", "/aprs/outbox", token=None)

        self.assertEqual(status, 401)

    def test_send_then_ack_via_rest(self):
        serial = APRSTransactionFake()
        serial.exclusive = True
        daemon, port = self.start_stack(serial)

        with daemon._kam_lock:
            daemon._aprs_session_uncertain = False
            daemon._aprs_safety_state = "unknown"

        with patch("aprs_tx.time"):
            status, payload = self.request(port, "POST", "/aprs/send", {
                "addressee": "W1ABC-7", "message": "Hello", "path": "",
                **_CONFIRM,
            })

        self.assertEqual(status, 200, payload)
        message_id = payload["result"]["message_id"]

        daemon._handle_packet(
            _packet("W1ABC-7", f":N5BA     :ack{message_id}")
        )

        _, payload = self.request(port, "GET", "/aprs/outbox")
        self.assertEqual(payload["result"]["messages"][0]["status"], "acked")

    def test_resend_without_addressee_accepted_by_rest_layer(self):
        _, port = self.start_stack(APRSTransactionFake())

        status, payload = self.request(port, "POST", "/aprs/send", {
            "resend_id": "ZZZZ", **_CONFIRM,
        })

        # Reaches the daemon (which refuses the unknown ID) rather than
        # failing REST validation for a missing addressee/message.
        self.assertNotEqual(payload["error"]["type"], "MissingParam")
        self.assertIn("ZZZZ", payload["error"]["message"])

    def test_resend_id_must_be_string(self):
        _, port = self.start_stack(APRSTransactionFake())

        status, payload = self.request(port, "POST", "/aprs/send", {
            "resend_id": 5, **_CONFIRM,
        })

        self.assertEqual(status, 400)

    def test_dashboard_has_sent_messages_panel(self):
        _, port = self.start_stack(APRSTransactionFake())

        conn = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=5
        )
        self.addCleanup(conn.close)
        conn.request("GET", "/?token=test-token")
        html = conn.getresponse().read().decode("utf-8")

        self.assertIn('id="aprsOutboxMessages"', html)
        self.assertIn('apiGet("/aprs/outbox")', html)
        self.assertIn("resend_id", html)


if __name__ == "__main__":
    unittest.main()
