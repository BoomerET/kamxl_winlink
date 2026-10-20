"""
Offline tests for the dashboard-facing automatic-ACK controls and
clearing the received-messages inbox.
"""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from packet import Packet
from reception_history import ReceptionHistory
from test_aprs_serial import APRSTransactionFake
from test_daemon import DaemonTestCase
from test_rest import RestTestCase


_CONFIRM = {
    "confirm_transmit": True,
    "confirm_vhf_port2_radio_ready": True,
    "confirm_hf_port1_disconnected": True,
}


def _message(source="N5BA-7", text="Hello", message_id="12", port=2):
    return Packet(
        source=source,
        destination="APDR16",
        digipeaters=(),
        port=port,
        frame_type="UI",
        payload=f":N5BA     :{text}{{{message_id}",
        raw="",
    )


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class AutoAckDaemonTests(DaemonTestCase):

    def setUp(self):
        patcher = patch("aprs_tx.time")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _daemon(self):
        serial = APRSTransactionFake()
        serial.exclusive = True
        daemon, socket_path = self.start_daemon(serial)
        return serial, daemon, self.connect(socket_path)

    def test_enable_verifies_unverified_session_itself(self):
        serial, daemon, client = self._daemon()
        self.assertTrue(daemon._aprs_session_uncertain)

        response = client.call("aprs.ack_enable", **_CONFIRM)

        self.assertTrue(response["ok"], response)
        self.assertEqual(serial.written[:2], [b"\x03", b"STATUS\r"])
        status = client.call("aprs.ack_status")["result"]
        self.assertTrue(status["enabled"])
        self.assertFalse(status["paused"])

    def test_incoming_message_is_acked(self):
        serial, daemon, client = self._daemon()
        client.call("aprs.ack_enable", **_CONFIRM)

        daemon._handle_packet(_message(message_id="12"))

        ack = b":N5BA-7   :ack12\r"
        self.assertTrue(_wait_for(lambda: ack in serial.written))
        self.assertTrue(_wait_for(
            lambda: client.call("aprs.ack_status")["result"]["acks_sent"] == 1
        ))
        status = client.call("aprs.ack_status")["result"]
        self.assertEqual(status["last_ack"]["to"], "N5BA-7")
        self.assertEqual(status["last_ack"]["message_id"], "12")

    def test_ack_after_config_change_reverifies(self):
        # A plain setting change leaves no link open, so the worker
        # re-verifies (Ctrl-C + STATUS) and sends the ACK.
        serial, daemon, client = self._daemon()
        client.call("aprs.ack_enable", **_CONFIRM)
        client.call("set", command="BTEXT", value="hi")
        self.assertTrue(daemon._aprs_session_uncertain)

        daemon._handle_packet(_message(message_id="34"))

        self.assertTrue(_wait_for(
            lambda: b":N5BA-7   :ack34\r" in serial.written
        ))

    def test_ack_paused_after_terminal_use(self):
        serial, daemon, client = self._daemon()
        client.call("aprs.ack_enable", **_CONFIRM)
        client.call("send_command", command="CONNECT W1ABC")

        status = client.call("aprs.ack_status")["result"]
        self.assertTrue(status["enabled"])
        self.assertTrue(status["paused"])
        self.assertIn("session", status["paused_reason"])

        written_before = len(serial.written)
        daemon._handle_packet(_message(message_id="56"))
        self.assertTrue(_wait_for(daemon._aprs_ack_queue.empty))
        time.sleep(0.2)

        # No Ctrl-C into the possible live session, no ACK, and
        # auto-ACK stays on (paused, not revoked).
        self.assertEqual(serial.written[written_before:], [])
        self.assertTrue(client.call("aprs.ack_status")["result"]["enabled"])

    def test_manual_send_resumes_paused_auto_ack(self):
        serial, daemon, client = self._daemon()
        client.call("aprs.ack_enable", **_CONFIRM)
        client.call("send_command", command="STATUS")
        self.assertTrue(client.call("aprs.ack_status")["result"]["paused"])

        response = client.call(
            "aprs.send_message", addressee="N5BA-7", message="hi", **_CONFIRM
        )
        self.assertTrue(response["ok"], response)

        self.assertFalse(client.call("aprs.ack_status")["result"]["paused"])

    def test_disable_reports_reason(self):
        serial, daemon, client = self._daemon()
        client.call("aprs.ack_enable", **_CONFIRM)

        client.call("aprs.ack_disable")

        status = client.call("aprs.ack_status")["result"]
        self.assertFalse(status["enabled"])
        self.assertEqual(status["disabled_reason"], "Turned off by operator")


class ClearInboxHistoryTests(unittest.TestCase):

    def test_clear_only_deletes_matching_addressee(self):
        path = os.path.join(tempfile.mkdtemp(), "h.sqlite3")
        history = ReceptionHistory(path)
        self.addCleanup(history.close)

        history.record(_message(text="one", message_id="1"))
        history.record(_message(text="two", message_id="2"))
        history.record(Packet(
            source="W1ABC", destination="APRS", digipeaters=(), port=2,
            frame_type="UI", payload=":K2XYZ    :not ours{9", raw="",
        ))

        self.assertEqual(history.clear_aprs_messages("n5ba"), 2)
        self.assertEqual(history.list_aprs_messages("N5BA"), [])
        self.assertEqual(len(history.list_aprs_messages("K2XYZ")), 1)
        # Packet counts/history are untouched.
        self.assertEqual(history.total_packets(), 3)


