"""
Background daemon that owns the KAM-XL's serial connection and
exposes its capabilities to other local processes over a Unix domain
socket, using a newline-delimited JSON protocol.

Why a daemon: only one process can hold a serial port open at a time.
Everything downstream of this milestone (REST API, web terminal, live
monitor, BBS, ...) is meant to talk to *this* process, not directly to
the TNC.

Protocol (newline-delimited JSON in both directions):

    Client -> Daemon (request):
        {"id": <string>, "method": <string>, "params": {...}}

    Daemon -> Client (response, matches the request's id):
        {"id": <string>, "ok": true, "result": <any>}
        {"id": <string>, "ok": false,
         "error": {"type": <string>, "message": <string>}}

    Daemon -> Client (event -- no "id", only sent to connections that
    have called "monitor.subscribe"):
        {"event": "packet", "data": {...}}

See docs/daemon.md for the full method list, usage, and a known
limitation around monitor traffic and command responses sharing one
physical serial stream.

Run directly:

    python3 kamxl_daemon.py --port COM8 --socket /tmp/kamxl.sock

Single KAM-XL / serial port per daemon instance -- see PROJECT.md for
why multi-device support isn't in scope yet.
"""

import argparse
import dataclasses
import json
import logging
import os
import signal
import socketserver
import threading
import time
import serial

from typing import Any, Callable, Dict, List, Optional, Set

import winlink_api

from kamxl import KAMXL, KAMError
from packet import Packet, PacketParser
from stations import StationTracker
from winlink import OutgoingMessage
from collections import deque
from reception_stats import ReceptionStats
from reception_writer import ReceptionWriter
from aprs import format_aprs_message

DEFAULT_SOCKET_PATH = "/tmp/kamxl.sock"
DEFAULT_BAUDRATE = 19200  # matches KAMXL's own constructor default

# Not configured here -- main() calls logging.basicConfig() so a real
# daemon run prints activity to its terminal. A NullHandler is added
# directly (rather than leaving this unconfigured) so importing this
# as a library -- e.g. tests/test_daemon.py -- doesn't trigger the
# logging module's "no handlers configured" fallback, which would
# otherwise print WARNING-and-above messages (missing params, command
# errors) straight to stderr during the offline test run.
logger = logging.getLogger("kamxl_daemon")
logger.addHandler(logging.NullHandler())


