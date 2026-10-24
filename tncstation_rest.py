"""
REST API in front of tncstation_daemon.py -- translates HTTP requests into
the daemon's newline-delimited JSON protocol over its Unix socket, and
proxies its "packet" events out as Server-Sent Events for live
monitoring over plain HTTP (no websockets needed).

This process does NOT talk to the KAM-XL directly -- it's a thin HTTP
front end for the daemon, which remains the single owner of the
serial connection. Run tncstation_daemon.py first.

Run directly:

    python3 tncstation_rest.py --daemon-socket /tmp/tncstation.sock --port 8080

See docs/rest_api.md for the full endpoint list, authentication, and
usage examples.

Security note: binds to 0.0.0.0 (all interfaces) by default, since
the point is reaching it from other devices on the LAN (a phone,
another PC). That means it needs authentication -- a bearer token,
checked on every request. The token still travels in cleartext over
plain HTTP, though, which is an acceptable bar for a trusted home LAN
but NOT safe to expose over the open internet (e.g. via port
forwarding) without putting TLS in front of it first.
"""

import argparse
import errno
import json
import logging
import os
import re
import secrets
import select
import signal
import socket
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from envfile import env_flag, load_dotenv as _load_dotenv
from tncstation_web import load_page
from urllib.parse import urlparse, parse_qs


DEFAULT_DAEMON_SOCKET = "/tmp/tncstation.sock"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080

# Daemon error types that mean "the request was well-formed, but the
# KAM-XL (or the daemon itself) couldn't fulfill it" -- as opposed to
# a malformed request, which is a client-side (4xx) problem.
_DAEMON_ERROR_STATUS = 502

# Dashboard: the landing page at GET / (moved there from the terminal
# page below, which now lives at GET /terminal -- see docs/history.md for
# the reasoning). A self-contained page summarizing the other four:
# KAM-XL serial link health (GET /status), APRS station count/most-
# recently-heard (GET /stations), and a manual "Check PBBS" button
# (GET /pbbs/messages). Deliberately does NOT auto-check Winlink mail
# or auto-check PBBS on load -- unlike /status and /stations (cheap,
# passive reads of data the daemon already has in memory), both PBBS
# and Winlink mail checks drive a real AX.25 connect/command/
# disconnect cycle against actual radio hardware (see the comments
# above the PBBS and Winlink route handlers below); auto-triggering
# that just from loading a dashboard would be surprising on-air
# activity and a slow page load, not a good default. The Winlink card
# is a static shortcut only (no auto data pull at all) -- checking
# mail needs a password, which has no business being entered anywhere
# but the dedicated Winlink page's own form.
# Markup, styles and script: tncstation_web/dashboard/ (index.html, style.css,
# app.js) -- see tncstation_web/__init__.py.
DASHBOARD_HTML = load_page("dashboard")

# Milestone 4 (terminal) + milestone 5 (live monitor): a single
# self-contained page (no build step, no CDN dependency) served
# directly by this process at GET /terminal -- a live packet feed on
# top (EventSource against /monitor/stream), a raw Terminal Mode
# command box below it (same as milestone 4). Reads ?token=... from
# its own URL and carries it forward on every request it makes, since
# that's the auth mechanism this REST layer accepts specifically so a
# browser context (which can't always set a custom header) can use.
# Markup, styles and script: tncstation_web/terminal/ (index.html, style.css,
# app.js) -- see tncstation_web/__init__.py.
TERMINAL_HTML = load_page("terminal")

# Milestone 6: a second self-contained page, served at GET /pbbs, for
# the KAM-XL's built-in firmware PBBS (mailbox): list messages, click
# one to read it, delete it (first read-only, by Dave's choice). Kept as
# its own page rather than folded into the terminal/monitor page --
# unlike those two (both live, continuously-updating views that
# naturally share screen space), this is list/detail navigation, a
# different enough shape to stand on its own. Linked from the
# terminal page's header and back again, both carrying ?token=
# forward the same way the terminal and monitor pane already do.
# Markup, styles and script: tncstation_web/pbbs/ (index.html, style.css,
# app.js) -- see tncstation_web/__init__.py.
PBBS_HTML = load_page("pbbs")

# Milestone 8: a page served at GET /winlink for checking (and, since
# the send-support extension, sending) Winlink mail. Two tabs sharing
# one page: "Check mail" (a form posting to POST /winlink/check,
# unchanged since milestone 8) and "Send mail" (a form posting to POST
# /winlink/send -- send-only, text-body-only, B2 only; see winlink.py's
# module docstring's "SEND SUPPORT" note for the full scope and its
# UNVERIFIED-AGAINST-A-REAL-GATEWAY caveat, which applies to this UI
# too). The password field on either form is never persisted anywhere
# (no localStorage, no cookie) and is cleared after every submit
# attempt, success or failure -- typed fresh each time, the same
# "acceptable for a trusted home LAN, not the open internet" posture
# already documented for the bearer token (see this file's own module
# docstring), extended here to a real Winlink account password rather
# than assumed to be fine without saying so.
# Markup, styles and script: tncstation_web/winlink/ (index.html, style.css,
# app.js) -- see tncstation_web/__init__.py.
WINLINK_HTML = load_page("winlink")

