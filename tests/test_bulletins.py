"""Offline tests for APRS bulletins and announcements."""

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from aprs import bulletin_kind, parse_aprs_message
from packet import Packet
from reception_history import ReceptionHistory
from test_aprs_serial import APRSTransactionFake
from test_daemon import DaemonTestCase
from test_rest import RestTestCase

import kamxl_web


def _p(source, payload):
    return Packet(source=source, destination="APRS", digipeaters=(), port=2,
                  frame_type="UI", payload=payload, raw="")


class BulletinKindTests(unittest.TestCase):

    def test_kinds(self):
        cases = {
            "BLN0": "bulletin", "BLN9": "bulletin",
            "BLNA": "announcement", "BLNZ": "announcement",
            "BLN1WX": "group", "BLN1HOTG": "group", "BLN3ARES": "group",
            "NWS-WARN": "nws", "NWS_ADVIS": "nws",
            "BLN": None, "BLNAB": None, "N5BA": None, "BLN1TOOLONG": None,
            "NWS": None, "ANSRVR": None,
        }
        for addressee, kind in cases.items():
            with self.subTest(addressee=addressee):
                self.assertEqual(bulletin_kind(addressee), kind)


class ParseBulletinTests(unittest.TestCase):

    def test_bulletin_has_no_id_and_keeps_braces(self):
        msg = parse_aprs_message(":BLN1     :Net 8pm {tonight}")
        self.assertEqual(msg.kind, "bulletin")
        self.assertEqual(msg.addressee, "BLN1")
        self.assertEqual(msg.text, "Net 8pm {tonight}")
        self.assertIsNone(msg.message_id)

    def test_wide_addressees_now_readable(self):
        self.assertEqual(parse_aprs_message(":BLN1HOTG :HOTG net").addressee, "BLN1HOTG")
        self.assertEqual(parse_aprs_message(":NWS-WARN :Warning").kind, "bulletin")

    def test_ordinary_messages_unchanged(self):
        msg = parse_aprs_message(":N5BA     :hello{12")
        self.assertEqual((msg.kind, msg.message_id), ("message", "12"))
        # Lowercase padded addressee is still rejected, as before.
        self.assertIsNone(parse_aprs_message(":n5ba     :hello"))


class HistoryBulletinTests(unittest.TestCase):

    def setUp(self):
        self.h = ReceptionHistory(os.path.join(tempfile.mkdtemp(), "h.db"))
        self.addCleanup(self.h.close)

    def test_grouped_latest_text_and_counts(self):
        self.h.record(_p("W5NGU-3", ":BLN1     :old text"), received_at=100)
        self.h.record(_p("W5NGU-3", ":BLN1     :new text"), received_at=200)
        self.h.record(_p("W5NGU-3", ":BLN2     :line two"), received_at=150)
        self.h.record(_p("K5DEN", ":BLNA     :Hamfest"), received_at=300)
        self.h.record(_p("N5BA-7", ":N5BA     :not a bulletin{1"), received_at=400)

        rows = self.h.list_bulletins()

        self.assertEqual([(r["source"], r["addressee"]) for r in rows],
                         [("K5DEN", "BLNA"), ("W5NGU-3", "BLN1"), ("W5NGU-3", "BLN2")])
        bln1 = rows[1]
        self.assertEqual(bln1["text"], "new text")
        self.assertEqual((bln1["first_heard"], bln1["last_heard"], bln1["count"]),
                         (100, 200, 2))
        self.assertEqual(rows[0]["kind"], "announcement")

    def test_since_filter(self):
        self.h.record(_p("W5NGU-3", ":BLN1     :old"), received_at=100)
        self.h.record(_p("K5DEN", ":BLN2     :recent"), received_at=1000)
        self.assertEqual([r["source"] for r in self.h.list_bulletins(since=500)], ["K5DEN"])

    def test_clear_and_prune_leave_messages(self):
        self.h.record(_p("W5NGU-3", ":BLN1     :old"), received_at=100)
        self.h.record(_p("W5NGU-3", ":BLN1     :new"), received_at=1000)
        self.h.record(_p("N5BA-7", ":N5BA     :keep me{1"), received_at=50)

        self.assertEqual(self.h.prune_bulletins(500), 1)
        self.assertEqual(self.h.list_bulletins()[0]["count"], 1)
        self.assertEqual(self.h.clear_bulletins(), 1)
        self.assertEqual(self.h.list_bulletins(), [])
        self.assertEqual(len(self.h.list_aprs_messages("N5BA")), 1)