class KAMDaemon:
    """
    Owns a connected KAMXL instance and dispatches JSON requests to
    it, serialized behind a single lock -- only one Terminal Mode
    transaction can be in flight on the wire at a time.
    """
    def __init__(
        self,
        kam: KAMXL,
        socket_path: str = DEFAULT_SOCKET_PATH,
        history_db: Optional[str] = None,
    ) -> None:
        self.kam = kam
        self.socket_path = socket_path
        self._reception_writer = None

        if history_db is not None:
            self._reception_writer = ReceptionWriter(history_db)
            self._reception_writer.start()
        self._kam_lock = threading.RLock()
        # APRS RF safety latch. Never assume command mode merely
        # because the serial device is open or the daemon started.
        self._aprs_safety_state = "unknown"
        self._aprs_safety_reason = "TNC operating mode not verified"

        # Conservative persistent-session guard.
        # A connection attempt may leave an AX.25 session active
        # even after its RPC handler has returned.
        # A daemon restart does not reset the physical TNC.
        # Its previous AX.25 session and terminal mode are unknown.
        # Never infer a safe state merely from opening serial.
        self._aprs_session_uncertain = True

        self._subscribers: Set["DaemonRequestHandler"] = set()
        self._subscribers_lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()

        # Milestone 7 (APRS mapping): built passively from whatever
        # MONITOR traffic the daemon happens to see -- see
        # stations.py. Reading _stations doesn't need _kam_lock (it
        # never touches the serial connection), but does need its own
        # lock, since the monitor thread writes to it from the
        # background while ordinary request-handling threads read it
        # via stations.list/stations.get.
        self._stations = StationTracker()
        self._stations_lock = threading.Lock()

        # APRS diagnostics — counters are maintained independently
        # of serial access and station tracking.
        self._stats_lock = threading.Lock()
        self._reception_stats = ReceptionStats()
        self._started_at = time.time()
        self._started_monotonic = time.monotonic()
        self._packets_received = 0
        self._packets_by_port: Dict[int, int] = {}
        self._last_packet_at: Optional[float] = None

        # Monotonic timestamps of packets received within the last 5 minutes.
        self._recent_packet_times = deque()
        # Minute-indexed APRS activity history.
        # Store only the most recent 60 minute buckets.
        self._activity_minutes: Dict[int, int] = {}        

        self._methods: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "ping": self._m_ping,
            "status": self._m_status,
            "get": self._m_get,
            "set": self._m_set,
            "send_command": self._m_send_command,
            "get_typed": self._m_get_typed,
            "set_typed": self._m_set_typed,
            "get_configuration": self._m_get_configuration,
            "connect_station": self._m_connect_station,
            "send_connected": self._m_send_connected,
            "read_connected": self._m_read_connected,
            "disconnect_station": self._m_disconnect_station,
            "pbbs.list_messages": self._m_pbbs_list_messages,
            "pbbs.read_message": self._m_pbbs_read_message,
            "winlink.check_mail": self._m_winlink_check_mail,
            "winlink.send_message": self._m_winlink_send_message,
            "winlink.account_exists": self._m_winlink_account_exists,
            "winlink.gateway_status": self._m_winlink_gateway_status,
            "winlink.nearby_gateways": self._m_winlink_nearby_gateways,
            "monitor.subscribe": self._m_monitor_subscribe,
            "monitor.unsubscribe": self._m_monitor_unsubscribe,
            "stations.list": self._m_stations_list,
            "stations.get": self._m_stations_get,
            "aprs.stats": self._m_aprs_stats,
            "aprs.history": self._m_aprs_history,
            "aprs.preview_message": self._m_aprs_preview_message,
            "aprs.send_message": self._m_aprs_send_message,
            "aprs.tx_safety": self._m_aprs_tx_safety,
            "aprs.tx_preflight": self._m_aprs_tx_preflight,
            "aprs.tx_get_unproto": self._m_aprs_tx_get_unproto,
            "aprs.tx_recover": self._m_aprs_tx_recover,
            "aprs.tx_initialize": self._m_aprs_tx_initialize,
            "aprs.tx_exit_kiss": self._m_aprs_tx_exit_kiss,
            "aprs.tx_diagnostics": self._m_aprs_tx_diagnostics,
        }

        self._server: Optional["_ThreadingUnixStreamServer"] = None
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

        # Always on, from construction -- not gated on a subscriber
        # the way monitor broadcasting itself started out (see
        # _monitor_loop()'s docstring below for why this changed).
        self._ensure_monitor_thread()

    # -----------------------------------------------------------------------
    # Method dispatch
    # -----------------------------------------------------------------------

    def dispatch(self, method: Optional[str], params: Dict[str, Any]) -> Any:
        handler = self._methods.get(method) if method else None

        if handler is None:
            raise KAMError(f"Unknown method: {method!r}")

        return handler(params or {})

    def _m_ping(self, params: Dict[str, Any]) -> str:
        return "pong"

    def _m_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._subscribers_lock:
            subscriber_count = len(self._subscribers)

        with self._kam_lock:
            connected = self.kam.is_connected

        with self._stats_lock:
            packets_received = self._packets_received
            packets_by_port = dict(self._packets_by_port)
            last_packet_at = self._last_packet_at
            now = time.monotonic()
            self._prune_packet_times(now)

            packets_last_1m = sum(
                1 for timestamp in self._recent_packet_times
                if timestamp > now - 60.0
            )

            packets_last_5m = len(self._recent_packet_times)
            current_minute = int(now // 60)

            activity_history = [
                self._activity_minutes.get(minute, 0)
                for minute in range(current_minute - 59, current_minute + 1)
            ]

        return {
            "connected": connected,
            "port": self.kam.port,
            "monitor_subscribers": subscriber_count,
            "diagnostics": {
                "packets_received": packets_received,
                "packets_by_port": packets_by_port,
                "last_packet_at": last_packet_at,
                "started_at": self._started_at,
                "uptime_seconds": now - self._started_monotonic,
                "packets_last_1m": packets_last_1m,
                "packets_last_5m": packets_last_5m,
                "rate_1m": float(packets_last_1m),
                "rate_5m": round(packets_last_5m / 5.0, 2),
                "activity_history": activity_history,
            },
        }

    def _m_get(self, params: Dict[str, Any]) -> str:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety(
                "TNC query or read may have changed session state"
            )
            return self.kam.get(params["command"])

    def _m_set(self, params: Dict[str, Any]) -> str:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("TNC configuration changed")
            return self.kam.set(params["command"], params["value"])

    def _invalidate_aprs_safety(self, reason: str) -> None:
        """
        Mark APRS transmission unsafe.

        Caller must hold _kam_lock.
        Only an explicit future verification procedure may
        establish a ready state.
        """
        self._aprs_safety_state = "unsafe"
        self._aprs_safety_reason = reason

    def _m_send_command(self, params: Dict[str, Any]) -> str:
        # Raw terminal commands may change the TNC's operating mode.
        # Mark the APRS safety state unsafe before touching serial.
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety(
                "Raw terminal command may have changed TNC state"
            )
            return self.kam.send_command(
                params["command"],
                command_timeout=params.get("timeout", 10),
            )

    def _m_get_typed(self, params: Dict[str, Any]) -> Any:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety(
                "TNC query or read may have changed session state"
            )
            return self.kam.get_typed(params["command"])

    def _m_set_typed(self, params: Dict[str, Any]) -> Any:
        # JSON has no tuple type -- a multi-port value arrives as a
        # list and needs converting back before it reaches kamxl.py's
        # isinstance(value, tuple) checks.
        value = params["value"]

        if isinstance(value, list):
            value = tuple(value)

        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("TNC typed configuration changed")
            return self.kam.set_typed(params["command"], value)

    def _m_get_configuration(self, params: Dict[str, Any]) -> Dict[str, str]:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety(
                "TNC query or read may have changed session state"
            )
            return self.kam.get_configuration()

    def _m_connect_station(self, params: Dict[str, Any]) -> str:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("AX.25 connection attempted")
            return self.kam.connect_station(
                params["callsign"],
                via=params.get("via"),
                timeout=params.get("timeout", 60),
            )

    def _m_send_connected(self, params: Dict[str, Any]) -> None:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("Connected data operation attempted")
            self.kam.send_connected(
                params["text"],
                add_cr=params.get("add_cr", True),
            )

        return None

    def _m_read_connected(self, params: Dict[str, Any]) -> str:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety(
                "TNC query or read may have changed session state"
            )
            return self.kam.read_connected(params.get("timeout", 5))

    def _m_disconnect_station(self, params: Dict[str, Any]) -> str:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("AX.25 disconnect attempted")
            return self.kam.disconnect_station(
                params.get("timeout", 30),
                command_mode_timeout=params.get("command_mode_timeout", 5)
            )

    def _m_pbbs_list_messages(
        self, params: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("PBBS operation attempted")
            messages = self.kam.list_pbbs_messages(
                mypbbs=params.get("mypbbs"),
                connect_timeout=params.get("connect_timeout", 15),
                read_timeout=params.get("read_timeout", 10),
            )

        return [dataclasses.asdict(message) for message in messages]

    def _m_pbbs_read_message(
        self, params: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("PBBS operation attempted")
            message = self.kam.read_pbbs_message(
                int(params["number"]),
                mypbbs=params.get("mypbbs"),
                connect_timeout=params.get("connect_timeout", 15),
                read_timeout=params.get("read_timeout", 10),
            )

        return dataclasses.asdict(message) if message is not None else None

    def _m_winlink_check_mail(
        self, params: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        # Deliberately never logs params here (or anywhere else this
        # request passes through -- the request-handling loop below
        # only ever logs the *method name* on success/failure, never
        # its params) -- params["password"] is a real Winlink account
        # password.
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("Winlink operation attempted")
            messages = self.kam.check_winlink_mail(
                gateway=params["gateway"],
                password=params["password"],
                mycall=params.get("mycall"),
                connect_timeout=params.get("connect_timeout", 60),
                read_timeout=params.get("read_timeout", 30),
            )

        return [dataclasses.asdict(message) for message in messages]

    def _m_winlink_send_message(
        self, params: Dict[str, Any]
    ) -> List[str]:
        # Same password-logging discipline as _m_winlink_check_mail()
        # above -- never logs params.
        #
        # See winlink.py's module docstring's "SEND SUPPORT" note for
        # scope (send-only, text-body-only, B2/FC only) and
        # UNVERIFIED-AGAINST-A-REAL-GATEWAY caveat.
        messages = [
            OutgoingMessage(
                to=list(m["to"]),
                subject=m["subject"],
                body=m["body"],
                cc=list(m.get("cc", [])),
                msg_type=m.get("msg_type", "Private"),
                mid=m.get("mid"),
            )
            for m in params["messages"]
        ]

        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("Winlink operation attempted")
            accepted_mids = self.kam.send_winlink_message(
                gateway=params["gateway"],
                password=params["password"],
                messages=messages,
                mycall=params.get("mycall"),
                connect_timeout=params.get("connect_timeout", 60),
                read_timeout=params.get("read_timeout", 30),
            )

        return accepted_mids

    def _m_aprs_send_message(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """One operator-approved APRS submission on verified VHF Port 2.

        A successful result means submitted to the TNC, not RF delivery.
        No automatic retry is performed.
        """
        from aprs_tx import (
            APRSTransmitError, _LIVE_APRS_CAPABILITY,
            execute_live_aprs_tx_transaction,
        )

        # Require per-call, exact confirmation; never cache authorization.
        if params.get("confirm_transmit") is not True:
            raise APRSTransmitError("Explicit confirm_transmit=true required")
        if params.get("confirm_vhf_port2_radio_ready") is not True:
            raise APRSTransmitError(
                "Confirm VHF Port 2 radio, audio, PTT, antenna and power "
                "are ready before live transmission"
            )
        if params.get("confirm_hf_port1_disconnected") is not True:
            raise APRSTransmitError("Confirm HF Port 1 radio disconnected")
        if params.get("path", "") != "":
            raise APRSTransmitError("First live APRS transmission must be direct")

        # Validate payload before touching serial or changing session state.
        payload = format_aprs_message(
            addressee=params["addressee"],
            message=params["message"],
            paclen=128,
        )

        with self._kam_lock:
            if self._aprs_session_uncertain or self._aprs_safety_state == "unsafe":
                raise APRSTransmitError(
                    "APRS session unverified; initialize with radios "
                    "disconnected before attempting live transmission"
                )
            if not self.kam.is_connected or (
                getattr(self.kam.serial, "exclusive", None) is not True
            ):
                raise APRSTransmitError("Exclusive serial connection required")
            if (self._monitor_stop.is_set() or
                self._monitor_thread is None or
                not self._monitor_thread.is_alive()):
                raise APRSTransmitError("APRS monitor thread not healthy")

            # Mark unsafe before the first potentially state-changing write.
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("APRS transmission in progress")
            try:
                result = execute_live_aprs_tx_transaction(
                    self.kam, payload, _capability=_LIVE_APRS_CAPABILITY
                )
            except Exception:
                self._invalidate_aprs_safety(
                    "APRS transmission failed or state uncertain; "
                    "do not retry automatically"
                )
                raise

            # Executor verified command-mode recovery and UNPROTO restoration.
            # Keep RF readiness unknown; never persist a transmit authorization.
            self._aprs_session_uncertain = False
            self._aprs_safety_state = "unknown"
            self._aprs_safety_reason = (
                "APRS submission completed; command mode and UNPROTO "
                "restored; RF delivery not confirmed"
            )
            return {**result, "payload": payload, "state": "unknown"}

    def _m_aprs_tx_preflight(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Query APRS transmit prerequisites under exclusive daemon access.

        A successful preflight is diagnostic only. It does not establish
        command-mode certainty or authorize RF transmission.
        """
        from aprs_tx import check_aprs_tx_preflight

        with self._kam_lock:
            if self._aprs_session_uncertain:
                return {
                    "passed": False,
                    "state": self._aprs_safety_state,
                    "reason": (
                        "APRS preflight blocked: TNC session state "
                        "has not been independently verified"
                    ),
                    "rf_enabled": False,
                    "transmitted": False,
                    "checks": [],
                }
            if self._aprs_safety_state == "unsafe":
                return {
                    "passed": False,
                    "state": "unsafe",
                    "reason": self._aprs_safety_reason,
                    "rf_enabled": False,
                    "transmitted": False,
                    "checks": [],
                }

            try:
                result = check_aprs_tx_preflight(self.kam)
            except Exception:
                self._aprs_session_uncertain = True
                self._invalidate_aprs_safety(
                    "APRS preflight failed or TNC response is uncertain"
                )
                raise

            return {
                **result,
                "state": self._aprs_safety_state,
                "reason": self._aprs_safety_reason,
            }

    def _m_aprs_tx_get_unproto(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Read the current UNPROTO setting without modifying it.

        Requires a previously verified APRS session.
        Does not authorize RF transmission.
        """
        from aprs_tx import APRSTransmitError, validate_unproto

        with self._kam_lock:
            if (
                self._aprs_session_uncertain
                or self._aprs_safety_state == "unsafe"
            ):
                raise APRSTransmitError(
                    "UNPROTO query blocked: APRS session is not verified"
                )

            try:
                original = validate_unproto(self.kam.get("UNPROTO"))
            except Exception:
                self._aprs_session_uncertain = True
                self._invalidate_aprs_safety(
                    "UNPROTO query failed; TNC state uncertain"
                )
                raise

            return {
                "unproto": original,
                "rf_enabled": False,
                "transmitted": False,
            }

    def _require_winlink_api_key(self) -> str:
        # Deliberately an env var only, read fresh on every call --
        # no --winlink-api-key CLI flag (chosen over the alternatives
        # specifically so the key never has to touch a config file or
        # show up in a process listing/shell history), and no need to
        # restart the daemon to pick up a change either.
        api_key = os.environ.get("WINLINK_API_KEY")

        if not api_key:
            raise KAMError(
                "WINLINK_API_KEY is not set -- the Winlink web-service "
                "API (account lookups, gateway listings) needs the "
                "access key issued by the Winlink Development Team. "
                "See PROJECT.md's \"Winlink Web Service API\" milestone."
            )

        return api_key

    def _m_winlink_account_exists(self, params: Dict[str, Any]) -> bool:
        # Never touches self.kam/_kam_lock -- this is a plain HTTPS
        # call to api.winlink.org, nothing to do with the KAM-XL or
        # its serial port at all. Holding _kam_lock for the duration
        # of a network round-trip would needlessly block ordinary
        # KAM-XL commands for no reason.
        return winlink_api.account_exists(
            params["callsign"], self._require_winlink_api_key()
        )

    def _m_winlink_gateway_status(
        self, params: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        gateways = winlink_api.get_gateway_status(
            self._require_winlink_api_key(),
            mode=params.get("mode", "AnyAll"),
            history_hours=params.get("history_hours", 48),
            service_codes=tuple(params.get("service_codes", ["PUBLIC"])),
        )

        return [dataclasses.asdict(gateway) for gateway in gateways]

    def _m_winlink_nearby_gateways(
        self, params: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        gateways = winlink_api.get_gateway_status(
            self._require_winlink_api_key(),
            mode=params.get("mode", "AnyAll"),
            history_hours=params.get("history_hours", 48),
            service_codes=tuple(params.get("service_codes", ["PUBLIC"])),
        )

        results = winlink_api.nearby_gateways(
            gateways,
            float(params["latitude"]),
            float(params["longitude"]),
            max_distance_km=params.get("max_distance_km"),
            limit=params.get("limit"),
        )

        return [
            {"gateway": dataclasses.asdict(gateway), "distance_km": distance}
            for gateway, distance in results
        ]

    def _m_monitor_subscribe(self, params: Dict[str, Any]) -> None:
        # Actual subscriber-set membership is handled by the request
        # handler itself (it needs to add *itself*) -- this just
        # guarantees the background broadcast thread is running (a
        # defensive no-op in the normal case, since __init__ already
        # started it; only matters if it somehow died).
        self._ensure_monitor_thread()
        return None

    def _m_monitor_unsubscribe(self, params: Dict[str, Any]) -> None:
        return None

    def _m_aprs_preview_message(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Format an outgoing APRS message without touching the TNC.

        This method performs no serial I/O and cannot transmit RF.
        """
        payload = format_aprs_message(
            addressee=params["addressee"],
            message=params["message"],
            paclen=128,
        )

        return {
            "payload": payload,
            "length": len(payload.encode("ascii")),
            "paclen": 128,
            "port": 2,
            "transmitted": False,
        }

    def _m_aprs_stats(self, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._stats_lock:
            return self._reception_stats.snapshot()

    def _m_aprs_history(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Return persistent packet reception history."""
        if self._reception_writer is None:
            return {
                "enabled": False,
                "scope": "persistent",
                "total_packets": 0,
                "hourly_counts": [],
            }

        since = params.get("since")

        if since is not None:
            if isinstance(since, bool) or not isinstance(since, int):
                raise ValueError("'since' must be a Unix timestamp integer")

        result = self._reception_writer.snapshot(since=since)
        result["enabled"] = True
        return result

    def _m_aprs_tx_safety(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Report APRS transmit safety without accessing the TNC."""
        with self._kam_lock:
            return {
                "state": self._aprs_safety_state,
                "reason": self._aprs_safety_reason,
                "rf_enabled": False,
            }

    def _m_stations_list(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        with self._stations_lock:
            self._stations.expire_stale(max_age_seconds=3600)
            stations = self._stations.list_stations()

        return [dataclasses.asdict(station) for station in stations]

    def _m_stations_get(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._stations_lock:
            self._stations.expire_stale(max_age_seconds=3600)
            station = self._stations.get_station(params["callsign"])

        return dataclasses.asdict(station) if station is not None else None

    def _m_aprs_tx_initialize(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Explicit operator-authorized APRS session initialization.

        Requires external confirmation that radio interfaces are
        disconnected. Never authorizes RF transmission.
        """
        from aprs_tx import APRSTransmitError, recover_aprs_command_mode

        if params.get("confirm_radio_disconnected") is not True:
            raise APRSTransmitError(
                "Initialization requires explicit confirmation "
                "that all radio interfaces are disconnected"
            )

        with self._kam_lock:
            # Assume uncertainty until verification completes.
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety(
                "APRS initialization in progress"
            )

            if (
                self._monitor_stop.is_set()
                or self._monitor_thread is None
                or not self._monitor_thread.is_alive()
            ):
                self._invalidate_aprs_safety(
                    "APRS initialization blocked: monitor thread not healthy"
                )
                raise APRSTransmitError(
                    "APRS initialization requires a running monitor thread"
                )

            try:
                result = recover_aprs_command_mode(self.kam)
            except Exception:
                self._aprs_session_uncertain = True
                self._invalidate_aprs_safety(
                    "APRS initialization failed; TNC state uncertain"
                )
                logger.exception(
                    "APRS initialization failed during "
                    "command-mode recovery"
                )
                raise

            # Both command mode and disconnected A/2 were verified.
            self._aprs_session_uncertain = False
            self._aprs_safety_state = "unknown"
            self._aprs_safety_reason = (
                "Command mode and disconnected A/2 verified; "
                "RF prerequisites not fully established"
            )

            return {
                **result,
                "initialized": True,
                "state": self._aprs_safety_state,
                "reason": self._aprs_safety_reason,
                "rf_enabled": False,
                "transmitted": False,
            }

    def _m_aprs_tx_recover(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Explicit operator-requested command-mode recovery.

        Recovery changes TNC operating mode but does not transmit
        APRS data or authorize RF transmission.
        """
        from aprs_tx import APRSTransmitError, recover_aprs_command_mode

        if params.get("confirm_recovery") is not True:
            raise APRSTransmitError(
                "Explicit confirm_recovery=true is required"
            )

        with self._kam_lock:
            if self._aprs_session_uncertain:
                raise APRSTransmitError(
                    "APRS recovery blocked: an AX.25 connection "
                    "was attempted and its final state has not "
                    "been independently verified."
                )
            self._invalidate_aprs_safety(
                "APRS command-mode recovery in progress"
            )

            try:
                result = recover_aprs_command_mode(self.kam)
            except Exception:
                self._aprs_session_uncertain = True
                self._invalidate_aprs_safety(
                    "APRS command-mode recovery failed; TNC state uncertain"
                )
                raise

            # Recovery verifies command mode and disconnected A/2
            # at this instant, but does not establish RF readiness.
            self._aprs_safety_state = "unknown"
            self._aprs_safety_reason = (
                "Command mode and disconnected A/2 verified; "
                "RF prerequisites not fully established"
            )

            return {
                **result,
                "state": self._aprs_safety_state,
                "reason": self._aprs_safety_reason,
            }

    def _m_aprs_tx_diagnostics(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Report APRS safety prerequisites without touching serial.

        This is a diagnostic snapshot, not authorization
        to transmit or recover the TNC.
        """
        with self._kam_lock:
            return {
                "serial_open": self.kam.is_connected,
                "serial_exclusive_requested": (
                    self.kam.is_connected
                    and getattr(self.kam.serial, "exclusive", None) is True
                ),
                "session_uncertain": self._aprs_session_uncertain,
                "safety_state": self._aprs_safety_state,
                "safety_reason": self._aprs_safety_reason,
                "command_mode_verified": False,
                "rf_enabled": False,
                "recovery_allowed": False,
            }

    def _m_aprs_tx_exit_kiss(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Explicit, fail-closed KISS exit attempt.

        Does not initialize APRS or authorize RF transmission.
        """
        from aprs_tx import APRSTransmitError

        if params.get("confirm_radio_disconnected") is not True:
            raise APRSTransmitError(
                "KISS exit requires explicit confirmation "
                "that all radio interfaces are disconnected"
            )

        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety(
                "KISS exit attempted; TNC state requires verification"
            )

            if (
                self._monitor_stop.is_set()
                or self._monitor_thread is None
                or not self._monitor_thread.is_alive()
            ):
                raise APRSTransmitError(
                    "KISS exit requires a healthy monitor thread"
                )

            try:
                response = self.kam.exit_kiss_mode()
            except Exception as exc:
                logger.exception("KAM-XL KISS exit failed")
                raise APRSTransmitError(
                    "KISS exit failed; TNC state remains uncertain"
                ) from exc
            return {
                "exit_sequence_sent": True,
                "response_hex": response.hex(),
                "response_text": response.decode(
                    "latin-1", errors="replace"
                )[:500],
                "terminal_mode_verified": False,
                "initialized": False,
                "rf_enabled": False,
                "transmitted": False,
            }

    # -----------------------------------------------------------------------
    # Monitor broadcast
    # -----------------------------------------------------------------------

    def _ensure_monitor_thread(self) -> None:
        with self._subscribers_lock:
            if self._monitor_thread and self._monitor_thread.is_alive():
                return

            self._monitor_stop.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
            )
            self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        """
        Continuously polls the KAM-XL for MONITOR traffic and feeds
        every decoded Packet to both the station tracker (milestone 7)
        and any live monitor.subscribe()'d clients.

        Milestone 5-6 behavior had this thread start on the first
        monitor.subscribe() and exit once the last subscriber left --
        fine when the only consumer was a live monitor pane, but
        milestone 7's station database needs traffic decoded
        continuously to build up a useful picture over time, not just
        while somebody happens to have the map page open. So this now
        runs for the daemon's whole lifetime (started once from
        __init__(), stopped only by shutdown()) -- subscriber count no
        longer has any bearing on whether it runs, only on whether
        _broadcast_packet() actually has anyone to send events to.
        """
        parser = PacketParser()

        while not self._monitor_stop.is_set():
            # Brief lock acquisition per poll (rather than holding it
            # for the whole loop) so ordinary get/set requests can
            # still interleave while monitoring is active. This does
            # NOT fully prevent unsolicited MONITOR traffic and a
            # command's response from arriving interleaved on the
            # wire itself -- see docs/daemon.md's "Known limitation".

            try:
                with self._kam_lock:
                    text = self.kam.read_available()
            except Exception:
                with self._kam_lock:
                    self._aprs_session_uncertain = True
                    self._invalidate_aprs_safety(
                        "Monitor thread failed; TNC state uncertain"
                    )

                logger.exception(
                    "KAM-XL monitor thread failed; "
                    "exiting so systemd can restart the daemon"
                )
                os._exit(1)

            if text:
                # Logged before parsing, unconditionally on any
                # non-empty read -- not just for completed packets --
                # so a line PacketParser's HEADER_RE doesn't recognize
                # (e.g. the KAM-XL's own <C>/<UA>/<UI>-style control-
                # packet annotations, on by default via MCOM/MRESP)
                # is still visible with -v/--verbose, instead of
                # silently vanishing with no way to tell "nothing
                # arrived" apart from "something arrived but didn't
                # parse".
                logger.debug("monitor raw: %r", text)

                for packet in parser.feed(text):
                    self._handle_packet(packet)

            time.sleep(0.05)

        # PacketParser only knows a packet is complete once the
        # *next* header line arrives -- so whatever was still being
        # assembled when the daemon started shutting down would
        # otherwise be silently lost. Flush it out now rather than
        # dropping it.
        for packet in parser.flush():
            self._handle_packet(packet)

    def _prune_packet_times(self, now: float) -> None:
        """Remove packet timestamps outside the rolling five-minute window.
 
        Caller must hold _stats_lock.
        """
        cutoff = now - 300.0

        while (
            self._recent_packet_times
            and self._recent_packet_times[0] <= cutoff
        ):
            self._recent_packet_times.popleft()    

    def _handle_packet(self, packet: Packet) -> None:
        now = time.monotonic()

        with self._stats_lock:
            self._reception_stats.update(packet)
            self._packets_received += 1
            self._packets_by_port[packet.port] = (
                self._packets_by_port.get(packet.port, 0) + 1
            )
            self._last_packet_at = time.time()

            self._recent_packet_times.append(now)
            self._prune_packet_times(now)
            minute = int(now // 60)
            self._activity_minutes[minute] = (
                self._activity_minutes.get(minute, 0) + 1
            )

            # Keep the current minute and the previous 59 minutes.
            cutoff_minute = minute - 59
            for old_minute in list(self._activity_minutes):
                if old_minute < cutoff_minute:
                    del self._activity_minutes[old_minute]
        if self._reception_writer is not None:
            self._reception_writer.submit(packet)
        with self._stations_lock:
            self._stations.update(packet)

        self._broadcast_packet(packet)

    def _broadcast_packet(self, packet: Packet) -> None:
        event = {
            "event": "packet",
            "data": dataclasses.asdict(packet),
        }

        with self._subscribers_lock:
            subscribers = list(self._subscribers)

        logger.debug(
            "packet: %s -> %s (port %s) to %d subscriber(s)",
            packet.source, packet.destination, packet.port,
            len(subscribers)
        )

        for handler in subscribers:
            handler.send_event(event)

    def add_subscriber(self, handler: "DaemonRequestHandler") -> None:
        with self._subscribers_lock:
            self._subscribers.add(handler)

        self._ensure_monitor_thread()

    def remove_subscriber(self, handler: "DaemonRequestHandler") -> None:
        with self._subscribers_lock:
            self._subscribers.discard(handler)

    # -----------------------------------------------------------------------
    # Server lifecycle
    # -----------------------------------------------------------------------

    def serve_forever(self) -> None:
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)

        self._server = _ThreadingUnixStreamServer(
            self.socket_path,
            DaemonRequestHandler
        )
        self._server.daemon_instance = self  # type: ignore[attr-defined]

        try:
            # socketserver's default poll_interval (0.5s) is how often
            # serve_forever()'s loop wakes up to check for a pending
            # shutdown() -- a tighter interval makes shutdown() (and
            # therefore every test that spins this daemon up and back
            # down) noticeably more responsive, at a negligible idle
            # CPU cost.
            self._server.serve_forever(poll_interval=0.1)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        # serve_forever() calls this again itself, from a different
        # thread, once its own serve_forever() loop unblocks as a
        # *result* of the self._server.shutdown() call below -- so
        # this can legitimately be entered concurrently by two
        # threads. Without this guard, one thread can see
        # self._server as not-None, then have the other thread set it
        # to None out from under it before it calls server_close().
        with self._shutdown_lock:
            if self._shutdown_done:
                return

            self._shutdown_done = True

            self._monitor_stop.set()
            if (
                self._monitor_thread is not None
                and self._monitor_thread is not threading.current_thread()
            ):
                self._monitor_thread.join()

            if self._reception_writer is not None:
                self._reception_writer.stop()

            if self._server is not None:
                self._server.shutdown()
                self._server.server_close()
                self._server = None

            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)

            self.kam.disconnect()


class _ThreadingUnixStreamServer(
    socketserver.ThreadingMixIn,
    socketserver.UnixStreamServer
):
    daemon_threads = True
    allow_reuse_address = True


class DaemonRequestHandler(socketserver.StreamRequestHandler):
    """
    One instance per connected client. Handles both directions on
    that client's socket: synchronous request/response, and (if
    subscribed) asynchronous "packet" events pushed by the daemon's
    monitor broadcast thread -- hence the write lock, since both can
    happen concurrently from different threads.
    """

    def setup(self) -> None:
        super().setup()
        self._write_lock = threading.Lock()
        self._subscribed = False
        # Unix domain sockets don't have a meaningful client_address
        # (it's typically empty) -- use the handling thread's id as a
        # short, unique-enough label to tell concurrent connections
        # apart in the log.
        self._client_id = f"conn-{threading.get_ident() % 10000}"
        logger.info("%s: connected", self._client_id)

    def handle(self) -> None:
        daemon: KAMDaemon = self.server.daemon_instance  # type: ignore[attr-defined]

        for raw_line in self.rfile:
            line = raw_line.strip()

            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                self._send({
                    "id": None,
                    "ok": False,
                    "error": {
                        "type": "ProtocolError",
                        "message": f"Invalid JSON: {exc}",
                    },
                })
                continue

            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params") or {}

            try:
                if method == "monitor.subscribe":
                    self._subscribed = True
                    daemon.add_subscriber(self)
                elif method == "monitor.unsubscribe":
                    self._subscribed = False
                    daemon.remove_subscriber(self)

                result = daemon.dispatch(method, params)

                logger.info("%s: %s -> ok", self._client_id, method)

                self._send({
                    "id": request_id,
                    "ok": True,
                    "result": result,
                })
            except KeyError as exc:
                logger.warning(
                    "%s: %s -> missing param %s",
                    self._client_id, method, exc
                )

                self._send({
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "type": "MissingParam",
                        "message": f"Missing required param: {exc}",
                    },
                })
            except Exception as exc:
                logger.warning(
                    "%s: %s -> %s: %s",
                    self._client_id, method, type(exc).__name__, exc
                )

                self._send({
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                })

    def finish(self) -> None:
        daemon: KAMDaemon = self.server.daemon_instance  # type: ignore[attr-defined]

        if self._subscribed:
            daemon.remove_subscriber(self)

        logger.info("%s: disconnected", self._client_id)

        super().finish()

    def send_event(self, event: Dict[str, Any]) -> None:
        self._send(event)

    def _send(self, obj: Dict[str, Any]) -> None:
        data = (json.dumps(obj) + "\n").encode("ascii")

        with self._write_lock:
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except OSError:
                # Client went away mid-write (e.g. a monitor event
                # racing a disconnect) -- nothing useful to do.
                pass


def _load_dotenv(path: str = ".env") -> None:
    """
    Populate os.environ from a simple KEY=VALUE .env file, if present
    -- e.g. WINLINK_API_KEY, so it doesn't have to be `export`ed by
    hand every time the daemon starts. A real environment variable
    that's already set always wins (standard dotenv behavior -- the
    file only fills in whatever isn't already set, it never
    overrides), so this is purely a convenience on top of the
    env-var-only design (see _require_winlink_api_key()'s docstring
    for why that was chosen over a CLI flag) -- not a config file the
    daemon depends on.

    Deliberately minimal, no third-party dotenv dependency (same
    stdlib-only choice kamxl_rest.py made for http.server and
    winlink_api.py made for urllib): blank lines and lines starting
    with "#" are skipped, "KEY=VALUE" is split on the first "=", and a
    value wrapped in matching single/double quotes has them stripped.
    No line continuation, no variable interpolation, no "export "
    prefix handling -- add it if a real need for any of that shows up.

    Silently does nothing if ``path`` doesn't exist -- this is a
    convenience, not a requirement; the daemon must keep working with
    real environment variables and no .env file at all, exactly as it
    did before this existed.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return

    for raw_line in lines:
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        if key and key not in os.environ:
            os.environ[key] = value


def main(argv: Optional[list] = None) -> None:
    # Before anything else -- including building the argument parser
    # below, whose --port/--socket/--baud defaults themselves read
    # os.environ -- so a .env-supplied value is indistinguishable from
    # a real exported one by the time either of those look for it.
    _load_dotenv()

    parser = argparse.ArgumentParser(
        description="KAM-XL background daemon"
    )
    parser.add_argument(
        "--port",
        default=os.environ.get("KAMXL_PORT"),
        required=os.environ.get("KAMXL_PORT") is None,
        help=(
            "Serial port the KAM-XL is on, e.g. /dev/ttyUSB0 or COM8 "
            "(default: $KAMXL_PORT; required if that's unset). No "
            "hardcoded fallback -- a wrong/missing port should fail "
            "fast, not silently try to open a device that isn't there "
            "and hang every subsequent command until its timeout."
        ),
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("KAMXL_SOCKET", DEFAULT_SOCKET_PATH),
        help=f"Unix socket path to listen on (default: {DEFAULT_SOCKET_PATH})",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=int(os.environ.get("KAMXL_BAUD", DEFAULT_BAUDRATE)),
        help=(
            f"Host serial baud rate (default: $KAMXL_BAUD, or "
            f"{DEFAULT_BAUDRATE} if that's unset -- must match the "
            f"KAM-XL's own HBAUD setting. Found the hard way: a "
            f"firmware flash can leave the KAM-XL at a different host "
            f"baud rate than before (e.g. 38400 instead of the usual "
            f"19200), and there was previously no way to tell this "
            f"daemon about that without editing code -- every command "
            f"would just silently time out instead of failing fast "
            f"with a clear mismatch. Check with a plain serial terminal "
            f"(e.g. minicom) if unsure what the KAM-XL is actually set "
            f"to right now."
        ),
    )

    parser.add_argument(
        "--history-db",
        default=os.environ.get("KAMXL_HISTORY_DB"),
        help=(
            "SQLite reception history database path "
            "(default: $KAMXL_HISTORY_DB; disabled if unset)"
        ),
    )

    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Also log individual packet broadcasts (DEBUG level)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    kam = KAMXL(args.port, baudrate=args.baud)
    kam.connect()

    daemon = KAMDaemon(
        kam,
        args.socket,
        history_db=args.history_db,
    )

    # socketserver.BaseServer.shutdown() blocks until serve_forever()'s
    # own loop notices and exits -- and it deadlocks if called from the
    # same thread that's running serve_forever(), since that thread is
    # exactly what's suspended (mid-loop) to run this signal handler in
    # the first place. It can only ever resume once the handler
    # returns, and the handler can't return until it does. So
    # serve_forever() runs on a background thread instead, and the
    # signal handler just flags a stop and lets the main thread do the
    # actual daemon.shutdown() call, safely, from outside that thread.
    shutdown_requested = threading.Event()

    def _handle_signal(signum: int, frame: Any) -> None:
        logger.info("Shutting down (signal %s)...", signum)
        shutdown_requested.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    server_thread = threading.Thread(
        target=daemon.serve_forever,
        daemon=True
    )
    server_thread.start()

    logger.info("KAM-XL daemon: %s -> %s", args.port, args.socket)

    try:
        while not shutdown_requested.is_set():
            shutdown_requested.wait(timeout=1)
    finally:
        daemon.shutdown()
        server_thread.join(timeout=5)


if __name__ == "__main__":
    main()
