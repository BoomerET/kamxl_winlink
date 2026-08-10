"""
Offline, end-to-end tests for kamxl_rest.py: a real KAMDaemon (wired
to the scripted fakes used elsewhere in this suite) plus a real
kamxl_rest HTTP server pointed at it, talked to with real HTTP
requests -- no KAM-XL hardware, no mocking of the socket/HTTP layers
themselves.
"""

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from fakes import CannedSerial, ChunkSerial, ScriptedSerial, make_kam

from kamxl_daemon import KAMDaemon
from packet import Packet
from pbbs import PBBSMessage, PBBSMessageSummary
from winlink import Proposal, WinlinkMessage
import kamxl_rest
import winlink_api


class RestTestCase(unittest.TestCase):
    """
    Starts a KAMDaemon (wired to a caller-supplied fake serial
    connection) and a kamxl_rest server pointed at it, both on
    throwaway addresses, in background threads.
    """

    def start_stack(self, serial, api_key="test-token"):
        kam = make_kam(serial)

        tmp_dir = tempfile.mkdtemp()
        socket_path = os.path.join(tmp_dir, "kamxl.sock")

        daemon = KAMDaemon(kam, socket_path)
        daemon_thread = threading.Thread(
            target=daemon.serve_forever,
            daemon=True
        )
        daemon_thread.start()

        deadline = time.monotonic() + 2

        while (
            not os.path.exists(socket_path)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        # addCleanup runs LIFO -- register thread.join() *before*
        # shutdown() so that, in execution order, shutdown() (which
        # unblocks serve_forever()'s loop) runs first and join() runs
        # second. Registered the other way around, join(timeout) just
        # burns its whole timeout waiting on a thread that shutdown()
        # hasn't been told to stop yet -- cost every single test in
        # this suite a wasted 2+ seconds before this was caught.
        self.addCleanup(daemon_thread.join, 2)
        self.addCleanup(daemon.shutdown)

        server = kamxl_rest.serve(
            socket_path,
            "127.0.0.1",
            0,  # let the OS pick a free port
            api_key
        )
        server_thread = threading.Thread(
            target=server.serve_forever,
            daemon=True
        )
        server_thread.start()

        self.addCleanup(server_thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        port = server.server_address[1]

        return daemon, port

    def request(
        self,
        port,
        method,
        path,
        body=None,
        token="test-token"
    ):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        headers = {}

        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        data = None

        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        payload = json.loads(response.read())

        return response.status, payload


class AuthTests(RestTestCase):
    def test_missing_token_rejected(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/ping", token=None)

        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["type"], "Unauthorized")

    def test_wrong_token_rejected(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/ping", token="wrong")

        self.assertEqual(status, 401)

    def test_correct_token_accepted(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/ping")

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_no_auth_mode_requires_no_token(self):
        _, port = self.start_stack(ScriptedSerial({}), api_key=None)

        status, payload = self.request(port, "GET", "/ping", token=None)

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_query_string_token_accepted(self):
        # The web terminal page and EventSource (neither can always
        # set a custom header) authenticate via ?token=... instead --
        # request() with token=None and the URL built manually here
        # to exercise that path specifically, rather than the
        # Authorization header.
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "GET", f"/ping?token=test-token", token=None
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_wrong_query_string_token_rejected(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "GET", "/ping?token=wrong", token=None
        )

        self.assertEqual(status, 401)

    def test_favicon_bypasses_auth(self):
        # Real bug found in practice: browsers request /favicon.ico
        # automatically and unauthenticated on first load of any page
        # here. Left to the normal auth-then-dispatch path, that
        # always failed auth and logged a 401 that looked like a
        # security problem but was really just routine browser
        # behavior -- see do_GET()'s own comment in kamxl_rest.py.
        # No ?token= or Authorization header given here on purpose.
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/favicon.ico")
        response = conn.getresponse()
        response.read()

        self.assertEqual(response.status, 204)


class EndpointTests(RestTestCase):
    def test_ping(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/ping")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], "pong")

    def test_status(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/status")

        self.assertEqual(status, 200)
        self.assertTrue(payload["result"]["connected"])

    def test_get_typed(self):
        _, port = self.start_stack(ScriptedSerial({
            "HBAUD": "HBAUD    0/1200",
        }))

        status, payload = self.request(port, "GET", "/params/HBAUD")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], [0, 1200])

    def test_set_typed_round_trip(self):
        _, port = self.start_stack(ScriptedSerial({
            "MONITOR": "MONITOR  ON/OFF",
        }))

        status, payload = self.request(
            port, "PUT", "/params/MONITOR",
            body={"value": [True, False]}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], [True, False])

    def test_set_typed_missing_value_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "PUT", "/params/MONITOR",
            body={}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_get_configuration(self):
        _, port = self.start_stack(ScriptedSerial({
            "DISPLAY": "MYCALL   AI6K-10/AI6K-10\r\nHBAUD    0/1200",
        }))

        status, payload = self.request(port, "GET", "/configuration")

        self.assertEqual(status, 200)
        self.assertEqual(
            payload["result"],
            {"MYCALL": "AI6K-10/AI6K-10", "HBAUD": "0/1200"}
        )

    def test_get_and_set_raw(self):
        _, port = self.start_stack(ScriptedSerial({
            "MYCALL": "MYCALL   AI6K-10/AI6K-10",
        }))

        status, payload = self.request(port, "GET", "/params/MYCALL/raw")
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], "AI6K-10/AI6K-10")

        status, payload = self.request(
            port, "PUT", "/params/MYCALL/raw",
            body={"value": "AI6K-11/AI6K-11"}
        )
        self.assertEqual(status, 200)

    def test_readonly_param_maps_to_502(self):
        _, port = self.start_stack(ScriptedSerial({
            "VERSION": "KAM-XL VERSION 1.24160",
        }))

        status, payload = self.request(
            port, "PUT", "/params/VERSION",
            body={"value": "TEST"}
        )

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["type"], "KAMError")


