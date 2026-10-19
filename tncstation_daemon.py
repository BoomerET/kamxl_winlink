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

    python3 tncstation_daemon.py --port COM8 --socket /tmp/tncstation.sock

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
import queue
import re

from typing import Any, Callable, Dict, List, Optional, Set

import winlink_api

from kamxl import KAMXL, KAMError
from packet import Packet, PacketParser
from tnc import TerminalTNC, create_tnc, normalize_tnc_mode, normalize_tnc_model
from stations import StationTracker
from winlink import OutgoingMessage
from collections import deque
from reception_stats import ReceptionStats
from reception_writer import ReceptionWriter
from aprs import format_aprs_message, parse_aprs_message, unwrap_third_party
from aprs_outbox import OutboundAprsTracker
from aprs_coverage import SETUP_KEY, first_hop, last_hop, normalize_setup

# Stations and objects not heard for this long drop off the map. Also
# how much stored history is replayed into the map at startup.
STATION_MAX_AGE = 3600

DEFAULT_SOCKET_PATH = "/tmp/tncstation.sock"
DEFAULT_BAUDRATE = 19200  # matches KAMXL's own constructor default

# Not configured here -- main() calls logging.basicConfig() so a real
# daemon run prints activity to its terminal. A NullHandler is added
# directly (rather than leaving this unconfigured) so importing this
# as a library -- e.g. tests/test_daemon.py -- doesn't trigger the
# logging module's "no handlers configured" fallback, which would
# otherwise print WARNING-and-above messages (missing params, command
# errors) straight to stderr during the offline test run.
logger = logging.getLogger("tncstation_daemon")

_CALLSIGN_RE = re.compile(r"[A-Z0-9]{1,6}(?:-[0-9]{1,2})?")


class _AutoTxPaused(Exception):
    """An automatic transmission was skipped for now; retried later."""


# Older name, kept for anything that still refers to it.
_BeaconPaused = _AutoTxPaused


def normalize_station_callsign(callsign: str) -> str:
    """Validate and upper-case this station's APRS callsign (SSID ok)."""
    value = (callsign or "").strip().upper()

    if not _CALLSIGN_RE.fullmatch(value):
        raise ValueError(
            f"Invalid APRS station callsign {callsign!r} "
            "(expected e.g. N5BA or N5BA-10)"
        )

    return value
logger.addHandler(logging.NullHandler())


