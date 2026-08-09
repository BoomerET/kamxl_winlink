# REST API

Milestone 3. `kamxl_rest.py` is a thin HTTP front end for
[the daemon](daemon.md) -- it translates HTTP requests into the
daemon's Unix socket protocol and proxies its `packet` events out as
Server-Sent Events. It does not talk to the KAM-XL directly; the
daemon remains the single owner of the serial connection. Start the
daemon first.

## Running it

```
python3 kamxl_daemon.py --port COM8 &
python3 kamxl_rest.py --daemon-socket /tmp/kamxl.sock --port 8080
```

| Flag | Env var | Default |
| --- | --- | --- |
| `--daemon-socket` | `KAMXL_SOCKET` | `/tmp/kamxl.sock` |
| `--host` | `KAMXL_REST_HOST` | `0.0.0.0` (all interfaces) |
| `--port` | `KAMXL_REST_PORT` | `8080` |
| `--api-key` | `KAMXL_REST_API_KEY` | random, generated and printed at startup |

`Ctrl-C`/`SIGTERM` shuts down cleanly, same as the daemon.

## Authentication

Binds to all interfaces by default so it's reachable from other
devices on your LAN (a phone, another PC), which means every request
needs a bearer token:

```
Authorization: Bearer <token>
```

If you don't pass `--api-key`, one is generated randomly and printed
to the terminal at startup -- save it. A request with a missing or
wrong token gets `401 Unauthorized`.

To skip auth entirely, pass `--no-auth` -- refused unless `--host` is
also `127.0.0.1`, since running without auth while reachable from the
whole LAN would defeat the point.