class ConnectDisconnectTests(RestTestCase):
    def test_connect_success(self):
        _, port = self.start_stack(
            CannedSerial(["*** CONNECTED to KD5EOC-10\r\n"])
        )

        status, payload = self.request(
            port, "POST", "/connect",
            body={"callsign": "KD5EOC-10", "timeout": 1}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], "*** CONNECTED to KD5EOC-10\r\n")

    def test_connect_missing_callsign_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "POST", "/connect", body={})

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_connect_busy_maps_to_502(self):
        _, port = self.start_stack(
            CannedSerial(["***(N0CALL) busy\r\n"])
        )

        status, payload = self.request(
            port, "POST", "/connect",
            body={"callsign": "N0CALL", "timeout": 1}
        )

        self.assertEqual(status, 502)
        self.assertEqual(payload["error"]["type"], "KAMConnectionError")

    def test_send_and_read_connected(self):
        _, port = self.start_stack(
            CannedSerial(["hello back!\r\n"])
        )

        status, payload = self.request(
            port, "POST", "/connected/send",
            body={"text": "hi there"}
        )
        self.assertEqual(status, 200)

        status, payload = self.request(
            port, "GET", "/connected/read?timeout=0.3"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], "hello back!\r\n")

    def test_disconnect(self):
        _, port = self.start_stack(
            CannedSerial(["cmd:", "cmd:*** DISCONNECTED\r\n"])
        )

        status, payload = self.request(
            port, "POST", "/disconnect",
            body={"timeout": 1}
        )

        self.assertEqual(status, 200)
        self.assertIn("DISCONNECTED", payload["result"])


class TerminalTests(RestTestCase):
    """
    Milestone 4: the raw command passthrough and the self-contained
    web terminal page served directly by kamxl_rest.py.
    """

    def test_exec_returns_raw_command_output(self):
        _, port = self.start_stack(ScriptedSerial({
            "BEACON": "BEACON EVERY 0",
        }))

        status, payload = self.request(
            port, "POST", "/terminal/exec",
            body={"command": "BEACON"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], "BEACON EVERY 0")

    def test_exec_missing_command_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "POST", "/terminal/exec",
            body={}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_page_served_at_terminal(self):
        # Moved from GET / to GET /terminal when the dashboard became
        # the new landing page -- see DashboardTests below.
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/terminal?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.getheader("Content-Type"), "text/html; charset=utf-8"
        )
        self.assertIn("kamxl web terminal", html)
        self.assertIn("/terminal/exec", html)
        # Milestone 5: the live monitor pane, driven by an EventSource
        # against /monitor/stream, lives on the same page.
        self.assertIn("/monitor/stream", html)
        self.assertIn("monitorFeed", html)

    def test_root_no_longer_serves_terminal_page(self):
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertNotIn("kamxl web terminal", html)

    def test_page_requires_auth_when_enabled(self):
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/terminal")
        response = conn.getresponse()
        response.read()

        self.assertEqual(response.status, 401)


