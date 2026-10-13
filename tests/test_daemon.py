"""
Offline, end-to-end tests for kamxl_daemon.py: a real KAMDaemon,
wired to the same scripted fake serial connections used elsewhere in
this suite, listening on a real (throwaway) Unix socket, talked to by
a real socket client -- no KAM-XL hardware, no mocking of the socket
layer itself.
"""

import json
import os
import socket
import tempfile
import threading
import time
import unittest

import winlink_api

from fakes import CannedSerial, ChunkSerial, ScriptedSerial, make_kam

from kamxl_daemon import KAMDaemon, _load_dotenv
from pbbs import PBBSMessage, PBBSMessageSummary
from winlink import OutgoingMessage, Proposal, WinlinkMessage


class _Client:
    """
    Minimal newline-delimited JSON client for exercising a KAMDaemon
    over its Unix socket.
    """

    def __init__(self, socket_path, timeout=2):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(socket_path)
        self.rfile = self.sock.makefile("r")
        self._next_id = 0

    def call(self, method, **params):
        self._next_id += 1
        request_id = str(self._next_id)

        self.sock.sendall((json.dumps({
            "id": request_id,
            "method": method,
            "params": params,
        }) + "\n").encode("ascii"))

        # Skip past any monitor "packet" events (no "id") that might
        # be interleaved before our matching response arrives.
        while True:
            response = json.loads(self._readline())

            if response.get("id") == request_id:
                return response

    def read_event(self, timeout=2):
        self.sock.settimeout(timeout)
        return json.loads(self._readline())

    def _readline(self):
        line = self.rfile.readline()

        if not line:
            raise ConnectionError("Daemon closed the connection")

        return line

    def close(self):
        # socket.makefile() keeps its own reference to the underlying
        # fd -- closing just self.sock without also closing self.rfile
        # leaves the connection technically still open at the OS
        # level (refcounted), so the server never sees EOF/reset.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        self.rfile.close()
        self.sock.close()


class DaemonTestCase(unittest.TestCase):
    """
    Starts a KAMDaemon wired to a caller-supplied fake serial
    connection, on a throwaway Unix socket, in a background thread.
    """

    def start_daemon(self, serial):
        kam = make_kam(serial)

        tmp_dir = tempfile.mkdtemp()
        socket_path = os.path.join(tmp_dir, "kamxl.sock")

        daemon = KAMDaemon(kam, socket_path)

        thread = threading.Thread(
            target=daemon.serve_forever,
            daemon=True
        )
        thread.start()

        deadline = time.monotonic() + 2

        while (
            not os.path.exists(socket_path)
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)

        # addCleanup runs LIFO -- register thread.join() *before*
        # shutdown() so shutdown() (which unblocks serve_forever()'s
        # loop) actually runs first. Registered the other way around,
        # join(timeout) just burns its whole timeout waiting on a
        # thread nothing has told to stop yet.
        self.addCleanup(thread.join, 2)
        self.addCleanup(daemon.shutdown)

        return daemon, socket_path

    def connect(self, socket_path):
        # The socket file existing (checked in start_daemon) doesn't
        # strictly guarantee the server is done with bind()+listen()
        # by the time a client tries to connect -- under load (this
        # suite spins up a fresh daemon thread per test), an early
        # connect() attempt can still occasionally see
        # ConnectionRefusedError. Retry briefly rather than assume
        # the first attempt lands.
        deadline = time.monotonic() + 2
        last_error = None

        while time.monotonic() < deadline:
            try:
                client = _Client(socket_path)
                break
            except ConnectionRefusedError as exc:
                last_error = exc
                time.sleep(0.02)
        else:
            raise last_error

        self.addCleanup(client.close)

        return client


class PingStatusTests(DaemonTestCase):
    def test_ping(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("ping")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], "pong")

    def test_status(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("status")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["connected"], True)
        self.assertEqual(response["result"]["port"], "COM_FAKE")
        self.assertEqual(response["result"]["monitor_subscribers"], 0)


