"""
Client for Winlink's HTTP web-service API (api.winlink.org) --
unrelated to winlink.py's B2F/RF protocol module. This talks HTTPS/
JSON directly to the Winlink Development Team's (WDT) Common Message
Server; no serial port, no AX.25, no kamxl.py KAMXL instance is
involved anywhere in this module.

Access requires an API key issued by the WDT. Dave's key (issued
August 2026, in response to the kamxl_winlink registration request)
carries four permissions: AccountExists, GatewayChannelReport,
GatewayListing, GatewayProximity -- see docs/history.md's "Winlink Web
Service API" milestone for the full approval email. tncstation_daemon.py
reads the key from the WINLINK_API_KEY environment variable at call
time (deliberately no --winlink-api-key CLI flag -- chosen over the
alternatives specifically so the key never has to touch a config file
or show up in a process listing/shell history). This module itself
takes the key as a plain argument and never reads the environment or
any file directly, so it stays usable/testable independent of that
choice.

RESEARCH BASIS, same practice as winlink.py: the WDT's own API docs
page (https://api.winlink.org/metadata) is a JavaScript-rendered
ServiceStack metadata page this project's tooling couldn't fetch (no
Chrome available this session), so every endpoint/format detail below
was instead cross-checked against Pat (https://github.com/la5nta/pat),
a real, actively-interoperating open-source Winlink client -- its
internal/cmsapi package specifically (MIT-licensed, source read at
https://github.com/la5nta/pat/blob/v1.0.0/internal/cmsapi/api.go and
client.go). Confirmed from that source:

  - Base URL: https://api.winlink.org
  - GET /account/exists?callsign=<CALL>&key=<KEY> ->
    {"CallsignExists": bool,
     "ResponseStatus": {"ErrorCode": str, "Message": str}}
    (an EMPTY ResponseStatus means success; a populated one is an
    error -- see account_exists() below.)
  - POST /gateway/status.json, application/x-www-form-urlencoded body
    of Mode / HistoryHours / (repeated) ServiceCodes AND key together
    (the key rides in the POST body here, NOT the query string --
    genuinely different from account/exists's own convention, kept
    exactly as observed rather than normalized away) ->
    {"ServerName": str, "ErrorCode": int, "Gateways": [...]}.
    Note this endpoint's error shape is a bare top-level "ErrorCode"
    int, NOT the nested "ResponseStatus" object account/exists uses --
    again kept as-is rather than assumed uniform.

NOT CONFIRMED: a distinct "GatewayProximity" endpoint. Nothing in
Pat's client (or anywhere else searched) calls a separate proximity
URL -- gateway/status.json's own Gateway records already carry
Latitude/Longitude. Working assumption, called out explicitly rather
than silently guessed at: GatewayProximity is the permission gating
*that* Latitude/Longitude data in the gateway/status.json response
(so a client can compute proximity itself), not a separate
server-side operation. nearby_gateways() below does that computation
locally (ordinary great-circle distance -- no protocol involved, nothing
that needed cross-checking against a reference client). This should be
treated as unverified until exercised against the live API with a
real key -- Dave holds the actual key value, so that confirmation can
only happen on his end.

Rate limiting: per the WDT's own approval email, "the API endpoints
should be queried sparingly" -- abuse risks the key being revoked.
This module makes exactly one HTTP request per call; no retry/backoff
loops, no polling built in here. Callers are responsible for not
hammering it -- e.g. caching gateway_status() results across requests
rather than re-fetching on every call (left to callers, e.g.
tncstation_daemon.py, rather than baked into this module, so a caller that
genuinely does need a fresh read isn't forced through a cache).
"""

import json
import urllib.error
import urllib.parse
import urllib.request

from dataclasses import dataclass, field
from math import asin, cos, radians, sin, sqrt
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


ROOT_URL = "https://api.winlink.org"
PATH_ACCOUNT_EXISTS = "/account/exists"
PATH_GATEWAY_STATUS = "/gateway/status.json"