class DashboardTests(RestTestCase):
    """
    The dashboard (GET /) landing page: replaces the terminal as the
    page served at "/" -- see PROJECT.md's "real home/dashboard page"
    milestone. Summarizes KAM-XL link status and APRS station count
    (both cheap, passive reads) plus a manual PBBS check; deliberately
    does not auto-check PBBS or Winlink mail (both drive a real AX.25
    connect cycle -- see the module comment above DASHBOARD_HTML).
    """

    def test_page_served_at_root(self):
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.getheader("Content-Type"), "text/html; charset=utf-8"
        )
        self.assertIn("kamxl dashboard", html)

    def test_page_links_to_all_other_pages(self):
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")

        self.assertIn("/terminal", html)
        self.assertIn("/pbbs", html)
        self.assertIn("/map", html)
        self.assertIn("/winlink", html)

    def test_page_does_not_auto_fetch_pbbs_or_winlink(self):
        # Both PBBS message listing and Winlink mail checking drive a
        # real AX.25 connect/command/disconnect cycle against actual
        # hardware -- the dashboard must not trigger either just from
        # being loaded. Checked at the source level: the only two
        # calls made unconditionally at the bottom of the script are
        # refreshStatus()/refreshStations() (cheap, passive reads);
        # "/pbbs/messages" only appears inside the "Check PBBS"
        # button's click handler (never called on its own), and
        # "/winlink/check" doesn't appear anywhere at all -- the
        # Winlink card is just a link to the real Winlink page, which
        # has its own password-gated form.
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")

        self.assertIn("refreshStatus();", html)
        self.assertIn("refreshStations();", html)

        unconditional_tail = html[html.rindex("refreshStations();"):]
        self.assertNotIn("/pbbs/messages", unconditional_tail)
        self.assertIn("pbbsCheckBtn.addEventListener", html)
        self.assertNotIn("/winlink/check", html)

    def test_page_requires_auth_when_enabled(self):
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/")
        response = conn.getresponse()
        response.read()

        self.assertEqual(response.status, 401)

    def test_page_javascript_is_syntactically_valid(self):
        # Same real regression class as MAP_HTML's own version of this
        # test (see that test's comment) -- DASHBOARD_HTML is a Python
        # string literal too, so it's just as exposed to Python's own
        # triple-quoted-string escape processing silently corrupting
        # embedded JS. Fetches the real served page and runs the
        # actual inline <script> body through `node --check`. Skipped
        # if node isn't on PATH.
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")

        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")

        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        inline_scripts = [s for s in scripts if s.strip()]
        self.assertEqual(
            len(inline_scripts), 1, "expected exactly one inline <script> body"
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False
        ) as handle:
            handle.write(inline_scripts[0])
            script_path = handle.name
        self.addCleanup(os.remove, script_path)

        result = subprocess.run(
            [node, "--check", script_path], capture_output=True, text=True
        )

        self.assertEqual(
            result.returncode,
            0,
            "DASHBOARD_HTML's inline <script> is not valid JavaScript:\n"
            + result.stderr,
        )