# Milestone 7: a third self-contained page, served at GET /map, showing
# stations.py's station database on a Leaflet + OpenStreetMap map.
# Leaflet is loaded from cdnjs (this project's one allowed CDN for
# browser-side JS, per its own conventions) -- the only external
# network dependency in this whole project, and only for the browser
# viewing this page, not for tncstation_rest.py itself (which never talks
# to the internet).
#
# Markers use real APRS symbol-table/symbol-code icons (Dave's explicit
# choice: the full ~190-symbol set from an external icon pack, not a
# hand-built SVG subset or plain color-coded dots -- see docs/history.md).
# This is a deliberate, one-off exception to the cdnjs-only rule above,
# since no APRS icon set is available there. Two real, separately
# licensed third-party sources are used, both pinned to a specific
# commit rather than an unpinned "latest" branch or tag:
#
#   - Sprite PNGs: hessu/aprs-symbols (github.com/hessu/aprs-symbols),
#     the APRS icon set maintained by Heikki Hannikainen/OH7LZB
#     (operator of aprs.fi). Licensing is mixed per-symbol (mostly
#     CC-BY-SA 2.0 original work, some public-domain-sourced elements)
#     -- see that repo's COPYRIGHT.md. Pinned to commit
#     f2286a9cd43eb6ba4501250b4c39fff111e3796c (2024-10-10) and served
#     via jsdelivr's GitHub-raw CDN.
#   - Symbol lookup table and sprite-grid math: ported inline (below,
#     in the page's own <script>) from OK-DMR/aprs-symbols'
#     aprs-symbols.js/aprs-symbols.css (github.com/OK-DMR/aprs-symbols,
#     MIT License, Copyright (c) 2019 Marek Sebera) -- only the ~15
#     lines of translation-table/grid-position logic are reused, not
#     loaded from a CDN.
#
# Real APRS symbol tables are literally "/" (primary) or "\" (alternate)
# -- any other symbol_table character means an *alternate table with
# overlay* (a digit/letter meant to be drawn over the base icon, e.g. a
# numbered object). This page renders the alternate table's base icon
# for that case, with the overlay character drawn as a small text
# badge in the icon's corner -- not as sprite art, because
# hessu/aprs-symbols has none: its own README says outright "does not
# contain additional symbols for overlays yet ... Maybe later!" (the
# aprs-symbols-SIZE-2.png filename is reserved for it but has no real
# artwork behind it as of the pinned commit above). Drawing a text
# badge instead of guessing at or fabricating overlay art keeps this
# consistent with the project's "never fabricate third-party art"
# rule while still surfacing the real information.
# symbol_table/symbol_code are still also shown as text in each
# marker's popup, as supplementary/debugging info.
# Markup, styles and script: tncstation_web/map/ (index.html, style.css,
# app.js) -- see tncstation_web/__init__.py.
MAP_HTML = load_page("map")

# Packet log search page: tncstation_web/log/.
LOG_HTML = load_page("log")

# Telemetry graphs page: tncstation_web/telemetry/.
TELEMETRY_HTML = load_page("telemetry")

logger = logging.getLogger("tncstation_rest")
logger.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# Daemon client
# ---------------------------------------------------------------------------

class DaemonUnavailable(Exception):
    """Couldn't reach the daemon's Unix socket at all."""
    pass


class DaemonTimeout(Exception):
    """
    The daemon didn't send a response within the expected time.

    Distinct from DaemonUnavailable: the daemon is reachable, it's just
    not answering yet -- most often because the underlying KAM-XL
    command (or AX.25 connect/disconnect) is still legitimately in
    progress and hasn't hit its own timeout yet.
    """
    pass


class DaemonClient:
    """
    Talks to a running tncstation_daemon.py over its Unix socket.

    Opens a fresh connection per call (and per SSE stream) rather than
    holding one open and sharing it across HTTP handler threads --
    simpler to reason about correctness-wise than synchronizing a
    single shared connection, and Unix socket connections are cheap
    enough that this isn't a meaningful cost for a LAN tool.
    """

    # Default socket-level read timeout for a single call(). Must stay
    # comfortably above kamxl.py's own default command_timeout (10s) --
    # otherwise this socket read gives up before the daemon has even
    # had a chance to hit *its* timeout and send back a clean JSON
    # error, and callers see a raw, unhandled socket.timeout instead.
    # Endpoints that relay a caller-supplied timeout (connect/
    # disconnect/read_connected, which can legitimately run well past
    # this) pass their own, larger _socket_timeout per call instead of
    # relying on this default -- see tncstation_rest.py's _h_connect etc.
    def __init__(self, socket_path: str, timeout: float = 15) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def call(
        self,
        method: str,
        _socket_timeout: Optional[float] = None,
        **params: Any
    ) -> Dict[str, Any]:
        sock = self._connect(_socket_timeout)

        try:
            sock.sendall(self._encode("1", method, params))

            rfile = sock.makefile("r")

            try:
                line = rfile.readline()
            except socket.timeout:
                raise DaemonTimeout(
                    f"Daemon didn't respond to {method!r} within "
                    f"{_socket_timeout if _socket_timeout is not None else self.timeout}s"
                )

            if not line:
                raise DaemonUnavailable(
                    "Daemon closed the connection unexpectedly"
                )

            return json.loads(line)
        finally:
            sock.close()

    def stream_events(self, method: str, **params: Any):
        """
        Subscribe via ``method`` (e.g. "monitor.subscribe") on a
        dedicated connection and yield each subsequent event dict.
        Yields None roughly every ``self.timeout`` seconds of silence
        instead of blocking forever, so callers (the SSE handler) can
        use that to send a keepalive and notice a closed HTTP client.

        The connection is closed (ending the daemon-side subscription
        too) once the caller stops iterating.
        """
        sock = self._connect()

        try:
            sock.sendall(self._encode("1", method, params))

            rfile = sock.makefile("r")
            ack = rfile.readline()

            if not ack:
                raise DaemonUnavailable(
                    "Daemon closed the connection unexpectedly"
                )

            while True:
                # Wait for readability with select() rather than just
                # calling readline() and catching socket.timeout.
                # Real-hardware bug: once a timed-out read fires once
                # on a socket.makefile() object, CPython's SocketIO
                # sets a sticky _timeout_occurred flag -- every *later*
                # read on that same file object then raises
                # "OSError: cannot read from timed out object"
                # immediately, instead of actually trying again. That
                # silently killed this generator (and the SSE stream
                # built on it) after the very first keepalive with no
                # packets, observed live: the connection would run for
                # exactly one ``self.timeout`` window, crash, and
                # EventSource would reconnect and repeat -- meaning
                # any packet arriving during the brief reconnect gap
                # was lost. select() only calls readline() once data
                # is actually known to be waiting, so the socket-level
                # timeout path (and that sticky flag) is never hit.
                ready, _, _ = select.select([sock], [], [], self.timeout)

                if not ready:
                    yield None
                    continue

                line = rfile.readline()

                if not line:
                    return

                line = line.strip()

                if line:
                    yield json.loads(line)
        finally:
            sock.close()

    def _connect(self, timeout: Optional[float] = None) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout if timeout is not None else self.timeout)

        try:
            sock.connect(self.socket_path)
        except (ConnectionRefusedError, FileNotFoundError) as exc:
            sock.close()
            raise DaemonUnavailable(
                f"Can't reach tncstation_daemon.py at {self.socket_path}: {exc}"
            )

        return sock

    @staticmethod
    def _encode(request_id: str, method: str, params: Dict[str, Any]) -> bytes:
        return (json.dumps({
            "id": request_id,
            "method": method,
            "params": params,
        }) + "\n").encode("ascii")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

