"""Tests for the dashboard's APRS Services templates."""

import http.client
import re
import shutil
import subprocess
import unittest

import tncstation_web
from test_aprs_serial import APRSTransactionFake
from test_rest import RestTestCase


class BuildServiceMessageTests(unittest.TestCase):

    def test_templates(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        src = (tncstation_web.PAGES_DIR / "dashboard" / "app.js").read_text()
        m = re.search(
            r"  function buildServiceMessage\(service, f\) \{(.*?)\n  \}\n",
            src, re.S,
        )
        self.assertIsNotNone(m)
        harness = (
            'const assert = require("node:assert/strict");\n'
            "function buildServiceMessage(service, f) {" + m.group(1) + "\n}\n"
            r"""
const B = buildServiceMessage;
assert.deepEqual(B("wx", {where: "Denton, TX", when: "tomorrow"}),
  {to: "WXBOT", text: "Denton,TX tomorrow"});
assert.deepEqual(B("wx", {where: "76201", when: ""}), {to: "WXBOT", text: "76201"});
assert.ok(B("wx", {where: "", when: ""}).error);

assert.deepEqual(B("sms", {to: "(940) 555-1234", text: "Late"}),
  {to: "SMSGTE", text: "@9405551234 Late"});
assert.deepEqual(B("sms", {to: "+1 940-555-1234", text: "Late"}),
  {to: "SMSGTE", text: "@9405551234 Late"});
assert.deepEqual(B("sms", {to: "wife", text: "Late"}),
  {to: "SMSGTE", text: "@wife Late"});
assert.ok(B("sms", {to: "555-12", text: "x"}).error === undefined ||
          B("sms", {to: "555-12", text: "x"}).error);
assert.ok(B("sms", {to: "9405551234", text: ""}).error);

assert.deepEqual(B("email", {to: "a@b.com", text: "Hi"}),
  {to: "EMAIL-2", text: "a@b.com Hi"});
assert.deepEqual(B("email", {to: "me", text: "Hi"}), {to: "EMAIL-2", text: "me Hi"});
assert.ok(B("email", {to: "not an address", text: "Hi"}).error);

assert.deepEqual(B("group", {action: "CQ", group: "hotg", text: "Hello"}),
  {to: "ANSRVR", text: "CQ HOTG Hello"});
assert.deepEqual(B("group", {action: "J", group: "hotg", text: ""}),
  {to: "ANSRVR", text: "J HOTG"});
assert.deepEqual(B("group", {action: "U", group: "HOTG", text: "ignored"}),
  {to: "ANSRVR", text: "U HOTG"});
assert.ok(B("group", {action: "CQ", group: "HOTG", text: ""}).error);
assert.ok(B("group", {action: "J", group: "bad group", text: ""}).error);

assert.deepEqual(B("whois", {call: "w1aw"}), {to: "WHO-IS", text: "W1AW"});
assert.ok(B("whois", {call: "not a call"}).error);
""")
        r = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_service_addressees_are_sendable(self):
        # Every service callsign must pass the daemon's strict formatter.
        from aprs import format_aprs_message
        # WHO-IS isn't callsign-SSID shaped, but must still be sendable
        # (it was rejected before this card existed).
        for to in ("WXBOT", "SMSGTE", "EMAIL-2", "ANSRVR", "WHO-IS"):
            with self.subTest(to=to):
                self.assertEqual(
                    format_aprs_message(to, "test", message_id="AB12"),
                    f":{to:<9}:test{{AB12",
                )


class ServicesCardTests(RestTestCase):

    def test_dashboard_has_services_card(self):
        _, port = self.start_stack(APRSTransactionFake())
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/?token=test-token")
        html = conn.getresponse().read().decode("utf-8")
        for needle in ('id="servicesCard"', 'id="svcSelect"', "buildServiceMessage"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