DEFAULT_TIMEOUT = 15.0

# (method, url, body, headers, timeout) -> (status_code, response_body)
#
# Injectable so tests can exercise the real parsing/error-handling
# logic below without ever touching the network -- same fakes-over-
# mocks discipline as tests/fakes.py's ScriptedSerial/CannedSerial for
# the serial port, just for HTTP instead.
Transport = Callable[
    [str, str, Optional[bytes], Dict[str, str], float],
    Tuple[int, bytes]
]


class WinlinkAPIError(Exception):
    """A call to Winlink's HTTP web-service API failed."""


def _default_transport(
    method: str,
    url: str,
    data: Optional[bytes],
    headers: Dict[str, str],
    timeout: float,
) -> Tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        # A non-2xx response still has a body worth reading (Winlink's
        # own error envelopes carry a real ErrorCode/Message) rather
        # than losing it to the exception unread.
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise WinlinkAPIError(f"Could not reach {ROOT_URL}: {exc.reason}") from exc


def _parse_json_response(path: str, status: int, body: bytes) -> Any:
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WinlinkAPIError(
            f"{path}: response wasn't valid JSON (HTTP {status}): "
            f"{body[:200]!r}"
        ) from exc

    if status // 100 != 2:
        raise WinlinkAPIError(f"{path}: HTTP {status}: {parsed}")

    return parsed


def _get(
    path: str,
    api_key: str,
    params: Dict[str, str],
    timeout: float,
    transport: Transport,
) -> Any:
    query = dict(params)
    query["key"] = api_key
    url = f"{ROOT_URL}{path}?{urllib.parse.urlencode(query)}"

    status, body = transport("GET", url, None, {"Accept": "application/json"}, timeout)
    return _parse_json_response(path, status, body)


def _post_form(
    path: str,
    api_key: str,
    form: List[Tuple[str, str]],
    timeout: float,
    transport: Transport,
) -> Any:
    url = f"{ROOT_URL}{path}"
    # key rides in the body here, not the query string -- see this
    # module's docstring's "genuinely different" note above.
    body_fields = list(form) + [("key", api_key)]
    data = urllib.parse.urlencode(body_fields).encode("ascii")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    status, body = transport("POST", url, data, headers, timeout)
    return _parse_json_response(path, status, body)


# ---------------------------------------------------------------------------
# AccountExists
# ---------------------------------------------------------------------------

def account_exists(
    callsign: str,
    api_key: str,
    timeout: float = DEFAULT_TIMEOUT,
    transport: Transport = _default_transport,
) -> bool:
    """
    True if ``callsign`` has an active Winlink account.
    """
    response = _get(
        PATH_ACCOUNT_EXISTS, api_key, {"callsign": callsign},
        timeout, transport,
    )

    error = (response.get("ResponseStatus") or {}).get("Message")
    if error:
        raise WinlinkAPIError(f"account/exists: {error}")

    return bool(response.get("CallsignExists"))


# ---------------------------------------------------------------------------
# GatewayListing / GatewayChannelReport
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GatewayChannel:
    """One RF channel of a gateway, from a gateway/status.json entry."""
    operating_hours: str
    supported_modes: str
    frequency: float
    service_code: str
    baud: str
    radio_range: str
    mode: int
    gridsquare: str
    antenna: str


@dataclass(frozen=True)
class Gateway:
    """One gateway row, from a gateway/status.json response."""
    callsign: str
    base_callsign: str
    requested_mode: str
    comments: str
    last_status: str
    latitude: float
    longitude: float
    channels: List[GatewayChannel] = field(default_factory=list)


