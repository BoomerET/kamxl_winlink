"""
Offline, end-to-end tests for tncstation_rest.py: a real KAMDaemon (wired
to the scripted fakes used elsewhere in this suite) plus a real
tncstation_rest HTTP server pointed at it, talked to with real HTTP
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

from tncstation_daemon import KAMDaemon
from packet import Packet
from pbbs import PBBSMessage, PBBSMessageSummary
from winlink import Proposal, WinlinkMessage
import tncstation_rest
import winlink_api


class RestTestCase(unittest.TestCase):
    """
    Starts a KAMDaemon (wired to a caller-supplied fake serial
    connection) and a tncstation_rest server pointed at it, both on
    throwaway addresses, in background threads.
    """

    def start_stack(
        self,
        serial,
        api_key="test-token",
        history_db=False,
    ):
        kam = make_kam(serial)

        tmp_dir = tempfile.mkdtemp()
        socket_path = os.path.join(tmp_dir, "kamxl.sock")

        database_path = (
            os.path.join(tmp_dir, "reception.sqlite3")
            if history_db
            else None
        )

        daemon = KAMDaemon(
            kam,
            socket_path,
            history_db=database_path,
            station_callsign="N5BA",
        )

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

        server = tncstation_rest.serve(
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
        # behavior -- see do_GET()'s own comment in tncstation_rest.py.
        # No ?token= or Authorization header given here on purpose.
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/favicon.ico")
        response = conn.getresponse()
        response.read()

        self.assertEqual(response.status, 204)


class TokenRedactionTests(RestTestCase):
    """The API key must never land in the access log (?token=...)."""

    def test_redact_token(self):
        redact = tncstation_rest.redact_token

        self.assertEqual(
            redact('"GET /status?token=abc123 HTTP/1.1" 200 -'),
            '"GET /status?token=… HTTP/1.1" 200 -',
        )
        self.assertEqual(
            redact("GET /aprs/history?since=1792335600&token=abc123&x=1"),
            "GET /aprs/history?since=1792335600&token=…&x=1",
        )
        self.assertEqual(redact("GET /ping?TOKEN=abc"), "GET /ping?TOKEN=…")
        # Only the token parameter, and only as a parameter.
        self.assertEqual(redact("GET /ping?mytoken=abc"), "GET /ping?mytoken=abc")
        self.assertEqual(redact("GET /stations"), "GET /stations")

    def _logged(self, path):
        _, port = self.start_stack(ScriptedSerial({}))

        with self.assertLogs("tncstation_rest", "INFO") as logs:
            self.request(port, "GET", path, token=None)

            # The access log line is written after the response is
            # sent; give the handler thread a moment to write it.
            deadline = time.monotonic() + 2
            while (
                not any("GET " in line for line in logs.output)
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

        return "\n".join(logs.output)

    def test_accepted_request_logs_no_token(self):
        output = self._logged("/ping?token=test-token")

        self.assertIn("/ping?token=…", output)
        self.assertNotIn("test-token", output)

    def test_rejected_request_logs_no_token(self):
        # A wrong guess is still somebody's secret (or a typo of yours).
        output = self._logged("/ping?token=almost-the-key")

        self.assertIn("401", output)
        self.assertNotIn("almost-the-key", output)


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
            "DISPLAY": "MYCALL   N5BA-10/N5BA-10\r\nHBAUD    0/1200",
        }))

        status, payload = self.request(port, "GET", "/configuration")

        self.assertEqual(status, 200)
        self.assertEqual(
            payload["result"],
            {"MYCALL": "N5BA-10/N5BA-10", "HBAUD": "0/1200"}
        )

    def test_get_and_set_raw(self):
        _, port = self.start_stack(ScriptedSerial({
            "MYCALL": "MYCALL   N5BA-10/N5BA-10",
        }))

        status, payload = self.request(port, "GET", "/params/MYCALL/raw")
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], "N5BA-10/N5BA-10")

        status, payload = self.request(
            port, "PUT", "/params/MYCALL/raw",
            body={"value": "N5BA-11/N5BA-11"}
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
    web terminal page served directly by tncstation_rest.py.
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
        self.assertIn("tncstation web terminal", html)
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
        self.assertNotIn("tncstation web terminal", html)

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
        self.assertIn("tncstation dashboard", html)

    def test_page_includes_persistent_reception_history(self):
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=5
        )
        self.addCleanup(conn.close)

        conn.request("GET", "/?token=test-token")
        response = conn.getresponse()
        html = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)

        # Historical chart and status elements.
        self.assertIn('id="historyCard"', html)
        self.assertIn('id="historyChart"', html)
        self.assertIn('id="historySummary"', html)

        # Historical data comes from SQLite, not session statistics.
        self.assertIn('"/aprs/history?since="', html)
        self.assertIn("renderPersistentHistory(", html)

        # The dashboard handles disabled persistence.
        self.assertIn(
            "Persistent reception history is disabled.", html
        )

        # Initial refresh and subsequent 60-second refreshes.
        self.assertIn("  refreshPersistentHistory();", html)
        self.assertIn(
            "setInterval(refreshPersistentHistory, 60000);",
            html,
        )

    def test_daily_history_respects_local_calendar_days(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")

        html = tncstation_rest.DASHBOARD_HTML

        # Extract the actual functions used by the dashboard.
        functions = []

        for name, args in (
            ("localDayStart", "date"),
            ("buildDailyHistory", "rows, days"),
        ):
            pattern = (
                r"  function " + name +
                r"\(" + re.escape(args) + r"\) \{"
                r"(.*?)"
                r"\n  \}\n"
            )

            match = re.search(pattern, html, re.DOTALL)

            self.assertIsNotNone(
                match,
                f"Dashboard function {name} not found",
            )

            functions.append(
                "function " + name + "(" + args + ") {"
                + match.group(1) + "\n}"
            )

        harness = r"""
const assert = require("node:assert/strict");

""" + "\n\n".join(functions) + r"""

function epoch(year, month, day, hour = 0, minute = 0) {
  return Math.floor(
    new Date(year, month - 1, day, hour, minute).getTime() / 1000
  );
}

