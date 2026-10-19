"""
Offline tests for "did it get out?" (hearing our own packets come back)
and phone notifications through ntfy.
"""

import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from contextlib import redirect_stderr
from unittest.mock import patch

import tncstation_daemon
from fakes import make_kam
from tncstation_daemon import KAMDaemon
from notify import Notifier, parse_events
from packet import Packet
from test_aprs_serial import APRSTransactionFake
from test_daemon import _Client
from test_rest import RestTestCase


_CONFIRM = {
    "confirm_transmit": True,
    "confirm_vhf_port2_radio_ready": True,
    "confirm_hf_port1_disconnected": True,
}
BEACON = {"latitude": 33.2275, "longitude": -97.1273, "comment": "N5BA KAM-XL"}
BEACON_PAYLOAD = "=3313.65N/09707.64W-N5BA KAM-XL"


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


class FakeOpener:
    """Stands in for urllib.request.urlopen."""

    def __init__(self, fail=None):
        self.requests = []
        self.fail = fail

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.fail:
            raise self.fail

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()

    def bodies(self):
        return [json.loads(r.data.decode()) for r in self.requests]


class NotifierTests(unittest.TestCase):

    def _notifier(self, opener=None, **kw):
        opener = opener or FakeOpener()
        n = Notifier("https://notify.example.net/", "aprs", opener=opener, **kw)
        self.addCleanup(n.stop)
        return n, opener

    def test_posts_json_with_auth_headers(self):
        n, op = self._notifier(token="tk", cf_client_id="id.access",
                               cf_client_secret="sekret",
                               click_url="http://laptop:8080/")
        self.assertTrue(n.notify("messages", "APRS from N5BA-7", "hi",
                                 tags=["incoming_envelope"], priority=4))
        self.assertTrue(_wait(lambda: n.status()["sent"] == 1))

        req = op.requests[0]
        self.assertEqual(req.full_url, "https://notify.example.net/")
        self.assertEqual(req.get_header("Authorization"), "Bearer tk")
        self.assertEqual(req.get_header("Cf-access-client-id"), "id.access")
        self.assertEqual(req.get_header("Cf-access-client-secret"), "sekret")
        body = op.bodies()[0]
        self.assertEqual(body, {
            "topic": "aprs", "title": "APRS from N5BA-7", "message": "hi",
            "priority": 4, "tags": ["incoming_envelope"],
            "click": "http://laptop:8080/",
        })

    def test_status_never_contains_secrets(self):
        n, _ = self._notifier(token="tk-secret", cf_client_id="id",
                              cf_client_secret="cf-secret")
        text = json.dumps(n.status())
        self.assertNotIn("tk-secret", text)
        self.assertNotIn("cf-secret", text)
        self.assertEqual(n.status()["auth"], "token+cloudflare")

    def test_event_filter_and_dedupe(self):
        n, op = self._notifier(events={"messages"})
        self.assertFalse(n.notify("bulletins", "t", "m"))
        self.assertTrue(n.notify("messages", "t", "m", dedupe_key=("N5BA-7", "12")))
        self.assertFalse(n.notify("messages", "t", "m", dedupe_key=("N5BA-7", "12")))
        self.assertTrue(n.notify("test", "t", "m"))  # tests always allowed
        self.assertTrue(_wait(lambda: n.status()["sent"] == 2))

    def test_failure_is_reported(self):
        err = urllib.error.HTTPError("https://x", 403, "Forbidden", {}, None)
        n, _ = self._notifier(opener=FakeOpener(fail=err))
        n.notify("messages", "t", "m")
        self.assertTrue(_wait(lambda: n.status()["failed"] == 1))
        self.assertIn("403", n.status()["last_error"])
        self.assertIn("Cloudflare", n.status()["last_error"])

    def test_validation(self):
        for kw in ({"url": "notify.example.net", "topic": "a"},
                   {"url": "https://x", "topic": "two words"},
                   {"url": "https://x", "topic": "a", "cf_client_id": "only-id"}):
            with self.assertRaises(ValueError, msg=kw):
                Notifier(opener=FakeOpener(), **kw)

    def test_parse_events(self):
        self.assertEqual(parse_events(None), {"messages", "watchdog", "backup"})
        self.assertEqual(parse_events("Messages, bulletins"), {"messages", "bulletins"})
        with self.assertRaises(ValueError):
            parse_events("messages,spam")