class PBBSEndpointTests(RestTestCase):
    """
    daemon.kam.list_pbbs_messages()/read_pbbs_message() are stubbed
    directly here (same reasoning as tests/test_daemon.py's
    PBBSTests) -- these are about the REST layer's own routing,
    query-param handling, and response shape, not the underlying
    connect/command/disconnect exchange (covered in tests/test_pbbs.py)
    or the KAM-XL text format itself (unverified against real
    hardware, see pbbs.py's module docstring).
    """

    def test_list_messages(self):
        daemon, port = self.start_stack(ScriptedSerial({}))

        daemon.kam.list_pbbs_messages = lambda **kwargs: [
            PBBSMessageSummary(
                number=6, msg_type="B", status=None, size=45,
                to="KEPS", from_call="W3IWI", date="10/19/01 09:37:11",
                pages=2, subject="Line Element set",
            )
        ]

        status, payload = self.request(port, "GET", "/pbbs/messages")

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["result"]), 1)
        self.assertEqual(payload["result"][0]["subject"], "Line Element set")

    def test_read_message(self):
        daemon, port = self.start_stack(ScriptedSerial({}))

        daemon.kam.read_pbbs_message = lambda number, **kwargs: PBBSMessage(
            number=number, date="02/10/92 10:30:58", from_call="KB0NYK",
            to="HELP", routing=None, body="hi",
        )

        status, payload = self.request(port, "GET", "/pbbs/messages/2")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["from_call"], "KB0NYK")

    def test_read_message_not_found(self):
        daemon, port = self.start_stack(ScriptedSerial({}))

        daemon.kam.read_pbbs_message = lambda number, **kwargs: None

        status, payload = self.request(port, "GET", "/pbbs/messages/999")

        self.assertEqual(status, 200)
        self.assertIsNone(payload["result"])

    def test_pbbs_page_served(self):
        daemon, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/pbbs?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.getheader("Content-Type"), "text/html; charset=utf-8"
        )
        self.assertIn("kamxl PBBS", html)
        self.assertIn("/pbbs/messages", html)

    def test_pbbs_page_requires_auth_when_enabled(self):
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/pbbs")
        response = conn.getresponse()
        response.read()

        self.assertEqual(response.status, 401)


class WinlinkEndpointTests(RestTestCase):
    """
    Milestone 8. daemon.kam.check_winlink_mail is stubbed directly,
    same reasoning as PBBSEndpointTests above -- these are about the
    REST layer's own routing, body validation, and response shape.
    """

    def test_check_mail(self):
        daemon, port = self.start_stack(ScriptedSerial({}))

        proposal = Proposal(
            msg_type="P", sender="N0CALL", via="AI6K-10",
            recipient="AI6K-10", mid="12345_N0CALL", size=42,
            raw="FB P N0CALL AI6K-10 AI6K-10 12345_N0CALL 42",
        )

        daemon.kam.check_winlink_mail = lambda **kwargs: [
            WinlinkMessage(
                title="Test Subject", body="Hello there.",
                proposal=proposal, raw="Test Subject\r\nHello there.",
            )
        ]

        status, payload = self.request(
            port, "POST", "/winlink/check",
            body={"gateway": "AI6K-10", "password": "FOOBAR"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["result"]), 1)
        self.assertEqual(payload["result"][0]["title"], "Test Subject")

    def test_check_mail_missing_gateway_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "POST", "/winlink/check",
            body={"password": "FOOBAR"}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_check_mail_missing_password_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "POST", "/winlink/check",
            body={"gateway": "AI6K-10"}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_check_mail_requires_auth_when_enabled(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "POST", "/winlink/check",
            body={"gateway": "AI6K-10", "password": "FOOBAR"},
            token=None
        )

        self.assertEqual(status, 401)

    def test_send_message(self):
        daemon, port = self.start_stack(ScriptedSerial({}))

        daemon.kam.send_winlink_message = lambda **kwargs: ["12345_AI6K"]

        status, payload = self.request(
            port, "POST", "/winlink/send",
            body={
                "gateway": "AI6K-10",
                "password": "FOOBAR",
                "messages": [
                    {"to": ["N0CALL"], "subject": "Hi", "body": "Hello"}
                ],
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], ["12345_AI6K"])

    def test_send_message_missing_gateway_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "POST", "/winlink/send",
            body={
                "password": "FOOBAR",
                "messages": [
                    {"to": ["N0CALL"], "subject": "Hi", "body": "Hello"}
                ],
            }
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_send_message_missing_password_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "POST", "/winlink/send",
            body={
                "gateway": "AI6K-10",
                "messages": [
                    {"to": ["N0CALL"], "subject": "Hi", "body": "Hello"}
                ],
            }
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_send_message_missing_messages_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "POST", "/winlink/send",
            body={"gateway": "AI6K-10", "password": "FOOBAR"}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_send_message_empty_messages_list_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "POST", "/winlink/send",
            body={"gateway": "AI6K-10", "password": "FOOBAR", "messages": []}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_send_message_missing_message_field_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "POST", "/winlink/send",
            body={
                "gateway": "AI6K-10",
                "password": "FOOBAR",
                # No "body" field on the one message.
                "messages": [{"to": ["N0CALL"], "subject": "Hi"}],
            }
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_send_message_requires_auth_when_enabled(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "POST", "/winlink/send",
            body={
                "gateway": "AI6K-10",
                "password": "FOOBAR",
                "messages": [
                    {"to": ["N0CALL"], "subject": "Hi", "body": "Hello"}
                ],
            },
            token=None
        )

        self.assertEqual(status, 401)

    def test_winlink_page_served(self):
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/winlink?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.getheader("Content-Type"), "text/html; charset=utf-8"
        )
        self.assertIn("kamxl Winlink", html)
        self.assertIn("/winlink/check", html)
        self.assertIn("/winlink/send", html)
        self.assertIn('type="password"', html)

    def test_winlink_page_requires_auth_when_enabled(self):
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/winlink")
        response = conn.getresponse()
        response.read()

        self.assertEqual(response.status, 401)