function row(timestamp, count) {
  return {
    hour_start: timestamp,
    packet_count: count
  };
}

function checkDayLengths(year, month, day, expectedHours) {
  const start = epoch(year, month, day);
  const next = epoch(year, month, day + 1);

  assert.equal((next - start) / 3600, expectedHours);
}

// America/Chicago: spring forward on March 8, 2026.
checkDayLengths(2026, 3, 8, 23);

// America/Chicago: fall back on November 1, 2026.
checkDayLengths(2026, 11, 1, 25);

// Spring-forward: both packets belong to March 8.
{
  const rows = [
    row(epoch(2026, 3, 8, 0), 3),
    row(epoch(2026, 3, 8, 3), 5),
    row(epoch(2026, 3, 9, 0), 7)
  ];

  const result = buildDailyHistory(rows, 2);

  assert.deepEqual(
    result.map(bucket => bucket.count),
    [8, 7]
  );
}

// Fall-back: both occurrences of 1 AM belong to November 1.
{
  const firstOneAM = Math.floor(
    Date.parse("2026-11-01T06:00:00Z") / 1000
  );

  const secondOneAM = Math.floor(
    Date.parse("2026-11-01T07:00:00Z") / 1000
  );

  const rows = [
    row(firstOneAM, 4),
    row(secondOneAM, 6),
    row(epoch(2026, 11, 2, 0), 2)
  ];

  const result = buildDailyHistory(rows, 2);

  assert.deepEqual(
    result.map(bucket => bucket.count),
    [10, 2]
  );
}
"""

        # Freeze the test's current date independently of real time.
        # The dashboard helper uses new Date() to choose its buckets.
        harness = (
            "const RealDate = Date;\n"
            "let fixedNow = '2026-03-09T17:00:00Z';\n"
            "class FixedDate extends RealDate {\n"
            "  constructor(...args) {\n"
            "    if (args.length === 0) {\n"
            "      super(fixedNow);\n"
            "    } else {\n"
            "      super(...args);\n"
            "    }\n"
            "  }\n"
            "  static now() { return RealDate.parse(fixedNow); }\n"
            "}\n"
            "global.Date = FixedDate;\n"
            + harness.replace(
                "// Fall-back: both occurrences",
                "fixedNow = '2026-11-02T18:00:00Z';\n"
                "// Fall-back: both occurrences",
            )
        )

        result = subprocess.run(
            [node, "-e", harness],
            env={**os.environ, "TZ": "America/Chicago"},
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            result.returncode,
            0,
            result.stderr,
        )

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
        # PBBS message listing and Winlink mail checking can initiate
        # real AX.25 connect/command/disconnect cycles. The dashboard
        # must never trigger these operations automatically.
        #
        # Its four automatic refresh calls are passive reads:
        # refreshStatus(), refreshStations(), refreshReceptionStats(),
        # and refreshPersistentHistory().
        #
        # PBBS messages are requested only by the Check PBBS button.
        # Winlink mail checking occurs on the dedicated Winlink page.
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

    def test_page_contains_reception_statistics(self):
        html = tncstation_rest.DASHBOARD_HTML

        self.assertIn('id="receptionStatsCard"', html)

        for element_id in (
            "rxTotal",
            "rxSources",
            "rxRelayed",
            "rxDirect",
            "rxTopSources",
            "rxTopDigis",
            "rxStatsStatus",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', html)

        self.assertIn("AX.25 Reception Statistics", html)
        self.assertIn("Top Heard Sources", html)
        self.assertIn("Top Digipeaters", html)

    def test_page_refreshes_reception_statistics(self):
        html = tncstation_rest.DASHBOARD_HTML

        self.assertIn('apiGet("/aprs/stats")', html)
        self.assertIn("refreshReceptionStats();", html)
        self.assertIn(
            "setInterval(refreshReceptionStats, 15000);",
            html,
        )

        # Callsigns must be escaped before insertion into HTML.
        self.assertIn("escapeHtml(entry.callsign)", html)

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
            len(inline_scripts), 3,
            "expected three inline <script> bodies (theme, features, page)"
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False
        ) as handle:
            # Each script is checked; joined so one node --check covers both.
            handle.write("\n;\n".join(inline_scripts))
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
    def test_reception_health_markup_and_styles(self):
        from tncstation_rest import DASHBOARD_HTML

        html = DASHBOARD_HTML

        self.assertIn('id="receptionHealth"', html)

        for state in (
            "receiving",
            "waiting",
            "quiet",
            "disconnected",
            "unavailable",
        ):
            self.assertIn("#receptionHealth." + state, html)

        self.assertIn("updateReceptionHealth(status.connected, diag)", html)

    def test_reception_health_states(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")

        from tncstation_rest import DASHBOARD_HTML

        html = DASHBOARD_HTML

        # Extract the actual function from the served dashboard source.
        match = re.search(
            r"  function updateReceptionHealth\(connected, diag\) \{"
            r"(.*?)"
            r"\n  \}\n",
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "Reception health function not found")

        function_source = (
            "function updateReceptionHealth(connected, diag) {"
            + match.group(1)
            + "\n}"
        )

        harness = r"""
const assert = require("node:assert/strict");

const element = { className: "", textContent: "" };

global.document = {
  getElementById: function(id) {
    assert.equal(id, "receptionHealth");
    return element;
  }
};

""" + function_source + r"""

function check(connected, diagnostics, expectedClass, expectedText) {
  element.className = "";
  element.textContent = "";

  updateReceptionHealth(connected, diagnostics);

  assert.equal(element.className, expectedClass);
  assert.equal(element.textContent, expectedText);
}

check(false, null,
      "disconnected", "Reception: Disconnected");

check(true, null,
      "unavailable", "Reception: Unknown (diagnostics unavailable)");

check(true, {
  packets_last_5m: 3,
  uptime_seconds: 600,
  last_packet_at: 12345
}, "receiving", "Reception: Receiving (last 5 minutes)");

check(true, {
  packets_last_5m: 0,
  uptime_seconds: 60,
  last_packet_at: null
}, "waiting", "Reception: Waiting for first packet");