class TypedCommandTests(DaemonTestCase):
    def test_get_typed_multiport_int(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "HBAUD": "HBAUD    0/1200",
        }))
        client = self.connect(socket_path)

        response = client.call("get_typed", command="HBAUD")

        self.assertTrue(response["ok"])
        # Tuples cross the wire as JSON arrays.
        self.assertEqual(response["result"], [0, 1200])

    def test_set_typed_multiport_bool_round_trip(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "MONITOR": "MONITOR  ON/OFF",
        }))
        client = self.connect(socket_path)

        # A JSON array in -> converted back to a tuple before it
        # reaches kamxl.py's isinstance(value, tuple) check.
        response = client.call(
            "set_typed",
            command="MONITOR",
            value=[True, False]
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], [True, False])

    def test_get_configuration(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "DISPLAY": "MYCALL   N5BA-10/N5BA-10\r\nHBAUD    0/1200",
        }))
        client = self.connect(socket_path)

        response = client.call("get_configuration")

        self.assertTrue(response["ok"])
        self.assertEqual(
            response["result"],
            {"MYCALL": "N5BA-10/N5BA-10", "HBAUD": "0/1200"}
        )


class SendCommandTests(DaemonTestCase):
    """
    The raw send_command passthrough added for the web terminal
    (milestone 4) -- unlike get/get_typed, it doesn't assume a
    "COMMAND value" response shape, so it works for commands kamxl.py
    has no typed metadata for at all.
    """

    def test_send_arbitrary_command_returns_raw_text(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "BEACON": "BEACON EVERY 0",
        }))
        client = self.connect(socket_path)

        response = client.call("send_command", command="BEACON")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], "BEACON EVERY 0")

    def test_unknown_command_still_returns_whatever_came_back(self):
        # No typed metadata, no scripted response -- just the echo
        # and prompt, same as an unrecognized command would look on
        # real hardware.
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("send_command", command="NOTAREALCOMMAND")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], "")


class ErrorHandlingTests(DaemonTestCase):
    def test_unknown_method(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("not_a_real_method")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "KAMError")

    def test_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("get")  # missing "command"

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")

    def test_readonly_command_error_reaches_client(self):
        _, socket_path = self.start_daemon(ScriptedSerial({
            "VERSION": "KAM-XL VERSION 1.24160",
        }))
        client = self.connect(socket_path)

        response = client.call(
            "set_typed",
            command="VERSION",
            value="TEST"
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "KAMError")
        self.assertIn("read-only", response["error"]["message"])


class ConnectStationTests(DaemonTestCase):
    def test_successful_connect(self):
        _, socket_path = self.start_daemon(
            CannedSerial(["*** CONNECTED to KD5EOC-10\r\n"])
        )
        client = self.connect(socket_path)

        response = client.call(
            "connect_station",
            callsign="KD5EOC-10",
            timeout=1
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], "*** CONNECTED to KD5EOC-10\r\n")

    def test_busy_raises_connection_error(self):
        _, socket_path = self.start_daemon(
            CannedSerial(["***(N0CALL) busy\r\n"])
        )
        client = self.connect(socket_path)

        response = client.call(
            "connect_station",
            callsign="N0CALL",
            timeout=1
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "KAMConnectionError")


class ConnectedModePassthroughTests(DaemonTestCase):
    def test_send_and_read_connected(self):
        _, socket_path = self.start_daemon(
            CannedSerial(["hello back!\r\n"])
        )
        client = self.connect(socket_path)

        send_response = client.call("send_connected", text="hi there")
        self.assertTrue(send_response["ok"])
        self.assertIsNone(send_response["result"])

        read_response = client.call("read_connected", timeout=0.3)
        self.assertTrue(read_response["ok"])
        self.assertEqual(read_response["result"], "hello back!\r\n")

    def test_disconnect_station(self):
        _, socket_path = self.start_daemon(
            CannedSerial(["cmd:", "cmd:*** DISCONNECTED\r\n"])
        )
        client = self.connect(socket_path)

        response = client.call("disconnect_station", timeout=1)

        self.assertTrue(response["ok"])
        self.assertIn("DISCONNECTED", response["result"])