# Each route: (HTTP method, compiled path regex, handler method name).
# Handlers are looked up by name on RESTRequestHandler so they can be
# plain instance methods with access to self.daemon / self._send_json
# / etc.
ROUTES: Tuple[Tuple[str, "re.Pattern", str], ...] = (
    ("GET", re.compile(r"^/ping$"), "_h_ping"),
    ("GET", re.compile(r"^/status$"), "_h_status"),
    ("GET", re.compile(r"^/configuration$"), "_h_get_configuration"),
    ("GET", re.compile(r"^/params/(?P<command>[A-Za-z0-9_]+)$"), "_h_get_typed"),
    ("PUT", re.compile(r"^/params/(?P<command>[A-Za-z0-9_]+)$"), "_h_set_typed"),
    ("GET", re.compile(r"^/params/(?P<command>[A-Za-z0-9_]+)/raw$"), "_h_get_raw"),
    ("PUT", re.compile(r"^/params/(?P<command>[A-Za-z0-9_]+)/raw$"), "_h_set_raw"),
    ("POST", re.compile(r"^/connect$"), "_h_connect"),
    ("POST", re.compile(r"^/disconnect$"), "_h_disconnect"),
    ("POST", re.compile(r"^/connected/send$"), "_h_send_connected"),
    ("GET", re.compile(r"^/connected/read$"), "_h_read_connected"),
    ("GET", re.compile(r"^/monitor/stream$"), "_h_monitor_stream"),
    ("GET", re.compile(r"^/$"), "_h_dashboard_page"),
    ("GET", re.compile(r"^/terminal$"), "_h_terminal_page"),
    ("POST", re.compile(r"^/terminal/exec$"), "_h_terminal_exec"),
    ("GET", re.compile(r"^/pbbs$"), "_h_pbbs_page"),
    ("GET", re.compile(r"^/pbbs/messages$"), "_h_pbbs_list_messages"),
    ("GET", re.compile(r"^/pbbs/messages/(?P<number>\d+)$"), "_h_pbbs_read_message"),
    ("POST", re.compile(r"^/pbbs/messages/(?P<number>\d+)/delete$"), "_h_pbbs_kill_message"),
    ("GET", re.compile(r"^/map$"), "_h_map_page"),
    ("GET", re.compile(r"^/stations$"), "_h_stations_list"),
    ("GET", re.compile(r"^/stations/history$"), "_h_stations_history"),
    ("GET", re.compile(r"^/objects$"), "_h_objects_list"),
    ("GET", re.compile(r"^/notify$"), "_h_notify_status"),
    ("POST", re.compile(r"^/notify/test$"), "_h_notify_test"),
    ("GET", re.compile(r"^/coverage$"), "_h_coverage_report"),
    ("GET", re.compile(r"^/log$"), "_h_log_page"),
    ("GET", re.compile(r"^/telemetry$"), "_h_telemetry_page"),
    ("GET", re.compile(r"^/telemetry/stations$"), "_h_telemetry_stations"),
    ("GET", re.compile(r"^/telemetry/(?P<callsign>[A-Za-z0-9\-]+)$"), "_h_telemetry_get"),
    ("GET", re.compile(r"^/backup$"), "_h_backup_status"),
    ("POST", re.compile(r"^/backup/now$"), "_h_backup_now"),
    ("GET", re.compile(r"^/packets/search$"), "_h_packets_search"),
    ("POST", re.compile(r"^/coverage/setup$"), "_h_coverage_set_setup"),
    ("GET", re.compile(r"^/stations/(?P<callsign>[A-Za-z0-9\-]+)$"), "_h_stations_get"),
    ("POST", re.compile(r"^/winlink/check$"), "_h_winlink_check_mail"),
    ("POST", re.compile(r"^/winlink/send$"), "_h_winlink_send_message"),
    ("GET", re.compile(r"^/winlink/account/(?P<callsign>[A-Za-z0-9\-]+)$"), "_h_winlink_account_exists"),
    ("GET", re.compile(r"^/winlink/gateways/nearby$"), "_h_winlink_gateways_nearby"),
    ("GET", re.compile(r"^/winlink/gateways$"), "_h_winlink_gateways"),
    ("GET", re.compile(r"^/winlink$"), "_h_winlink_page"),
    ("GET", re.compile(r"^/aprs/stats$"), "_h_aprs_stats"),
    ("GET", re.compile(r"^/aprs/history$"), "_h_aprs_history"),
    ("GET", re.compile(r"^/aprs/messages$"), "_h_aprs_messages"),
    ("POST", re.compile(r"^/aprs/send$"), "_h_aprs_send_message"),
    ("GET", re.compile(r"^/aprs/outbox$"), "_h_aprs_outbox"),
    ("GET", re.compile(r"^/aprs/ack$"), "_h_aprs_ack_status"),
    ("POST", re.compile(r"^/aprs/ack$"), "_h_aprs_ack_set"),
    ("POST", re.compile(r"^/aprs/messages/clear$"), "_h_aprs_clear_messages"),
    ("GET", re.compile(r"^/aprs/beacon$"), "_h_aprs_beacon_status"),
    ("GET", re.compile(r"^/aprs/retry$"), "_h_aprs_retry_status"),
    ("POST", re.compile(r"^/aprs/retry$"), "_h_aprs_retry_set"),
    ("POST", re.compile(r"^/aprs/outbox/clear$"), "_h_aprs_outbox_clear"),
    ("GET", re.compile(r"^/aprs/bulletins$"), "_h_aprs_bulletins"),
    ("POST", re.compile(r"^/aprs/bulletins/clear$"), "_h_aprs_bulletins_clear"),
    ("POST", re.compile(r"^/aprs/beacon$"), "_h_aprs_beacon_set"),
    ("POST", re.compile(r"^/aprs/beacon/now$"), "_h_aprs_beacon_now"),
)