class WinlinkApiEndpointTests(RestTestCase):
    """
    The Winlink HTTP web-service API endpoints (account lookups,
    gateway listings -- winlink_api.py). Unlike WinlinkEndpointTests
    above, these never touch daemon.kam -- winlink_api.account_exists()/
    get_gateway_status() are patched directly on the module object
    kamxl_daemon.py calls through, same technique as
    tests/test_daemon.py's WinlinkApiTests.
    """

    def setUp(self):
        self._old_key = os.environ.get("WINLINK_API_KEY")
        os.environ["WINLINK_API_KEY"] = "TEST_KEY"
        self.addCleanup(self._restore_key)

    def _restore_key(self):
        if self._old_key is None:
            os.environ.pop("WINLINK_API_KEY", None)
        else:
            os.environ["WINLINK_API_KEY"] = self._old_key

    def _patch(self, name, fake):
        original = getattr(winlink_api, name)
        setattr(winlink_api, name, fake)
        self.addCleanup(setattr, winlink_api, name, original)

    def test_account_exists(self):
        self._patch("account_exists", lambda callsign, api_key, **kwargs: True)

        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/winlink/account/AI6K")

        self.assertEqual(status, 200)
        self.assertTrue(payload["result"])

    def test_account_exists_requires_auth_when_enabled(self):
        self._patch("account_exists", lambda callsign, api_key, **kwargs: True)

        _, port = self.start_stack(ScriptedSerial({}))

        status, _ = self.request(
            port, "GET", "/winlink/account/AI6K", token=None
        )

        self.assertEqual(status, 401)

    def test_missing_api_key_is_daemon_error(self):
        os.environ.pop("WINLINK_API_KEY", None)

        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/winlink/account/AI6K")

        self.assertNotEqual(status, 200)
        self.assertEqual(payload["error"]["type"], "KAMError")
        self.assertIn("WINLINK_API_KEY", payload["error"]["message"])

    def test_gateways(self):
        gateway = winlink_api.Gateway(
            callsign="KD5EOC-10", base_callsign="KD5EOC",
            requested_mode="Packet", comments="", last_status="",
            latitude=33.2, longitude=-97.1, channels=[],
        )
        self._patch("get_gateway_status", lambda api_key, **kwargs: [gateway])

        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/winlink/gateways")

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["result"]), 1)
        self.assertEqual(payload["result"][0]["callsign"], "KD5EOC-10")

    def test_gateways_service_codes_comma_split(self):
        captured = {}

        def fake_get_gateway_status(api_key, **kwargs):
            captured.update(kwargs)
            return []

        self._patch("get_gateway_status", fake_get_gateway_status)

        _, port = self.start_stack(ScriptedSerial({}))

        self.request(
            port, "GET",
            "/winlink/gateways?service_codes=PUBLIC,EMCOMM"
        )

        # Daemon layer converts JSON-array params to tuples before
        # passing them on (same "list -> tuple" convention docs/daemon.md
        # documents for every other multi-value param).
        self.assertEqual(captured["service_codes"], ("PUBLIC", "EMCOMM"))

    def test_nearby_gateways(self):
        near = winlink_api.Gateway(
            callsign="NEAR-10", base_callsign="NEAR",
            requested_mode="Packet", comments="", last_status="",
            latitude=33.05, longitude=-97.05, channels=[],
        )
        self._patch("get_gateway_status", lambda api_key, **kwargs: [near])

        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "GET", "/winlink/gateways/nearby?lat=33.0&lon=-97.0"
        )

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["result"]), 1)
        self.assertEqual(payload["result"][0]["gateway"]["callsign"], "NEAR-10")
        self.assertGreater(payload["result"][0]["distance_km"], 0)

    def test_nearby_gateways_missing_lat_is_400(self):
        self._patch("get_gateway_status", lambda api_key, **kwargs: [])

        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "GET", "/winlink/gateways/nearby?lon=-97.0"
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")