class PBBSTests(DaemonTestCase):
    """
    KAMXL.list_pbbs_messages()/read_pbbs_message() are already
    covered directly (call order, argument flow, error handling) in
    tests/test_pbbs.py using stubbed primitives -- chaining a real
    connect+command+disconnect exchange through one shared
    CannedSerial queue doesn't work cleanly (read_connected()'s
    "collect for N seconds" semantics drains every queued chunk in a
    single call, regardless of which logical step they were meant
    for). So here, daemon.kam.list_pbbs_messages/read_pbbs_message
    are stubbed directly too -- these tests are about what the daemon
    layer itself adds on top: lock acquisition, params passed
    through, and Packet-style dataclass -> JSON-safe dict conversion.
    """

    def test_list_messages_returns_json_safe_dicts(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        daemon.kam.list_pbbs_messages = lambda **kwargs: [
            PBBSMessageSummary(
                number=6, msg_type="B", status=None, size=45,
                to="KEPS", from_call="W3IWI", date="10/19/01 09:37:11",
                pages=2, subject="Line Element set",
            )
        ]

        client = self.connect(socket_path)
        response = client.call("pbbs.list_messages")

        self.assertTrue(response["ok"])
        self.assertEqual(len(response["result"]), 1)
        self.assertEqual(
            response["result"][0]["subject"], "Line Element set"
        )
        self.assertEqual(response["result"][0]["from_call"], "W3IWI")

    def test_list_messages_passes_params_through(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        captured = {}

        def fake_list(**kwargs):
            captured.update(kwargs)
            return []

        daemon.kam.list_pbbs_messages = fake_list

        client = self.connect(socket_path)
        client.call(
            "pbbs.list_messages",
            mypbbs="N5BA-2",
            connect_timeout=10,
            read_timeout=3
        )

        self.assertEqual(captured["mypbbs"], "N5BA-2")
        self.assertEqual(captured["connect_timeout"], 10)
        self.assertEqual(captured["read_timeout"], 3)

    def test_read_message_returns_dict(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        daemon.kam.read_pbbs_message = lambda number, **kwargs: PBBSMessage(
            number=number, date="02/10/92 10:30:58", from_call="KB0NYK",
            to="HELP", routing=None, body="hi",
        )

        client = self.connect(socket_path)
        response = client.call("pbbs.read_message", number=2)

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["from_call"], "KB0NYK")
        self.assertEqual(response["result"]["number"], 2)

    def test_read_message_not_found_returns_none(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        daemon.kam.read_pbbs_message = lambda number, **kwargs: None

        client = self.connect(socket_path)
        response = client.call("pbbs.read_message", number=999)

        self.assertTrue(response["ok"])
        self.assertIsNone(response["result"])

    def test_read_message_missing_number_is_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("pbbs.read_message")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")


class WinlinkTests(DaemonTestCase):
    """
    Milestone 8. Same reasoning as PBBSTests above:
    daemon.kam.check_winlink_mail is stubbed directly rather than
    driven through a real connect/handshake/proposal exchange (already
    covered in tests/test_winlink.py's KAMXLWinlinkIntegrationTests) --
    these are about what the daemon layer itself adds: lock
    acquisition, params passed through, dataclass -> JSON-safe dict
    conversion (including the nested Proposal dataclass).
    """

    def test_check_mail_returns_json_safe_dicts(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

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

        client = self.connect(socket_path)
        response = client.call(
            "winlink.check_mail", gateway="N5BA-10", password="FOOBAR"
        )

        self.assertTrue(response["ok"])
        self.assertEqual(len(response["result"]), 1)
        self.assertEqual(response["result"][0]["title"], "Test Subject")
        self.assertEqual(
            response["result"][0]["proposal"]["sender"], "N0CALL"
        )

    def test_check_mail_passes_params_through(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        captured = {}

        def fake_check(**kwargs):
            captured.update(kwargs)
            return []

        daemon.kam.check_winlink_mail = fake_check

        client = self.connect(socket_path)
        client.call(
            "winlink.check_mail",
            gateway="N5BA-10",
            password="FOOBAR",
            mycall="N5BA-2",
            connect_timeout=90,
            read_timeout=15,
        )

        self.assertEqual(captured["gateway"], "N5BA-10")
        self.assertEqual(captured["password"], "FOOBAR")
        self.assertEqual(captured["mycall"], "N5BA-2")
        self.assertEqual(captured["connect_timeout"], 90)
        self.assertEqual(captured["read_timeout"], 15)

    def test_missing_gateway_is_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.check_mail", password="FOOBAR")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")

    def test_missing_password_is_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.check_mail", gateway="N5BA-10")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")


class WinlinkSendMessageTests(DaemonTestCase):
    """
    Milestone 8's send-support extension. Same reasoning as
    WinlinkTests above: daemon.kam.send_winlink_message is stubbed
    directly (the real connect/handshake/proposal exchange is covered
    in tests/test_winlink.py's
    KAMXLSendWinlinkMessageIntegrationTests) -- these are about what
    the daemon layer itself adds: lock acquisition, JSON dict ->
    OutgoingMessage conversion, params passed through.
    """

    def test_send_message_returns_accepted_mids(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        daemon.kam.send_winlink_message = lambda **kwargs: ["12345_N5BA"]

        client = self.connect(socket_path)
        response = client.call(
            "winlink.send_message",
            gateway="N5BA-10",
            password="FOOBAR",
            messages=[{"to": ["N0CALL"], "subject": "Hi", "body": "Hello"}],
        )

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], ["12345_N5BA"])

    def test_send_message_converts_dicts_to_outgoing_messages(self):
        daemon, socket_path = self.start_daemon(ScriptedSerial({}))

        captured = {}

        def fake_send(**kwargs):
            captured.update(kwargs)
            return []

        daemon.kam.send_winlink_message = fake_send

        client = self.connect(socket_path)
        client.call(
            "winlink.send_message",
            gateway="N5BA-10",
            password="FOOBAR",
            mycall="N5BA-2",
            connect_timeout=90,
            read_timeout=15,
            messages=[
                {
                    "to": ["N0CALL"], "cc": ["N1CALL"],
                    "subject": "Hi", "body": "Hello",
                },
            ],
        )

        self.assertEqual(captured["gateway"], "N5BA-10")
        self.assertEqual(captured["password"], "FOOBAR")
        self.assertEqual(captured["mycall"], "N5BA-2")
        self.assertEqual(captured["connect_timeout"], 90)
        self.assertEqual(captured["read_timeout"], 15)

        messages = captured["messages"]
        self.assertEqual(len(messages), 1)
        self.assertIsInstance(messages[0], OutgoingMessage)
        self.assertEqual(messages[0].to, ["N0CALL"])
        self.assertEqual(messages[0].cc, ["N1CALL"])
        self.assertEqual(messages[0].subject, "Hi")
        self.assertEqual(messages[0].body, "Hello")

    def test_missing_gateway_is_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call(
            "winlink.send_message",
            password="FOOBAR",
            messages=[{"to": ["N0CALL"], "subject": "Hi", "body": "Hello"}],
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")

    def test_missing_messages_is_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call(
            "winlink.send_message", gateway="N5BA-10", password="FOOBAR"
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")


class WinlinkApiTests(DaemonTestCase):
    """
    The Winlink HTTP web-service API wiring (account lookups, gateway
    listings -- winlink_api.py, unrelated to the B2F/RF winlink.py
    tests above). These never touch daemon.kam at all -- kamxl_daemon.py
    calls winlink_api.account_exists()/get_gateway_status() directly,
    with no serial port involved -- so the real network calls are
    swapped out here for direct module-level fakes (kamxl_daemon.py
    does `import winlink_api` rather than `from winlink_api import
    ...`, specifically so reassigning an attribute on the module
    object here is visible to it), the same "swap the real thing for
    a fake at the seam" discipline as daemon.kam.send_winlink_message
    above, just one level further out.
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

    def test_account_exists_passes_callsign_and_key(self):
        calls = []

        self._patch(
            "account_exists",
            lambda callsign, api_key, **kwargs: calls.append(
                (callsign, api_key)
            ) or True
        )

        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.account_exists", callsign="N5BA")

        self.assertTrue(response["ok"])
        self.assertTrue(response["result"])
        self.assertEqual(calls, [("N5BA", "TEST_KEY")])

    def test_account_exists_missing_callsign_is_missing_param(self):
        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.account_exists")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")

    def test_missing_api_key_raises_clear_error(self):
        os.environ.pop("WINLINK_API_KEY", None)

        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.account_exists", callsign="N5BA")

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "KAMError")
        self.assertIn("WINLINK_API_KEY", response["error"]["message"])

    def test_gateway_status_returns_dicts(self):
        gateway = winlink_api.Gateway(
            callsign="KD5EOC-10", base_callsign="KD5EOC",
            requested_mode="Packet", comments="", last_status="",
            latitude=33.2, longitude=-97.1,
            channels=[
                winlink_api.GatewayChannel(
                    operating_hours="24/7", supported_modes="PACKET",
                    frequency=145.09, service_code="PUBLIC", baud="1200",
                    radio_range="25", mode=1, gridsquare="EM13ov",
                    antenna="Vertical",
                ),
            ],
        )
        self._patch(
            "get_gateway_status", lambda api_key, **kwargs: [gateway]
        )

        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.gateway_status")

        self.assertTrue(response["ok"])
        self.assertEqual(len(response["result"]), 1)
        self.assertEqual(response["result"][0]["callsign"], "KD5EOC-10")
        self.assertEqual(len(response["result"][0]["channels"]), 1)
        self.assertEqual(
            response["result"][0]["channels"][0]["gridsquare"], "EM13ov"
        )

    def test_nearby_gateways_returns_distance_km(self):
        near = winlink_api.Gateway(
            callsign="NEAR-10", base_callsign="NEAR",
            requested_mode="Packet", comments="", last_status="",
            latitude=33.05, longitude=-97.05, channels=[],
        )
        self._patch(
            "get_gateway_status", lambda api_key, **kwargs: [near]
        )

        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call(
            "winlink.nearby_gateways", latitude=33.0, longitude=-97.0
        )

        self.assertTrue(response["ok"])
        self.assertEqual(len(response["result"]), 1)
        self.assertEqual(response["result"][0]["gateway"]["callsign"], "NEAR-10")
        self.assertGreater(response["result"][0]["distance_km"], 0)

    def test_nearby_gateways_missing_latitude_is_missing_param(self):
        self._patch("get_gateway_status", lambda api_key, **kwargs: [])

        _, socket_path = self.start_daemon(ScriptedSerial({}))
        client = self.connect(socket_path)

        response = client.call("winlink.nearby_gateways", longitude=-97.0)

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "MissingParam")


class LoadDotenvTests(unittest.TestCase):
    """
    kamxl_daemon.py's minimal .env loader -- reads WINLINK_API_KEY (or
    anything else) out of a KEY=VALUE file so it doesn't have to be
    exported by hand every time the daemon starts. See _load_dotenv()'s
    own docstring for the full scope (no third-party dotenv dependency,
    no interpolation/line-continuation support).
    """

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._env_path = os.path.join(self._tmp_dir, ".env")

        self._saved_environ = dict(os.environ)
        self.addCleanup(self._restore_environ)

    def _restore_environ(self):
        os.environ.clear()
        os.environ.update(self._saved_environ)

    def _write_env(self, content):
        with open(self._env_path, "w") as f:
            f.write(content)

    def test_missing_file_is_a_no_op(self):
        # Must not raise -- a .env file is a convenience, not a
        # requirement, and the daemon has to keep working without one.
        _load_dotenv(os.path.join(self._tmp_dir, "does-not-exist.env"))

    def test_sets_previously_unset_variable(self):
        os.environ.pop("WINLINK_API_KEY", None)
        self._write_env("WINLINK_API_KEY=ABC123\n")

        _load_dotenv(self._env_path)

        self.assertEqual(os.environ["WINLINK_API_KEY"], "ABC123")

    def test_real_environment_variable_takes_precedence(self):
        # Standard dotenv semantics -- the file only fills in gaps, it
        # never overrides something already exported for real.
        os.environ["WINLINK_API_KEY"] = "REAL_VALUE"
        self._write_env("WINLINK_API_KEY=FROM_DOTENV\n")

        _load_dotenv(self._env_path)

        self.assertEqual(os.environ["WINLINK_API_KEY"], "REAL_VALUE")

    def test_blank_lines_and_comments_skipped(self):
        self._write_env("\n# a comment\nFOO_TEST_VAR=bar\n")

        _load_dotenv(self._env_path)

        self.assertEqual(os.environ["FOO_TEST_VAR"], "bar")

    def test_quoted_value_is_unquoted(self):
        self._write_env('FOO_TEST_VAR="bar baz"\n')

        _load_dotenv(self._env_path)

        self.assertEqual(os.environ["FOO_TEST_VAR"], "bar baz")

    def test_line_without_equals_sign_skipped(self):
        self._write_env("not a valid line\nFOO_TEST_VAR=bar\n")

        _load_dotenv(self._env_path)

        self.assertEqual(os.environ["FOO_TEST_VAR"], "bar")


class MonitorSubscribeTests(DaemonTestCase):
    # A packet is only known to be "complete" once the *next* header
    # line arrives (see packet.py's PacketParser docstring), so this
    # includes a third header purely to complete the second packet --
    # otherwise it would just sit pending, waiting for the monitor
    # loop to stop and flush it (see test_shutdown_stops_monitor_thread
    # below for that path instead).
    CHUNKS = [
        "KD5EOC-10>BEACON/2:\r\n",
        "Winlink 2000 RMS Packet Server\r\n",
        "N5BA-4>BEACON/2:\r\n",
        "N5BA-4 Linux Node http://digipi.org/\r\n",
        "N0CALL>BEACON/2:\r\n",
    ]

    def test_subscribe_receives_packet_events(self):
        _, socket_path = self.start_daemon(ChunkSerial(self.CHUNKS))
        client = self.connect(socket_path)

        subscribe_response = client.call("monitor.subscribe")
        self.assertTrue(subscribe_response["ok"])

        first = client.read_event(timeout=3)
        self.assertEqual(first["event"], "packet")
        self.assertEqual(first["data"]["source"], "KD5EOC-10")
        self.assertEqual(first["data"]["destination"], "BEACON")
        self.assertEqual(first["data"]["port"], 2)

        second = client.read_event(timeout=3)
        self.assertEqual(second["data"]["source"], "N5BA-4")
        self.assertEqual(
            second["data"]["payload"],
            "N5BA-4 Linux Node http://digipi.org/"
        )

    def test_status_reflects_subscriber_count(self):
        _, socket_path = self.start_daemon(ChunkSerial([]))
        client = self.connect(socket_path)

        before = client.call("status")
        self.assertEqual(before["result"]["monitor_subscribers"], 0)

        client.call("monitor.subscribe")

        after = client.call("status")
        self.assertEqual(after["result"]["monitor_subscribers"], 1)

        client.call("monitor.unsubscribe")

        final = client.call("status")
        self.assertEqual(final["result"]["monitor_subscribers"], 0)

    def test_monitor_thread_starts_with_no_subscribers(self):
        # Milestone 7: the monitor thread is now always-on from the
        # moment a KAMDaemon is constructed, not started on-demand by
        # the first monitor.subscribe() -- needed so the station
        # database (stations.py) builds passively even if nobody ever
        # opens the map page or a live monitor pane. Confirm it's
        # already running with zero subscribers, rather than only
        # after the first one connects.
        daemon, _ = self.start_daemon(ChunkSerial([]))

        self.assertIsNotNone(daemon._monitor_thread)
        self.assertTrue(daemon._monitor_thread.is_alive())

        with daemon._subscribers_lock:
            self.assertEqual(len(daemon._subscribers), 0)

    def test_monitor_thread_outlives_last_subscriber(self):
        # The milestone 5/6 behavior stopped the monitor thread once
        # its last subscriber left (a background thread outliving
        # every subscriber was a real resource leak back then, since
        # broadcasting packets was the thread's only job). Milestone 7
        # deliberately changed that: the thread now also feeds the
        # always-on station tracker, so it has a reason to keep
        # running with zero subscribers -- confirm it does.
        daemon, socket_path = self.start_daemon(ChunkSerial([
            "N0CALL>BEACON/2:\r\n",
            "still going after the last subscriber leaves\r\n",
        ]))
        client = self.connect(socket_path)

        client.call("monitor.subscribe")
        time.sleep(0.3)  # let the monitor loop pick up the chunks

        client.close()
        time.sleep(0.3)  # give an (incorrect) stop a moment to happen

        self.assertTrue(daemon._monitor_thread.is_alive())

        with daemon._subscribers_lock:
            self.assertEqual(len(daemon._subscribers), 0)

    def test_shutdown_stops_monitor_thread(self):
        # The always-on thread still needs to stop *somewhere* --
        # confirm daemon.shutdown() does it (and flushes/discards
        # whatever was still pending, rather than raising), now that
        # "last subscriber leaves" no longer does.
        daemon, socket_path = self.start_daemon(ChunkSerial([
            "N0CALL>BEACON/2:\r\n",
            "final packet, never followed by another header\r\n",
        ]))
        self.connect(socket_path)

        deadline = time.monotonic() + 2
        while (
            not daemon._monitor_thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

        self.assertTrue(daemon._monitor_thread.is_alive())

        daemon.shutdown()

        deadline = time.monotonic() + 2
        while (
            daemon._monitor_thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

        self.assertFalse(daemon._monitor_thread.is_alive())


class StationsTests(DaemonTestCase):
    """
    Milestone 7: the always-on monitor thread (see
    MonitorSubscribeTests above) decodes APRS position reports out of
    ordinary MONITOR traffic into stations.py's StationTracker --
    with no monitor.subscribe() call required, unlike packet events.
    """

    def _wait_for_stations(self, client, timeout=2):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            response = client.call("stations.list")
            stations = response["result"]

            if stations:
                return stations

            time.sleep(0.05)

        return []

    def test_stations_list_reflects_decoded_position(self):
        _, socket_path = self.start_daemon(ChunkSerial([
            "N5BA-9>APRS/1:\r\n",
            "!4903.50N/07201.75W-Test comment\r\n",
            "N0CALL>BEACON/1:\r\n",  # completes the N5BA-9 packet
        ]))
        client = self.connect(socket_path)

        stations = self._wait_for_stations(client)

        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0]["callsign"], "N5BA-9")
        self.assertAlmostEqual(
            stations[0]["latitude"], 49 + 3.50 / 60, places=6
        )
        self.assertEqual(stations[0]["comment"], "Test comment")

    def test_stations_get_known_callsign(self):
        _, socket_path = self.start_daemon(ChunkSerial([
            "N5BA-9>APRS/1:\r\n",
            "!4903.50N/07201.75W-Test comment\r\n",
            "N0CALL>BEACON/1:\r\n",
        ]))
        client = self.connect(socket_path)

        self._wait_for_stations(client)

        response = client.call("stations.get", callsign="N5BA-9")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["callsign"], "N5BA-9")

    def test_stations_get_unknown_callsign_returns_none(self):
        _, socket_path = self.start_daemon(ChunkSerial([]))
        client = self.connect(socket_path)

        response = client.call("stations.get", callsign="NOBODY")

        self.assertTrue(response["ok"])
        self.assertIsNone(response["result"])

    def test_stations_list_empty_when_no_position_traffic(self):
        _, socket_path = self.start_daemon(ChunkSerial([
            "N0CALL>BEACON/1:\r\n",
            "just chatter, no position report\r\n",
        ]))
        client = self.connect(socket_path)

        time.sleep(0.3)  # let the monitor loop pick it up either way

        response = client.call("stations.list")

        self.assertTrue(response["ok"])
        self.assertEqual(response["result"], [])


if __name__ == "__main__":
    unittest.main()