check(true, {
  packets_last_5m: 0,
  uptime_seconds: 600,
  last_packet_at: 12345
}, "quiet", "Reception: Quiet (no packets in 5 minutes)");

// Reception watchdog states take precedence over the 5-minute view:
// own stations still arriving must not read as "Receiving".
check(true, {
  packets_last_5m: 4,
  uptime_seconds: 7200,
  last_packet_at: 12345,
  watchdog: { state: "only_own", minutes_since_other: 41.2 }
}, "quiet", "Reception: Only own stations heard (41 min)");

check(true, {
  packets_last_5m: 0,
  uptime_seconds: 7200,
  last_packet_at: 12345,
  watchdog: { state: "silent" }
}, "disconnected", "Reception: Nothing heard");
"""

        result = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(
            result.returncode,
            0,
            "Reception health JavaScript test failed:\n" + result.stderr,
        )

    def test_reception_charts_render_horizontal_bars(self):
        html = tncstation_rest.DASHBOARD_HTML

        self.assertIn("receptionChartRow", html)
        self.assertIn("receptionBarTrack", html)
        self.assertIn("receptionBarFill", html)

        self.assertIn("maxPackets", html)
        self.assertIn("packets / maxPackets * 100", html)
        self.assertIn("percentage.toFixed(1)", html)

        # Both lists use the same rendering function.
        self.assertIn(
            'renderReceptionRows("rxTopSources", visible, true)',
            html,
        )
        self.assertIn(
            'renderReceptionRows("rxTopDigis", stats.top_digipeaters, false)',
            html,
        )

    def test_reception_source_filtering(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")

        html = tncstation_rest.DASHBOARD_HTML

        match = re.search(
            r"  function filterReceptionSources"
            r"\(entries, query, minimum, sortBy\) \{"
            r"(.*?)"
            r"\n  \}\n",
            html,
            re.DOTALL,
        )

        self.assertIsNotNone(
            match, "Reception source filtering function not found"
        )

        function_source = (
            "function filterReceptionSources"
            "(entries, query, minimum, sortBy) {"
            + match.group(1)
            + "\n}"
        )

        harness = r"""
const assert = require("node:assert/strict");

const sources = [
    {callsign: "N5BA", packets: 20},
    {callsign: "W5NGU-3", packets: 10},
    {callsign: "K5AAA", packets: 5},
    {callsign: "N5TEST", packets: 2},
    {callsign: "K5BBB", packets: 1}
];

// Partial callsign search.
assert.deepEqual(
    filterReceptionSources(sources, "N5", 1, "most")
        .map(entry => entry.callsign),
    ["N5BA", "N5TEST"]
);

// Case-insensitive search.
assert.deepEqual(
    filterReceptionSources(sources, "n5ba", 1, "most")
        .map(entry => entry.callsign),
    ["N5BA"]
);

// Minimum packet threshold.
assert.deepEqual(
    filterReceptionSources(sources, "", 5, "most")
        .map(entry => entry.callsign),
    ["N5BA", "W5NGU-3", "K5AAA"]
);

// Most heard first.
assert.deepEqual(
    filterReceptionSources(sources, "", 1, "most")
        .map(entry => entry.callsign),
    ["N5BA", "W5NGU-3", "K5AAA", "N5TEST", "K5BBB"]
);

// Least heard first.
assert.deepEqual(
    filterReceptionSources(sources, "", 1, "least")
        .map(entry => entry.callsign),
    ["K5BBB", "N5TEST", "K5AAA", "W5NGU-3", "N5BA"]
);

// Alphabetical callsign sorting.
assert.deepEqual(
    filterReceptionSources(sources, "", 1, "callsign")
        .map(entry => entry.callsign),
    ["K5AAA", "K5BBB", "N5BA", "N5TEST", "W5NGU-3"]
);

// Search and minimum count together.
assert.deepEqual(
    filterReceptionSources(sources, "N5", 5, "most")
        .map(entry => entry.callsign),
    ["N5BA"]
);

// Empty dataset.
assert.deepEqual(
    filterReceptionSources([], "N5", 1, "most"),
    []
);

// Filtering must not reorder the original dataset.
assert.equal(sources[0].callsign, "N5BA");
assert.equal(sources[4].callsign, "K5BBB");

console.log("Reception source filtering tests passed");
"""

        result = subprocess.run(
            [node, "-e", function_source + "\n" + harness],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )

    def test_reception_source_display_filters_and_refresh(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")

        html = tncstation_rest.DASHBOARD_HTML

        functions = []
        for name, arguments in (
            ("filterReceptionSources", "entries, query, minimum, sortBy"),
            ("updateReceptionSourceDisplay", ""),
        ):
            pattern = (
                r"  function " + name + r"\(" +
                re.escape(arguments) + r"\) \{" +
                r"(.*?)\n  \}\n"
            )
            match = re.search(pattern, html, re.DOTALL)
            self.assertIsNotNone(match, name + " not found")

            functions.append(
                "function " + name + "(" + arguments + ") {" +
                match.group(1) + "\n}"
            )

        harness = r"""
const assert = require("node:assert/strict");

const elements = {
    rxSourceSearch: {value: ""},
    rxSourceMinimum: {value: "1"},
    rxSourceSort: {value: "most"},
    rxSourceCount: {textContent: ""}
};

global.document = {
    getElementById: id => elements[id]
};

let rendered = [];

function renderReceptionRows(id, entries, linkToMap) {
    assert.equal(id, "rxTopSources");
    assert.equal(linkToMap, true);
    rendered = entries.map(entry => entry.callsign);
}

var receptionSources = Array.from(
    {length: 15},
    (_, index) => ({
        callsign: "N5TEST-" + index,
        packets: 15 - index
    })
);

// Default dashboard shows only ten.
updateReceptionSourceDisplay();
assert.equal(rendered.length, 10);
assert.equal(rendered[0], "N5TEST-0");
assert.match(elements.rxSourceCount.textContent, /top 10 of 15/);

// Search includes stations outside the top ten.
elements.rxSourceSearch.value = "N5TEST-14";
updateReceptionSourceDisplay();
assert.deepEqual(rendered, ["N5TEST-14"]);