class StationsEndpointTests(RestTestCase):
    """
    Milestone 7. Populates daemon._stations directly via
    StationTracker.update() rather than driving real MONITOR traffic
    through ChunkSerial (already covered end-to-end in
    tests/test_daemon.py's StationsTests) -- these are only about the
    REST layer's own routing and response shape.
    """

    def _seed_station(self, daemon, callsign="AI6K-9"):
        packet = Packet(
            source=callsign,
            destination="APRS",
            digipeaters=(),
            port=1,
            payload="!4903.50N/07201.75W-Test comment",
            raw="",
            frame_type="UI",
        )
        daemon._stations.update(packet, now=1000.0)

    def test_stations_list(self):
        daemon, port = self.start_stack(ScriptedSerial({}))
        self._seed_station(daemon)

        status, payload = self.request(port, "GET", "/stations")

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["result"]), 1)
        self.assertEqual(payload["result"][0]["callsign"], "AI6K-9")
        self.assertEqual(payload["result"][0]["comment"], "Test comment")

    def test_stations_list_empty_by_default(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/stations")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], [])

    def test_stations_get_known_callsign(self):
        daemon, port = self.start_stack(ScriptedSerial({}))
        self._seed_station(daemon)

        status, payload = self.request(port, "GET", "/stations/AI6K-9")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["callsign"], "AI6K-9")

    def test_stations_get_unknown_callsign_returns_none(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/stations/NOBODY")

        self.assertEqual(status, 200)
        self.assertIsNone(payload["result"])

    def test_map_page_served(self):
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/map?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.getheader("Content-Type"), "text/html; charset=utf-8"
        )
        self.assertIn("kamxl station map", html)
        self.assertIn("/stations", html)
        self.assertIn("leaflet", html.lower())

    def test_map_page_renders_real_aprs_icons(self):
        # Milestone 7 extension: markers use real APRS symbol-table
        # icons (hessu/aprs-symbols sprites + OK-DMR/aprs-symbols'
        # lookup logic ported inline), not Leaflet's plain default
        # marker -- see kamxl_rest.py's module comment above MAP_HTML.
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/map?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertIn("aprs-symbols", html)
        self.assertIn("aprsSymbolPosition", html)
        self.assertIn("aprsIcon", html)
        # Pinned commit, not an unpinned branch/tag -- see module comment.
        self.assertIn("f2286a9cd43eb6ba4501250b4c39fff111e3796c", html)

    def test_map_page_renders_overlay_characters_as_text_badge(self):
        # A station using an "alternate table with overlay" symbol
        # (a digit/letter instead of plain "/" or "\") gets that
        # character drawn as a small text badge -- hessu/aprs-symbols
        # has no real overlay sprite art (its own README says so; see
        # the module comment above MAP_HTML), so this checks for the
        # CSS class + escapeHtml() call rather than any sprite/image
        # reference for the overlay itself.
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/map?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")

        self.assertIn("aprsOverlay", html)
        self.assertIn("pos.overlay", html)
        self.assertIn("escapeHtml(pos.overlay)", html)

    def test_map_page_javascript_is_syntactically_valid(self):
        # Real regression, not a hypothetical: MAP_HTML is a Python
        # string literal, and the APRS icon feature's symbol lookup
        # table used JS escapes (\" and \\) that looked correct in the
        # source but were being silently *unescaped* by Python's own
        # (non-raw) triple-quoted string processing before the page
        # ever reached a browser -- turning a working-looking JS array
        # into invalid JavaScript. Because a syntax error anywhere in
        # a <script> block prevents the whole block from running (not
        # just the broken statement), this broke everything the
        # script did: the map never initialized (black screen) and
        # the terminal/pbbs/winlink header links never got their href
        # set (clicking did nothing). Fixed by making MAP_HTML a raw
        # string (r\"\"\"...\"\"\"). Grepping for substrings (the test
        # above) can't catch this class of bug -- it needs a real JS
        # parser on the actual served bytes. Skipped if node isn't on
        # PATH, since this project has no other Node/JS-tooling
        # dependency.
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")

        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/map?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")

        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        inline_scripts = [s for s in scripts if s.strip()]
        self.assertEqual(
            len(inline_scripts), 1, "expected exactly one inline <script> body"
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False
        ) as handle:
            handle.write(inline_scripts[0])
            script_path = handle.name
        self.addCleanup(os.remove, script_path)

        result = subprocess.run(
            [node, "--check", script_path], capture_output=True, text=True
        )

        self.assertEqual(
            result.returncode,
            0,
            "MAP_HTML's inline <script> is not valid JavaScript:\n"
            + result.stderr,
        )

    def test_map_page_requires_auth_when_enabled(self):
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/map")
        response = conn.getresponse()
        response.read()

        self.assertEqual(response.status, 401)