class KAMDaemon:
    """
    Owns a connected TNC driver (a tnc.TerminalTNC -- KAMXL or OpenTNC;
    still called ``kam`` throughout) and dispatches JSON requests to it,
    serialized behind a single lock -- only one Terminal Mode
    transaction can be in flight on the wire at a time.
    """
    def __init__(
        self,
        kam: TerminalTNC,
        socket_path: str = DEFAULT_SOCKET_PATH,
        history_db: Optional[str] = None,
        station_callsign: Optional[str] = None,
        aprs_path: str = "",
        aprs_tocall: str = "APRS",
        beacon: Optional[Dict[str, Any]] = None,
        beacon_interval_minutes: float = 30,
        watchdog_minutes: float = 30,
        retry_count: int = 3,
        retry_interval_seconds: float = 60,
        history_retention_days: float = 0,
        notifier: Optional[Any] = None,
        backup: Optional[Any] = None,
    ) -> None:
        from aprs_tx import validate_aprs_path

        self.kam = kam

        # Digipeater path for everything we transmit on APRS (messages,
        # resends, ACKs, beacons). "" = direct. The command line
        # defaults this to WIDE1-1,WIDE2-1 (TNCSTATION_APRS_PATH).
        self.aprs_path = validate_aprs_path(aprs_path)

        # Position beacon payload, built and validated once up front
        # (None = no position configured, beacon unavailable).
        self._beacon_payload: Optional[str] = None
        self._beacon_position: Optional[Dict[str, Any]] = None
        if beacon is not None:
            from aprs import format_aprs_position
            symbol = beacon.get("symbol", "/-")
            if not isinstance(symbol, str) or len(symbol) != 2:
                raise ValueError(
                    "Beacon symbol must be two characters, e.g. '/-'"
                )
            self._beacon_payload = format_aprs_position(
                float(beacon["latitude"]),
                float(beacon["longitude"]),
                symbol_table=symbol[0],
                symbol_code=symbol[1],
                comment=beacon.get("comment", "") or "",
            )
            self._beacon_position = {
                "latitude": float(beacon["latitude"]),
                "longitude": float(beacon["longitude"]),
                "symbol": symbol,
                "comment": beacon.get("comment", "") or "",
            }

        # Reception watchdog: warn when nothing but our own stations has
        # been heard on Port 2 for this long. 0 disables it.
        if watchdog_minutes and watchdog_minutes < 5:
            raise ValueError("Watchdog threshold must be 0 or >= 5 minutes")
        self._watchdog_seconds = float(watchdog_minutes or 0) * 60

        # Automatic resend of unacknowledged messages (off by default).
        if not 1 <= int(retry_count) <= 5:
            raise ValueError("Retry count must be between 1 and 5")
        if retry_interval_seconds < 30:
            raise ValueError("Retry interval must be at least 30 seconds")
        self._retry_count = int(retry_count)
        self._retry_interval = float(retry_interval_seconds)

        if beacon_interval_minutes < 10:
            raise ValueError("Beacon interval must be at least 10 minutes")
        self._beacon_interval = float(beacon_interval_minutes) * 60

        # This station's APRS callsign (TNCSTATION_CALLSIGN / --callsign):
        # the addressee the inbox shows, the callsign automatic ACKs
        # answer for, and the addressee acks for our own sent messages
        # must carry. Must match the KAM-XL's MYCALL exactly (SSID
        # included). None disables all three -- nothing is ever
        # acked or matched for a callsign we don't know.
        self.station_callsign = (
            normalize_station_callsign(station_callsign)
            if station_callsign is not None else None
        )

        # AX.25 destination ("tocall") on everything we transmit -- see
        # aprs_tx.DEFAULT_TOCALL. The command line defaults this to it;
        # constructing the daemon directly keeps the old "APRS".
        from aprs_tx import validate_tocall

        self.aprs_tocall = validate_tocall(aprs_tocall)
        kam.aprs_tocall = self.aprs_tocall

        # A KISS-mode TNC adds no source address of its own: frames go
        # out from the station callsign.
        if hasattr(kam, "mycall") and not getattr(kam, "mycall", None):
            kam.mycall = self.station_callsign

        self.socket_path = socket_path
        self._reception_writer = None

        if history_db is not None:
            self._reception_writer = ReceptionWriter(
                history_db, retention_days=history_retention_days
            )
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

        # Automatic APRS acknowledgments.
        # Disabled by default; no RF authorization is implied.
        self._aprs_auto_ack_enabled = False

        # Automatic ACK authorization is never inherited across restarts.
        # These confirmations must be explicitly supplied by the operator.
        self._aprs_ack_authorized = False
        self._aprs_ack_vhf_ready = False
        self._aprs_ack_hf_disconnected = False
        self._aprs_ack_queue = queue.Queue(maxsize=100)
        self._aprs_ack_stop = threading.Event()
        self._aprs_ack_thread = None

        # Messages we sent, keyed by the {ID we appended, so incoming
        # ackID/rejID replies can be matched back to them.
        self._outbox = OutboundAprsTracker(
            self.station_callsign or "",
            on_change=self._persist_outbound,
        )

        # Restore sent messages from the history database, if any.
        if self._reception_writer is not None:
            try:
                restored = self._outbox.load(
                    self._reception_writer.load_outbound(200)
                )
                if restored:
                    logger.info("Restored %d sent APRS message(s)", restored)
            except Exception:
                logger.exception("Could not restore sent APRS messages")

        # Coverage: the current station setup (radio/antenna) label,
        # stamped on every transmission and received packet. Persisted
        # in the history database; "" = not set.
        self._setup = ""
        if self._reception_writer is not None:
            try:
                self._setup = normalize_setup(
                    self._reception_writer.get_setting(SETUP_KEY) or ""
                )
            except Exception:
                logger.exception("Could not load the station setup")
            self._reception_writer.setup = self._setup

        # Automatic resend state. Off by default; Lock order:
        # _kam_lock -> _retry_lock.
        self._retry_lock = threading.Lock()
        self._retry_enabled = False
        self._retry_disabled_reason: Optional[str] = None
        self._retry_paused_until = 0.0
        self._retries_sent = 0
        self._retry_stop = threading.Event()

        # True after anything that may have left an AX.25 link or
        # conversational session open (terminal, connect, PBBS, ...).
        # The auto-ACK worker won't Ctrl-C its way into a verification
        # while this is set -- that could knock an operator out of a
        # live session -- and pauses instead until a send/verify.
        self._aprs_link_may_be_active = False
        self._aprs_ack_disabled_reason: Optional[str] = None
        self._aprs_acks_sent = 0
        self._aprs_last_ack: Optional[Dict[str, Any]] = None

        # Position beacon state. Off by default; never inherited across
        # restarts except via --beacon / TNCSTATION_BEACON at startup.
        # Lock order: _kam_lock -> _beacon_lock.
        self._beacon_lock = threading.Lock()
        self._beacon_enabled = False
        self._beacon_disabled_reason: Optional[str] = None
        self._beacon_next_at: Optional[float] = None
        self._beacon_last_at: Optional[float] = None
        self._beacon_last_result: Optional[str] = None
        self._beacons_sent = 0
        self._beacon_stop = threading.Event()
        self._beacon_thread: Optional[threading.Thread] = None
        # "Did it get out?" -- the last time we heard our own beacon
        # repeated by a digipeater or sent back from APRS-IS by an iGate.
        self._beacon_heard: Dict[str, Any] = {}

        # Optional phone notifications (notify.Notifier); None = off.
        self._notifier = notifier

        # Optional daily history backups (db_backup.BackupManager).
        self._backup = backup
        self._backup_stop = threading.Event()
        if backup is not None:
            backup._on_failure = lambda message: self._push(
                "backup", "tncstation backup failed", message,
                tags=["warning"], priority=4,
            )
            backup._on_recovery = lambda result: self._push(
                "backup", "tncstation backup working again",
                f"Saved {result['file']}.", tags=["white_check_mark"],
            )

        # Track recently handled message IDs for duplicate detection.
        self._aprs_ack_recent = {}
        self._aprs_ack_lock = threading.Lock()

        # Milestone 7 (APRS mapping): built passively from whatever
        # MONITOR traffic the daemon happens to see -- see
        # stations.py. Reading _stations doesn't need _kam_lock (it
        # never touches the serial connection), but does need its own
        # lock, since the monitor thread writes to it from the
        # background while ordinary request-handling threads read it
        # via stations.list/stations.get.
        self._stations = StationTracker()
        self._stations_lock = threading.Lock()
        self._restore_stations()

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
        # Reception watchdog bookkeeping (wall-clock times, Port 2 only).
        # "Own" = any SSID of our base callsign (e.g. N5BA-7 handheld);
        # "other" = everyone else, including digipeaters and iGates.
        self._last_other_packet_at: Optional[float] = None
        self._last_other_source: Optional[str] = None
        self._last_own_packet_at: Optional[float] = None
        self._watchdog_alerting = False
        self._watchdog_stop = threading.Event()

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
            "stations.history": self._m_stations_history,
            "coverage.status": self._m_coverage_status,
            "packets.search": self._m_packets_search,
            "telemetry.stations": self._m_telemetry_stations,
            "telemetry.get": self._m_telemetry_get,
            "backup.status": self._m_backup_status,
            "backup.now": self._m_backup_now,
            "coverage.set_setup": self._m_coverage_set_setup,
            "coverage.report": self._m_coverage_report,
            "objects.list": self._m_objects_list,
            "notify.status": self._m_notify_status,
            "notify.test": self._m_notify_test,
            "stations.get": self._m_stations_get,
            "aprs.stats": self._m_aprs_stats,
            "aprs.history": self._m_aprs_history,
            "aprs.messages": self._m_aprs_messages,
            "aprs.ack_status": self._m_aprs_ack_status,
            "aprs.ack_enable": self._m_aprs_ack_enable,
            "aprs.ack_disable": self._m_aprs_ack_disable,
            "aprs.preview_message": self._m_aprs_preview_message,
            "aprs.send_message": self._m_aprs_send_message,
            "aprs.outbox": self._m_aprs_outbox,
            "aprs.clear_messages": self._m_aprs_clear_messages,
            "aprs.beacon_status": self._m_aprs_beacon_status,
            "aprs.beacon_enable": self._m_aprs_beacon_enable,
            "aprs.beacon_disable": self._m_aprs_beacon_disable,
            "aprs.beacon_now": self._m_aprs_beacon_now,
            "aprs.retry_status": self._m_aprs_retry_status,
            "aprs.retry_enable": self._m_aprs_retry_enable,
            "aprs.retry_disable": self._m_aprs_retry_disable,
            "aprs.clear_outbox": self._m_aprs_clear_outbox,
            "aprs.bulletins": self._m_aprs_bulletins,
            "aprs.clear_bulletins": self._m_aprs_clear_bulletins,
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

        # Start the ACK worker, but do not authorize RF transmissions.
        self._ensure_aprs_ack_worker()

        threading.Thread(
            target=self._watchdog_loop, name="kamxl-watchdog", daemon=True
        ).start()

        threading.Thread(
            target=self._retry_loop, name="kamxl-aprs-retry", daemon=True
        ).start()

        if self._backup is not None:
            threading.Thread(
                target=self._backup_loop, name="kamxl-backup", daemon=True
            ).start()

        # Same for the beacon scheduler: running, but off.
        self._beacon_thread = threading.Thread(
            target=self._beacon_loop, name="kamxl-aprs-beacon", daemon=True
        )
        self._beacon_thread.start()

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
            "tnc_model": getattr(self.kam, "MODEL", "KAM-XL"),
            "tnc_mode": getattr(self.kam, "MODE", "terminal"),
            "aprs_tocall": self.aprs_tocall,
            "aprs_tx_supported": getattr(self.kam, "APRS_TX_SUPPORTED", True),
            "features": self._features(),
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
                "watchdog": self._watchdog_snapshot(),
            },
        }

    # -- Reception watchdog ---------------------------------------------

    def _is_own_station(self, source: str) -> bool:
        if not self.station_callsign or not source:
            return False
        base = self.station_callsign.split("-")[0]
        return source.strip().upper().split("-")[0] == base

    def _watchdog_snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        """
        Is the receiver hearing the network, or only our own stations?

        States: "disabled", "starting" (not enough uptime to judge),
        "ok", "only_own" (only N5BA-* heard -- the classic symptom of
        receiver desense/noise, e.g. the radio sitting on an amplifier,
        or a digipeater outage), "silent" (nothing at all heard).
        """
        now = time.time() if now is None else now
        threshold = self._watchdog_seconds

        with self._stats_lock:
            last_other = self._last_other_packet_at
            last_other_source = self._last_other_source
            last_own = self._last_own_packet_at

        result: Dict[str, Any] = {
            "threshold_minutes": threshold / 60,
            "last_other_packet_at": last_other,
            "last_other_source": last_other_source,
            "last_own_packet_at": last_own,
            "minutes_since_other": (
                round((now - last_other) / 60, 1)
                if last_other is not None else None
            ),
        }

        if not threshold:
            return {**result, "state": "disabled", "message": None}

        # Measure from startup if nothing has been heard yet.
        reference = last_other if last_other is not None else self._started_at

        if now - reference < threshold:
            state = "ok" if last_other is not None else "starting"
            return {**result, "state": state, "message": None}

        minutes = int((now - reference) // 60)
        own_recent = last_own is not None and now - last_own < threshold

        if own_recent:
            return {
                **result,
                "state": "only_own",
                "message": (
                    f"Only your own stations heard for {minutes} min. "
                    "The KAM-XL decodes fine, so the receiver is likely "
                    "desensed by nearby noise (amplifier, power supply, "
                    "computer) or the digipeater you rely on is down."
                ),
            }

        return {
            **result,
            "state": "silent",
            "message": (
                f"Nothing heard on Port 2 for {minutes} min. Check the "
                "radio: frequency (144.390), volume, squelch, the Port 2 "
                "audio cable, and nearby noise sources."
            ),
        }

    def _backup_loop(self) -> None:
        """Check once a minute whether a history backup is due."""
        while not self._backup_stop.wait(60.0):
            try:
                self._backup.tick()
            except Exception:
                logger.exception("Backup scheduler error")

    def _m_backup_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._backup is None:
            return {"enabled": False}
        return self._backup.status()

    def _m_backup_now(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Back up the history database now (waits for it to finish)."""
        if self._backup is None:
            raise ValueError(
                "Backups are off; set TNCSTATION_BACKUP_DIR to turn them on"
            )
        result = self._backup.run()
        return {**result, "status": self._backup.status()}

    def _watchdog_loop(self) -> None:
        """Log when reception goes quiet and when it recovers."""
        while not self._watchdog_stop.wait(30.0):
            self._watchdog_tick()

    def _watchdog_tick(self) -> None:
        """One check: log on entering and on leaving an alert state."""
        snapshot = self._watchdog_snapshot()
        alert = snapshot["state"] in ("only_own", "silent")

        if alert and not self._watchdog_alerting:
            logger.warning("Reception watchdog: %s", snapshot["message"])
            self._push(
                "watchdog", f"{self.kam.MODEL} reception problem", snapshot["message"],
                tags=["warning"], priority=4,
            )
        elif not alert and self._watchdog_alerting:
            logger.info(
                "Reception watchdog: recovered (heard %s)",
                snapshot["last_other_source"],
            )
            self._push(
                "watchdog", f"{self.kam.MODEL} reception back",
                f"Hearing other stations again (latest: "
                f"{snapshot['last_other_source']}).",
                tags=["white_check_mark"],
            )

        self._watchdog_alerting = alert

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

    def _features(self) -> Dict[str, bool]:
        """
        Which optional pages this station's TNC supports; the web pages
        hide the rest. The terminal needs Terminal Mode; PBBS and Winlink
        need a TNC they've been written for (the KAM-XL).
        """
        terminal = getattr(self.kam, "MODE", "terminal") != "kiss"
        return {
            "terminal": terminal,
            "pbbs": terminal and getattr(self.kam, "HAS_PBBS", True),
            "winlink": terminal and getattr(self.kam, "HAS_WINLINK", True),
        }

    def _mark_link_may_be_active(self) -> None:
        """
        Note that a terminal, connected-mode, PBBS or Winlink operation
        is starting, so automatic transmissions won't Ctrl-C into a
        session it may leave open. Caller must hold _kam_lock.

        Not in KISS mode: there is no command mode and no connected
        mode, so no session can be open -- the KISS driver refuses those
        operations without sending anything. (Setting the flag there
        paused auto-ACK and beacons until the next manual send, just
        for opening the PBBS, Winlink or Terminal page.)
        """
        if getattr(self.kam, "MODE", "terminal") != "kiss":
            self._aprs_link_may_be_active = True

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
            self._mark_link_may_be_active()
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
            self._mark_link_may_be_active()
            return self.kam.connect_station(
                params["callsign"],
                via=params.get("via"),
                timeout=params.get("timeout", 60),
            )

    def _m_send_connected(self, params: Dict[str, Any]) -> None:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("Connected data operation attempted")
            self._mark_link_may_be_active()
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
            self._mark_link_may_be_active()
            return self.kam.read_connected(params.get("timeout", 5))

    def _m_disconnect_station(self, params: Dict[str, Any]) -> str:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("AX.25 disconnect attempted")
            self._mark_link_may_be_active()
            result = self.kam.disconnect_station(
                params.get("timeout", 30),
                command_mode_timeout=params.get("command_mode_timeout", 5)
            )
            self._aprs_link_may_be_active = False
            return result

    def _m_pbbs_list_messages(
        self, params: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("PBBS operation attempted")
            self._mark_link_may_be_active()
            messages = self.kam.list_pbbs_messages(
                mypbbs=params.get("mypbbs"),
                connect_timeout=params.get("connect_timeout", 15),
                read_timeout=params.get("read_timeout", 10),
            )
            # The operation connects and disconnects on its own.
            self._aprs_link_may_be_active = False

        return [dataclasses.asdict(message) for message in messages]

    def _m_pbbs_read_message(
        self, params: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        with self._kam_lock:
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("PBBS operation attempted")
            self._mark_link_may_be_active()
            message = self.kam.read_pbbs_message(
                int(params["number"]),
                mypbbs=params.get("mypbbs"),
                connect_timeout=params.get("connect_timeout", 15),
                read_timeout=params.get("read_timeout", 10),
            )
            # The operation connects and disconnects on its own.
            self._aprs_link_may_be_active = False

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
            self._mark_link_may_be_active()
            messages = self.kam.check_winlink_mail(
                gateway=params["gateway"],
                password=params["password"],
                mycall=params.get("mycall"),
                connect_timeout=params.get("connect_timeout", 60),
                read_timeout=params.get("read_timeout", 30),
            )
            # The operation connects and disconnects on its own.
            self._aprs_link_may_be_active = False

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
            self._mark_link_may_be_active()
            accepted_mids = self.kam.send_winlink_message(
                gateway=params["gateway"],
                password=params["password"],
                messages=messages,
                mycall=params.get("mycall"),
                connect_timeout=params.get("connect_timeout", 60),
                read_timeout=params.get("read_timeout", 30),
            )
            # The operation connects and disconnects on its own.
            self._aprs_link_may_be_active = False

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
            execute_live_aprs_tx_transaction, require_aprs_tx_support,
        )

        # Fail closed on a TNC model whose transmit path isn't verified.
        require_aprs_tx_support(self.kam)

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
        # Digipeater path: the daemon's configured one unless the
        # request names its own ("" or "DIRECT" = no digipeaters).
        from aprs_tx import validate_aprs_path

        path = (
            validate_aprs_path(params["path"])
            if params.get("path") is not None
            else self.aprs_path
        )

        # Every message carries a {ID so the recipient acknowledges it.
        # A resend reuses the original ID: a late ack for the first
        # attempt still matches, and the recipient can drop the dupe.
        resend_id = params.get("resend_id")

        if resend_id is not None:
            original = self._outbox.get(str(resend_id))

            if original is None:
                raise APRSTransmitError(
                    f"No sent APRS message with ID {resend_id!r}"
                )
            if original.status != "pending":
                raise APRSTransmitError(
                    f"APRS message {resend_id!r} was already {original.status}"
                )

            addressee, text = original.addressee, original.text
            message_id = original.message_id
        else:
            addressee, text = params["addressee"], params["message"]
            # Validate before consuming an ID.
            format_aprs_message(addressee=addressee, message=text, paclen=128)
            message_id = self._outbox.allocate_id()

        # Validate payload before touching serial or changing session state.
        payload = format_aprs_message(
            addressee=addressee,
            message=text,
            paclen=128,
            message_id=message_id,
        )

        with self._kam_lock:
            if not self.kam.is_connected or (
                getattr(self.kam.serial, "exclusive", None) is not True
            ):
                raise APRSTransmitError("Exclusive serial connection required")
            if (self._monitor_stop.is_set() or
                self._monitor_thread is None or
                not self._monitor_thread.is_alive()):
                raise APRSTransmitError("APRS monitor thread not healthy")

            # After a daemon restart, terminal use, a config change, etc.
            # the TNC's mode is unknown. Re-verify it here (Ctrl-C +
            # STATUS, no RF) rather than making the operator run a
            # separate initialize step.
            if self._aprs_session_uncertain or self._aprs_safety_state == "unsafe":
                self._verify_aprs_session_locked()

            # Mark unsafe before the first potentially state-changing write.
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety("APRS transmission in progress")
            try:
                result = execute_live_aprs_tx_transaction(
                    self.kam, payload, path,
                    _capability=_LIVE_APRS_CAPABILITY,
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
            record = self._outbox.register(
                message_id=message_id,
                addressee=addressee,
                text=text,
                payload=payload,
                path=path,
            )
            self._log_own_tx("message", message_id, path)

            if resend_id is None:
                with self._retry_lock:
                    retry_on = self._retry_enabled
                if retry_on:
                    record = self._outbox.schedule_retries(
                        message_id, self._retry_count, self._retry_interval
                    ) or record

            return {
                **result,
                "payload": payload,
                "path": path,
                "state": "unknown",
                "message_id": message_id,
                "outbound": record.to_dict(),
            }

    def _m_aprs_outbox(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Messages sent this daemon session and their ack status.

        Never touches the TNC.
        """
        with self._retry_lock:
            retry_enabled = self._retry_enabled

        return {
            "station": self.station_callsign,
            "path": self.aprs_path,
            "persistent": self._reception_writer is not None,
            "auto_retry": retry_enabled,
            "messages": self._outbox.list(),
        }

    def _persist_outbound(self, record: Dict[str, Any]) -> None:
        """Tracker on_change hook: save the record if history is on."""
        if self._reception_writer is None:
            return
        try:
            self._reception_writer.save_outbound(record)
        except Exception:
            logger.exception("Could not save sent APRS message")

    def _m_aprs_clear_outbox(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Forget all sent messages (and any scheduled resends)."""
        deleted = self._outbox.clear()
        if self._reception_writer is not None:
            deleted = max(deleted, self._reception_writer.clear_outbound())
        logger.info("Cleared %d sent APRS message(s)", deleted)
        return {"deleted": deleted}

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

    def _m_aprs_messages(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Return stored APRS messages without accessing the TNC."""

        addressee = params.get("addressee") or self.station_callsign
        limit = params.get("limit", 100)

        if not addressee:
            raise KAMError(
                "No APRS station callsign configured (TNCSTATION_CALLSIGN)"
            )

        if self._reception_writer is None:
            return {
                "enabled": False,
                "messages": [],
            }

        messages = self._reception_writer.list_aprs_messages(
            addressee=addressee,
            limit=limit,
        )

        return {
            "enabled": True,
            "addressee": addressee,
            "messages": messages,
        }

    # -- Position beacon ----------------------------------------------

    def _m_aprs_beacon_status(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Beacon configuration and state. Never touches the TNC."""
        with self._beacon_lock:
            enabled = self._beacon_enabled
            result = {
                "configured": self._beacon_payload is not None,
                "enabled": enabled,
                "disabled_reason": (
                    None if enabled else self._beacon_disabled_reason
                ),
                "position": self._beacon_position,
                "payload": self._beacon_payload,
                "path": self.aprs_path,
                "interval_minutes": self._beacon_interval / 60,
                "beacons_sent": self._beacons_sent,
                "last_beacon_at": self._beacon_last_at,
                "last_result": self._beacon_last_result,
                "next_beacon_at": (
                    self._beacon_next_at if enabled else None
                ),
            }

        result["paused"] = enabled and (
            (self._aprs_session_uncertain
             or self._aprs_safety_state == "unsafe")
            and self._aprs_link_may_be_active
        )
        with self._beacon_lock:
            result["heard_back"] = dict(self._beacon_heard)
        return result

    def _require_beacon_authorization(self, params: Dict[str, Any]) -> None:
        from aprs_tx import APRSTransmitError, require_aprs_tx_support

        require_aprs_tx_support(self.kam)

        if params.get("confirm_transmit") is not True:
            raise APRSTransmitError("Explicit confirm_transmit=true required")
        if params.get("confirm_vhf_port2_radio_ready") is not True:
            raise APRSTransmitError(
                "Confirm VHF Port 2 radio, audio, PTT, antenna and power "
                "are ready"
            )
        if params.get("confirm_hf_port1_disconnected") is not True:
            raise APRSTransmitError("Confirm HF Port 1 radio disconnected")
        if self._beacon_payload is None:
            raise APRSTransmitError(
                "No beacon position configured (set TNCSTATION_LATITUDE and "
                "TNCSTATION_LONGITUDE)"
            )

    def _m_aprs_beacon_enable(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Turn the periodic position beacon on (first one in ~5 s)."""
        self._require_beacon_authorization(params)

        with self._beacon_lock:
            self._beacon_enabled = True
            self._beacon_disabled_reason = None
            self._beacon_next_at = time.time() + 5

        logger.info(
            "APRS position beacon enabled: every %g min via %s",
            self._beacon_interval / 60, self.aprs_path or "direct",
        )
        return self._m_aprs_beacon_status({})

    def _m_aprs_beacon_disable(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        with self._beacon_lock:
            self._beacon_enabled = False
            self._beacon_disabled_reason = "Turned off by operator"
            self._beacon_next_at = None

        logger.info("APRS position beacon disabled")
        return self._m_aprs_beacon_status({})

    def _m_aprs_beacon_now(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Send one position beacon right now (operator-initiated)."""
        self._require_beacon_authorization(params)

        # Operator is at the controls: verifying the TNC is fine even
        # if a terminal session was used (same as a manual send).
        result = self._beacon_transmit(operator_initiated=True)
        return {**result, "status": self._m_aprs_beacon_status({})}

    def _guarded_transmit(
        self,
        payload: str,
        path: str,
        what: str,
        operator_initiated: bool,
    ) -> Dict[str, Any]:
        """
        Submit one payload through the guarded live TX path.

        Shared by beacons and automatic resends. When the TNC state is
        unknown it is re-verified first -- except for automatic
        transmissions while a terminal/connected session may be open,
        which raise _AutoTxPaused instead of Ctrl-C'ing into it.
        Any failure after the TNC was touched leaves APRS latched
        unsafe and is re-raised for the caller to fail closed.
        """
        from aprs_tx import (
            APRSTransmitError, _LIVE_APRS_CAPABILITY,
            execute_live_aprs_tx_transaction,
        )

        with self._kam_lock:
            if not self.kam.is_connected or (
                getattr(self.kam.serial, "exclusive", None) is not True
            ):
                raise APRSTransmitError("Exclusive serial connection required")

            if (
                self._monitor_stop.is_set()
                or self._monitor_thread is None
                or not self._monitor_thread.is_alive()
            ):
                raise APRSTransmitError("APRS monitor thread not healthy")

            if (
                self._aprs_session_uncertain
                or self._aprs_safety_state == "unsafe"
            ):
                if self._aprs_link_may_be_active and not operator_initiated:
                    raise _AutoTxPaused(
                        "a terminal, connected-mode, PBBS or Winlink "
                        "session may be open"
                    )
                try:
                    self._verify_aprs_session_locked()
                except APRSTransmitError as exc:
                    if operator_initiated:
                        raise
                    raise _AutoTxPaused(str(exc)) from exc

            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety(f"APRS {what} transmission in progress")

            try:
                result = execute_live_aprs_tx_transaction(
                    self.kam, payload, path,
                    _capability=_LIVE_APRS_CAPABILITY,
                )
            except Exception as exc:
                self._invalidate_aprs_safety(
                    f"APRS {what} failed or state uncertain"
                )
                # Tell callers this failed *after* touching the TNC,
                # so they fail closed (vs. a refusal beforehand).
                exc.tx_attempted = True
                raise

            self._aprs_session_uncertain = False
            self._aprs_safety_state = "unknown"
            self._aprs_safety_reason = (
                f"APRS {what} submitted; RF delivery not confirmed"
            )

        return result

    def _beacon_transmit(self, operator_initiated: bool) -> Dict[str, Any]:
        """
        Submit the beacon through the guarded TX path.

        Raises _AutoTxPaused when the TNC can't be verified without
        risking an open session (automatic beacons only).
        """
        from aprs_tx import APRSTransmitError

        payload = self._beacon_payload
        if payload is None:
            raise APRSTransmitError("No beacon position configured")

        try:
            result = self._guarded_transmit(
                payload, self.aprs_path, "beacon", operator_initiated
            )
        except Exception as exc:
            if getattr(exc, "tx_attempted", False):
                self._beacon_failed()
            raise

        self._log_own_tx("beacon", "", self.aprs_path)
        now = time.time()
        with self._beacon_lock:
            self._beacons_sent += 1
            self._beacon_last_at = now
            self._beacon_last_result = "sent"
            if self._beacon_enabled:
                self._beacon_next_at = now + self._beacon_interval

        logger.info(
            "APRS position beacon submitted via %s: %s",
            self.aprs_path or "direct", payload,
        )
        return {**result, "payload": payload, "path": self.aprs_path}

    def _beacon_failed(self) -> None:
        with self._beacon_lock:
            self._beacon_enabled = False
            self._beacon_next_at = None
            self._beacon_disabled_reason = (
                "Turned off after a beacon transmission failed; "
                "TNC state needs checking"
            )
            self._beacon_last_result = "failed"

    def _beacon_loop(self) -> None:
        """Send the beacon on schedule while enabled."""
        while not self._beacon_stop.wait(1.0):
            with self._beacon_lock:
                due = (
                    self._beacon_enabled
                    and self._beacon_next_at is not None
                    and time.time() >= self._beacon_next_at
                )

            if not due:
                continue

            try:
                self._beacon_transmit(operator_initiated=False)
            except _AutoTxPaused as exc:
                logger.warning("APRS beacon skipped (paused): %s", exc)
                with self._beacon_lock:
                    self._beacon_last_result = "paused"
                    if self._beacon_enabled:
                        self._beacon_next_at = time.time() + 60
            except Exception:
                logger.exception("APRS beacon failed; beacon turned off")

    # -- Automatic resend of unacknowledged messages -------------------

    def _m_aprs_retry_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._retry_lock:
            enabled = self._retry_enabled
            result = {
                "enabled": enabled,
                "disabled_reason": None if enabled else self._retry_disabled_reason,
                "retry_count": self._retry_count,
                "interval_seconds": self._retry_interval,
                "retries_sent": self._retries_sent,
            }
        result["paused"] = enabled and time.time() < self._retry_paused_until
        return result

    def _m_aprs_retry_enable(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Turn on automatic resends (operator authorization required).

        Applies to messages sent from now on.
        """
        self._require_transmit_confirmations(params)
        with self._retry_lock:
            self._retry_enabled = True
            self._retry_disabled_reason = None
        logger.info(
            "Automatic APRS resend enabled: up to %d resends, first after %gs",
            self._retry_count, self._retry_interval,
        )
        return self._m_aprs_retry_status({})

    def _m_aprs_retry_disable(self, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._retry_lock:
            self._retry_enabled = False
            self._retry_disabled_reason = "Turned off by operator"
        logger.info("Automatic APRS resend disabled")
        return self._m_aprs_retry_status({})

    def _require_transmit_confirmations(self, params: Dict[str, Any]) -> None:
        from aprs_tx import APRSTransmitError, require_aprs_tx_support

        require_aprs_tx_support(self.kam)

        if params.get("confirm_transmit") is not True:
            raise APRSTransmitError("Explicit confirm_transmit=true required")
        if params.get("confirm_vhf_port2_radio_ready") is not True:
            raise APRSTransmitError(
                "Confirm VHF Port 2 radio, audio, PTT, antenna and power "
                "are ready"
            )
        if params.get("confirm_hf_port1_disconnected") is not True:
            raise APRSTransmitError("Confirm HF Port 1 radio disconnected")

    def _retry_loop(self) -> None:
        while not self._retry_stop.wait(1.0):
            try:
                self._retry_tick()
            except Exception:
                logger.exception("APRS resend worker failed")

    def _retry_tick(self, now: Optional[float] = None) -> None:
        """Send any due automatic resends (one pass)."""
        now = time.time() if now is None else now

        with self._retry_lock:
            if not self._retry_enabled or now < self._retry_paused_until:
                return

        for record in self._outbox.expire_retries(now):
            logger.info(
                "APRS message %s to %s: no ack after %d attempts; giving up",
                record.message_id, record.addressee, record.attempts,
            )

        for record in self._outbox.due_retries(now):
            with self._retry_lock:
                if not self._retry_enabled:
                    return

            try:
                self._guarded_transmit(
                    record.payload, record.path, "resend",
                    operator_initiated=False,
                )
            except _AutoTxPaused as exc:
                logger.warning("APRS resend postponed (paused): %s", exc)
                with self._retry_lock:
                    self._retry_paused_until = time.time() + 60
                return
            except Exception as exc:
                if getattr(exc, "tx_attempted", False):
                    with self._retry_lock:
                        self._retry_enabled = False
                        self._retry_disabled_reason = (
                            "Turned off after a resend failed; TNC state "
                            "needs checking"
                        )
                    logger.error(
                        "APRS resend of %s failed; automatic resend turned "
                        "off", record.message_id,
                    )
                else:
                    logger.warning(
                        "APRS resend of %s not sent: %s",
                        record.message_id, exc,
                    )
                    with self._retry_lock:
                        self._retry_paused_until = time.time() + 60
                return

            # Re-check: an ack may have arrived while we transmitted.
            current = self._outbox.get(record.message_id)
            self._outbox.register(
                record.message_id, record.addressee, record.text,
                record.payload, record.path,
            )
            self._log_own_tx("message", record.message_id, record.path)
            if current is not None and current.status == "pending":
                self._outbox.retry_sent(record.message_id, self._retry_interval)

            with self._retry_lock:
                self._retries_sent += 1

            logger.info(
                "APRS message %s to %s resent automatically (attempt %d)",
                record.message_id, record.addressee, record.attempts + 1,
            )

    def _m_aprs_bulletins(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Bulletins/announcements heard in the last ``days`` (default 7)."""
        days = params.get("days", 7)
        if isinstance(days, bool) or not isinstance(days, (int, float)) \
                or not 0 < days <= 365:
            raise ValueError("'days' must be between 1 and 365")

        if self._reception_writer is None:
            return {"enabled": False, "bulletins": []}

        since = int(time.time() - days * 86400)
        return {
            "enabled": True,
            "days": days,
            "bulletins": self._reception_writer.list_bulletins(since=since),
        }

    def _m_aprs_clear_bulletins(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._reception_writer is None:
            return {"enabled": False, "deleted": 0}
        deleted = self._reception_writer.clear_bulletins()
        logger.info("Cleared %d stored bulletin copy(ies)", deleted)
        return {"enabled": True, "deleted": deleted}

    def _m_aprs_clear_messages(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Delete stored received APRS messages. Never touches the TNC."""
        addressee = params.get("addressee") or self.station_callsign

        if self._reception_writer is None:
            return {"enabled": False, "deleted": 0}

        if not addressee:
            raise KAMError(
                "No APRS station callsign configured (TNCSTATION_CALLSIGN)"
            )

        deleted = self._reception_writer.clear_aprs_messages(addressee)
        logger.info("Cleared %d stored APRS message(s) for %s",
                    deleted, addressee)

        return {"enabled": True, "addressee": addressee, "deleted": deleted}

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

    def _m_aprs_ack_enable(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Explicitly authorize automatic APRS acknowledgments."""
        from aprs_tx import APRSTransmitError, require_aprs_tx_support

        require_aprs_tx_support(self.kam)

        if params.get("confirm_transmit") is not True:
            raise APRSTransmitError(
                "Explicit confirm_transmit=true required"
            )

        if params.get("confirm_vhf_port2_radio_ready") is not True:
            raise APRSTransmitError(
                "Confirm VHF Port 2 radio, audio, PTT, "
                "antenna and power are ready"
            )

        if params.get("confirm_hf_port1_disconnected") is not True:
            raise APRSTransmitError(
                "Confirm HF Port 1 radio disconnected"
            )

        with self._kam_lock:
            if not self.kam.is_connected or (
                getattr(self.kam.serial, "exclusive", None) is not True
            ):
                raise APRSTransmitError(
                    "Exclusive serial connection required"
                )

            if (
                self._monitor_stop.is_set()
                or self._monitor_thread is None
                or not self._monitor_thread.is_alive()
            ):
                raise APRSTransmitError(
                    "APRS monitor thread not healthy"
                )

            if (
                self._aprs_session_uncertain
                or self._aprs_safety_state == "unsafe"
            ):
                self._verify_aprs_session_locked()

            with self._aprs_ack_lock:
                if (
                    self._aprs_ack_stop.is_set()
                    or self._aprs_ack_thread is None
                    or not self._aprs_ack_thread.is_alive()
                ):
                    raise APRSTransmitError(
                        "APRS ACK worker not running"
                    )

                self._aprs_ack_authorized = True
                self._aprs_ack_vhf_ready = True
                self._aprs_ack_hf_disconnected = True
                self._aprs_auto_ack_enabled = True
                self._aprs_ack_disabled_reason = None

        logger.info("Automatic APRS ACKs explicitly enabled")

        return {
            "enabled": True,
            "pending": self._aprs_ack_queue.qsize(),
        }

    def _m_aprs_ack_disable(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Revoke automatic APRS ACK authorization."""
        # Serialize revocation with the guarded TX path.
        # Lock order must remain _kam_lock -> _aprs_ack_lock.
        with self._kam_lock:
            with self._aprs_ack_lock:
                self._aprs_auto_ack_enabled = False
                self._aprs_ack_authorized = False
                self._aprs_ack_vhf_ready = False
                self._aprs_ack_hf_disconnected = False
                self._aprs_ack_disabled_reason = "Turned off by operator"

        # Discard requests that have not yet been processed.
        discarded = 0

        while True:
            try:
                self._aprs_ack_queue.get_nowait()
            except queue.Empty:
                break

            self._aprs_ack_queue.task_done()
            discarded += 1

        logger.info(
            "Automatic APRS ACKs disabled; %d pending requests discarded",
            discarded,
        )

        return {
            "enabled": False,
            "pending": self._aprs_ack_queue.qsize(),
            "discarded": discarded,
        }

    def _m_aprs_ack_status(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Report automatic APRS ACK status without hardware access."""
        with self._aprs_ack_lock:
            enabled = self._aprs_auto_ack_enabled
            disabled_reason = self._aprs_ack_disabled_reason
            acks_sent = self._aprs_acks_sent
            last_ack = self._aprs_last_ack

        # Plain attribute reads; no _kam_lock needed for a status hint.
        paused = enabled and (
            (self._aprs_session_uncertain
             or self._aprs_safety_state == "unsafe")
            and self._aprs_link_may_be_active
        )

        return {
            "enabled": enabled,
            "paused": paused,
            "paused_reason": (
                "A terminal, connected-mode, PBBS or Winlink session may "
                "still be open; ACKs resume after the next APRS send "
                "(or turning auto-ACK off and on)"
                if paused else None
            ),
            "disabled_reason": None if enabled else disabled_reason,
            "acks_sent": acks_sent,
            "last_ack": last_ack,
            "station": self.station_callsign,
            "pending": self._aprs_ack_queue.qsize(),
            "worker_running": (
                self._aprs_ack_thread is not None
                and self._aprs_ack_thread.is_alive()
            ),
        }

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

    def _restore_stations(self) -> None:
        """
        Rebuild the map after a restart by replaying the last hour of
        stored packets (the same window stations stay on the map) into
        the station tracker. Only the map is rebuilt -- replayed
        packets never trigger ACKs, notifications or other actions.
        """
        if self._reception_writer is None:
            return
        try:
            packets = self._reception_writer.packets_since(
                int(time.time() - STATION_MAX_AGE)
            )
            with self._stations_lock:
                for packet, received_at in packets:
                    self._stations.update(packet, now=received_at)
                count = len(self._stations.list_stations())
            if count:
                logger.info(
                    "Restored %d station(s) on the map from %d stored "
                    "packet(s)", count, len(packets),
                )
        except Exception:
            logger.exception("Could not restore stations from history")

    def _m_objects_list(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """APRS objects/items (repeaters, nets, events) heard recently."""
        with self._stations_lock:
            self._stations.expire_stale(max_age_seconds=STATION_MAX_AGE)
            objects = self._stations.list_objects()

        return [dataclasses.asdict(obj) for obj in objects]

    def _m_stations_list(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        with self._stations_lock:
            self._stations.expire_stale(max_age_seconds=STATION_MAX_AGE)
            stations = self._stations.list_stations()

        return [dataclasses.asdict(station) for station in stations]

    def _m_stations_history(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Stations heard in the last ``hours`` (2-720, default 24) with
        their tracks, from the history database -- for showing more
        than the live map's last hour.
        """
        if self._reception_writer is None:
            raise ValueError(
                "Longer tracks need the history database (TNCSTATION_HISTORY_DB)"
            )
        hours = params.get("hours", 24)
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) \
                or not 1 <= hours <= 720:
            raise ValueError("hours must be a number from 1 to 720")
        stations = self._reception_writer.station_tracks(
            int(time.time() - hours * 3600)
        )
        return {"hours": hours, "stations": stations}

    def _m_stations_get(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._stations_lock:
            self._stations.expire_stale(max_age_seconds=STATION_MAX_AGE)
            station = self._stations.get_station(params["callsign"])

        return dataclasses.asdict(station) if station is not None else None

    def _m_aprs_tx_initialize(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Verify the TNC is back in command mode with stream A/2 idle.

        Sends only Ctrl-C and STATUS -- nothing that can key a radio --
        so radios may stay connected. Never authorizes RF transmission.
        (``aprs.send_message`` runs this same check itself when needed,
        so calling it separately is optional.)
        """
        from aprs_tx import APRSTransmitError

        with self._kam_lock:
            if (
                self._monitor_stop.is_set()
                or self._monitor_thread is None
                or not self._monitor_thread.is_alive()
            ):
                self._aprs_session_uncertain = True
                self._invalidate_aprs_safety(
                    "APRS initialization blocked: monitor thread not healthy"
                )
                raise APRSTransmitError(
                    "APRS initialization requires a running monitor thread"
                )

            result = self._verify_aprs_session_locked()

            return {
                **result,
                "initialized": True,
                "state": self._aprs_safety_state,
                "reason": self._aprs_safety_reason,
                "rf_enabled": False,
                "transmitted": False,
            }

    def _verified_reason(self) -> str:
        """Safety-state reason after a successful session check."""
        if getattr(self.kam, "MODE", "terminal") == "kiss":
            return ("KISS mode: nothing to verify on the TNC; "
                    "RF prerequisites not fully established")
        return ("Command mode and disconnected A/2 verified; "
                "RF prerequisites not fully established")

    def _verify_aprs_session_locked(self) -> Dict[str, Any]:
        """
        Ctrl-C back to command mode and confirm A/2 is disconnected.

        Caller must hold _kam_lock. Writes only Ctrl-C and STATUS.
        On success the session is marked verified (state "unknown",
        i.e. verified but not RF-authorized); on failure it stays
        latched unsafe and the error says why.
        """
        from aprs_tx import APRSTransmitError, recover_aprs_command_mode

        # Assume uncertainty until verification completes.
        self._aprs_session_uncertain = True
        self._invalidate_aprs_safety("APRS session verification in progress")

        try:
            result = recover_aprs_command_mode(self.kam)
        except Exception as exc:
            self._invalidate_aprs_safety(
                "APRS session verification failed; TNC state uncertain"
            )
            # Most likely an AX.25 session is open; don't let the
            # auto-ACK worker keep Ctrl-C'ing into it.
            self._aprs_link_may_be_active = True
            logger.exception("APRS session verification failed")
            raise APRSTransmitError(
                f"Could not verify the {getattr(self.kam, 'MODEL', 'TNC')} is idle in command mode "
                "(is a connected AX.25 session still open on stream A?). "
                "Nothing was transmitted."
            ) from exc

        self._aprs_session_uncertain = False
        self._aprs_link_may_be_active = False
        self._aprs_safety_state = "unknown"
        self._aprs_safety_reason = self._verified_reason()

        if getattr(self.kam, "MODE", "terminal") == "kiss":
            logger.info("APRS session ready (KISS mode: no command mode or "
                        "streams on the TNC to verify)")
        else:
            logger.info("APRS session verified (command mode, A/2 idle)")

        return result

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
            self._aprs_safety_reason = self._verified_reason()

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
                logger.exception("%s KISS exit failed", self.kam.MODEL)
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
        # The driver knows its own MONITOR header format (KAM-XL
        # "SRC>DST/2: <UI>:" vs TNC-2 "SRC>DST:payload").
        parser = self.kam.make_packet_parser()

        # See TerminalTNC.MONITOR_IDLE_FLUSH (None for the KAM-XL).
        idle_flush = getattr(self.kam, "MONITOR_IDLE_FLUSH", None)
        last_data = time.monotonic()

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
                    "TNC monitor thread failed; "
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

                last_data = time.monotonic()

                for packet in parser.feed(text):
                    self._handle_packet(packet)
            elif (
                idle_flush is not None
                and time.monotonic() - last_data >= idle_flush
            ):
                for packet in parser.flush_pending():
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

    def _ensure_aprs_ack_worker(self) -> None:
        """Start the APRS ACK worker without enabling transmissions."""
        if (
            self._aprs_ack_thread is not None
            and self._aprs_ack_thread.is_alive()
        ):
            return

        if self._aprs_ack_stop.is_set():
            return

        self._aprs_ack_thread = threading.Thread(
            target=self._aprs_ack_worker_loop,
            name="kamxl-aprs-ack",
            daemon=True,
        )
        self._aprs_ack_thread.start()

    def _aprs_ack_is_authorized(self) -> bool:
        """Check automatic ACK authorization without accessing hardware."""
        with self._aprs_ack_lock:
            return (
                self._aprs_auto_ack_enabled
                and self._aprs_ack_authorized
                and self._aprs_ack_vhf_ready
                and self._aprs_ack_hf_disconnected
                and not self._aprs_ack_stop.is_set()
            )

    def _aprs_ack_transmit(self, source: str, message_id: str) -> dict:
        """Submit one authorized APRS ACK through the guarded TX path."""
        from aprs import format_aprs_ack
        from aprs_tx import (
            APRSTransmitError,
            _LIVE_APRS_CAPABILITY,
            execute_live_aprs_tx_transaction,
        )

        # Validate the payload before acquiring serial access.
        payload = format_aprs_ack(source, message_id)

        with self._kam_lock:
            if not self._aprs_ack_is_authorized():
                raise APRSTransmitError(
                    "Automatic APRS ACK transmission not authorized"
                )

            if not self.kam.is_connected or (
                getattr(self.kam.serial, "exclusive", None) is not True
            ):
                raise APRSTransmitError(
                    "Exclusive serial connection required"
                )

            if (
                self._monitor_stop.is_set()
                or self._monitor_thread is None
                or not self._monitor_thread.is_alive()
            ):
                raise APRSTransmitError(
                    "APRS monitor thread not healthy"
                )

            if (
                self._aprs_session_uncertain
                or self._aprs_safety_state == "unsafe"
            ):
                if self._aprs_link_may_be_active:
                    # Don't Ctrl-C into what may be a live session.
                    raise APRSTransmitError(
                        "APRS session uncertain (a connected session may "
                        "be open); ACK transmission paused"
                    )

                # Same Ctrl-C + STATUS check a manual send runs.
                self._verify_aprs_session_locked()

            # Mark the session uncertain before touching the TNC.
            self._aprs_session_uncertain = True
            self._invalidate_aprs_safety(
                "Automatic APRS ACK transmission in progress"
            )

            try:
                result = execute_live_aprs_tx_transaction(
                    self.kam,
                    payload,
                    self.aprs_path,
                    _capability=_LIVE_APRS_CAPABILITY,
                )

            except Exception:
                self._invalidate_aprs_safety(
                    "Automatic APRS ACK failed; "
                    "manual verification required"
                )

                # Fail closed: revoke automatic transmission
                # authorization after any uncertain TX outcome.
                with self._aprs_ack_lock:
                    self._aprs_auto_ack_enabled = False
                    self._aprs_ack_authorized = False
                    self._aprs_ack_vhf_ready = False
                    self._aprs_ack_hf_disconnected = False
                    self._aprs_ack_disabled_reason = (
                        "Turned off after an ACK transmission failed; "
                        "TNC state needs checking"
                    )

                # Discard pending ACK requests after authorization
                # has been revoked. Do not acquire _kam_lock again:
                # this method already holds it.
                discarded = 0
                while True:
                    try:
                        self._aprs_ack_queue.get_nowait()
                    except queue.Empty:
                        break

                    self._aprs_ack_queue.task_done()
                    discarded += 1

                logger.error(
                    "Automatic APRS ACKs disabled following "
                    "transmission failure; discarded %d pending ACKs",
                    discarded,
                )
                raise

            # Command mode and UNPROTO restoration were verified.
            self._aprs_session_uncertain = False
            self._aprs_safety_state = "unknown"
            self._aprs_safety_reason = (
                "APRS ACK submitted; RF delivery not confirmed"
            )

            with self._aprs_ack_lock:
                self._aprs_acks_sent += 1
                self._aprs_last_ack = {
                    "to": source,
                    "message_id": message_id,
                    "at": time.time(),
                }

            return {**result, "payload": payload}

    def _aprs_ack_worker_loop(self) -> None:
        """Process queued ACK requests without blocking APRS reception."""
        from aprs_tx import APRSTransmitError

        while not self._aprs_ack_stop.is_set():
            try:
                candidate = self._aprs_ack_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                # Recheck authorization when processing, not just
                # when the packet was originally received.
                with self._aprs_ack_lock:
                    enabled = self._aprs_auto_ack_enabled

                if not enabled or self._aprs_ack_stop.is_set():
                    continue

                source, message_id = candidate

                # Submit one ACK through the guarded transmission path.
                # Authorization and TNC safety are rechecked there.
                result = self._aprs_ack_transmit(source, message_id)

                logger.info(
                    "APRS ACK submitted: source=%s message_id=%s "
                    "submitted_to_tnc=%s",
                    source,
                    message_id,
                    result.get("submitted_to_tnc", False),
                )

            except APRSTransmitError as exc:
                logger.warning("APRS ACK not sent: %s", exc)
            except Exception:
                logger.exception("APRS ACK worker processing failed")
            finally:
                self._aprs_ack_queue.task_done()

    def _aprs_ack_candidate(self, packet: Packet):
        """Identify incoming APRS messages eligible for an ACK.

        Returns (originator, message_id). For a message gated from
        APRS-IS by an iGate (third-party ``}`` packet) the originator is
        the station inside the header, not the iGate: that's who the
        ACK must be addressed to, and the iGate gates our RF ACK back
        to APRS-IS.
        """
        if self.station_callsign is None:
            return None

        if packet.port != 2:
            return None

        if packet.frame_type not in ("UI", None):
            return None

        source, payload = unwrap_third_party(packet.source, packet.payload)
        message = parse_aprs_message(payload)

        if message is None:
            return None

        if message.kind != "message" or message.message_id is None:
            return None

        # Only acknowledge messages addressed to this station.
        if message.addressee != self.station_callsign:
            return None

        # Never acknowledge our own transmissions (including our own
        # packets gated back to RF by an iGate).
        if source.upper() == self.station_callsign:
            return None

        return source.upper(), message.message_id

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

            if packet.port == 2:
                wall = time.time()
                if self._is_own_station(packet.source):
                    self._last_own_packet_at = wall
                else:
                    self._last_other_packet_at = wall
                    self._last_other_source = packet.source

            # Keep the current minute and the previous 59 minutes.
            cutoff_minute = minute - 59
            for old_minute in list(self._activity_minutes):
                if old_minute < cutoff_minute:
                    del self._activity_minutes[old_minute]
        if self._reception_writer is not None:
            self._reception_writer.submit(packet)
        # Queue APRS acknowledgments without blocking reception.
        # No ACKs are queued unless explicitly enabled.
        if self._aprs_ack_is_authorized():
            candidate = self._aprs_ack_candidate(packet)

            if candidate is not None:
                now = time.monotonic()

                with self._aprs_ack_lock:
                    # Suppress rapid duplicate requests, but allow
                    # later retries if the sender missed our ACK.
                    last_queued = self._aprs_ack_recent.get(candidate)

                    if (
                        last_queued is not None
                        and now - last_queued < 10.0
                    ):
                        candidate = None
                    else:
                        self._aprs_ack_recent = {
                            key: timestamp
                            for key, timestamp
                            in self._aprs_ack_recent.items()
                            if now - timestamp < 60.0
                        }

                        self._aprs_ack_recent[candidate] = now

                if candidate is not None:
                    try:
                        self._aprs_ack_queue.put_nowait(candidate)
                    except queue.Full:
                        logger.warning(
                            "APRS ACK queue full; acknowledgment dropped"
                        )
        self._observe_outbound_reply(packet)
        self._observe_own_packet(packet)
        self._notify_incoming(packet)

        with self._stations_lock:
            self._stations.update(packet)

        self._broadcast_packet(packet)

    # -- "Did it get out?" and phone notifications -----------------------

    @staticmethod
    def _transmitting_digipeater(packet: Packet) -> Optional[str]:
        """
        The digipeater that sent this copy: the last callsign among the
        used hops (see aprs_coverage.last_hop). Falls back to the last
        starred alias (e.g. "WIDE1") when no callsign was inserted.
        """
        hop = last_hop(packet.digipeaters)
        if hop:
            return hop
        for entry in reversed(packet.digipeaters or ()):
            if entry.endswith("*"):
                return entry.rstrip("*").upper()
        return None

    def _observe_own_packet(self, packet: Packet) -> None:
        """
        Notice copies of our own transmissions coming back: repeated by
        a digipeater (source is us, path has a used hop), or sent back
        to RF from APRS-IS by an iGate (a third-party packet whose
        originator is us). Either proves the transmission got out.
        """
        if self.station_callsign is None or packet.port != 2:
            return
        if packet.frame_type not in ("UI", None):
            return

        try:
            digipeater = igate = None
            payload = packet.payload

            if packet.source.upper() == self.station_callsign:
                digipeater = self._transmitting_digipeater(packet)
                if digipeater is None:
                    return
            elif payload.startswith("}"):
                inner_source, inner_payload = unwrap_third_party(
                    packet.source, payload
                )
                if inner_source != self.station_callsign:
                    return
                igate, payload = packet.source.upper(), inner_payload
            else:
                return

            now = time.time()
            # Coverage credits the digipeater that heard us directly
            # (the first hop), not the last one that repeated it.
            via, station = (
                ("digi", first_hop(packet.digipeaters) or "")
                if digipeater else ("igate", igate)
            )
            if self._beacon_payload is not None and payload == self._beacon_payload:
                self._record_own_heard(now, "beacon", "", via, station)
                with self._beacon_lock:
                    if digipeater:
                        self._beacon_heard["digipeated_by"] = digipeater
                        self._beacon_heard["digipeated_at"] = now
                    if igate:
                        self._beacon_heard["igated_by"] = igate
                        self._beacon_heard["igated_at"] = now
                return

            message = parse_aprs_message(payload)
            if message is not None and message.message_id is not None \
                    and message.kind == "message":
                self._record_own_heard(
                    now, "message", message.message_id, via, station
                )
                self._outbox.heard_back(
                    message.message_id, message.addressee,
                    digipeater=digipeater, igate=igate,
                )
        except Exception:
            logger.exception("Own-packet tracking failed")

    # -- Telemetry (see aprs_telemetry.py) -------------------------------

    def _telemetry_since(self, params: Dict[str, Any]) -> int:
        if self._reception_writer is None:
            raise ValueError(
                "Telemetry needs the history database (TNCSTATION_HISTORY_DB)"
            )
        days = params.get("days", 7)
        if isinstance(days, bool) or not isinstance(days, (int, float)) \
                or not 0 < days <= 3650:
            raise ValueError("days must be a number from 1 to 3650")
        return int(time.time() - days * 86400)

    def _m_telemetry_stations(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Stations that sent telemetry in the last ``days`` (default 7)."""
        since = self._telemetry_since(params)
        return {
            "days": params.get("days", 7),
            "stations": self._reception_writer.telemetry_stations(since),
        }

    def _m_telemetry_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """One station's telemetry, scaled and named, for charting."""
        since = self._telemetry_since(params)
        callsign = params.get("callsign")
        if not isinstance(callsign, str) or not re.match(
                r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2})?$", callsign.strip().upper()):
            raise ValueError("callsign must be like W5NGU-3")
        result = self._reception_writer.telemetry_series(
            callsign.strip().upper(), since
        )
        result["days"] = params.get("days", 7)
        return result

    # -- Packet log search -----------------------------------------------

    _SEARCH_CALL_RE = re.compile(r"^[A-Z0-9]{1,6}(-[A-Z0-9]{1,2})?\*?$|^[A-Z0-9]{1,6}\*$")

    def _m_packets_search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Search stored packets. Filters (all optional, combined with
        AND): ``callsign`` (sender; "W5NGU" any SSID, "W5N*" prefix),
        ``via`` (a digipeater in the path, same forms), ``text``
        (payload substring), ``days`` (look back; default 7, 0 = all),
        ``setup`` (station setup label). Page with ``before_id``.
        A callsign search also returns a ``summary`` of that station.
        """
        if self._reception_writer is None:
            raise ValueError(
                "The packet log needs the history database (TNCSTATION_HISTORY_DB)"
            )

        def call_param(name):
            value = params.get(name)
            if value in (None, ""):
                return None
            if not isinstance(value, str):
                raise ValueError(f"{name} must be text")
            value = value.strip().upper()
            if not self._SEARCH_CALL_RE.match(value):
                raise ValueError(
                    f"{name} must be a callsign like W5NGU-3, W5NGU or W5N*"
                )
            return value

        callsign, via = call_param("callsign"), call_param("via")
        text = params.get("text") or None
        if text is not None and (not isinstance(text, str) or len(text) > 100):
            raise ValueError("text must be at most 100 characters")
        days = params.get("days", 7)
        if isinstance(days, bool) or not isinstance(days, (int, float)) \
                or not 0 <= days <= 3650:
            raise ValueError("days must be a number from 0 (all) to 3650")
        setup = params.get("setup")
        if setup is not None and not isinstance(setup, str):
            raise ValueError("setup must be text")
        before_id = params.get("before_id")
        if before_id is not None and (
                isinstance(before_id, bool) or not isinstance(before_id, int)):
            raise ValueError("before_id must be an integer")
        limit = params.get("limit", 200)
        if isinstance(limit, bool) or not isinstance(limit, int) \
                or not 1 <= limit <= 1000:
            raise ValueError("limit must be from 1 to 1000")

        since = int(time.time() - days * 86400) if days else None
        packets, more = self._reception_writer.search_packets(
            callsign=callsign, text=text, via=via, since=since, setup=setup,
            before_id=before_id, limit=limit,
        )
        result = {"packets": packets, "more": more, "days": days}
        if callsign and before_id is None:
            result["summary"] = self._reception_writer.station_summary(
                callsign, since
            )
        return result

    # -- Coverage (see aprs_coverage.py) ---------------------------------

    def _log_own_tx(self, kind: str, ref: str, path: str) -> None:
        if self._reception_writer is None:
            return
        try:
            self._reception_writer.log_own_tx(
                time.time(), kind, ref, path, self._setup
            )
        except Exception:
            logger.exception("Could not log transmission for coverage")

    def _record_own_heard(self, heard_at, kind, ref, via, station) -> None:
        if self._reception_writer is None:
            return
        try:
            self._reception_writer.record_own_heard(
                heard_at, kind, ref, via, station
            )
        except Exception:
            logger.exception("Could not record heard-back for coverage")

    def _coverage_home(self):
        if self._beacon_position is None:
            return None
        return (
            self._beacon_position["latitude"],
            self._beacon_position["longitude"],
        )

    def _m_coverage_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "enabled": self._reception_writer is not None,
            "setup": self._setup,
        }

    def _m_coverage_set_setup(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Set the station setup label (radio/antenna) used from now on."""
        if self._reception_writer is None:
            raise ValueError(
                "Coverage needs the history database (TNCSTATION_HISTORY_DB)"
            )
        setup = normalize_setup(params.get("setup", ""))
        self._reception_writer.set_setting(SETUP_KEY, setup)
        self._setup = setup
        self._reception_writer.setup = setup
        logger.info("Station setup set to %r", setup)
        return self._m_coverage_status({})

    def _m_coverage_report(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Per-setup coverage for the last ``days`` (default 30)."""
        days = params.get("days", 30)
        if isinstance(days, bool) or not isinstance(days, (int, float)) \
                or not 0 < days <= 365:
            raise ValueError("days must be a number from 1 to 365")
        status = self._m_coverage_status({})
        if self._reception_writer is None:
            return {**status, "days": days, "home": None, "setups": []}
        home = self._coverage_home()
        setups = self._reception_writer.coverage_report(
            time.time() - days * 86400, home, self._is_own_station
        )
        return {
            **status,
            "days": days,
            "home": (
                {"latitude": home[0], "longitude": home[1]} if home else None
            ),
            "setups": setups,
        }

    def _push(self, event: str, title: str, message: str, **kwargs) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.notify(event, title, message, **kwargs)
        except Exception:
            logger.exception("Could not queue notification")

    def _notify_incoming(self, packet: Packet) -> None:
        """Phone notification for messages to us and new bulletins."""
        if self._notifier is None or self.station_callsign is None:
            return
        if packet.port != 2 or packet.frame_type not in ("UI", None):
            return
        try:
            source, payload = unwrap_third_party(packet.source, packet.payload)
            message = parse_aprs_message(payload)
            if message is None or source.upper() == self.station_callsign:
                return

            if message.kind == "message" and \
                    message.addressee == self.station_callsign:
                self._push(
                    "messages", f"APRS from {source}", message.text,
                    tags=["incoming_envelope"], priority=4,
                    dedupe_key=(source, message.message_id or message.text),
                )
            elif message.kind == "bulletin":
                self._push(
                    "bulletins", f"APRS bulletin from {source} ({message.addressee})",
                    message.text, tags=["loudspeaker"],
                    dedupe_key=(source, message.addressee, message.text),
                )
        except Exception:
            logger.exception("Notification hook failed")

    def _m_notify_status(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if self._notifier is None:
            return {"enabled": False}
        return self._notifier.status()

    def _m_notify_test(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Queue a test notification and wait briefly for the result."""
        if self._notifier is None:
            raise KAMError(
                "Phone notifications aren't set up (TNCSTATION_NTFY_URL and "
                "TNCSTATION_NTFY_TOPIC in .env)"
            )
        before = self._notifier.status()
        self._notifier.notify(
            "test", "tncstation test notification",
            f"Notifications from {self.station_callsign or 'the tncstation daemon'} "
            "are working.", tags=["radio"],
        )
        deadline = time.time() + 12
        while time.time() < deadline:
            now_status = self._notifier.status()
            if (now_status["sent"], now_status["failed"]) != \
                    (before["sent"], before["failed"]):
                return {
                    "delivered": now_status["sent"] > before["sent"],
                    "error": None if now_status["sent"] > before["sent"]
                    else now_status["last_error"],
                    "status": now_status,
                }
            time.sleep(0.1)
        return {"delivered": False, "error": "No response from the server yet",
                "status": self._notifier.status()}

    def _observe_outbound_reply(self, packet: Packet) -> None:
        """Match an incoming ack/rej against messages we sent."""
        if packet.port != 2 or packet.frame_type not in ("UI", None):
            return

        try:
            source, payload = unwrap_third_party(
                packet.source, packet.payload
            )
            message = parse_aprs_message(payload)

            if message is None:
                return

            updated = self._outbox.observe(source, message)
        except Exception:
            # Never let ack bookkeeping take down the monitor thread.
            logger.exception("APRS outbound ack matching failed")
            return

        if updated is not None:
            logger.info(
                "APRS message %s to %s %s",
                updated.message_id,
                updated.addressee,
                updated.status,
            )
            self._broadcast_event({
                "event": "aprs_outbound",
                "data": updated.to_dict(),
            })

    def _broadcast_packet(self, packet: Packet) -> None:
        event = {
            "event": "packet",
            "data": dataclasses.asdict(packet),
        }

        self._broadcast_event(
            event,
            log_detail=(packet.source, packet.destination, packet.port),
        )

    def _broadcast_event(
        self,
        event: Dict[str, Any],
        log_detail: Optional[tuple] = None,
    ) -> None:
        with self._subscribers_lock:
            subscribers = list(self._subscribers)

        if log_detail is not None:
            logger.debug(
                "packet: %s -> %s (port %s) to %d subscriber(s)",
                *log_detail,
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

            # Disable automatic acknowledgments before shutdown.
            with self._aprs_ack_lock:
                self._aprs_auto_ack_enabled = False
                self._aprs_ack_authorized = False
                self._aprs_ack_vhf_ready = False
                self._aprs_ack_hf_disconnected = False

            self._aprs_ack_stop.set()
            self._beacon_stop.set()
            self._watchdog_stop.set()
            self._retry_stop.set()
            self._backup_stop.set()
            if self._notifier is not None:
                self._notifier.stop()

            # Discard ACKs waiting in the queue. An ACK already
            # being processed remains the worker's responsibility.
            discarded = 0
            while True:
                try:
                    self._aprs_ack_queue.get_nowait()
                except queue.Empty:
                    break

                self._aprs_ack_queue.task_done()
                discarded += 1

            if discarded:
                logger.info(
                    "Discarded %d pending APRS ACKs during shutdown",
                    discarded,
                )

            if (
                self._aprs_ack_thread is not None
                and self._aprs_ack_thread is not threading.current_thread()
            ):
                self._aprs_ack_thread.join()

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


# Shared with tncstation_rest.py so both programs read the same .env file.
from envfile import apply_legacy_env, env_flag, load_dotenv as _load_dotenv  # noqa: E402



def main(argv: Optional[list] = None) -> None:
    # Before anything else -- including building the argument parser
    # below, whose --port/--socket/--baud defaults themselves read
    # os.environ -- so a .env-supplied value is indistinguishable from
    # a real exported one by the time either of those look for it.
    _load_dotenv()
    legacy_settings = apply_legacy_env()

    parser = argparse.ArgumentParser(
        description="tncstation background daemon"
    )
    parser.add_argument(
        "--tnc",
        default=os.environ.get("TNCSTATION_TNC") or "kamxl",
        help=(
            "TNC model on --port: kamxl (Kantronics KAM-XL), opentnc "
            "(NA6D/AE6EO OpenTNC), or kiss (a TNC already in KISS mode, or "
            "Direwolf -- --port tcp://localhost:8001) "
            "(default: $TNCSTATION_TNC, or kamxl)"
        ),
    )
    parser.add_argument(
        "--tnc-mode",
        default=os.environ.get("TNCSTATION_MODE") or "",
        help=(
            "terminal or kiss. KISS mode is monitoring and APRS only (no "
            "connected mode, PBBS, Winlink or TNC commands). "
            "(default: $TNCSTATION_MODE, or the TNC's own default: terminal "
            "for kamxl/opentnc, kiss for kiss)"
        ),
    )
    parser.add_argument(
        "--port",
        default=os.environ.get("TNCSTATION_PORT"),
        required=os.environ.get("TNCSTATION_PORT") is None,
        help=(
            "Serial port the TNC is on, e.g. /dev/ttyUSB0, /dev/ttyACM0 or COM8, "
            "or tcp://host:port for KISS over TCP (Direwolf) "
            "(default: $TNCSTATION_PORT; required if that's unset). No "
            "hardcoded fallback -- a wrong/missing port should fail "
            "fast, not silently try to open a device that isn't there "
            "and hang every subsequent command until its timeout."
        ),
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("TNCSTATION_SOCKET", DEFAULT_SOCKET_PATH),
        help=f"Unix socket path to listen on (default: {DEFAULT_SOCKET_PATH})",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=int(os.environ.get("TNCSTATION_BAUD", DEFAULT_BAUDRATE)),
        help=(
            f"Host serial baud rate (default: $TNCSTATION_BAUD, or "
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
        default=os.environ.get("TNCSTATION_HISTORY_DB"),
        help=(
            "SQLite reception history database path "
            "(default: $TNCSTATION_HISTORY_DB; disabled if unset)"
        ),
    )

    parser.add_argument(
        "--callsign",
        default=os.environ.get("TNCSTATION_CALLSIGN"),
        required=os.environ.get("TNCSTATION_CALLSIGN") is None,
        help=(
            "This station's APRS callsign, exactly as the KAM-XL's "
            "MYCALL (SSID included), e.g. N5BA or N5BA-10 (default: "
            "$TNCSTATION_CALLSIGN; required if that's unset). Used for the "
            "inbox, automatic ACKs, and matching acks to sent messages."
        ),
    )

    parser.add_argument(
        "--backup-dir",
        default=os.environ.get("TNCSTATION_BACKUP_DIR") or None,
        help=(
            "Back up the history database daily into this folder, e.g. a "
            "mounted network share (default: $TNCSTATION_BACKUP_DIR; off)"
        ),
    )
    parser.add_argument(
        "--backup-keep",
        type=int,
        default=int(os.environ.get("TNCSTATION_BACKUP_KEEP", "7")),
        help="How many daily backups to keep (default: $TNCSTATION_BACKUP_KEEP, or 7)",
    )
    parser.add_argument(
        "--backup-time",
        default=os.environ.get("TNCSTATION_BACKUP_TIME") or "03:30",
        help="Daily backup time, HH:MM local (default: $TNCSTATION_BACKUP_TIME, or 03:30)",
    )
    parser.add_argument(
        "--backup-allow-same-disk",
        action="store_true",
        default=os.environ.get("TNCSTATION_BACKUP_ALLOW_SAME_DISK", "").strip().lower()
        in ("1", "true", "yes", "on"),
        help=(
            "Allow a backup folder on the same disk as the database "
            "(normally refused: it usually means the share isn't mounted)"
        ),
    )

    parser.add_argument(
        "--history-days",
        type=float,
        default=float(os.environ.get("TNCSTATION_HISTORY_DAYS", "90")),
        help=(
            "Delete packet history older than this many days (checked at "
            "startup and daily); 0 keeps everything, otherwise at least "
            "31 so the 30-day chart stays complete (default: "
            "$TNCSTATION_HISTORY_DAYS, or 90). Messages are never pruned."
        ),
    )

    parser.add_argument(
        "--aprs-tocall",
        default=os.environ.get("TNCSTATION_TOCALL") or None,
        help=(
            "AX.25 destination (\"tocall\") on everything sent: identifies "
            "the software (default: $TNCSTATION_TOCALL, or APZTNC, an "
            "experimental-range identifier until tncstation has its own)"
        ),
    )
    parser.add_argument(
        "--aprs-path",
        default=os.environ.get("TNCSTATION_APRS_PATH", "WIDE1-1,WIDE2-1"),
        help=(
            "Digipeater path for everything sent on APRS (messages, "
            "ACKs, beacons) (default: $TNCSTATION_APRS_PATH, or "
            "WIDE1-1,WIDE2-1). Use DIRECT for no digipeaters."
        ),
    )

    parser.add_argument(
        "--latitude",
        type=float,
        default=os.environ.get("TNCSTATION_LATITUDE"),
        help="Station latitude for the position beacon, decimal degrees "
             "(default: $TNCSTATION_LATITUDE)",
    )

    parser.add_argument(
        "--longitude",
        type=float,
        default=os.environ.get("TNCSTATION_LONGITUDE"),
        help="Station longitude, decimal degrees, west negative "
             "(default: $TNCSTATION_LONGITUDE)",
    )

    parser.add_argument(
        "--beacon-symbol",
        default=os.environ.get("TNCSTATION_BEACON_SYMBOL", "/-"),
        help="APRS symbol table + code, 2 characters (default: "
             "$TNCSTATION_BEACON_SYMBOL, or /- = house)",
    )

    parser.add_argument(
        "--beacon-comment",
        default=os.environ.get("TNCSTATION_BEACON_COMMENT", ""),
        help="Beacon comment, up to 43 characters "
             "(default: $TNCSTATION_BEACON_COMMENT)",
    )

    parser.add_argument(
        "--beacon-interval",
        type=float,
        default=float(os.environ.get("TNCSTATION_BEACON_INTERVAL", "30")),
        help="Minutes between beacons, at least 10 "
             "(default: $TNCSTATION_BEACON_INTERVAL, or 30)",
    )

    parser.add_argument(
        "--beacon",
        action="store_true",
        default=os.environ.get("TNCSTATION_BEACON", "").strip().lower()
        in ("1", "true", "yes", "on"),
        help=(
            "Start the position beacon at startup (default: "
            "$TNCSTATION_BEACON). Needs --latitude/--longitude. Can also be "
            "toggled from the dashboard."
        ),
    )

    parser.add_argument(
        "--watchdog-minutes",
        type=float,
        default=float(os.environ.get("TNCSTATION_WATCHDOG_MINUTES", "30")),
        help=(
            "Warn when nothing but your own stations has been heard on "
            "Port 2 for this many minutes; 0 disables (default: "
            "$TNCSTATION_WATCHDOG_MINUTES, or 30; minimum 5)"
        ),
    )

    parser.add_argument(
        "--auto-retry",
        action="store_true",
        default=os.environ.get("TNCSTATION_AUTO_RETRY", "").strip().lower()
        in ("1", "true", "yes", "on"),
        help=(
            "Start with automatic resend of unacknowledged messages on "
            "(default: $TNCSTATION_AUTO_RETRY). Can also be toggled from "
            "the dashboard."
        ),
    )

    parser.add_argument(
        "--retry-count",
        type=int,
        default=int(os.environ.get("TNCSTATION_RETRY_COUNT", "3")),
        help="Automatic resends per message, 1-5 (default: "
             "$TNCSTATION_RETRY_COUNT, or 3)",
    )

    parser.add_argument(
        "--retry-interval",
        type=float,
        default=float(os.environ.get("TNCSTATION_RETRY_INTERVAL", "60")),
        help="Seconds before the first resend, doubling after each; "
             "at least 30 (default: $TNCSTATION_RETRY_INTERVAL, or 60)",
    )

    parser.add_argument(
        "--ntfy-url",
        default=os.environ.get("TNCSTATION_NTFY_URL"),
        help="ntfy server for phone notifications, e.g. https://ntfy.sh "
             "(default: $TNCSTATION_NTFY_URL; notifications off if unset)",
    )

    parser.add_argument(
        "--ntfy-topic",
        default=os.environ.get("TNCSTATION_NTFY_TOPIC"),
        help="ntfy topic your phone subscribes to (default: $TNCSTATION_NTFY_TOPIC)",
    )

    parser.add_argument(
        "--ntfy-events",
        default=os.environ.get("TNCSTATION_NTFY_EVENTS"),
        help="What to notify: messages, bulletins, watchdog, backup (comma list; "
             "default: $TNCSTATION_NTFY_EVENTS, or messages,watchdog,backup)",
    )

    parser.add_argument(
        "--auto-ack",
        action="store_true",
        default=os.environ.get("TNCSTATION_AUTO_ACK", "").strip().lower()
        in ("1", "true", "yes", "on"),
        help=(
            "Turn on automatic APRS acknowledgments at startup "
            "(default: $TNCSTATION_AUTO_ACK). Starting the daemon with this "
            "is the operator's standing authorization for ACK "
            "transmissions on VHF Port 2. Can also be toggled from the "
            "dashboard."
        ),
    )

    parser.add_argument(
        "-v", "--verbose",
        action=argparse.BooleanOptionalAction,
        default=env_flag("VERBOSE"),
        help=(
            "Also log individual packet broadcasts (DEBUG level) "
            "(default: $TNCSTATION_VERBOSE; --no-verbose overrides it)"
        ),
    )
    args = parser.parse_args(argv)

    # Validate before opening the serial port.
    try:
        station_callsign = normalize_station_callsign(args.callsign)
    except ValueError as exc:
        parser.error(str(exc))

    try:
        tnc_model = normalize_tnc_model(args.tnc)
        tnc_mode = normalize_tnc_mode(tnc_model, args.tnc_mode)
    except ValueError as exc:
        parser.error(str(exc))

    from aprs_tx import (
        DEFAULT_TOCALL, APRSTransmitError, validate_aprs_path, validate_tocall,
    )
    from aprs import format_aprs_position

    try:
        aprs_path = validate_aprs_path(args.aprs_path)
        aprs_tocall = validate_tocall(args.aprs_tocall or DEFAULT_TOCALL)
    except APRSTransmitError as exc:
        parser.error(str(exc))

    beacon_config = None
    if (args.latitude is None) != (args.longitude is None):
        parser.error("Set both --latitude and --longitude (or neither)")
    if args.latitude is not None:
        beacon_config = {
            "latitude": float(args.latitude),
            "longitude": float(args.longitude),
            "symbol": args.beacon_symbol,
            "comment": args.beacon_comment,
        }
        try:
            if len(args.beacon_symbol) != 2:
                raise ValueError("Beacon symbol must be 2 characters, e.g. /-")
            format_aprs_position(
                beacon_config["latitude"], beacon_config["longitude"],
                args.beacon_symbol[0], args.beacon_symbol[1],
                args.beacon_comment,
            )
        except ValueError as exc:
            parser.error(f"Invalid beacon settings: {exc}")
    if args.watchdog_minutes and args.watchdog_minutes < 5:
        parser.error("--watchdog-minutes must be 0 (off) or at least 5")
    if not 1 <= args.retry_count <= 5:
        parser.error("--retry-count must be between 1 and 5")
    if args.retry_interval < 30:
        parser.error("--retry-interval must be at least 30 seconds")
    if args.history_days and args.history_days < 31:
        parser.error("--history-days must be 0 (keep all) or at least 31")
    backup = None
    if args.backup_dir:
        if not args.history_db:
            parser.error("--backup-dir needs --history-db (TNCSTATION_HISTORY_DB)")
        from db_backup import BackupManager, parse_backup_time
        try:
            # Validated before the serial port is opened. The folder
            # itself may not be mounted yet; that's checked per backup.
            backup = BackupManager(
                args.history_db,
                args.backup_dir,
                keep=args.backup_keep,
                at=parse_backup_time(args.backup_time),
                allow_same_disk=args.backup_allow_same_disk,
            )
        except ValueError as exc:
            parser.error(str(exc))
    notifier = None
    if args.ntfy_url or args.ntfy_topic:
        if not (args.ntfy_url and args.ntfy_topic):
            parser.error("Set both TNCSTATION_NTFY_URL and TNCSTATION_NTFY_TOPIC (or neither)")
        from notify import Notifier, parse_events
        try:
            # Built (and validated) before the serial port is opened.
            notifier = Notifier(
                url=args.ntfy_url,
                topic=args.ntfy_topic,
                # Secrets: environment/.env only, never command-line flags
                # (those show up in process listings).
                token=os.environ.get("TNCSTATION_NTFY_TOKEN") or None,
                cf_client_id=os.environ.get("TNCSTATION_NTFY_CF_ACCESS_CLIENT_ID") or None,
                cf_client_secret=os.environ.get(
                    "TNCSTATION_NTFY_CF_ACCESS_CLIENT_SECRET") or None,
                events=parse_events(args.ntfy_events),
                click_url=os.environ.get("TNCSTATION_NTFY_CLICK_URL") or None,
            )
        except ValueError as exc:
            parser.error(str(exc))
    if args.beacon_interval < 10:
        parser.error("--beacon-interval must be at least 10 minutes")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if legacy_settings:
        logger.info(
            "Using old setting names %s -- they still work, but rename "
            "KAMXL_ to TNCSTATION_ in .env when convenient",
            ", ".join(legacy_settings),
        )

    kam = create_tnc(tnc_model, args.port, baudrate=args.baud, mode=tnc_mode)
    kam.connect()

    daemon = KAMDaemon(
        kam,
        args.socket,
        history_db=args.history_db,
        station_callsign=station_callsign,
        aprs_path=aprs_path,
        aprs_tocall=aprs_tocall,
        beacon=beacon_config,
        beacon_interval_minutes=args.beacon_interval,
        watchdog_minutes=args.watchdog_minutes,
        retry_count=args.retry_count,
        retry_interval_seconds=args.retry_interval,
        history_retention_days=args.history_days,
        notifier=notifier,
        backup=backup,
    )

    if backup is not None:
        logger.info(
            "History backups: daily at %02d:%02d to %s, keeping %d",
            backup.at[0], backup.at[1], backup.backup_dir, backup.keep,
        )

    if notifier is not None:
        logger.info(
            "Phone notifications: %s topic %s (%s)",
            notifier.url, notifier.topic, ", ".join(sorted(notifier.events)),
        )

    if args.auto_retry:
        try:
            daemon.dispatch("aprs.retry_enable", {
                "confirm_transmit": True,
                "confirm_vhf_port2_radio_ready": True,
                "confirm_hf_port1_disconnected": True,
            })
        except Exception as exc:
            logger.warning(
                "Automatic resend could not be enabled at startup: %s", exc
            )

    if args.beacon:
        try:
            daemon.dispatch("aprs.beacon_enable", {
                "confirm_transmit": True,
                "confirm_vhf_port2_radio_ready": True,
                "confirm_hf_port1_disconnected": True,
            })
        except Exception as exc:
            logger.warning(
                "Position beacon could not be enabled at startup: %s", exc
            )

    if args.auto_ack:
        try:
            daemon.dispatch("aprs.ack_enable", {
                "confirm_transmit": True,
                "confirm_vhf_port2_radio_ready": True,
                "confirm_hf_port1_disconnected": True,
            })
        except Exception as exc:
            # Keep running; it can be turned on from the dashboard.
            logger.warning(
                "Automatic APRS ACKs could not be enabled at startup: %s",
                exc,
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

    logger.info(
        "tncstation daemon (%s, %s mode): %s -> %s",
        kam.MODEL, tnc_mode, args.port, args.socket,
    )

    try:
        while not shutdown_requested.is_set():
            shutdown_requested.wait(timeout=1)
    finally:
        daemon.shutdown()
        server_thread.join(timeout=5)


if __name__ == "__main__":
    main()