// Change search and sorting.
elements.rxSourceSearch.value = "N5TEST";
elements.rxSourceSort.value = "least";
elements.rxSourceMinimum.value = "5";
updateReceptionSourceDisplay();

assert.equal(rendered.length, 11);
assert.equal(rendered[0], "N5TEST-10");
assert.equal(rendered[10], "N5TEST-0");

// Simulate fresh statistics arriving from the daemon.
receptionSources = receptionSources.map(entry => ({
    callsign: entry.callsign,
    packets: entry.packets + 1
}));

updateReceptionSourceDisplay();

// Current selections are preserved.
assert.equal(elements.rxSourceSearch.value, "N5TEST");
assert.equal(elements.rxSourceSort.value, "least");
assert.equal(elements.rxSourceMinimum.value, "5");

// The new packet counts affect filtering.
assert.equal(rendered.length, 12);
assert.equal(rendered[0], "N5TEST-11");

// Clear filters and restore compact view.
elements.rxSourceSearch.value = "";
elements.rxSourceSort.value = "most";
elements.rxSourceMinimum.value = "1";

updateReceptionSourceDisplay();
assert.equal(rendered.length, 10);

console.log("Reception display integration tests passed");
"""

        result = subprocess.run(
            [node, "-e", "\n".join(functions) + "\n" + harness],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )

    def test_reception_chart_bar_widths(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")

        html = tncstation_rest.DASHBOARD_HTML

        # Extract the actual dashboard function, not a duplicate.
        match = re.search(
            r"  function renderReceptionRows\(elementId, entries, linkToMap\) \{"
            r"(.*?)"
            r"\n  \}\n",
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(
            match, "Reception chart rendering function not found"
        )

        function_source = (
            "function renderReceptionRows(elementId, entries, linkToMap) {"
            + match.group(1)
            + "\n}"
        )

        harness = r"""
const assert = require("node:assert/strict");

const elements = {};

global.document = {
    getElementById: function(id) {
        if (!elements[id]) {
            elements[id] = {
                innerHTML: "",
                textContent: ""
            };
        }
        return elements[id];
    }
};

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function widths(html) {
    return Array.from(
        html.matchAll(/style="width:([0-9.]+)%"/g),
        match => Number(match[1])
    );
}

// Different packet counts.
renderReceptionRows("test", [
    {callsign: "N5BA", packets: 10},
    {callsign: "K5TEST", packets: 5},
    {callsign: "W5NGU-3", packets: 1}
]);

assert.deepEqual(
    widths(elements.test.innerHTML),
    [100, 50, 10]
);

// Equal counts.
renderReceptionRows("test", [
    {callsign: "N5BA", packets: 3},
    {callsign: "K5TEST", packets: 3}
]);

assert.deepEqual(
    widths(elements.test.innerHTML),
    [100, 100]
);

// Zero counts must not produce NaN or Infinity.
renderReceptionRows("test", [
    {callsign: "N5BA", packets: 0},
    {callsign: "K5TEST", packets: 0}
]);

assert.deepEqual(
    widths(elements.test.innerHTML),
    [0, 0]
);

// Empty list.
renderReceptionRows("empty", []);

assert.equal(
    elements.empty.textContent,
    "None heard yet."
);

// Callsigns must be escaped before insertion into HTML.
renderReceptionRows("test", [
    {callsign: "<script>alert(1)</script>", packets: 1}
]);

assert.ok(
    elements.test.innerHTML.includes(
        "&lt;script&gt;alert(1)&lt;/script&gt;"
    )
);

assert.ok(
    !elements.test.innerHTML.includes("<script>")
);
// Simulate the dashboard's authenticated URL helper.
function authedUrl(path) {
    return path + (path.includes("?") ? "&" : "?") +
        "token=test-token";
}

// Source stations should link to the APRS map.
renderReceptionRows("sources", [
    {callsign: "N5BA-9", packets: 10}
], true);

assert.ok(
    elements.sources.innerHTML.includes(
        '/map?station=N5BA-9&amp;token=test-token'
    )
);

assert.ok(
    elements.sources.innerHTML.includes(
        'target="_blank" rel="noopener noreferrer"'
    )
);

// Digipeaters should remain plain text.
renderReceptionRows("digis", [
    {callsign: "W5NGU-3", packets: 10}
], false);

assert.ok(
    !elements.digis.innerHTML.includes("<a ")
);

// A callsign containing HTML must not inject markup.
renderReceptionRows("sources", [
    {callsign: "<script>", packets: 1}
], true);

assert.ok(
    !elements.sources.innerHTML.includes("<script>")
);

assert.ok(
    elements.sources.innerHTML.includes("%3Cscript%3E")
);
console.log("Reception chart behavior: PASS");
"""

        result = subprocess.run(
            [node, "-e", function_source + "\n" + harness],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(
            result.returncode,
            0,
            "Reception chart behavioral test failed:\n"
            + result.stdout
            + result.stderr,
        )

    def test_map_station_search_behavior(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")

        html = tncstation_rest.MAP_HTML

        # Extract production code, including the form submit handler.
        match = re.search(
            r"  function selectMapStation\(callsign\) \{"
            r"(.*?)"
            r"\n  async function refresh\(\) \{",
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(
            match, "Map station search code not found"
        )

        source = (
            "function selectMapStation(callsign) {"
            + match.group(1)
        )

        harness = r"""
const assert = require("node:assert/strict");

const status = {
    hidden: true,
    textContent: ""
};

const searchInput = {
    value: ""
};

const handlers = {};

global.document = {
    getElementById(id) {
        if (id === "stationSearchForm") {
            return {
                addEventListener(event, callback) {
                    handlers[event] = callback;
                }
            };
        }
        if (id === "stationSearch") return searchInput;
        throw new Error("Unexpected element: " + id);
    }
};

const stationSelectionStatus = status;

let stationSelectionHandled = false;
let selectedPosition = null;
let selectedZoom = null;
let popupOpened = null;
let detailsOpened = null;