class RoutingErrorTests(RestTestCase):
    def test_unknown_route_is_404(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/nope")

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["type"], "NotFound")

    def test_wrong_method_is_405(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "POST", "/ping")

        self.assertEqual(status, 405)
        self.assertEqual(payload["error"]["type"], "MethodNotAllowed")


class MonitorStreamTests(RestTestCase):
    def test_stream_delivers_a_packet_event(self):
        _, port = self.start_stack(ChunkSerial([
            "KD5EOC-10>BEACON/2:\r\n",
            "Winlink 2000 RMS Packet Server\r\n",
            "AI6K-4>BEACON/2:\r\n",
        ]))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request(
            "GET", "/monitor/stream",
            headers={"Authorization": "Bearer test-token"}
        )
        response = conn.getresponse()

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.getheader("Content-Type"), "text/event-stream"
        )

        buf = b""
        deadline = time.monotonic() + 3
        frame = None

        while time.monotonic() < deadline:
            chunk = response.fp.read1(4096)

            if not chunk:
                break

            buf += chunk

            if b"\n\n" in buf:
                frame, _, buf = buf.partition(b"\n\n")

                if frame.startswith(b"data:"):
                    break

                frame = None

        self.assertIsNotNone(frame, "No SSE data frame received in time")

        event = json.loads(frame[len(b"data:"):].decode("utf-8"))

        self.assertEqual(event["event"], "packet")
        self.assertEqual(event["data"]["source"], "KD5EOC-10")
        self.assertEqual(event["data"]["destination"], "BEACON")

    def test_stream_accepts_query_string_token(self):
        # The web terminal's live monitor pane (milestone 5) connects
        # via EventSource, which can't set an Authorization header --
        # it relies entirely on this fallback.
        _, port = self.start_stack(ChunkSerial([]))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/monitor/stream?token=test-token")
        response = conn.getresponse()

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.getheader("Content-Type"), "text/event-stream"
        )