# A ?token=<API key> query parameter (the dashboard pages pass the key
# that way -- see _check_auth()) would otherwise land in the access log
# in full, readable by anyone who can read the journal.
_TOKEN_PARAM_RE = re.compile(r"([?&]token=)[^&\s\"']*", re.IGNORECASE)


def redact_token(text: str) -> str:
    """Replace the value of any ?token=/&token= query parameter with "…"."""
    return _TOKEN_PARAM_RE.sub(r"\1…", text)


class RESTRequestHandler(BaseHTTPRequestHandler):
    # Set on the class by serve() below, so every handler instance
    # (one per request, per BaseHTTPRequestHandler's design) shares
    # the same DaemonClient and API key without needing a custom
    # server subclass to pass them through.
    daemon: DaemonClient
    api_key: Optional[str]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Route stdlib's own per-request access log through our
        # logger instead of straight to stderr, so -v/--verbose and
        # the rest of the logging setup applies to it too. Skips
        # /favicon.ico -- every browser requests it automatically and
        # unauthenticated on first load of any page here, and logging
        # that routine, harmless request (as a 401, no less -- see
        # do_GET() below) just adds noise, not a real access worth a
        # line in the log.
        if urlparse(self.path).path == "/favicon.ico":
            return

        logger.info(
            "%s - %s", self.address_string(), redact_token(fmt % args)
        )

    # -- Dispatch -----------------------------------------------------

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/favicon.ico":
            # Browsers request this unconditionally on first load of
            # any page here, without the Authorization header or
            # ?token= -- there's no cookie/session for them to have
            # picked it up from. Left to the normal dispatch path,
            # that always fails auth and logs a 401 that looks like a
            # security problem but is really just a browser being a
            # browser. Answering 204 here (no favicon to serve, and
            # none needed) tells the browser "nothing here, don't ask
            # again" instead -- deliberately bypasses _check_auth()
            # entirely, since there's nothing sensitive being served.
            self.send_response(204)
            self.end_headers()
            return

        self._dispatch("GET")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, http_method: str) -> None:
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        method_matched_path = False

        for route_method, pattern, handler_name in ROUTES:
            match = pattern.match(path)

            if match is None:
                continue

            method_matched_path = True

            if route_method != http_method:
                continue

            handler = getattr(self, handler_name)

            try:
                handler(match.groupdict(), query)
            except DaemonTimeout as exc:
                self._send_json(504, {
                    "ok": False,
                    "error": {"type": "DaemonTimeout", "message": str(exc)},
                })
            except DaemonUnavailable as exc:
                self._send_json(503, {
                    "ok": False,
                    "error": {"type": "DaemonUnavailable", "message": str(exc)},
                })
            except Exception as exc:
                logger.exception("Unhandled error in %s", handler_name)
                self._send_json(500, {
                    "ok": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                })

            return

        if method_matched_path:
            self._send_json(405, {
                "ok": False,
                "error": {"type": "MethodNotAllowed", "message": f"{http_method} not allowed on {path}"},
            })
        else:
            self._send_json(404, {
                "ok": False,
                "error": {"type": "NotFound", "message": f"No such endpoint: {path}"},
            })

    def _check_auth(self) -> bool:
        if not self.api_key:
            return True

        header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.api_key}"

        if secrets.compare_digest(header, expected):
            return True

        # The web terminal (milestone 4) and live monitor streaming
        # (milestone 5) both run in a browser context that can't
        # always set a custom header -- EventSource never can, and a
        # page loaded from a bare URL shouldn't require typing the
        # token into a JS prompt. Accept the same token as a
        # `?token=` query parameter as a fallback. Checked second
        # (the header is still preferred) since a URL is more likely
        # to end up logged or in browser history than a header is.
        query = parse_qs(urlparse(self.path).query)
        token = query.get("token", [None])[0]

        if token is not None and secrets.compare_digest(token, self.api_key):
            return True

        self._send_json(401, {
            "ok": False,
            "error": {
                "type": "Unauthorized",
                "message": (
                    "Missing or invalid Authorization: Bearer <token> "
                    "header (or ?token=<key> query parameter)"
                ),
            },
        })
        return False

    # -- Body / response helpers --------------------------------------

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))

        if length == 0:
            return {}

        raw = self.rfile.read(length)

        if not raw.strip():
            return {}

        return json.loads(raw)

    def _send_json(self, status: int, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _relay(
        self,
        method: str,
        _socket_timeout: Optional[float] = None,
        **params: Any
    ) -> None:
        """
        Call a daemon method and translate its {"ok": ...} response
        directly into the equivalent HTTP response.

        _socket_timeout overrides how long we wait on our end for the
        daemon's reply. It must stay above whatever "timeout" (if any)
        is being passed through in **params for the underlying KAM-XL
        operation -- otherwise this call gives up before the daemon
        could possibly have an answer yet, surfacing as a raw
        DaemonTimeout instead of the operation actually completing.
        """
        response = self.daemon.call(
            method,
            _socket_timeout=_socket_timeout,
            **params
        )

        if response.get("ok"):
            self._send_json(200, {"ok": True, "result": response.get("result")})
            return

        error = response.get("error") or {}

        status = (
            400
            if error.get("type") == "MissingParam"
            else _DAEMON_ERROR_STATUS
        )

        self._send_json(status, {"ok": False, "error": error})

    # -- Endpoint handlers ----------------------------------------------

    def _h_ping(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("ping")

    def _h_status(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("status")

    def _h_get_configuration(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("get_configuration")

    def _h_get_typed(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("get_typed", command=params["command"])

    def _h_set_typed(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "value" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'value'"},
            })
            return

        self._relay("set_typed", command=params["command"], value=body["value"])

    def _h_get_raw(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("get", command=params["command"])

    def _h_set_raw(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "value" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'value'"},
            })
            return

        self._relay("set", command=params["command"], value=body["value"])

    def _h_connect(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "callsign" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'callsign'"},
            })
            return

        connect_timeout = body.get("timeout", 60)

        self._relay(
            "connect_station",
            callsign=body["callsign"],
            via=body.get("via"),
            timeout=connect_timeout,
            # +5s margin over the KAM-XL-side timeout we just passed
            # through, so this socket doesn't give up before the
            # daemon could possibly have an answer.
            _socket_timeout=connect_timeout + 5,
        )

    def _h_disconnect(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()
        disconnect_timeout = body.get("timeout", 30)
        command_mode_timeout = body.get("command_mode_timeout", 5)

        self._relay(
            "disconnect_station",
            timeout=disconnect_timeout,
            command_mode_timeout=command_mode_timeout,
            # The Ctrl-C step and the DISCONNECT step run sequentially
            # on the daemon side, so the worst case is their sum, not
            # just the larger of the two.
            _socket_timeout=disconnect_timeout + command_mode_timeout + 5,
        )

    def _h_send_connected(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "text" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'text'"},
            })
            return

        self._relay(
            "send_connected",
            text=body["text"],
            add_cr=body.get("add_cr", True),
        )

    def _h_read_connected(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        timeout = float(query.get("timeout", ["5"])[0])
        self._relay(
            "read_connected",
            timeout=timeout,
            _socket_timeout=timeout + 5,
        )

    def _h_dashboard_page(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = DASHBOARD_HTML.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _h_terminal_page(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = TERMINAL_HTML.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _h_terminal_exec(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "command" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'command'"},
            })
            return

        command_timeout = body.get("timeout", 10)

        self._relay(
            "send_command",
            command=body["command"],
            timeout=command_timeout,
            _socket_timeout=command_timeout + 5,
        )

    def _h_monitor_stream(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            for event in self.daemon.stream_events("monitor.subscribe"):
                if event is None:
                    chunk = b": keepalive\n\n"
                else:
                    chunk = f"data: {json.dumps(event)}\n\n".encode("utf-8")

                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client navigated away / closed the EventSource -- not an
            # error, just the end of this stream.
            pass

    # -- PBBS (milestone 6) ---------------------------------------------
    #
    # Thin wrapper around the KAM-XL's own firmware PBBS. Each call
    # drives a full connect/command/disconnect cycle on the daemon side
    # (see KAMXL.list_pbbs_messages()/read_pbbs_message()/
    # kill_pbbs_message()),
    # so the socket timeout budget has to cover connect_timeout +
    # read_timeout + disconnect_station()'s own worst case (up to
    # ~35s between its command_mode_timeout and timeout defaults),
    # not just the individual step timeouts.

    def _h_pbbs_page(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = PBBS_HTML.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _h_pbbs_list_messages(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        mypbbs = query.get("mypbbs", [None])[0]
        connect_timeout = float(query.get("connect_timeout", ["15"])[0])
        read_timeout = float(query.get("read_timeout", ["10"])[0])

        self._relay(
            "pbbs.list_messages",
            mypbbs=mypbbs,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            _socket_timeout=connect_timeout + read_timeout + 40,
        )

    def _h_pbbs_read_message(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        mypbbs = query.get("mypbbs", [None])[0]
        connect_timeout = float(query.get("connect_timeout", ["15"])[0])
        read_timeout = float(query.get("read_timeout", ["10"])[0])

        self._relay(
            "pbbs.read_message",
            number=int(params["number"]),
            mypbbs=mypbbs,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            _socket_timeout=connect_timeout + read_timeout + 40,
        )

    def _h_pbbs_kill_message(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        mypbbs = query.get("mypbbs", [None])[0]
        connect_timeout = float(query.get("connect_timeout", ["15"])[0])
        read_timeout = float(query.get("read_timeout", ["10"])[0])

        # Three PBBS commands (L, K, L), each up to read_timeout.
        self._relay(
            "pbbs.kill_message",
            number=int(params["number"]),
            mypbbs=mypbbs,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            _socket_timeout=connect_timeout + 3 * read_timeout + 40,
        )

    # -- Stations / map (milestone 7) ------------------------------------
    #
    # Unlike PBBS, these never drive a connect/command/disconnect
    # cycle -- stations.list/stations.get just read the daemon's
    # already-built-up, in-memory StationTracker (fed passively by its
    # always-on monitor thread), so the default socket timeout is
    # plenty and no per-call _socket_timeout override is needed.

    def _h_map_page(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = MAP_HTML.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _h_stations_history(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        """Stations and tracks over the last ?hours= (default 24)."""
        try:
            hours = float(query.get("hours", ["24"])[0])
        except (TypeError, ValueError):
            hours = None
        if hours is None or not 1 <= hours <= 720:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "InvalidParam",
                          "message": "hours must be a number from 1 to 720"},
            })
            return
        self._relay("stations.history", hours=hours, _socket_timeout=60)

    def _h_objects_list(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("objects.list")

    def _h_backup_status(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        """History backup settings, last result and next run."""
        self._relay("backup.status")

    def _h_backup_now(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        """Back up now. A big database over a slow share takes a while."""
        self._relay("backup.now", _socket_timeout=900)

    def _telemetry_days(self, query: Dict[str, Any]) -> Optional[float]:
        try:
            days = float(query.get("days", ["7"])[0])
        except (TypeError, ValueError):
            days = None
        if days is None or not 0 < days <= 3650:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "InvalidParam",
                          "message": "days must be a number from 1 to 3650"},
            })
            return None
        return days

    def _h_telemetry_page(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = TELEMETRY_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _h_telemetry_stations(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        days = self._telemetry_days(query)
        if days is not None:
            self._relay("telemetry.stations", days=days, _socket_timeout=30)

    def _h_telemetry_get(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        days = self._telemetry_days(query)
        if days is not None:
            self._relay("telemetry.get", callsign=params["callsign"],
                        days=days, _socket_timeout=30)

    def _h_log_page(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = LOG_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _h_packets_search(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        """
        Search the packet log: ?callsign=&via=&text=&days=&setup=
        &before_id=&limit= (see packets.search). No TNC access.
        """
        search: Dict[str, Any] = {}
        for name in ("callsign", "via", "text", "setup"):
            if name in query:
                search[name] = query[name][0]
        try:
            for name, kind in (("days", float), ("before_id", int),
                               ("limit", int)):
                if name in query and query[name][0] != "":
                    search[name] = kind(query[name][0])
        except ValueError:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "InvalidParam",
                          "message": "days, before_id and limit must be numbers"},
            })
            return
        self._relay("packets.search", _socket_timeout=30, **search)

    def _h_coverage_report(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        """Per-setup coverage (?days=N, default 30). No TNC access."""
        try:
            days = float(query.get("days", ["30"])[0])
        except (TypeError, ValueError):
            days = None
        if days is None or not 0 < days <= 365:
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "days must be a number from 1 to 365",
                },
            })
            return
        self._relay("coverage.report", days=days, _socket_timeout=30)

    def _h_coverage_set_setup(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        """Set the station setup label: {"setup": "J-pole 16 ft"}."""
        body = self._read_json_body()
        if not isinstance(body.get("setup", ""), str):
            self._send_json(400, {
                "ok": False,
                "error": {"type": "InvalidParam",
                          "message": "setup must be text"},
            })
            return
        self._relay("coverage.set_setup", setup=body.get("setup", ""))

    def _h_notify_status(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        """Phone-notification settings and counters (never the secrets)."""
        self._relay("notify.status")

    def _h_notify_test(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("notify.test", _socket_timeout=20)

    def _h_stations_list(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("stations.list")

    def _h_stations_get(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("stations.get", callsign=params["callsign"])

    def _h_aprs_stats(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        self._relay("aprs.stats")

    def _h_aprs_messages(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Return stored APRS messages without transmitting."""

        # Default: the daemon's own configured station callsign.
        addressee = query.get("addressee", [None])[0]

        try:
            limit = int(query.get("limit", ["100"])[0])
        except (ValueError, TypeError):
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "limit must be an integer",
                },
            })
            return

        if not 1 <= limit <= 500:
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "limit must be between 1 and 500",
                },
            })
            return

        self._relay(
            "aprs.messages",
            addressee=addressee,
            limit=limit,
        )

    def _h_aprs_history(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Return persistent APRS reception history."""

        since_values = query.get("since")

        if since_values is None:
            self._relay("aprs.history")
            return

        if len(since_values) != 1:
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "'since' must be specified exactly once",
                },
            })
            return

        try:
            since = int(since_values[0])
        except (TypeError, ValueError):
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "'since' must be a Unix timestamp integer",
                },
            })
            return

        self._relay("aprs.history", since=since)

    def _h_aprs_send_message(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Submit one explicitly authorized APRS message to the daemon."""
        body = self._read_json_body()

        resend_id = body.get("resend_id")

        if resend_id is not None and (
            not isinstance(resend_id, str) or not resend_id
        ):
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "resend_id must be a nonempty string",
                },
            })
            return

        # A resend takes addressee/text from the original message.
        required = () if resend_id is not None else ("addressee", "message")

        for field in required:
            if not isinstance(body.get(field), str) or not body[field]:
                self._send_json(400, {
                    "ok": False,
                    "error": {
                        "type": "MissingParam",
                        "message": f"Missing required body field: {field!r}",
                    },
                })
                return

        extra = {"resend_id": resend_id} if resend_id is not None else {
            "addressee": body["addressee"],
            "message": body["message"],
        }

        # Path: the daemon's configured one unless the body names one.
        if body.get("path") is not None:
            extra["path"] = body["path"]

        self._relay(
            "aprs.send_message",
            **extra,
            confirm_transmit=body.get("confirm_transmit") is True,
            confirm_vhf_port2_radio_ready=(
                body.get("confirm_vhf_port2_radio_ready") is True
            ),
            confirm_hf_port1_disconnected=(
                body.get("confirm_hf_port1_disconnected") is True
            ),
            _socket_timeout=45,
        )

    def _h_aprs_outbox(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Sent APRS messages and their ack status (no TNC access)."""
        self._relay("aprs.outbox")

    def _h_aprs_ack_status(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Automatic-ACK status (no TNC access)."""
        self._relay("aprs.ack_status")

    def _h_aprs_ack_set(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Turn automatic ACKs on or off: body {"enabled": true|false}.

        Turning them on is the operator's authorization for ACK
        transmissions, so it supplies the daemon's three confirmations
        (same as the dashboard's single "Transmit now" checkbox).
        """
        body = self._read_json_body()
        enabled = body.get("enabled")

        if not isinstance(enabled, bool):
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "'enabled' must be true or false",
                },
            })
            return

        if not enabled:
            self._relay("aprs.ack_disable")
            return

        self._relay(
            "aprs.ack_enable",
            confirm_transmit=True,
            confirm_vhf_port2_radio_ready=True,
            confirm_hf_port1_disconnected=True,
            _socket_timeout=30,
        )

    def _h_aprs_retry_status(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Automatic-resend status (no TNC access)."""
        self._relay("aprs.retry_status")

    def _h_aprs_retry_set(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Automatic resend on/off: body {"enabled": true|false}.

        Turning it on is the operator's authorization for resend
        transmissions, so it supplies the daemon's confirmations.
        """
        body = self._read_json_body()
        enabled = body.get("enabled")

        if not isinstance(enabled, bool):
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "'enabled' must be true or false",
                },
            })
            return

        if not enabled:
            self._relay("aprs.retry_disable")
            return

        self._relay(
            "aprs.retry_enable",
            confirm_transmit=True,
            confirm_vhf_port2_radio_ready=True,
            confirm_hf_port1_disconnected=True,
        )

    def _h_aprs_bulletins(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Bulletins heard recently (?days=N, default 7). No TNC access."""
        try:
            days = float(query.get("days", ["7"])[0])
        except (TypeError, ValueError):
            days = None
        if days is None or not 0 < days <= 365:
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "days must be a number from 1 to 365",
                },
            })
            return
        self._relay("aprs.bulletins", days=days)

    def _h_aprs_bulletins_clear(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        self._relay("aprs.clear_bulletins")

    def _h_aprs_outbox_clear(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Forget all sent messages (no TNC access)."""
        self._relay("aprs.clear_outbox")

    def _h_aprs_beacon_status(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Position beacon status (no TNC access)."""
        self._relay("aprs.beacon_status")

    def _h_aprs_beacon_set(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Turn the periodic beacon on/off: body {"enabled": true|false}.

        Turning it on is the operator's authorization for beacon
        transmissions, so it supplies the daemon's confirmations.
        """
        body = self._read_json_body()
        enabled = body.get("enabled")

        if not isinstance(enabled, bool):
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "'enabled' must be true or false",
                },
            })
            return

        if not enabled:
            self._relay("aprs.beacon_disable")
            return

        self._relay(
            "aprs.beacon_enable",
            confirm_transmit=True,
            confirm_vhf_port2_radio_ready=True,
            confirm_hf_port1_disconnected=True,
        )

    def _h_aprs_beacon_now(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Send one beacon now (the button press is the authorization)."""
        self._relay(
            "aprs.beacon_now",
            confirm_transmit=True,
            confirm_vhf_port2_radio_ready=True,
            confirm_hf_port1_disconnected=True,
            _socket_timeout=45,
        )

    def _h_aprs_clear_messages(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Delete all stored received APRS messages for this station."""
        self._relay("aprs.clear_messages")

    # -- Winlink (milestone 8) -------------------------------------------
    #
    # Receive-only MVP (see winlink.py's module docstring) -- drives a
    # full connect/handshake/proposal/message/disconnect cycle on the
    # daemon side (KAMXL.check_winlink_mail()), so the socket timeout
    # budget mirrors the PBBS endpoints' reasoning above. POST with a
    # JSON body, not GET with query params, specifically because the
    # request carries a real account password -- never acceptable in
    # a URL (query strings end up in server access logs, browser
    # history, etc.).

    def _h_winlink_page(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = WINLINK_HTML.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _h_winlink_check_mail(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        for field in ("gateway", "password"):
            if field not in body:
                self._send_json(400, {
                    "ok": False,
                    "error": {
                        "type": "MissingParam",
                        "message": f"Missing required body field: {field!r}",
                    },
                })
                return

        connect_timeout = body.get("connect_timeout", 60)
        read_timeout = body.get("read_timeout", 30)

        self._relay(
            "winlink.check_mail",
            gateway=body["gateway"],
            password=body["password"],
            mycall=body.get("mycall"),
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            _socket_timeout=connect_timeout + (read_timeout * 3) + 40,
        )

    def _h_winlink_send_message(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        # Send-only, text-body-only, B2/FC only -- see winlink.py's
        # module docstring's "SEND SUPPORT" note for the full scope,
        # and its "UNVERIFIED AGAINST A REAL GATEWAY" caveat, which
        # still applies as of this endpoint's addition. Same POST/JSON
        # body reasoning as _h_winlink_check_mail() above (a real
        # account password, never in a URL).
        body = self._read_json_body()

        for field in ("gateway", "password", "messages"):
            if field not in body:
                self._send_json(400, {
                    "ok": False,
                    "error": {
                        "type": "MissingParam",
                        "message": f"Missing required body field: {field!r}",
                    },
                })
                return

        if not isinstance(body["messages"], list) or not body["messages"]:
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "MissingParam",
                    "message": "'messages' must be a non-empty list",
                },
            })
            return

        for message in body["messages"]:
            for field in ("to", "subject", "body"):
                if field not in message:
                    self._send_json(400, {
                        "ok": False,
                        "error": {
                            "type": "MissingParam",
                            "message": f"Each message needs a {field!r} field",
                        },
                    })
                    return

        connect_timeout = body.get("connect_timeout", 60)
        read_timeout = body.get("read_timeout", 30)

        self._relay(
            "winlink.send_message",
            gateway=body["gateway"],
            password=body["password"],
            messages=body["messages"],
            mycall=body.get("mycall"),
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            _socket_timeout=connect_timeout + (read_timeout * 3) + 40,
        )

    # -- Winlink web-service API (August 2026) ----------------------------
    #
    # A different Winlink surface entirely from check/send above: these
    # three call api.winlink.org's HTTP API (winlink_api.py), not the
    # KAM-XL's own AX.25/B2F session -- no serial port involved, no
    # account password either, so plain GET with query params is fine
    # here (unlike check/send, which need POST/JSON specifically to
    # keep a password out of the URL). The daemon reads the actual
    # WINLINK_API_KEY itself (see tncstation_daemon.py) -- this REST layer
    # never sees or handles that key.
    #
    # 20s socket-timeout headroom is generous for a single HTTPS
    # round-trip to api.winlink.org (winlink_api.py's own default is
    # 15s) -- deliberately not tight, since a slow/loaded WDT server
    # shouldn't surface here as a raw DaemonTimeout when the daemon
    # itself is still legitimately waiting on a response.

    def _h_winlink_account_exists(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay(
            "winlink.account_exists",
            callsign=params["callsign"],
            _socket_timeout=20,
        )

    def _h_winlink_gateways(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        mode = query.get("mode", ["AnyAll"])[0]
        history_hours = int(query.get("history_hours", ["48"])[0])
        service_codes = query.get("service_codes", ["PUBLIC"])
        if len(service_codes) == 1 and "," in service_codes[0]:
            service_codes = [code.strip() for code in service_codes[0].split(",")]

        self._relay(
            "winlink.gateway_status",
            mode=mode,
            history_hours=history_hours,
            service_codes=service_codes,
            _socket_timeout=20,
        )

    def _h_winlink_gateways_nearby(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        if "lat" not in query or "lon" not in query:
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "MissingParam",
                    "message": "Missing required query param: 'lat' and/or 'lon'",
                },
            })
            return

        max_distance_km = query.get("max_distance_km", [None])[0]
        limit = query.get("limit", [None])[0]

        self._relay(
            "winlink.nearby_gateways",
            latitude=float(query["lat"][0]),
            longitude=float(query["lon"][0]),
            max_distance_km=float(max_distance_km) if max_distance_km else None,
            limit=int(limit) if limit else None,
            _socket_timeout=20,
        )


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

class _RestHTTPServer(ThreadingHTTPServer):
    # socketserver's default poll_interval (0.5s) is how often
    # serve_forever()'s loop wakes up to check for a pending
    # shutdown() -- overriding the default here (rather than passing
    # poll_interval at each serve_forever() call site) covers both
    # main() and anything else -- e.g. tests/test_rest.py -- that
    # calls serve_forever() directly on a server built by serve().
    def serve_forever(self, poll_interval: float = 0.1) -> None:
        super().serve_forever(poll_interval=poll_interval)


def serve(
    daemon_socket: str,
    host: str,
    port: int,
    api_key: Optional[str],
) -> ThreadingHTTPServer:
    RESTRequestHandler.daemon = DaemonClient(daemon_socket)
    RESTRequestHandler.api_key = api_key

    return _RestHTTPServer((host, port), RESTRequestHandler)


def _bind_failure_hint(exc: OSError, host: str, port: int) -> Optional[str]:
    """
    One clear line for the usual reasons the web server can't listen,
    instead of a traceback -- or None for anything else.
    """
    if exc.errno == errno.EADDRINUSE:
        return (
            f"Port {port} is already in use by another program (Pat's web "
            f"interface, a proxy or another web app often takes 8080). Set "
            f"TNCSTATION_REST_PORT in .env to a free port, e.g. 8090 -- "
            f"'ss -ltnp' shows what is listening where."
        )
    if exc.errno == errno.EACCES:
        return (
            f"Not allowed to listen on port {port}: ports below 1024 need "
            f"root. Set TNCSTATION_REST_PORT in .env to 1024 or higher."
        )
    if exc.errno == errno.EADDRNOTAVAIL:
        return (
            f"Can't listen on address {host!r}: it isn't one of this "
            f"computer's addresses. Leave TNCSTATION_REST_HOST unset (whole "
            f"LAN) or set it to 127.0.0.1."
        )
    return None


def main(argv: Optional[list] = None) -> None:
    # Same .env file as the daemon (read from the working directory;
    # real environment variables still win), so one file configures
    # both: TNCSTATION_REST_API_KEY, TNCSTATION_REST_PORT, TNCSTATION_SOCKET, ...
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="REST API for tncstation_daemon.py"
    )
    parser.add_argument(
        "--daemon-socket",
        default=os.environ.get("TNCSTATION_SOCKET", DEFAULT_DAEMON_SOCKET),
        help=f"Path to the daemon's Unix socket (default: {DEFAULT_DAEMON_SOCKET})",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("TNCSTATION_REST_HOST", DEFAULT_HOST),
        help=f"Address to bind to (default: {DEFAULT_HOST} -- all interfaces)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TNCSTATION_REST_PORT", DEFAULT_PORT)),
        help=f"Port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("TNCSTATION_REST_API_KEY"),
        help="Bearer token required on every request. Random one generated "
             "and printed at startup if not given. Pass --no-auth to disable "
             "(only if --host is 127.0.0.1 / localhost).",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable authentication. Refused unless --host is localhost.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action=argparse.BooleanOptionalAction,
        default=env_flag("VERBOSE"),
        help=(
            "Also log each proxied daemon call (DEBUG level) "
            "(default: $TNCSTATION_VERBOSE; --no-verbose overrides it)"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.no_auth:
        if args.host not in ("127.0.0.1", "localhost"):
            parser.error(
                "--no-auth is only allowed with --host 127.0.0.1 "
                "(this API is reachable from your whole LAN otherwise)"
            )
        api_key = None
        logger.warning("Authentication disabled (--no-auth, localhost only)")
    else:
        api_key = args.api_key or secrets.token_urlsafe(32)

        if not args.api_key:
            logger.info("Generated API key (save this): %s", api_key)

    try:
        server = serve(args.daemon_socket, args.host, args.port, api_key)
    except OSError as exc:
        hint = _bind_failure_hint(exc, args.host, args.port)
        if hint is None:
            raise
        logger.error("%s", hint)
        raise SystemExit(2)

    # Same reasoning as tncstation_daemon.py's main(): serve_forever() runs
    # on a background thread so that shutdown() -- called from the
    # signal handler's thread -- doesn't deadlock waiting for a loop
    # it would otherwise be blocking itself.
    shutdown_requested = threading.Event()

    def _handle_signal(signum: int, frame: Any) -> None:
        logger.info("Shutting down (signal %s)...", signum)
        shutdown_requested.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    logger.info(
        "tncstation REST API: http://%s:%s -> %s",
        args.host, args.port, args.daemon_socket
    )

    try:
        while not shutdown_requested.is_set():
            shutdown_requested.wait(timeout=1)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


if __name__ == "__main__":
    main()