const map = {
    setView(position, zoom) {
        selectedPosition = position;
        selectedZoom = zoom;
    }
};

function showStationDetails(callsign) {
    detailsOpened = callsign;
}

function makeMarker(callsign, lat, lon) {
    return {
        getLatLng() {
            return [lat, lon];
        },
        openPopup() {
            popupOpened = callsign;
        }
    };
}

const markers = {
    "N5BA": makeMarker("N5BA", 33.2, -96.8),
    "N5BA-9": makeMarker("N5BA-9", 33.3, -96.9),
    "W5NGU-3": makeMarker("W5NGU-3", 33.1, -97.0),
    "K5TEST": makeMarker("K5TEST", 32.9, -96.7)
};

function submit(value) {
    searchInput.value = value;

    let prevented = false;
    handlers.submit({
        preventDefault() {
            prevented = true;
        }
    });

    assert.equal(prevented, true);
}

assert.equal(typeof handlers.submit, "function");

// Exact match takes precedence over partial matches.
submit("N5BA");

assert.equal(popupOpened, "N5BA");
assert.equal(detailsOpened, "N5BA");
assert.deepEqual(selectedPosition, [33.2, -96.8]);
assert.equal(selectedZoom, 13);
assert.equal(status.hidden, true);

// Case-insensitive search.
submit("w5ngu-3");

assert.equal(popupOpened, "W5NGU-3");
assert.equal(detailsOpened, "W5NGU-3");

// Unique partial match.
submit("test");

assert.equal(popupOpened, "K5TEST");
assert.equal(detailsOpened, "K5TEST");

// Ambiguous partial match must not select a station.
popupOpened = null;
detailsOpened = null;

submit("N5BA-");

assert.equal(popupOpened, "N5BA-9");

// N5BA matches two stations, so no automatic selection.
popupOpened = null;
detailsOpened = null;

submit("N5");

assert.equal(popupOpened, null);
assert.equal(detailsOpened, null);
assert.equal(status.hidden, false);
assert.match(status.textContent, /2 stations match/);

// Unknown callsign produces a useful message.
submit("ZZ9UNKNOWN");

assert.equal(status.hidden, false);
assert.match(
    status.textContent,
    /has no known position on this map/
);

// Empty search.
submit(" ");

assert.equal(status.hidden, false);
assert.match(
    status.textContent,
    /Enter a callsign/
);

// Manual searches supersede pending deep links.
stationSelectionHandled = false;
submit("K5TEST");

assert.equal(stationSelectionHandled, true);
assert.equal(detailsOpened, "K5TEST");

