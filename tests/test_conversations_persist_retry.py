"""
Offline tests for sent-message persistence, automatic resend of
unacknowledged messages, and the dashboard conversation view.
"""

import io
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import kamxl_daemon
from aprs import parse_aprs_message
from aprs_outbox import OutboundAprsTracker
from fakes import make_kam
from kamxl_daemon import KAMDaemon
from packet import Packet
from reception_history import ReceptionHistory
from test_aprs_serial import APRSTransactionFake
from test_daemon import _Client
from test_rest import RestTestCase


_CONFIRM = {
    "confirm_transmit": True,
    "confirm_vhf_port2_radio_ready": True,
    "confirm_hf_port1_disconnected": True,
}


class _Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def _ack(source, message_id):
    return Packet(
        source=source, destination="APK005", digipeaters=(), port=2,
        frame_type="UI", payload=f":N5BA     :ack{message_id}", raw="",
    )


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class TrackerRetryTests(unittest.TestCase):

    def setUp(self):
        self.clock = _Clock()
        self.changes = []
        self.tracker = OutboundAprsTracker(
            "N5BA", clock=self.clock, on_change=self.changes.append
        )

    def _send(self):
        mid = self.tracker.allocate_id()
        self.tracker.register(mid, "W1ABC", "hi", "p", "WIDE1-1,WIDE2-1")
        return mid

    def test_register_reports_change_with_path(self):
        mid = self._send()
        self.assertEqual(self.changes[-1]["message_id"], mid)
        self.assertEqual(self.changes[-1]["path"], "WIDE1-1,WIDE2-1")

    def test_schedule_backoff_and_give_up(self):
        mid = self._send()
        self.tracker.schedule_retries(mid, count=2, interval=60)

        self.assertEqual(self.tracker.due_retries(), [])
        self.clock.now += 60
        self.assertEqual([r.message_id for r in self.tracker.due_retries()], [mid])

        # First resend: attempts 2, next wait doubles to 120 s.
        self.tracker.register(mid, "W1ABC", "hi", "p")
        rec = self.tracker.retry_sent(mid, 60)
        self.assertEqual((rec.attempts, rec.retries_left), (2, 1))
        self.assertEqual(rec.next_retry_at, self.clock.now + 120)

        self.clock.now += 120
        self.tracker.register(mid, "W1ABC", "hi", "p")
        rec = self.tracker.retry_sent(mid, 60)
        self.assertEqual((rec.attempts, rec.retries_left), (3, 0))
        self.assertEqual(rec.next_retry_at, self.clock.now + 240)

        # No more resends due; after the last wait it gives up.
        self.clock.now += 240
        self.assertEqual(self.tracker.due_retries(), [])
        expired = self.tracker.expire_retries()
        self.assertEqual([r.message_id for r in expired], [mid])
        rec = self.tracker.get(mid)
        self.assertTrue(rec.gave_up)
        self.assertEqual(rec.status, "pending")  # late ack still welcome

    def test_ack_cancels_retries_even_after_giving_up(self):
        mid = self._send()
        self.tracker.schedule_retries(mid, 3, 60)
        self.tracker.observe("W1ABC", parse_aprs_message(f":N5BA     :ack{mid}"))

        rec = self.tracker.get(mid)
        self.assertEqual(rec.status, "acked")
        self.assertIsNone(rec.next_retry_at)
        self.assertEqual(rec.retries_left, 0)
        self.clock.now += 9999
        self.assertEqual(self.tracker.due_retries(), [])

    def test_load_drops_schedule_and_continues_ids(self):
        mid = self._send()
        self.tracker.schedule_retries(mid, 3, 60)
        saved = self.tracker.list()

        fresh = OutboundAprsTracker("N5BA", clock=_Clock(5.0))
        self.assertEqual(fresh.load(saved), 1)
        rec = fresh.get(mid)
        self.assertIsNone(rec.next_retry_at)
        self.assertEqual(rec.retries_left, 0)
        self.assertNotEqual(fresh.allocate_id(), mid)

        # A late ack after the restart still matches.
        fresh.observe("W1ABC", parse_aprs_message(f":N5BA     :ack{mid}"))
        self.assertEqual(fresh.get(mid).status, "acked")

    def test_clear(self):
        self._send()
        self.assertEqual(self.tracker.clear(), 1)
        self.assertEqual(self.tracker.list(), [])


