"""
Push notifications to a phone through an ntfy server (https://ntfy.sh
or a self-hosted one), optionally behind Cloudflare Access.

Everything is sent from a background thread so a slow or unreachable
server never delays packet handling. Duplicate events (the same APRS
message heard directly and again via an iGate, a bulletin repeated
every few minutes) are suppressed for a while.

Configuration (all optional; disabled unless URL and topic are set):

    TNCSTATION_NTFY_URL            e.g. https://ntfy.sh or https://notify.example.net
    TNCSTATION_NTFY_TOPIC          topic your phone subscribes to
    TNCSTATION_NTFY_TOKEN          ntfy access token (Authorization: Bearer)
    TNCSTATION_NTFY_CF_ACCESS_CLIENT_ID / TNCSTATION_NTFY_CF_ACCESS_CLIENT_SECRET
                              Cloudflare Access service-token headers
    TNCSTATION_NTFY_EVENTS         comma list: messages, bulletins, watchdog, backup
                              (default: messages,watchdog,backup)
    TNCSTATION_NTFY_CLICK_URL      link opened when the notification is tapped
"""

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional

logger = logging.getLogger("tncstation_notify")
logger.addHandler(logging.NullHandler())

EVENT_TYPES = ("messages", "bulletins", "watchdog", "backup")
DEFAULT_EVENTS = ("messages", "watchdog", "backup")


def parse_events(text: Optional[str]) -> frozenset:
    if text is None or not text.strip():
        return frozenset(DEFAULT_EVENTS)
    events = {e.strip().lower() for e in text.split(",") if e.strip()}
    unknown = events - set(EVENT_TYPES)
    if unknown:
        raise ValueError(
            f"Unknown notification event(s): {', '.join(sorted(unknown))} "
            f"(choose from {', '.join(EVENT_TYPES)})"
        )
    return frozenset(events)


class Notifier:
    """Sends ntfy notifications on a background thread."""

    def __init__(
        self,
        url: str,
        topic: str,
        token: Optional[str] = None,
        cf_client_id: Optional[str] = None,
        cf_client_secret: Optional[str] = None,
        events=DEFAULT_EVENTS,
        click_url: Optional[str] = None,
        timeout: float = 10.0,
        dedupe_seconds: float = 900.0,
        opener: Optional[Callable] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not url or not url.startswith(("http://", "https://")):
            raise ValueError("Notification URL must start with http:// or https://")
        if not topic or "/" in topic or " " in topic:
            raise ValueError("Notification topic must be a single word")
        if bool(cf_client_id) != bool(cf_client_secret):
            raise ValueError(
                "Set both Cloudflare Access client ID and secret (or neither)"
            )

        self.url = url.rstrip("/")
        self.topic = topic
        self.events = frozenset(events)
        self.click_url = click_url
        self._token = token
        self._cf = (cf_client_id, cf_client_secret) if cf_client_id else None
        self._timeout = timeout
        self._dedupe_seconds = dedupe_seconds
        self._open = opener or urllib.request.urlopen
        self._clock = clock

        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=200)
        self._recent: Dict[tuple, float] = {}
        self._lock = threading.Lock()
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.last_sent_at: Optional[float] = None
        self.last_error: Optional[str] = None

        self._thread = threading.Thread(
            target=self._run, name="kamxl-notify", daemon=True
        )
        self._thread.start()

    # -- public ----------------------------------------------------------

    def notify(
        self,
        event: str,
        title: str,
        message: str,
        tags: Optional[list] = None,
        priority: int = 3,
        dedupe_key: Optional[tuple] = None,
    ) -> bool:
        """Queue one notification. Returns False if filtered or dropped."""
        if event != "test" and event not in self.events:
            return False

        if dedupe_key is not None:
            key = (event,) + tuple(dedupe_key)
            now = self._clock()
            with self._lock:
                self._recent = {
                    k: t for k, t in self._recent.items()
                    if now - t < self._dedupe_seconds
                }
                if key in self._recent:
                    return False
                self._recent[key] = now

        body = {
            "topic": self.topic,
            "title": title,
            "message": message,
            "priority": priority,
        }
        if tags:
            body["tags"] = list(tags)
        if self.click_url:
            body["click"] = self.click_url

        try:
            self._queue.put_nowait(body)
            return True
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return False

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": True,
                "server": self.url,
                "topic": self.topic,
                "events": sorted(self.events),
                "auth": (
                    "token+cloudflare" if self._token and self._cf
                    else "token" if self._token
                    else "cloudflare" if self._cf
                    else "none"
                ),
                "sent": self.sent,
                "failed": self.failed,
                "dropped": self.dropped,
                "last_sent_at": self.last_sent_at,
                "last_error": self.last_error,
            }

    def stop(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    # -- worker ----------------------------------------------------------

    def _request(self, body: dict) -> urllib.request.Request:
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.url + "/", data=data, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", "tncstation-daemon")
        if self._token:
            request.add_header("Authorization", "Bearer " + self._token)
        if self._cf:
            request.add_header("CF-Access-Client-Id", self._cf[0])
            request.add_header("CF-Access-Client-Secret", self._cf[1])
        return request

    def send_now(self, body: dict) -> None:
        """Send synchronously (used by the worker). Raises on failure."""
        with self._open(self._request(body), timeout=self._timeout) as response:
            status = getattr(response, "status", 200)
            if status >= 300:
                raise RuntimeError(f"HTTP {status}")

    def _run(self) -> None:
        while True:
            body = self._queue.get()
            if body is None:
                return
            try:
                self.send_now(body)
            except urllib.error.HTTPError as exc:
                self._failed(f"HTTP {exc.code} {exc.reason}"
                             + (" (check the Cloudflare Access service token)"
                                if exc.code in (401, 403) else ""))
            except Exception as exc:  # network errors, timeouts
                self._failed(str(exc) or exc.__class__.__name__)
            else:
                with self._lock:
                    self.sent += 1
                    self.last_sent_at = self._clock()
                    self.last_error = None

    def _failed(self, reason: str) -> None:
        with self._lock:
            self.failed += 1
            self.last_error = reason
        logger.warning("Notification not delivered: %s", reason)