class _Stack:

    def start(self, notifier=None, beacon=BEACON):
        patcher = patch("aprs_tx.time")
        patcher.start()
        self.addCleanup(patcher.stop)
        serial = APRSTransactionFake()
        serial.exclusive = True
        sock = os.path.join(tempfile.mkdtemp(), "k.sock")
        daemon = KAMDaemon(make_kam(serial), sock, station_callsign="N5BA",
                           aprs_path="WIDE1-1,WIDE2-1", beacon=beacon,
                           notifier=notifier)
        t = threading.Thread(target=daemon.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 2)
        self.addCleanup(daemon.shutdown)
        _wait(lambda: os.path.exists(sock))
        client = _Client(sock)
        self.addCleanup(client.close)
        return serial, daemon, client


def _pkt(source, payload, digis=()):
    return Packet(source=source, destination="APRS", digipeaters=tuple(digis),
                  port=2, frame_type="UI", payload=payload, raw="")


class HeardBackTests(_Stack, unittest.TestCase):

    def test_message_digipeated_and_igated(self):
        serial, daemon, client = self.start()
        sent = client.call("aprs.send_message", addressee="N5BA-7",
                           message="hi", **_CONFIRM)["result"]
        payload, mid = sent["payload"], sent["message_id"]

        daemon._handle_packet(_pkt("N5BA", payload, ("KC5JMD-2", "W5NGU-3*", "WIDE2-1")))
        daemon._handle_packet(_pkt(
            "W5CGC-10", "}N5BA>APRS,TCPIP,W5CGC-10*:" + payload, ("WA0D-1*",)))

        rec = client.call("aprs.outbox")["result"]["messages"][0]
        self.assertEqual(rec["message_id"], mid)
        self.assertEqual(rec["digipeated_by"], "W5NGU-3")
        self.assertEqual(rec["igated_by"], "W5CGC-10")
        self.assertEqual(rec["status"], "pending")  # not an ack

    def test_own_packet_without_used_hop_ignored(self):
        serial, daemon, client = self.start()
        sent = client.call("aprs.send_message", addressee="N5BA-7",
                           message="hi", **_CONFIRM)["result"]
        daemon._handle_packet(_pkt("N5BA", sent["payload"], ("WIDE1-1", "WIDE2-1")))
        rec = client.call("aprs.outbox")["result"]["messages"][0]
        self.assertIsNone(rec["digipeated_by"])

    def test_someone_elses_copy_ignored(self):
        serial, daemon, client = self.start()
        sent = client.call("aprs.send_message", addressee="N5BA-7",
                           message="hi", **_CONFIRM)["result"]
        daemon._handle_packet(_pkt("K5XYZ", sent["payload"], ("W5NGU-3*",)))
        self.assertIsNone(client.call("aprs.outbox")["result"]["messages"][0]["digipeated_by"])

    def test_beacon_heard_back(self):
        serial, daemon, client = self.start()
        self.assertTrue(client.call("aprs.beacon_now", **_CONFIRM)["ok"])
        self.assertEqual(client.call("aprs.beacon_status")["result"]["heard_back"], {})

        daemon._handle_packet(_pkt("N5BA", BEACON_PAYLOAD, ("W5NGU-3*", "WIDE2-1")))

        heard = client.call("aprs.beacon_status")["result"]["heard_back"]
        self.assertEqual(heard["digipeated_by"], "W5NGU-3")
        self.assertIsNotNone(heard["digipeated_at"])

    def test_survives_restart_with_history(self):
        db = os.path.join(tempfile.mkdtemp(), "h.db")
        patcher = patch("aprs_tx.time")
        patcher.start()
        self.addCleanup(patcher.stop)

        def daemon_on(db):
            s = APRSTransactionFake(); s.exclusive = True
            sock = os.path.join(tempfile.mkdtemp(), "k.sock")
            d = KAMDaemon(make_kam(s), sock, station_callsign="N5BA", history_db=db)
            th = threading.Thread(target=d.serve_forever, daemon=True); th.start()
            _wait(lambda: os.path.exists(sock))
            return d, _Client(sock)

        d1, c1 = daemon_on(db)
        sent = c1.call("aprs.send_message", addressee="N5BA-7", message="hi",
                       **_CONFIRM)["result"]
        d1._handle_packet(_pkt("N5BA", sent["payload"], ("W5NGU-3*",)))
        c1.close(); d1.shutdown()

        d2, c2 = daemon_on(db)
        self.addCleanup(d2.shutdown); self.addCleanup(c2.close)
        rec = c2.call("aprs.outbox")["result"]["messages"][0]
        self.assertEqual(rec["digipeated_by"], "W5NGU-3")


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, event, title, message, **kw):
        self.calls.append((event, title, message, kw))
        return True

    def status(self):
        return {"enabled": True, "sent": 0, "failed": 0, "last_error": None}

    def stop(self):
        pass