class HistoryOutboundTests(unittest.TestCase):

    def test_upsert_list_clear(self):
        h = ReceptionHistory(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(h.close)
        h.save_outbound({"message_id": "A1", "sent_at": 2.0, "status": "pending"})
        h.save_outbound({"message_id": "B2", "sent_at": 3.0, "status": "pending"})
        h.save_outbound({"message_id": "A1", "sent_at": 2.0, "status": "acked"})

        rows = h.list_outbound()
        self.assertEqual([r["message_id"] for r in rows], ["B2", "A1"])
        self.assertEqual(rows[1]["status"], "acked")
        self.assertEqual(h.clear_outbound(), 2)
        self.assertEqual(h.list_outbound(), [])


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class _Stack:

    def start(self, history_db=None, serial=None, **kwargs):
        patcher = patch("aprs_tx.time")
        patcher.start()
        self.addCleanup(patcher.stop)

        serial = serial or APRSTransactionFake()
        serial.exclusive = True
        socket_path = os.path.join(tempfile.mkdtemp(), "k.sock")
        daemon = KAMDaemon(
            make_kam(serial), socket_path, history_db=history_db,
            station_callsign="N5BA", **kwargs,
        )
        thread = threading.Thread(target=daemon.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(daemon.shutdown)

        deadline = time.monotonic() + 2
        while not os.path.exists(socket_path) and time.monotonic() < deadline:
            time.sleep(0.01)
        client = _Client(socket_path)
        self.addCleanup(client.close)
        return serial, daemon, client


class PersistenceTests(_Stack, unittest.TestCase):

    def test_sent_messages_survive_restart(self):
        db = os.path.join(tempfile.mkdtemp(), "h.db")

        serial, daemon, client = self.start(history_db=db)
        sent = client.call(
            "aprs.send_message", addressee="N5BA-7", message="hi", **_CONFIRM
        )["result"]
        mid = sent["message_id"]
        daemon.shutdown()

        serial2, daemon2, client2 = self.start(history_db=db)
        outbox = client2.call("aprs.outbox")["result"]
        self.assertTrue(outbox["persistent"])
        self.assertEqual([m["message_id"] for m in outbox["messages"]], [mid])

        # Ack heard after the restart is matched and saved.
        daemon2._handle_packet(_ack("N5BA-7", mid))
        self.assertEqual(
            client2.call("aprs.outbox")["result"]["messages"][0]["status"],
            "acked",
        )
        h = ReceptionHistory(db)
        self.addCleanup(h.close)
        self.assertEqual(h.list_outbound()[0]["status"], "acked")

        # New IDs don't collide with the restored one.
        again = client2.call(
            "aprs.send_message", addressee="N5BA-7", message="2", **_CONFIRM
        )["result"]
        self.assertNotEqual(again["message_id"], mid)

    def test_clear_outbox(self):
        db = os.path.join(tempfile.mkdtemp(), "h.db")
        serial, daemon, client = self.start(history_db=db)
        client.call(
            "aprs.send_message", addressee="N5BA-7", message="hi", **_CONFIRM
        )

        self.assertEqual(client.call("aprs.clear_outbox")["result"]["deleted"], 1)
        self.assertEqual(client.call("aprs.outbox")["result"]["messages"], [])
        h = ReceptionHistory(db)
        self.addCleanup(h.close)
        self.assertEqual(h.list_outbound(), [])

    def test_without_history_db_not_persistent(self):
        serial, daemon, client = self.start()
        self.assertFalse(client.call("aprs.outbox")["result"]["persistent"])


class AutoRetryTests(_Stack, unittest.TestCase):

    def _send(self, client):
        return client.call(
            "aprs.send_message", addressee="N5BA-7", message="hi", **_CONFIRM
        )["result"]

    def test_off_by_default_no_schedule(self):
        serial, daemon, client = self.start()
        sent = self._send(client)
        self.assertIsNone(sent["outbound"]["next_retry_at"])
        self.assertFalse(client.call("aprs.retry_status")["result"]["enabled"])

    def test_enable_requires_confirmations(self):
        serial, daemon, client = self.start()
        self.assertFalse(client.call("aprs.retry_enable")["ok"])

    def test_due_resend_is_transmitted_with_same_id(self):
        serial, daemon, client = self.start()
        client.call("aprs.retry_enable", **_CONFIRM)
        sent = self._send(client)
        self.assertEqual(sent["outbound"]["retries_left"], 3)
        payload = sent["payload"].encode() + b"\r"

        daemon._retry_tick(now=sent["outbound"]["next_retry_at"] + 1)

        self.assertEqual(serial.written.count(payload), 2)
        rec = daemon._outbox.get(sent["message_id"])
        self.assertEqual(rec.attempts, 2)
        self.assertEqual(rec.retries_left, 2)
        self.assertEqual(
            client.call("aprs.retry_status")["result"]["retries_sent"], 1
        )

    def test_ack_stops_resends(self):
        serial, daemon, client = self.start()
        client.call("aprs.retry_enable", **_CONFIRM)
        sent = self._send(client)
        daemon._handle_packet(_ack("N5BA-7", sent["message_id"]))

        daemon._retry_tick(now=time.time() + 3600)

        self.assertEqual(
            serial.written.count(sent["payload"].encode() + b"\r"), 1
        )

    def test_disabled_retry_does_not_send(self):
        serial, daemon, client = self.start()
        client.call("aprs.retry_enable", **_CONFIRM)
        sent = self._send(client)
        client.call("aprs.retry_disable")

        daemon._retry_tick(now=time.time() + 3600)

        self.assertEqual(
            serial.written.count(sent["payload"].encode() + b"\r"), 1
        )

    def test_paused_after_terminal_use(self):
        serial, daemon, client = self.start()
        client.call("aprs.retry_enable", **_CONFIRM)
        sent = self._send(client)
        client.call("send_command", command="STATUS")
        before = len(serial.written)

        daemon._retry_tick(now=time.time() + 3600)

        self.assertEqual(serial.written[before:], [])
        status = client.call("aprs.retry_status")["result"]
        self.assertTrue(status["enabled"])
        self.assertTrue(status["paused"])

    def test_failed_resend_turns_auto_resend_off(self):
        serial, daemon, client = self.start()
        client.call("aprs.retry_enable", **_CONFIRM)
        sent = self._send(client)
        serial.fail_command = "PACLEN"

        daemon._retry_tick(now=time.time() + 3600)

        status = client.call("aprs.retry_status")["result"]
        self.assertFalse(status["enabled"])
        self.assertIn("failed", status["disabled_reason"])

    def test_restart_never_resends_old_messages(self):
        db = os.path.join(tempfile.mkdtemp(), "h.db")
        serial, daemon, client = self.start(history_db=db)
        client.call("aprs.retry_enable", **_CONFIRM)
        sent = self._send(client)
        daemon.shutdown()

        serial2, daemon2, client2 = self.start(history_db=db)
        client2.call("aprs.retry_enable", **_CONFIRM)
        daemon2._retry_tick(now=time.time() + 3600)

        self.assertNotIn(sent["payload"].encode() + b"\r", serial2.written)

    def test_settings_validation(self):
        kam = make_kam(APRSTransactionFake())
        with self.assertRaises(ValueError):
            KAMDaemon(kam, "/tmp/x.sock", retry_count=0)
        with self.assertRaises(ValueError):
            KAMDaemon(kam, "/tmp/x.sock", retry_interval_seconds=10)


class RetryCommandLineTests(unittest.TestCase):

    def test_bad_retry_settings_rejected_before_serial(self):
        for argv in (["--retry-count", "9"], ["--retry-interval", "5"]):
            with patch.dict(os.environ, {"KAMXL_CALLSIGN": "N5BA"}, clear=True), \
                 patch.object(kamxl_daemon, "_load_dotenv"), \
                 patch.object(kamxl_daemon, "KAMXL") as kamxl_cls, \
                 redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    kamxl_daemon.main(["--port", "/dev/null"] + argv)
            kamxl_cls.assert_not_called()


# ---------------------------------------------------------------------------
# REST + dashboard
# ---------------------------------------------------------------------------

class RestConversationTests(RestTestCase):

    def test_retry_endpoints(self):
        serial = APRSTransactionFake()
        serial.exclusive = True
        _, port = self.start_stack(serial)

        self.assertFalse(self.request(port, "GET", "/aprs/retry")[1]["result"]["enabled"])
        self.assertEqual(
            self.request(port, "POST", "/aprs/retry", {"enabled": "yes"})[0], 400
        )
        status, payload = self.request(port, "POST", "/aprs/retry", {"enabled": True})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["result"]["enabled"])
        status, payload = self.request(port, "POST", "/aprs/retry", {"enabled": False})
        self.assertFalse(payload["result"]["enabled"])

    def test_outbox_clear_endpoint(self):
        _, port = self.start_stack(APRSTransactionFake())
        status, payload = self.request(port, "POST", "/aprs/outbox/clear", {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["deleted"], 0)

    def test_dashboard_has_conversation_view(self):
        import http.client
        _, port = self.start_stack(APRSTransactionFake())
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/?token=test-token")
        html = conn.getresponse().read().decode("utf-8")

        for needle in ('id="convList"', 'id="convReplyBtn"', 'id="aprsAutoRetry"',
                       'id="convAlertsBtn"', '"/aprs/outbox/clear"', "Notification"):
            self.assertIn(needle, html)

    def test_thread_building_in_node(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")

        import re
        from kamxl_rest import DASHBOARD_HTML

        match = re.search(
            r"  function buildThreads\(\) \{(.*?)\n  \}\n", DASHBOARD_HTML, re.S
        )
        self.assertIsNotNone(match)
        harness = """
const assert = require("node:assert/strict");
var readUpTo = { "N5BA-7": 150 };
var convState = {
  received: [
    { source: "N5BA-7", received_at: 100, message: "old" },
    { source: "N5BA-7", received_at: 200, message: "new" },
    { source: "KC5TIL-3", received_at: 50, message: "hi" }
  ],
  sent: [
    { addressee: "N5BA-7", sent_at: 150, text: "mine" },
    { addressee: "W5XYZ", sent_at: 300, text: "only sent" }
  ]
};
function buildThreads() {""" + match.group(1) + """
}
var t = buildThreads();
assert.deepEqual(t.map(x => x.call), ["W5XYZ", "N5BA-7", "KC5TIL-3"]);
var n5ba = t[1];
assert.deepEqual(n5ba.items.map(i => i.dir), ["in", "out", "in"]);
assert.equal(n5ba.unread, 1);
assert.equal(t[2].unread, 1);
assert.equal(t[0].unread, 0);
"""
        result = subprocess.run([node, "-e", harness], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