def _parse_gateway_channel(raw: Dict[str, Any]) -> GatewayChannel:
    return GatewayChannel(
        operating_hours=raw.get("OperatingHours", ""),
        supported_modes=raw.get("SupportedModes", ""),
        frequency=float(raw.get("Frequency") or 0.0),
        service_code=raw.get("ServiceCode", ""),
        baud=raw.get("Baud", ""),
        radio_range=raw.get("RadioRange", ""),
        mode=int(raw.get("Mode") or 0),
        gridsquare=raw.get("Gridsquare", ""),
        antenna=raw.get("Antenna", ""),
    )


def _parse_gateway(raw: Dict[str, Any]) -> Gateway:
    return Gateway(
        callsign=raw.get("Callsign", ""),
        base_callsign=raw.get("BaseCallsign", ""),
        requested_mode=raw.get("RequestedMode", ""),
        comments=raw.get("Comments", ""),
        last_status=raw.get("LastStatus", ""),
        latitude=float(raw.get("Latitude") or 0.0),
        longitude=float(raw.get("Longitude") or 0.0),
        channels=[
            _parse_gateway_channel(channel)
            for channel in raw.get("GatewayChannels") or []
        ],
    )


def get_gateway_status(
    api_key: str,
    mode: str = "AnyAll",
    history_hours: int = 48,
    service_codes: Sequence[str] = ("PUBLIC",),
    timeout: float = DEFAULT_TIMEOUT,
    transport: Transport = _default_transport,
) -> List[Gateway]:
    """
    Fetch the current gateway/channel listing -- covers both the
    GatewayListing and GatewayChannelReport permissions granted with
    the API key (both surface through this one response; the WDT
    doesn't split them across separate URLs, per Pat's own
    GetGatewayStatus()).

    mode: "packet", "pactor", "robustpacket", "allhf", or "AnyAll".
    history_hours: clamped to the API's own 48-hour maximum.
    service_codes: defaults to ("PUBLIC",) -- non-public codes
    (EMCOMM, private group codes) need those groups' own separate
    permission, per https://winlink.org/content/gateway_channels.
    """
    form: List[Tuple[str, str]] = [
        ("Mode", mode),
        ("HistoryHours", str(min(history_hours, 48))),
    ]
    for code in service_codes:
        form.append(("ServiceCodes", code))

    response = _post_form(PATH_GATEWAY_STATUS, api_key, form, timeout, transport)

    error_code = response.get("ErrorCode")
    if error_code:
        raise WinlinkAPIError(
            f"gateway/status.json: ErrorCode {error_code} "
            f"(server {response.get('ServerName')!r})"
        )

    return [_parse_gateway(raw) for raw in response.get("Gateways") or []]


# ---------------------------------------------------------------------------
# GatewayProximity (client-side -- see module docstring's "NOT CONFIRMED")
# ---------------------------------------------------------------------------

_EARTH_RADIUS_KM = 6371.0088


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/long points, in km."""
    lat1, lon1, lat2, lon2 = (radians(v) for v in (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * asin(sqrt(a))


def nearby_gateways(
    gateways: Sequence[Gateway],
    latitude: float,
    longitude: float,
    max_distance_km: Optional[float] = None,
    limit: Optional[int] = None,
) -> List[Tuple[Gateway, float]]:
    """
    Sort ``gateways`` (as returned by get_gateway_status()) by
    great-circle distance from (latitude, longitude), nearest first.
    Returns (Gateway, distance_km) pairs.

    NOT a Winlink API operation -- see this module's docstring's "NOT
    CONFIRMED" note. This is ordinary client-side geometry over data
    gateway/status.json already provides.

    Gateways with (0, 0) coordinates (no reported location) are
    excluded rather than sorted in as if they were really at the
    intersection of the equator and the prime meridian.
    """
    results = [
        (gw, _haversine_km(latitude, longitude, gw.latitude, gw.longitude))
        for gw in gateways
        if gw.latitude != 0.0 or gw.longitude != 0.0
    ]
    results.sort(key=lambda pair: pair[1])

    if max_distance_km is not None:
        results = [pair for pair in results if pair[1] <= max_distance_km]

    if limit is not None:
        results = results[:limit]

    return results