Browser contexts that can't set a custom header -- `EventSource`
(used by `/monitor/stream`), or just pasting a URL into the address
bar to load the [web terminal](#web-terminal) -- can instead pass the
same token as a query parameter: `?token=<token>`. Checked as a
fallback only; the header is still preferred everywhere a client can
set one, since a token in a URL is more likely to end up logged
somewhere (browser history, server access logs) than one in a header.

**Security note:** the token travels in cleartext over plain HTTP.
That's an acceptable bar for a trusted home LAN, but this is *not*
safe to expose over the open internet (e.g. via port forwarding)
without putting TLS in front of it first (a reverse proxy, for
example).

## Response format

Mirrors the daemon's own protocol for consistency:

```json
{"ok": true, "result": <any>}
```
```json
{"ok": false, "error": {"type": "<string>", "message": "<string>"}}
```

## Endpoints

All bodies and responses are JSON. See
[api_reference.md](api_reference.md) for what each underlying `KAMXL`
call actually does.

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| GET | `/ping` | | |
| GET | `/status` | | `{"connected", "port", "monitor_subscribers"}` |
| GET | `/configuration` | | Full `DISPLAY` dump |
| GET | `/params/<COMMAND>` | | Typed get (`get_typed`) |
| PUT | `/params/<COMMAND>` | `{"value": ...}` | Typed set (`set_typed`) |
| GET | `/params/<COMMAND>/raw` | | Raw string get |
| PUT | `/params/<COMMAND>/raw` | `{"value": "..."}` | Raw string set |
| POST | `/connect` | `{"callsign", "via"?, "timeout"?}` | AX.25 CONNECT |
| POST | `/disconnect` | `{"timeout"?}` | AX.25 DISCONNECT |
| POST | `/connected/send` | `{"text", "add_cr"?}` | Send while connected |
| GET | `/connected/read?timeout=5` | | Read while connected |
| GET | `/monitor/stream` | | Server-Sent Events, see below |
| POST | `/terminal/exec` | `{"command", "timeout"?}` | Raw command passthrough, see [Web terminal](#web-terminal) |
| GET | `/` | | Serves the web terminal page |
| GET | `/pbbs/messages` | | List PBBS messages, see [PBBS](#pbbs) |
| GET | `/pbbs/messages/<N>` | | Read PBBS message `N`; `result` is `null` if not found |
| GET | `/pbbs` | | Serves the PBBS web page |
| GET | `/stations` | | List known stations, see [Stations / map](#stations--map) |
| GET | `/stations/<CALLSIGN>` | | One station; `result` is `null` if never heard |
| GET | `/map` | | Serves the station map web page |
| POST | `/winlink/check` | `{"gateway", "password", "mycall"?, "connect_timeout"?, "read_timeout"?}` | Check/download Winlink mail, see [Winlink](#winlink) |
| POST | `/winlink/send` | `{"gateway", "password", "messages": [{"to", "subject", "body", "cc"?, "msg_type"?, "mid"?}, ...], "mycall"?, "connect_timeout"?, "read_timeout"?}` | Send 1-5 Winlink messages, see [Winlink](#winlink) |
| GET | `/winlink` | | Serves the Winlink web page (check-mail and send-mail tabs) |
| GET | `/winlink/account/<CALLSIGN>` | | Does `<CALLSIGN>` have an active Winlink account? See [Winlink web-service API](#winlink-web-service-api) |
| GET | `/winlink/gateways?mode=&history_hours=&service_codes=` | | Gateway/channel listing, see [Winlink web-service API](#winlink-web-service-api) |
| GET | `/winlink/gateways/nearby?lat=&lon=&max_distance_km=&limit=` | | Gateway listing sorted by distance from a point, see [Winlink web-service API](#winlink-web-service-api) |

Multi-port values (`MYCALL`, `HBAUD`, `MONITOR`, ...) cross the wire
as JSON arrays: `{"value": [true, false]}`.

`/connect`, `/disconnect`, and `/connected/read` accept a `timeout`
(seconds) that's passed straight through to the underlying KAM-XL
operation -- e.g. `{"timeout": 90}` for a slow digipeated connect.
`/disconnect` also accepts `command_mode_timeout` (default `5`),
which separately bounds the initial Ctrl-C step that returns to
Command mode before the DISCONNECT itself is attempted -- the two run
sequentially, so a slow/unresponsive Ctrl-C step won't be masked by a
generous `timeout` on the DISCONNECT step alone.
This layer automatically waits a bit longer than that on its own end
(so it doesn't give up on you before the daemon could possibly have
an answer); a `504` means even that extended wait wasn't enough,
which usually means the KAM-XL operation is still genuinely in
progress rather than actually broken -- retry, or check status/logs
before assuming something's wrong.

### Status codes

| Status | Meaning |
| --- | --- |
| 200 | Success |
| 400 | Malformed request (missing required field) |
| 401 | Missing/invalid `Authorization` header (or `?token=` query parameter) |
| 404 | No such endpoint |
| 405 | Endpoint exists, wrong HTTP method |
| 502 | Request reached the daemon, but the KAM-XL/daemon rejected or failed it (`error.type` is the underlying exception, e.g. `KAMConnectionError`, `KAMTimeoutError`, `KAMError`) |
| 503 | Can't reach the daemon at all -- probably not running |
| 504 | Reached the daemon, but it hasn't answered yet within the time this layer is willing to wait. Distinct from 502: the operation may still be in progress on the KAM-XL side (most likely on a slow/failed AX.25 connect) rather than having actually failed |

`GET /favicon.ico` is a deliberate exception to all of the above: it
bypasses auth entirely and always returns `204` with no body, never
`401`. Browsers request this path automatically and unauthenticated on
first load of any page here (there's no cookie/session for them to
have picked a token up from), so leaving it to the normal auth path
would log a routine, harmless request as an "Unauthorized" failure --
see `RESTRequestHandler.do_GET()` in `kamxl_rest.py`.

## Examples

```
curl -H "Authorization: Bearer $TOKEN" http://kam-host:8080/status

curl -H "Authorization: Bearer $TOKEN" \
     -X PUT -d '{"value": [true, false]}' \
     http://kam-host:8080/params/MONITOR

curl -H "Authorization: Bearer $TOKEN" \
     -X POST -d '{"callsign": "KD5EOC-10", "via": "RSSTN"}' \
     http://kam-host:8080/connect
```

## Live monitoring (`/monitor/stream`)

A [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events)
stream -- plain HTTP, no websockets needed, works directly with a
browser's `EventSource`:

```javascript
const source = new EventSource("http://kam-host:8080/monitor/stream");
// Note: EventSource can't set an Authorization header. See below.

source.onmessage = (e) => {
    const event = JSON.parse(e.data);
    console.log(event.data.source, "->", event.data.destination, event.data.payload);
};
```

Sends a `: keepalive` comment roughly every request timeout period of
silence to keep the connection alive through proxies -- `EventSource`
clients can ignore these (they don't fire `onmessage`).

Internally, waiting for the next event uses `select.select()` rather
than relying on the socket's own read timeout -- a real bug, found
live on hardware: once a `socket.timeout` fires once on a
`socket.makefile()` object, CPython sets a sticky flag that makes
every *later* read on that same file object fail immediately with
`OSError: cannot read from timed out object` instead of trying again.
That silently killed the stream after the very first idle period
(EventSource would then reconnect and repeat, discarding whatever
packet arrived during the gap). `select()` only lets a read happen
once data is actually waiting, so that path is never triggered.

**`EventSource` and auth:** the browser `EventSource` API can't set
custom headers, so authenticate it with the `?token=` query parameter
described under [Authentication](#authentication) instead:

```javascript
const source = new EventSource(
    `http://kam-host:8080/monitor/stream?token=${token}`
);
```

```
curl -N -H "Authorization: Bearer $TOKEN" http://kam-host:8080/monitor/stream
```

## Web terminal

`GET /` serves a single self-contained HTML page (no build step, no
external dependency) with two panes: a live packet monitor on top, a
terminal-like command box below.

The terminal pane: type any raw Terminal Mode command (`VERSION`,
`DISPLAY`, `BEACON`, `MHEARD`, ...) and see the KAM-XL's raw
response, same as a serial terminal program would show. It POSTs to
`/terminal/exec`, which passes the command straight through to
`KAMXL.send_command()` -- no assumption about response shape, so it
works for anything, not just commands `kamxl.py` has typed metadata
for.

The monitor pane (milestone 5): connects to `/monitor/stream` via
`EventSource` and prints each packet as it arrives -- time, port, a
`<TAG>` if the KAM-XL included one (see `frame_type` in
[api_reference.md](api_reference.md) -- `<UI>` is an ordinary beacon,
most others are AX.25 control/supervisory traffic between other
stations), source -> destination (with any digipeat path), payload.
A status indicator (`live` / `reconnecting...`) reflects the
connection state; `EventSource` reconnects on its own if the
connection drops. This only shows traffic if `MONITOR` is actually
`ON` for the relevant port(s) on the KAM-XL -- the page doesn't
enable it for you, so if the feed stays empty, type `MONITOR ON/ON`
(multi-port values are slash-separated, not space-separated) into
the terminal pane first, or `PUT /params/MONITOR`.

```
curl -H "Authorization: Bearer $TOKEN" \
     -X POST -d '{"command": "VERSION"}' \
     http://kam-host:8080/terminal/exec
```

Open it in a browser at `http://kam-host:8080/?token=<token>` -- the
page reads `token` from its own URL and carries it forward on every
request it makes (see the query-string auth fallback above). Without
a valid token, `GET /` itself returns `401` just like any other
endpoint when authentication is enabled.

## PBBS

`GET /pbbs` serves a second self-contained page: a **read-only**
browser for the KAM-XL's own built-in firmware PBBS (mailbox). Not a
BBS this project implements -- the firmware handles message storage,
forwarding, and SYSOP access; this is just a list-and-read view over
it. Linked from the web terminal's header, and back again.

Behind it, `GET /pbbs/messages` and `GET /pbbs/messages/<N>` each
drive a full AX.25 connect/command/disconnect cycle against the
KAM-XL's `MYPBBS` on every call -- there's no persistent PBBS
session, so expect each request to take a few seconds, not be
instant. A connect from the local serial terminal gets automatic
SYSOP privilege per the manual, no password needed.

```
curl -H "Authorization: Bearer $TOKEN" http://kam-host:8080/pbbs/messages
curl -H "Authorization: Bearer $TOKEN" http://kam-host:8080/pbbs/messages/6
```

`/pbbs/messages/<N>` returns `{"ok": true, "result": null}` (not a
404) if `N` doesn't resolve to a message -- distinguishing "asked for
something that doesn't exist" from "the endpoint itself doesn't
exist."

**Verified against real hardware**, for both an empty mailbox and a
populated one. Each endpoint accepts an optional `read_timeout` query
parameter (default `10`) -- this is a worst-case ceiling, not a fixed
wait: the underlying library polls the connected-mode response in
short slices and returns as soon as the PBBS's `ENTER COMMAND:`
prompt reappears. This replaced an earlier fixed-wait design after a
real message on hardware had its last line silently truncated because
it took slightly longer than the old 5s window to fully arrive.

## Stations / map

Milestone 7 (APRS mapping). `GET /map` serves a third self-contained
page: a live map, built with [Leaflet](https://leafletjs.com/) and
OpenStreetMap tiles (both loaded from `cdnjs.cloudflare.com` by the
*browser* viewing the page -- `kamxl_rest.py` itself never talks to the
internet). Linked from the web terminal's and PBBS page's headers, and
back again.

Markers use real APRS symbol-table icons, not Leaflet's plain default
marker. This is a deliberate, one-off exception to the cdnjs-only rule
above, since no APRS icon set is available there -- two real,
separately licensed third-party sources are used instead, both pinned
to a specific commit:

- **Sprite images:** [hessu/aprs-symbols](https://github.com/hessu/aprs-symbols),
  the icon set maintained by Heikki Hannikainen/OH7LZB (operator of
  aprs.fi). Licensing is mixed per-symbol (mostly CC-BY-SA 2.0
  original work, some public-domain-sourced elements -- see that
  repo's `COPYRIGHT.md`). Pinned to commit
  `f2286a9cd43eb6ba4501250b4c39fff111e3796c` (2024-10-10) and loaded
  by the browser via jsdelivr's GitHub-raw CDN, not vendored into this
  repo.
- **Symbol lookup logic:** the ~15-line translation-table and
  sprite-grid-position math is ported inline into `MAP_HTML`'s own
  `<script>` from [OK-DMR/aprs-symbols](https://github.com/OK-DMR/aprs-symbols)'
  `aprs-symbols.js`/`aprs-symbols.css` (MIT License, Copyright (c)
  2019 Marek Sebera) -- not loaded from a CDN, just the logic reused
  with attribution.

Real APRS symbol tables are literally `/` (primary) or `\` (alternate)
-- any other `symbol_table` character means an *alternate table with
overlay* (a digit/letter meant to be drawn over the base icon, e.g. a
numbered object). The map renders the alternate table's base icon for
that case but doesn't draw the overlay glyph itself -- a real,
documented gap, not a guess. A symbol code outside the chart entirely
(malformed/missing data) falls back to Leaflet's plain default marker
rather than drawing nothing.

Unlike PBBS, `GET /stations` and `GET /stations/<CALLSIGN>` never
drive an AX.25 connect/command/disconnect cycle -- they just read
whatever `kamxl_daemon.py`'s always-on monitor thread has already
decoded into its in-memory station database (see
[daemon.md](daemon.md#concurrency-model) and `stations.py`), so these
calls are fast and don't need a generous timeout the way PBBS calls
do.

```
curl -H "Authorization: Bearer $TOKEN" http://kam-host:8080/stations
curl -H "Authorization: Bearer $TOKEN" http://kam-host:8080/stations/AI6K-9
```

`/stations/<CALLSIGN>` returns `{"ok": true, "result": null}` (not a
404) for a callsign never heard, same "doesn't exist" vs. "no data
yet" reasoning as `/pbbs/messages/<N>`.

Each `Station` in the response: `callsign`, `latitude`, `longitude`,
`symbol_table`, `symbol_code`, `comment`, `last_heard` (epoch
seconds), `packet_count`. The map page renders each with its real APRS
symbol icon (see above) and a popup showing position, comment,
`symbol_table`/`symbol_code` as text, and how long ago it was last
heard; it polls `/stations` every 15 seconds and only actually
pans/zooms to fit all stations once, the first time any appear, so it
doesn't yank the view out from under someone who's since panned around
manually.

**Unverified against a real captured APRS session.** `aprs.py`'s
position-report parsing is built from the public APRS Protocol
Reference spec (not the KAM-XL manual -- APRS is a separate,
open protocol layered on top of ordinary AX.25 UI frames), and the
station database is straightforward once a position decodes -- but
neither has been checked yet against real APRS traffic the way
`packet.py`'s `HEADER_RE` and `pbbs.py`'s parsing eventually were.
Treat it as a first draft; see `aprs.py`'s module docstring for two
specific known simplifications (compressed positions unsupported,
position ambiguity not fully modeled).

## Winlink

Milestone 8. `GET /winlink` serves a fourth self-contained page: a
form (RMS gateway callsign, your Winlink account password, an
optional `mycall` override) that posts to `POST /winlink/check` and
renders whatever mail comes back. Linked from the terminal, PBBS, and
map pages' headers, and back again.

**The password field deserves its own callout.** It's sent as a
regular JSON body field (`POST`, never a URL/query parameter -- see
the [Authentication](#authentication) section's own reasoning for why
that matters generally, doubly so for a real email account password
rather than just this API's own bearer token) over the same plain
HTTP this whole API already uses -- acceptable on a trusted home LAN,
same posture as everything else here, **not safe over the open
internet** without TLS in front of it. The page never persists the
password (no cookie, no `localStorage`) and clears the field after
every submit attempt, success or failure -- typed fresh each time you
check mail rather than sitting in the page.

```
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"gateway": "KD5EOC-10", "password": "..."}' \
  http://kam-host:8080/winlink/check
```

Unlike PBBS/stations, this drives a genuinely slower exchange (AX.25
connect + secure login + FBB proposal negotiation), so give it real
time -- the endpoint's socket timeout budget scales with
`read_timeout` (default 30s) times three, since the underlying
`KAMXL.check_winlink_mail()` polls through three separate stages
(handshake, proposals, message bodies), plus `connect_timeout` and a
margin for `disconnect_station()`.

**Both ASCII and B2 protocol tiers for receiving; send support added
afterward (see below).** See `winlink.py`'s module docstring,
`lzhuf.py`'s module docstring, and
[api_reference.md](api_reference.md#winlink-milestone-8) for the full
scope writeup. The secure-login response algorithm is confirmed
correct independent of real-hardware testing, verified against a
trusted open-source reference implementation's own test vectors. A
live test against a real gateway (KD5EOC-10) confirmed the SID
exchange and secure-login challenge-response work end-to-end, and
surfaced two real bugs (both since fixed): a KAM-XL echo-back
mistaken for a gateway reply, and a hang when the gateway disconnected
mid-exchange because it required B2 support -- see api_reference.md's
writeup for both.

**That second finding is what prompted implementing real B2 support**:
`winlink.py` now claims `B2` in its own SID, and `check_winlink_mail()`
can retrieve mail proposed either the legacy ASCII way (`FB`, plain
title+body) or via B2 (`FC`, LZHUF-compressed, binary-framed,
carrying Winlink's real structured header and attachment metadata --
see api_reference.md's `WinlinkMessage` writeup for the field
differences). The LZHUF codec (new `lzhuf.py` module) is cross-checked
against two independent reference implementations and round-trips
correctly in tests, but real end-to-end interop with a real gateway's
own B2-compressed bytes remains unverified -- that needs an account
with actual mail waiting on a B2-enforcing gateway, which hasn't
happened yet.

**A third real-hardware test surfaced a disconnect unrelated to B2.**
With B2 now claimed, a live test against KD5EOC-10 completed the SID
exchange, secure login, and B2 proposal negotiation successfully, then
the gateway printed `*** Unknown client types are not allowed on
production servers -- use cms-z.winlink.org - Disconnecting` and
dropped the link -- the production Winlink CMS rejecting this
module's own client identification, not a protocol bug. The
`KAMConnectionError` this raises used to guess "this can happen if the
gateway requires B2 protocol support," which went stale and misleading
the moment this unrelated cause turned up; it now just quotes the
gateway's own reason verbatim rather than guessing. This is a
registration/gatekeeping policy issue on Winlink's infrastructure, not
something `check_winlink_mail()` can resolve by itself -- see
`winlink.py`'s module docstring's "KNOWN DISCONNECT REASONS" section
for the full writeup of both real causes found so far.

**Send support** (`POST /winlink/send`): built once the client
identity/rename work above was underway, in parallel with the pending
registration question. Send-only, text-body-only (no attachments),
B2/FC only -- see `winlink.py`'s module docstring's "SEND SUPPORT"
note for the full scope and its own UNVERIFIED-AGAINST-A-REAL-GATEWAY
caveat, which applies here too.

```
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "gateway": "KD5EOC-10",
    "password": "...",
    "messages": [
      {"to": ["N0CALL"], "subject": "Test", "body": "Hello via kamxl_winlink"}
    ]
  }' \
  http://kam-host:8080/winlink/send
```

`result` is a list of the MIDs the gateway actually accepted (empty
if none were -- not itself an error). The `/winlink` page's "Send
mail" tab wraps this in a small form (To/Cc/Subject/body), same
password-hygiene posture as the check-mail tab (never persisted,
cleared after every submit).

## Winlink web-service API

August 2026, once Dave's `kamxl_winlink` API key came through from the
WDT. A completely different Winlink surface from everything above:
these three endpoints call `api.winlink.org`'s HTTPS/JSON API
(`winlink_api.py`), not the KAM-XL's own AX.25/B2F session at all --
no serial port, no `connect_timeout`/`read_timeout`, no password
either, so plain `GET` with query params is fine here (unlike
`/winlink/check`/`/winlink/send`, which need `POST`/JSON specifically
to keep a real account password out of a URL). The daemon reads the
actual API key itself from `$WINLINK_API_KEY` (see
[daemon.md](daemon.md#winlink-api-key)) -- this REST layer never
handles that key, and none of these three endpoints will work until
it's set on the daemon's own environment.

```
curl -H "Authorization: Bearer $TOKEN" \
  http://kam-host:8080/winlink/account/AI6K

curl -H "Authorization: Bearer $TOKEN" \
  "http://kam-host:8080/winlink/gateways?service_codes=PUBLIC"

curl -H "Authorization: Bearer $TOKEN" \
  "http://kam-host:8080/winlink/gateways/nearby?lat=33.0&lon=-97.0&limit=5"
```

`/winlink/gateways`'s `service_codes` accepts a comma-separated list
(`PUBLIC,EMCOMM`) -- defaults to `PUBLIC` alone if omitted, matching
`winlink_api.get_gateway_status()`'s own default. `/winlink/gateways/nearby`
requires `lat`/`lon`; `max_distance_km`/`limit` are optional filters
applied after sorting nearest-first. Neither `/winlink/gateways` nor
`/winlink/gateways/nearby` is a verified Winlink API operation in the
"proximity" sense -- see `winlink_api.py`'s module docstring's "NOT
CONFIRMED" note: the distance sort is plain client-side geometry over
data `gateway/status.json` already returns, not a separate WDT
endpoint. All three endpoints (and the underlying `winlink_api.py`
module) are themselves unverified against the live API with a real
key -- see PROJECT.md's "Winlink Web Service API" milestone.

## Testing

`tests/test_rest.py` runs a real `KAMDaemon` (wired to the same
scripted fakes used elsewhere) plus a real `kamxl_rest` HTTP server
pointed at it, talked to with real HTTP requests -- the socket and
HTTP layers are exercised for real; only the serial connection
underneath is faked. Included in `python3 run_tests.py`.