class MessageCopyTests(unittest.TestCase):
    """A resend (or a second path) is one message in the inbox."""

    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(), "h.sqlite3")
        self.history = ReceptionHistory(path)
        self.addCleanup(self.history.close)

    def test_resends_merge_into_the_first_copy(self):
        # Seen on the air: the KAM-XL resent #6CSQ a minute later.
        first = 1_760_000_000
        self.history.record(_message(text="Testing OpenTNC", message_id="6CSQ"),
                            received_at=first)
        self.history.record(_message(text="Testing OpenTNC", message_id="6CSQ"),
                            received_at=first + 63)

        (message,) = self.history.list_aprs_messages("N5BA")

        self.assertEqual(message["received_at"], first)
        self.assertEqual(message["copies"], 2)
        self.assertEqual(message["last_received_at"], first + 63)

    def test_different_messages_stay_apart(self):
        t = 1_760_000_000
        self.history.record(_message(text="one", message_id="1"), received_at=t)
        self.history.record(_message(text="two", message_id="2"), received_at=t + 5)
        # Same text, different ID: a new message.
        self.history.record(_message(text="one", message_id="3"), received_at=t + 9)

        messages = self.history.list_aprs_messages("N5BA")

        self.assertEqual([m["message"] for m in messages], ["one", "two", "one"])
        self.assertTrue(all(m["copies"] == 1 for m in messages))

    def test_reused_id_much_later_is_a_new_message(self):
        t = 1_760_000_000
        self.history.record(_message(text="ok", message_id="1"), received_at=t)
        self.history.record(_message(text="ok", message_id="1"),
                            received_at=t + 86400)

        self.assertEqual(len(self.history.list_aprs_messages("N5BA")), 2)

    def test_a_long_retry_chain_stays_one_message(self):
        # Each copy within the window of the previous one.
        t = 1_760_000_000
        for offset in (0, 1800, 3600, 5400):
            self.history.record(_message(text="hello", message_id="7"),
                                received_at=t + offset)

        (message,) = self.history.list_aprs_messages("N5BA")
        self.assertEqual(message["copies"], 4)

    def test_limit_counts_messages_not_copies(self):
        t = 1_760_000_000
        for n in range(3):
            for copy in range(3):
                self.history.record(
                    _message(text=f"msg {n}", message_id=str(n)),
                    received_at=t + n * 10 + copy,
                )

        messages = self.history.list_aprs_messages("N5BA", limit=2)

        self.assertEqual([m["message"] for m in messages], ["msg 2", "msg 1"])
        self.assertEqual([m["copies"] for m in messages], [3, 3])


class RestAutoAckAndInboxTests(RestTestCase):

    def test_ack_status_endpoint(self):
        _, port = self.start_stack(APRSTransactionFake())

        status, payload = self.request(port, "GET", "/aprs/ack")

        self.assertEqual(status, 200)
        self.assertFalse(payload["result"]["enabled"])

    def test_ack_toggle_requires_boolean(self):
        _, port = self.start_stack(APRSTransactionFake())

        status, _ = self.request(port, "POST", "/aprs/ack", {"enabled": "yes"})

        self.assertEqual(status, 400)

    def test_ack_toggle_on_and_off(self):
        serial = APRSTransactionFake()
        serial.exclusive = True
        _, port = self.start_stack(serial)

        status, payload = self.request(
            port, "POST", "/aprs/ack", {"enabled": True}
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(
            self.request(port, "GET", "/aprs/ack")[1]["result"]["enabled"]
        )

        status, payload = self.request(
            port, "POST", "/aprs/ack", {"enabled": False}
        )
        self.assertEqual(status, 200, payload)
        self.assertFalse(
            self.request(port, "GET", "/aprs/ack")[1]["result"]["enabled"]
        )

    def test_clear_inbox(self):
        daemon, port = self.start_stack(APRSTransactionFake(), history_db=True)
        daemon._handle_packet(_message(text="hello", message_id="1"))

        self.assertTrue(_wait_for(lambda: len(
            self.request(port, "GET", "/aprs/messages")[1]["result"]["messages"]
        ) == 1))

        status, payload = self.request(port, "POST", "/aprs/messages/clear", {})

        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["deleted"], 1)
        _, payload = self.request(port, "GET", "/aprs/messages")
        self.assertEqual(payload["result"]["messages"], [])

    def test_clear_inbox_without_history_db(self):
        _, port = self.start_stack(APRSTransactionFake())

        status, payload = self.request(port, "POST", "/aprs/messages/clear", {})

        self.assertEqual(status, 200)
        self.assertFalse(payload["result"]["enabled"])

    def test_dashboard_has_controls(self):
        import http.client
        _, port = self.start_stack(APRSTransactionFake())
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/?token=test-token")
        html = conn.getresponse().read().decode("utf-8")

        self.assertIn('id="aprsAutoAck"', html)
        self.assertIn('id="aprsClearInbox"', html)
        self.assertIn('"/aprs/messages/clear"', html)


if __name__ == "__main__":
    unittest.main()