console.log("Map station search behavior: PASS");
"""

        result = subprocess.run(
            [node, "-e", harness.replace(
                'assert.equal(typeof handlers.submit, "function");',
                source + '\nassert.equal(typeof handlers.submit, "function");',
            )],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(
            result.returncode,
            0,
            "Map station search behavioral test failed:\n"
            + result.stdout
            + result.stderr,
        )

    def test_map_station_selection_support(self):
        html = tncstation_rest.MAP_HTML

        self.assertIn(
            'params.get("station")',
            html,
        )
        self.assertIn(
            'id="stationSelectionStatus"',
            html,
        )
        self.assertIn(
            "stationSelectionHandled = selectMapStation(requestedStation)",
            html,
        )
        self.assertIn(
            "var marker = markers[selected]",
            html,
        )
        self.assertIn(
            "marker.openPopup()",
            html,
        )
        self.assertIn(
            "showStationDetails(selected)",
            html,
        )
        self.assertIn(
            "stationSelectionHandled = true",
            html,
        )
        self.assertIn(
            "has no known position on this map",
            html,
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
        self.assertIn("tncstation PBBS", html)
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
            msg_type="P", sender="N0CALL", via="N5BA-10",
            recipient="N5BA-10", mid="12345_N0CALL", size=42,
            raw="FB P N0CALL N5BA-10 N5BA-10 12345_N0CALL 42",
        )

        daemon.kam.check_winlink_mail = lambda **kwargs: [
            WinlinkMessage(
                title="Test Subject", body="Hello there.",
                proposal=proposal, raw="Test Subject\r\nHello there.",
            )
        ]

        status, payload = self.request(
            port, "POST", "/winlink/check",
            body={"gateway": "N5BA-10", "password": "FOOBAR"}
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
            body={"gateway": "N5BA-10"}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_check_mail_requires_auth_when_enabled(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "POST", "/winlink/check",
            body={"gateway": "N5BA-10", "password": "FOOBAR"},
            token=None
        )

        self.assertEqual(status, 401)

    def test_send_message(self):
        daemon, port = self.start_stack(ScriptedSerial({}))

        daemon.kam.send_winlink_message = lambda **kwargs: ["12345_N5BA"]

        status, payload = self.request(
            port, "POST", "/winlink/send",
            body={
                "gateway": "N5BA-10",
                "password": "FOOBAR",
                "messages": [
                    {"to": ["N0CALL"], "subject": "Hi", "body": "Hello"}
                ],
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], ["12345_N5BA"])

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
                "gateway": "N5BA-10",
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
            body={"gateway": "N5BA-10", "password": "FOOBAR"}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_send_message_empty_messages_list_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "POST", "/winlink/send",
            body={"gateway": "N5BA-10", "password": "FOOBAR", "messages": []}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "MissingParam")

    def test_send_message_missing_message_field_is_400(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "POST", "/winlink/send",
            body={
                "gateway": "N5BA-10",
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
                "gateway": "N5BA-10",
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
        self.assertIn("tncstation Winlink", html)
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
    tncstation_daemon.py calls through, same technique as
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

        status, payload = self.request(port, "GET", "/winlink/account/N5BA")

        self.assertEqual(status, 200)
        self.assertTrue(payload["result"])

    def test_account_exists_requires_auth_when_enabled(self):
        self._patch("account_exists", lambda callsign, api_key, **kwargs: True)

        _, port = self.start_stack(ScriptedSerial({}))

        status, _ = self.request(
            port, "GET", "/winlink/account/N5BA", token=None
        )

        self.assertEqual(status, 401)

    def test_missing_api_key_is_daemon_error(self):
        os.environ.pop("WINLINK_API_KEY", None)

        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/winlink/account/N5BA")

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

class ReceptionHistoryEndpointTests(RestTestCase):
    """End-to-end tests for GET /aprs/history."""

    def test_history_disabled(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "GET", "/aprs/history"
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        history = payload["result"]

        self.assertFalse(history["enabled"])
        self.assertEqual(history["scope"], "persistent")
        self.assertEqual(history["total_packets"], 0)
        self.assertEqual(history["hourly_counts"], [])

    def test_history_requires_authentication(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "GET", "/aprs/history", token=None
        )

        self.assertEqual(status, 401)
        self.assertIn("error", payload)

    def test_history_rejects_invalid_since(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "GET", "/aprs/history?since=yesterday"
        )

        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            payload["error"]["type"],
            "InvalidParam",
        )

    def test_history_returns_persisted_packets(self):
        """HTTP history endpoint returns packets stored in SQLite."""

        daemon, port = self.start_stack(
            ScriptedSerial({}),
            history_db=True,
        )

        packet = Packet(
            source="N5BA",
            destination="APRS",
            digipeaters=("W5NGU-3*",),
            port=2,
            payload=">SQLite REST integration test",
            raw="N5BA>APRS,W5NGU-3*:>SQLite REST integration test",
            frame_type="UI",
        )

        daemon._handle_packet(packet)

        # Wait for the background SQLite writer to initialize.
        self.assertTrue(
            daemon._reception_writer.wait_ready(timeout=5.0)
        )

        # Flush the queued packet before querying through HTTP.
        daemon._reception_writer.stop()

        status, payload = self.request(
            port,
            "GET",
            "/aprs/history",
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        history = payload["result"]

        self.assertTrue(history["enabled"])
        self.assertEqual(history["scope"], "persistent")
        self.assertEqual(history["total_packets"], 1)

        self.assertEqual(
            sum(
                entry["packet_count"]
                for entry in history["hourly_counts"]
            ),
            1,
        )

class ReceptionStatsEndpointTests(RestTestCase):
    """End-to-end tests for GET /aprs/stats."""

    def test_empty_reception_stats(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/aprs/stats")

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        stats = payload["result"]

        self.assertEqual(stats["scope"], "since_daemon_start")
        self.assertEqual(stats["total_packets"], 0)
        self.assertEqual(stats["unique_sources"], 0)
        self.assertEqual(stats["top_sources"], [])
        self.assertEqual(stats["top_digipeaters"], [])
        self.assertEqual(stats["used_digipeater_packets"], 0)
        self.assertEqual(
            stats["no_identified_digipeater_packets"], 0
        )

    def test_reception_stats_with_packets(self):
        daemon, port = self.start_stack(ScriptedSerial({}))

        for source, digipeaters in (
            ("N5BA", ("W5NGU-3*", "WIDE2-1")),
            ("N5BA", ("W5NGU-3*",)),
            ("K5TEST", ()),
        ):
            daemon._handle_packet(Packet(
                source=source,
                destination="APRS",
                digipeaters=digipeaters,
                port=2,
                payload=">Test packet",
                raw="",
                frame_type="UI",
            ))

        status, payload = self.request(port, "GET", "/aprs/stats")

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

        stats = payload["result"]

        self.assertEqual(stats["total_packets"], 3)
        self.assertEqual(stats["unique_sources"], 2)
        self.assertEqual(
            stats["top_sources"][0],
            {"callsign": "N5BA", "packets": 2},
        )
        self.assertEqual(
            stats["top_digipeaters"],
            [{"callsign": "W5NGU-3", "packets": 2}],
        )
        self.assertEqual(stats["used_digipeater_packets"], 2)
        self.assertEqual(
            stats["no_identified_digipeater_packets"], 1
        )

    def test_reception_stats_requires_authentication(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(
            port, "GET", "/aprs/stats", token=None
        )

        self.assertEqual(status, 401)
        self.assertIn("error", payload)

class StationsEndpointTests(RestTestCase):
    """
    Milestone 7. Populates daemon._stations directly via
    StationTracker.update() rather than driving real MONITOR traffic
    through ChunkSerial (already covered end-to-end in
    tests/test_daemon.py's StationsTests) -- these are only about the
    REST layer's own routing and response shape.
    """

    def _seed_station(self, daemon, callsign="N5BA-9"):
        packet = Packet(
            source=callsign,
            destination="APRS",
            digipeaters=(),
            port=1,
            payload="!4903.50N/07201.75W-Test comment",
            raw="",
            frame_type="UI",
        )
        daemon._stations.update(packet)

    def test_stations_list(self):
        daemon, port = self.start_stack(ScriptedSerial({}))
        self._seed_station(daemon)

        status, payload = self.request(port, "GET", "/stations")

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["result"]), 1)
        self.assertEqual(payload["result"][0]["callsign"], "N5BA-9")
        self.assertEqual(payload["result"][0]["comment"], "Test comment")

    def test_stations_list_empty_by_default(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/stations")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], [])

    def test_stations_get_known_callsign(self):
        daemon, port = self.start_stack(ScriptedSerial({}))
        self._seed_station(daemon)

        status, payload = self.request(port, "GET", "/stations/N5BA-9")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["callsign"], "N5BA-9")

    def test_stations_get_unknown_callsign_returns_none(self):
        _, port = self.start_stack(ScriptedSerial({}))

        status, payload = self.request(port, "GET", "/stations/NOBODY")

        self.assertEqual(status, 200)
        self.assertIsNone(payload["result"])

    def test_expired_station_not_listed(self):
        daemon, port = self.start_stack(ScriptedSerial({}))
        self._seed_station(daemon)

        # Make the station older than the one-hour expiration limit.
        with daemon._stations_lock:
            daemon._stations.update(
                Packet(
                    source="N5BA-9",
                    destination="APRS",
                    digipeaters=(),
                    port=1,
                    payload="!4903.50N/07201.75W-Test comment",
                    raw="",
                    frame_type="UI",
                ),
                now=1000.0,
            )

        status, payload = self.request(port, "GET", "/stations")

        self.assertEqual(status, 200)
        self.assertEqual(payload["result"], [])

    def test_expired_station_get_returns_none(self):
        daemon, port = self.start_stack(ScriptedSerial({}))
        self._seed_station(daemon)
        with daemon._stations_lock:
            daemon._stations.update(
                Packet(
                    source="N5BA-9",
                    destination="APRS",
                    digipeaters=(),
                    port=1,
                    payload="!4903.50N/07201.75W-Test comment",
                    raw="",
                    frame_type="UI",
                ),
                now=1000.0,
            )


        status, payload = self.request(
            port, "GET", "/stations/N5BA-9"
        )

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
        self.assertIn("tncstation station map", html)
        self.assertIn("/stations", html)
        self.assertIn("leaflet", html.lower())

    def test_map_page_renders_real_aprs_icons(self):
        # Milestone 7 extension: markers use real APRS symbol-table
        # icons (hessu/aprs-symbols sprites + OK-DMR/aprs-symbols'
        # lookup logic ported inline), not Leaflet's plain default
        # marker -- see tncstation_rest.py's module comment above MAP_HTML.
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
            len(inline_scripts), 3,
            "expected three inline <script> bodies (theme, features, page)"
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", delete=False
        ) as handle:
            # Each script is checked; joined so one node --check covers both.
            handle.write("\n;\n".join(inline_scripts))
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

    def test_map_station_details_markup(self):
        html = tncstation_rest.MAP_HTML

        self.assertIn('id="stationDetails"', html)
        self.assertIn('id="closeStationDetails"', html)
        self.assertIn('id="detailCallsign"', html)
        self.assertIn('id="stationDetailsBody"', html)
        self.assertIn('class="viewStationDetails"', html.replace(
            "class='viewStationDetails'", 'class="viewStationDetails"'
        ))

        self.assertIn("showStationDetails(callsign)", html)
        self.assertIn('authedUrl("/stations/"', html)
        self.assertIn("encodeURIComponent(callsign)", html)
        self.assertIn("escapeHtml(String(value))", html)
        self.assertIn("stationDetails.hidden = true", html)
        self.assertIn("Station no longer available.", html)
        self.assertIn("Unable to load station details:", html)

    def test_map_digipeater_controls(self):
        html = tncstation_rest.MAP_HTML

        self.assertIn('id="showDigiPaths"', html)
        self.assertIn('id="digiStationFilter"', html)
        self.assertIn("Show Digipeater Paths", html)
        self.assertIn('new Option("All stations", "")', html)

    def test_map_digipeater_layer_lifecycle(self):
        html = tncstation_rest.MAP_HTML

        self.assertIn("L.layerGroup()", html)
        self.assertIn("digiPaths.addTo(map)", html)
        self.assertIn("map.removeLayer(digiPaths)", html)
        self.assertIn("digiPaths.clearLayers()", html)
        self.assertIn("updateDigipeaterPaths(stations)", html)

    def test_map_digipeater_resolver_present(self):
        html = tncstation_rest.MAP_HTML

        self.assertIn("function resolveDigipeaterLinks(stations)", html)
        self.assertIn("function drawDigipeaterPaths()", html)
        self.assertIn('entry.endsWith("*")', html)
        self.assertIn("seen[key]", html)
        self.assertIn("bindTooltip(", html)

    def test_map_digipeater_resolver_behavior(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")

        html = tncstation_rest.MAP_HTML

        match = re.search(
            r"  function resolveDigipeaterLinks\(stations\) \{"
            r"(.*?)"
            r"\n  \}\n",
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match, "Digipeater resolver not found")

        function_source = (
            "function resolveDigipeaterLinks(stations) {"
            + match.group(1)
            + "\n}"
        )

        harness = r"""
const assert = require("node:assert/strict");

""" + function_source + r"""

const stations = [
  {
    callsign: "N5BA",
    latitude: 33.2,
    longitude: -96.8,
    recent_packets: [
      { digipeaters: ["W5NGU-3*", "WIDE2-1"] },
      { digipeaters: ["W5NGU-3*", "WIDE2-1"] },
      { digipeaters: ["WIDE2-1"] }
    ]
  },
  {
    callsign: "W5NGU-3",
    latitude: 33.3,
    longitude: -97.1,
    recent_packets: []
  },
  {
    callsign: "K5TEST",
    latitude: 33.4,
    longitude: -96.9,
    recent_packets: [
      { digipeaters: ["UNKNOWN-1*"] },
      { digipeaters: ["WIDE1-1*"] }
    ]
  }
];

const links = resolveDigipeaterLinks(stations);

assert.equal(links.length, 1);
assert.equal(links[0].source, "N5BA");
assert.equal(links[0].digipeater, "W5NGU-3");
assert.deepEqual(links[0].from, [33.2, -96.8]);
assert.deepEqual(links[0].to, [33.3, -97.1]);

console.log("Digipeater resolver behavior: PASS");
"""

        result = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(
            result.returncode,
            0,
            "Digipeater resolver JavaScript test failed:\n"
            + result.stderr,
        )

    def test_map_digipeater_filter_behavior(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")

        html = tncstation_rest.MAP_HTML

        def extract_function(name, args):
            pattern = (
                r"  function " + re.escape(name) +
                r"\(" + re.escape(args) + r"\) \{"
                r"(.*?)\n  \}\n"
            )
            match = re.search(pattern, html, re.DOTALL)
            self.assertIsNotNone(match, name + " not found")
            return (
                "function " + name + "(" + args + ") {"
                + match.group(1) + "\n}"
            )

        source = "\n".join([
            extract_function("drawDigipeaterPaths", ""),
            extract_function("updateDigipeaterPaths", "stations"),
        ])

        harness = r"""
const assert = require("node:assert/strict");

const rendered = [];
let clears = 0;

const digiPaths = {
  clearLayers() {
    clears++;
    rendered.length = 0;
  }
};

const digiStationFilter = {
  value: "",
  options: [],
  replaceChildren(...options) {
    this.options = options;
    this.value = "";
  },
  add(option) {
    this.options.push(option);
  }
};

function Option(text, value) {
  this.text = text;
  this.value = value;
}

const L = {
  polyline(points, options) {
    return {
      bindTooltip() {
        return this;
      },
      addTo() {
        rendered.push(points);
        return this;
      }
    };
  }
};

function escapeHtml(value) {
  return String(value);
}

let currentDigiLinks = [];

function resolveDigipeaterLinks(stations) {
  return stations;
}

""" + source + r"""

const links = [
  {
    source: "N5BA",
    digipeater: "W5NGU-3",
    from: [33.2, -96.8],
    to: [33.3, -97.1]
  },
  {
    source: "K5TEST",
    digipeater: "W5NGU-3",
    from: [33.4, -96.9],
    to: [33.3, -97.1]
  }
];

// All stations: both links visible.
updateDigipeaterPaths(links);
assert.equal(rendered.length, 2);
assert.deepEqual(
  digiStationFilter.options.map(option => option.value),
  ["", "K5TEST", "N5BA"]
);

// Selecting N5BA displays only its link.
digiStationFilter.value = "N5BA";
drawDigipeaterPaths();
assert.equal(rendered.length, 1);
assert.deepEqual(rendered[0][0], [33.2, -96.8]);

// Refresh preserves the selected station.
updateDigipeaterPaths(links);
assert.equal(digiStationFilter.value, "N5BA");
assert.equal(rendered.length, 1);

// If the selected station disappears, reset to all.
updateDigipeaterPaths([links[1]]);
assert.equal(digiStationFilter.value, "");
assert.equal(rendered.length, 1);

// An empty update clears the paths and dropdown.
updateDigipeaterPaths([]);
assert.equal(rendered.length, 0);
assert.equal(digiStationFilter.options.length, 1);
assert.ok(clears >= 5);

console.log("Digipeater filter behavior: PASS");
"""

        result = subprocess.run(
            [node, "-e", harness],
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(
            result.returncode,
            0,
            "Digipeater filter JavaScript test failed:\n"
            + result.stderr,
        )

    def test_map_station_details_request_generation(self):
        html = tncstation_rest.MAP_HTML

        self.assertIn("var stationDetailsRequestId = 0;", html)
        self.assertIn("var requestId = ++stationDetailsRequestId;", html)
        self.assertIn("stationDetailsRequestId++;", html)

        self.assertGreaterEqual(
            html.count("if (requestId !== stationDetailsRequestId) return;"),
            2,
        )

    def test_map_page_requires_auth_when_enabled(self):
        _, port = self.start_stack(ScriptedSerial({}))

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)

        conn.request("GET", "/map")
        response = conn.getresponse()
        response.read()

        self.assertEqual(response.status, 401)

    def test_stations_list_includes_position_history(self):
        daemon, port = self.start_stack(ScriptedSerial({}))
        self._seed_station(daemon)

        status, payload = self.request(port, "GET", "/stations")

        self.assertEqual(status, 200)

        station = payload["result"][0]
        history = station["history"]

        self.assertEqual(len(history), 1)
        self.assertAlmostEqual(
            history[0]["latitude"], station["latitude"]
        )
        self.assertAlmostEqual(
            history[0]["longitude"], station["longitude"]
        )
        self.assertEqual(
            history[0]["timestamp"], station["last_heard"]
        )
    def test_stations_get_includes_recent_packets(self):
        daemon, port = self.start_stack(ScriptedSerial({}))
        self._seed_station(daemon)

        status, payload = self.request(
            port, "GET", "/stations/N5BA-9"
        )

        self.assertEqual(status, 200)

        station = payload["result"]
        self.assertIn("recent_packets", station)
        self.assertEqual(len(station["recent_packets"]), 1)

        packet = station["recent_packets"][0]
        self.assertEqual(packet["destination"], "APRS")
        self.assertEqual(
            packet["payload"],
            "!4903.50N/07201.75W-Test comment",
        )
        self.assertEqual(packet["frame_type"], "UI")

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
            "N5BA-4>BEACON/2:\r\n",
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
                    try:
                        conn.sendall(
                            (json.dumps(
                                {"id": "1", "ok": True, "result": "slow"}
                            ) + "\n").encode("ascii")
                        )
                    except (BrokenPipeError, ConnectionResetError):
                        # Expected when the timeout test disconnects
                        # before the delayed response is sent.
                        pass
                finally:
                    conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        # Cleanups run in reverse registration order.
        # Signal shutdown, wait for the worker, then close the socket.
        self.addCleanup(server_sock.close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(stop.set)

        return socket_path

    def test_default_timeout_too_short_raises_daemon_timeout(self):
        socket_path = self.start_slow_daemon(delay=0.3)
        client = tncstation_rest.DaemonClient(socket_path, timeout=0.1)

        with self.assertRaises(tncstation_rest.DaemonTimeout):
            client.call("ping")

    def test_per_call_socket_timeout_override_waits_long_enough(self):
        # Same slow daemon and the same short default -- but this call
        # asks for a longer wait, exactly like _h_connect/_h_disconnect
        # do when a caller passes a large "timeout" body field.
        socket_path = self.start_slow_daemon(delay=0.3)
        client = tncstation_rest.DaemonClient(socket_path, timeout=0.1)

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
                                "data": {"source": "N5BA-4"},
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
        client = tncstation_rest.DaemonClient(socket_path, timeout=0.05)

        events = client.stream_events("monitor.subscribe")

        for _ in range(5):
            self.assertIsNone(next(events))

    def test_receives_event_after_keepalives(self):
        # Mirrors the real usage pattern: idle for a while (nothing on
        # the air yet), then a packet actually arrives.
        socket_path = self.start_idle_daemon(event_after=0.2)
        client = tncstation_rest.DaemonClient(socket_path, timeout=0.05)

        events = client.stream_events("monitor.subscribe")

        seen_event = None
        for _ in range(20):
            item = next(events)

            if item is not None:
                seen_event = item
                break

        self.assertIsNotNone(seen_event, "Never received the event")
        self.assertEqual(seen_event["data"]["source"], "N5BA-4")


if __name__ == "__main__":
    unittest.main()