class NotificationHookTests(_Stack, unittest.TestCase):

    def test_incoming_message_and_bulletin(self):
        rec = RecordingNotifier()
        serial, daemon, client = self.start(notifier=rec)

        daemon._handle_packet(_pkt("N5BA-7", ":N5BA     :Lunch at noon?{12"))
        daemon._handle_packet(_pkt("W5NGU-3", ":BLN1     :DCARA meeting Tue"))
        daemon._handle_packet(_pkt("N5BA-7", ":K5XYZ    :not for us{3"))
        daemon._handle_packet(_pkt("N5BA-7", ":N5BA     :ack12"))
        # Message to us that arrived through an iGate (third-party).
        daemon._handle_packet(_pkt(
            "W5CGC-10", "}KC5TIL>APRS,TCPIP,W5CGC-10*::N5BA     :Hi from IS{AB"))

        events = [(c[0], c[1], c[2]) for c in rec.calls]
        self.assertEqual(events, [
            ("messages", "APRS from N5BA-7", "Lunch at noon?"),
            ("bulletins", "APRS bulletin from W5NGU-3 (BLN1)", "DCARA meeting Tue"),
            ("messages", "APRS from KC5TIL", "Hi from IS"),
        ])
        self.assertEqual(rec.calls[0][3]["dedupe_key"], ("N5BA-7", "12"))

    def test_watchdog_alert_and_recovery(self):
        rec = RecordingNotifier()
        serial, daemon, client = self.start(notifier=rec)
        states = iter([
            {"state": "only_own", "message": "Only your own stations heard",
             "last_other_source": None},
            {"state": "ok", "message": None, "last_other_source": "W5NGU-3"},
        ])
        with patch.object(daemon, "_watchdog_snapshot", lambda now=None: next(states)):
            daemon._watchdog_tick()
            daemon._watchdog_tick()
        self.assertEqual([c[1] for c in rec.calls],
                         ["KAM-XL reception problem", "KAM-XL reception back"])

    def test_duplicate_copies_notify_once(self):
        op = FakeOpener()
        n = Notifier("https://x.example", "aprs", opener=op)
        serial, daemon, client = self.start(notifier=n)
        msg = ":N5BA     :Lunch at noon?{12"
        daemon._handle_packet(_pkt("N5BA-7", msg))
        daemon._handle_packet(_pkt("W5CGC-10", "}N5BA-7>APK005,TCPIP,W5CGC-10*:" + msg))
        self.assertTrue(_wait(lambda: n.status()["sent"] == 1))
        time.sleep(0.2)
        self.assertEqual(len(op.requests), 1)

    def test_notify_test_rpc(self):
        op = FakeOpener()
        n = Notifier("https://x.example", "aprs", opener=op)
        serial, daemon, client = self.start(notifier=n)
        result = client.call("notify.test", )["result"]
        self.assertTrue(result["delivered"])
        self.assertEqual(op.bodies()[0]["title"], "tncstation test notification")

    def test_notify_test_without_config(self):
        serial, daemon, client = self.start()
        self.assertFalse(client.call("notify.status")["result"]["enabled"])
        self.assertFalse(client.call("notify.test")["ok"])


class NotifyCommandLineTests(unittest.TestCase):

    def _exits(self, env):
        base = {"TNCSTATION_CALLSIGN": "N5BA"}
        base.update(env)
        with patch.dict(os.environ, base, clear=True), \
             patch.object(tncstation_daemon, "_load_dotenv"), \
             patch.object(tncstation_daemon, "KAMXL") as kamxl_cls, \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                tncstation_daemon.main(["--port", "/dev/null"])
        kamxl_cls.assert_not_called()

    def test_url_without_topic(self):
        self._exits({"TNCSTATION_NTFY_URL": "https://notify.example.net"})

    def test_bad_url_and_events(self):
        self._exits({"TNCSTATION_NTFY_URL": "notify.example.net", "TNCSTATION_NTFY_TOPIC": "a"})
        self._exits({"TNCSTATION_NTFY_URL": "https://x", "TNCSTATION_NTFY_TOPIC": "a",
                     "TNCSTATION_NTFY_EVENTS": "everything"})

    def test_half_cloudflare_pair(self):
        self._exits({"TNCSTATION_NTFY_URL": "https://x", "TNCSTATION_NTFY_TOPIC": "a",
                     "TNCSTATION_NTFY_CF_ACCESS_CLIENT_ID": "id-only"})


class NotifyRestTests(RestTestCase):

    def test_status_endpoint_disabled(self):
        _, port = self.start_stack(APRSTransactionFake())
        status, payload = self.request(port, "GET", "/notify")
        self.assertEqual(status, 200)
        self.assertFalse(payload["result"]["enabled"])


if __name__ == "__main__":
    unittest.main()