class DaemonBulletinTests(DaemonTestCase):

    def test_bulletins_need_history(self):
        _, socket_path = self.start_daemon(APRSTransactionFake())
        client = self.connect(socket_path)
        result = client.call("aprs.bulletins")["result"]
        self.assertFalse(result["enabled"])

    def test_bulletin_is_never_auto_acked(self):
        serial = APRSTransactionFake()
        serial.exclusive = True
        daemon, socket_path = self.start_daemon(serial)
        client = self.connect(socket_path)
        with patch("aprs_tx.time"):
            client.call("aprs.ack_enable", confirm_transmit=True,
                        confirm_vhf_port2_radio_ready=True,
                        confirm_hf_port1_disconnected=True)
            before = list(serial.written)
            # Addressed to a bulletin, not to us: no ACK candidate.
            self.assertIsNone(daemon._aprs_ack_candidate(
                _p("W5NGU-3", ":BLN1     :Net tonight{AB")))
            daemon._handle_packet(_p("W5NGU-3", ":BLN1     :Net tonight{AB"))
            time.sleep(0.3)
        self.assertEqual(serial.written, before)


class RestBulletinTests(RestTestCase):

    def test_list_and_clear(self):
        daemon, port = self.start_stack(APRSTransactionFake(), history_db=True)
        daemon._handle_packet(_p("W5NGU-3", ":BLN1     :DCARA meeting Tue 7pm"))

        deadline = time.monotonic() + 5
        rows = []
        while time.monotonic() < deadline and not rows:
            rows = self.request(port, "GET", "/aprs/bulletins")[1]["result"]["bulletins"]
            time.sleep(0.05)
        self.assertEqual(rows[0]["text"], "DCARA meeting Tue 7pm")

        status, payload = self.request(port, "POST", "/aprs/bulletins/clear", {})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"]["deleted"], 1)

    def test_days_validation(self):
        _, port = self.start_stack(APRSTransactionFake())
        self.assertEqual(self.request(port, "GET", "/aprs/bulletins?days=0")[0], 400)
        self.assertEqual(self.request(port, "GET", "/aprs/bulletins?days=abc")[0], 400)

    def test_dashboard_has_card(self):
        import http.client
        _, port = self.start_stack(APRSTransactionFake())
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/?token=test-token")
        html = conn.getresponse().read().decode("utf-8")
        self.assertIn('id="bulletinList"', html)
        self.assertIn('"/aprs/bulletins?days="', html)


class BulletinLabelJsTests(unittest.TestCase):

    def test_labels(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        src = (kamxl_web.PAGES_DIR / "dashboard" / "app.js").read_text()
        m = re.search(r"  function bulletinLabel\(b\) \{(.*?)\n  \}\n", src, re.S)
        self.assertIsNotNone(m)
        harness = ('const assert = require("node:assert/strict");\n'
                   "function bulletinLabel(b) {" + m.group(1) + "\n}\n" + """
assert.equal(bulletinLabel({addressee: "BLN1", kind: "bulletin"}), "Bulletin 1");
assert.equal(bulletinLabel({addressee: "BLNA", kind: "announcement"}), "Announcement A");
assert.equal(bulletinLabel({addressee: "BLN1ARES", kind: "group"}), "ARES #1");
assert.equal(bulletinLabel({addressee: "NWS-WARN", kind: "nws"}), "NWS WARN");
""")
        r = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