class DaemonClientTimeoutTests(unittest.TestCase):
    """
    Directly exercises DaemonClient's socket-timeout handling against a
    minimal fake "daemon" that delays its response by a controlled
    amount -- no KAMDaemon/KAMXL involved, just the socket layer.

    Regression coverage for a real bug: DaemonClient's own socket read
    timeout could fire before the daemon had a chance to answer, since
    both defaulted to the same 10s window. Most visible on /connect
    and /disconnect, whose default timeouts (60s/30s) are relayed to
    the daemon but were *not* reflected in how long the REST layer
    itself waited -- so a legitimately-in-progress connect attempt
    surfaced as an unhandled socket.timeout / 500 instead of either
    succeeding or getting a clean answer from the daemon.
    """

    def start_slow_daemon(self, delay):
        tmp_dir = tempfile.mkdtemp()
        socket_path = os.path.join(tmp_dir, "slow.sock")

        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(socket_path)
        server_sock.listen(1)
        server_sock.settimeout(0.1)

        stop = threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    conn, _ = server_sock.accept()
                except socket.timeout:
                    continue

                try:
                    conn.recv(65536)  # the request line; contents unused
                    time.sleep(delay)
                    conn.sendall(
                        (json.dumps(
                            {"id": "1", "ok": True, "result": "slow"}
                        ) + "\n").encode("ascii")
                    )
                finally:
                    conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        self.addCleanup(stop.set)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server_sock.close)

        return socket_path

    def test_default_timeout_too_short_raises_daemon_timeout(self):
        socket_path = self.start_slow_daemon(delay=0.3)
        client = kamxl_rest.DaemonClient(socket_path, timeout=0.1)

        with self.assertRaises(kamxl_rest.DaemonTimeout):
            client.call("ping")

    def test_per_call_socket_timeout_override_waits_long_enough(self):
        # Same slow daemon and the same short default -- but this call
        # asks for a longer wait, exactly like _h_connect/_h_disconnect
        # do when a caller passes a large "timeout" body field.
        socket_path = self.start_slow_daemon(delay=0.3)
        client = kamxl_rest.DaemonClient(socket_path, timeout=0.1)

        response = client.call("ping", _socket_timeout=1)

        self.assertEqual(response["result"], "slow")


class StreamEventsKeepaliveTests(unittest.TestCase):
    """
    Regression coverage for a real bug found live on hardware:
    DaemonClient.stream_events() crashed with
    "OSError: cannot read from timed out object" immediately after
    the *first* keepalive timeout. CPython's socket.makefile() sets a
    sticky _timeout_occurred flag the first time a read hits
    socket.timeout -- every *later* read on that same file object
    then fails immediately instead of actually trying again. Observed
    as the SSE stream dying and EventSource silently reconnecting
    every ~15s, discarding whatever packet happened to arrive during
    the reconnect gap. Fixed with select.select()-based polling so
    readline() only ever runs once data is already known to be
    waiting, never hitting the socket-level timeout path at all.
    """

    def start_idle_daemon(self, event_after=None):
        """
        A minimal fake daemon: accepts one connection, acks the
        subscribe request, then sends nothing else (simulating no
        packets arriving) unless ``event_after`` (seconds) is given,
        in which case it sends exactly one event after that delay and
        then goes idle again.
        """
        tmp_dir = tempfile.mkdtemp()
        socket_path = os.path.join(tmp_dir, "idle.sock")

        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(socket_path)
        server_sock.listen(1)
        server_sock.settimeout(0.1)

        stop = threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    conn, _ = server_sock.accept()
                except socket.timeout:
                    continue

                try:
                    conn.recv(65536)  # the subscribe request; unused
                    conn.sendall(
                        (json.dumps({"id": "1", "ok": True}) + "\n")
                        .encode("ascii")
                    )

                    if event_after is not None:
                        time.sleep(event_after)
                        conn.sendall(
                            (json.dumps({
                                "event": "packet",
                                "data": {"source": "AI6K-4"},
                            }) + "\n").encode("ascii")
                        )

                    while not stop.is_set():
                        time.sleep(0.05)
                finally:
                    conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        self.addCleanup(stop.set)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server_sock.close)

        return socket_path

    def test_survives_multiple_keepalive_cycles(self):
        # The old implementation crashed on the *second* read after
        # the first timeout -- five clean Nones in a row proves it's
        # not just surviving one cycle by luck.
        socket_path = self.start_idle_daemon()
        client = kamxl_rest.DaemonClient(socket_path, timeout=0.05)

        events = client.stream_events("monitor.subscribe")

        for _ in range(5):
            self.assertIsNone(next(events))

    def test_receives_event_after_keepalives(self):
        # Mirrors the real usage pattern: idle for a while (nothing on
        # the air yet), then a packet actually arrives.
        socket_path = self.start_idle_daemon(event_after=0.2)
        client = kamxl_rest.DaemonClient(socket_path, timeout=0.05)

        events = client.stream_events("monitor.subscribe")

        seen_event = None
        for _ in range(20):
            item = next(events)

            if item is not None:
                seen_event = item
                break

        self.assertIsNotNone(seen_event, "Never received the event")
        self.assertEqual(seen_event["data"]["source"], "AI6K-4")


if __name__ == "__main__":
    unittest.main()
