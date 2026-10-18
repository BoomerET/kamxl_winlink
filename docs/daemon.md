# Background Daemon

Milestone 2. Only one process can hold the KAM-XL's serial port open
at a time, so `kamxl_daemon.py` becomes the single owner of the
connection -- everything downstream (REST API, web terminal, live
monitor, BBS, ...) is meant to talk to *it*, not directly to the TNC.

Single KAM-XL / serial port per daemon instance for now -- see
"Scope" below.

## Running it

```
python3 kamxl_daemon.py --port COM8 --socket /tmp/kamxl.sock
```

Both flags are optional and fall back to environment variables, then
defaults:

| Flag | Env var | Default |
| --- | --- | --- |
| `--port` | `KAMXL_PORT` | *(required -- no fallback)* |
| `--socket` | `KAMXL_SOCKET` | `/tmp/kamxl.sock` |
| `--baud` | `KAMXL_BAUD` | `19200` |
| `--callsign` | `KAMXL_CALLSIGN` | *(required -- no fallback)* |
| `--history-db` | `KAMXL_HISTORY_DB` | *(off)* |
| `--history-days` | `KAMXL_HISTORY_DAYS` | `90` (`0` = keep all, else min 31) |
| `--auto-ack` | `KAMXL_AUTO_ACK=1` | *(off)* |
| `--aprs-path` | `KAMXL_APRS_PATH` | `WIDE1-1,WIDE2-1` (`DIRECT` = none) |
| `--latitude` / `--longitude` | `KAMXL_LATITUDE` / `KAMXL_LONGITUDE` | *(none -- beacon unavailable)* |
| `--beacon-symbol` | `KAMXL_BEACON_SYMBOL` | `/-` (house) |
| `--beacon-comment` | `KAMXL_BEACON_COMMENT` | *(empty, max 43 chars)* |
| `--beacon-interval` | `KAMXL_BEACON_INTERVAL` | `30` minutes (min 10) |
| `--beacon` | `KAMXL_BEACON=1` | *(off)* |
| `--watchdog-minutes` | `KAMXL_WATCHDOG_MINUTES` | `30` (`0` = off, min 5) |
| `--auto-retry` | `KAMXL_AUTO_RETRY=1` | *(off)* |
| `--ntfy-url` | `KAMXL_NTFY_URL` | *(off)* |
| `--ntfy-topic` | `KAMXL_NTFY_TOPIC` | *(off)* |
| `--ntfy-events` | `KAMXL_NTFY_EVENTS` | `messages,watchdog` |
| *(env only)* | `KAMXL_NTFY_TOKEN`, `KAMXL_NTFY_CF_ACCESS_CLIENT_ID`, `KAMXL_NTFY_CF_ACCESS_CLIENT_SECRET`, `KAMXL_NTFY_CLICK_URL` | *(none)* |
| `--retry-count` | `KAMXL_RETRY_COUNT` | `3` (1-5) |
| `--retry-interval` | `KAMXL_RETRY_INTERVAL` | `60` seconds (min 30), doubling |

`--callsign` is this station's APRS callsign, exactly as the KAM-XL's
MYCALL (SSID included, e.g. `N5BA` or `N5BA-10`). It decides which
received messages show in the inbox and get auto-acked, and which
incoming acks match messages you sent. Like `--port`, it has no
default: the daemon refuses to start without it.

`--auto-ack` turns automatic APRS acknowledgments on at startup (put
`KAMXL_AUTO_ACK=1` in `.env` to make that permanent); it can also be
toggled from the dashboard. If the TNC check fails at startup the
daemon keeps running with auto-ACK off and logs why.

`--aprs-path` is the digipeater path for everything the daemon
transmits on APRS: messages, resends, automatic ACKs and beacons. A
per-request `path` on `aprs.send_message` overrides it.

The position beacon needs `--latitude` and `--longitude` (decimal
degrees, west/south negative). `--beacon` starts it at boot; it can
also be switched on/off or fired once from the dashboard. All beacon
settings are validated before the serial port is opened.

The reception watchdog warns (in the log and as a dashboard banner)
when nothing has been heard on Port 2 for `--watchdog-minutes`, or
when only this station's own callsigns (any SSID of `--callsign`,
e.g. a handheld) have been heard -- the symptom of a desensed
receiver or a digipeater outage. It logs once on entering the alert
and once on recovery.

Automatic resend (`--auto-retry`, or the dashboard toggle) resends a
message that hasn't been acknowledged, reusing its message ID: first
after `--retry-interval` seconds, then waiting twice as long after
each resend, up to `--retry-count` resends. After the last one it
waits once more and then marks the message "No ack". It follows the
same pause rule as auto-ACK and beacons and turns itself off if a
resend transmission fails. With `--history-db`, sent messages are
saved and restored after a restart, but resends are never resumed
after a restart.

`--history-days` limits how long per-packet history is kept. The
writer thread deletes older rows at startup and then once a day, in
small batches so recording never stalls. Received and sent messages
are not pruned (use Clear on the dashboard); repeated bulletin
copies are. SQLite reuses the freed
space for new rows; the file itself doesn't shrink unless you run
`sqlite3 reception.sqlite3 VACUUM` with the daemon stopped.

Phone notifications go to an ntfy server (ntfy.sh or self-hosted) as
JSON posts from a background thread: messages addressed to you
(including ones gated from APRS-IS), optionally new bulletins, and
reception-watchdog alerts/recoveries. Duplicate copies of the same
message or bulletin are suppressed. The token and Cloudflare Access
secrets are read only from the environment/.env -- deliberately not
command-line flags, which show up in process listings -- and are never
returned by the API or logged.

`--port` has no hardcoded default on purpose: a wrong or missing
serial device should fail immediately with a clear error, not
silently try to open a port that isn't there and hang every
subsequent command until its timeout (a real bug found the hard way
-- see PROJECT.md's milestone 3 notes).

`--baud` must match the KAM-XL's own host baud rate (`HBAUD`) or
every command will time out instead of failing fast with a clear
mismatch error -- another real gap found the hard way: a firmware
flash can leave the KAM-XL at a different host baud than before (e.g.
38400 instead of the usual 19200), and this daemon previously had no
way to be told about that short of editing code. If unsure what the
KAM-XL is actually set to right now, check with a plain serial
terminal (e.g. `minicom`) first -- see
[troubleshooting.md](troubleshooting.md#every-command-times-out-after-a-firmware-flash)
for how to reset it to a known rate with `ABAUD`.

### Winlink API key

`winlink.account_exists`/`winlink.gateway_status`/`winlink.nearby_gateways`
(see the methods table below) need an API key issued by the Winlink
Development Team, read from the `WINLINK_API_KEY` environment
variable -- deliberately no `--winlink-api-key` flag, chosen
specifically so the key never has to touch a config file or show up
in a process listing/shell history. Read fresh on every call (no
daemon restart needed to pick up a change). If unset, those three
methods raise a clear `KAMError` rather than failing some other way;
every other method works fine without it -- it's only needed for
those three.

```
export WINLINK_API_KEY=...
python3 kamxl_daemon.py --port COM8 --socket /tmp/kamxl.sock
```

**Or, a `.env` file** (added after the above, once re-exporting the
key by hand every startup got old): `main()` calls a minimal built-in
`_load_dotenv()` before anything else, which reads `KEY=VALUE` lines
out of a `.env` file in the current directory, if one exists, and
fills in whatever isn't already a real environment variable --

```
# .env (gitignored -- confirm it's actually in your .gitignore before
# creating this; the repo's own .gitignore already lists it)
WINLINK_API_KEY=your-key-here
```

A real exported `WINLINK_API_KEY` always wins over the file (standard
dotenv precedence), so this is purely a convenience layered on top of
the env-var design above, not a replacement for it -- no third-party
`python-dotenv` dependency, no interpolation or line-continuation
support, just `KEY=VALUE`/blank lines/`#` comments. See
`_load_dotenv()`'s own docstring in `kamxl_daemon.py` for the exact
parsing rules.

**To confirm a key actually works** without starting the daemon or
REST server at all: `python3 examples/winlink_api_check.py [CALLSIGN]`
-- makes exactly one cheap `account_exists()` call (same "query
sparingly" discipline as the WDT itself asked for) and reports whether
the key, network path, and parsing are all working end-to-end. See
[examples/README.md](../examples/README.md) for more.

Runs in the foreground; `Ctrl-C` (or `SIGTERM`) closes the KAM-XL
connection, removes the socket file, and exits cleanly.

Logs connections, disconnections, and each request's outcome to the
terminal by default (`INFO` level). Add `-v`/`--verbose` to also log
individual packet broadcasts, and the raw text pulled off the wire
before it's even parsed (`DEBUG` level) -- both off by default since
a busy MONITOR session could otherwise flood the terminal. The raw
line is useful for telling "nothing arrived" apart from "something
arrived but didn't look like a packet header" -- e.g. a line
`packet.py`'s `HEADER_RE` doesn't recognize for some other reason.
(A real, since-fixed version of that exact failure mode: an earlier
`HEADER_RE` had no allowance at all for the KAM-XL's own
`<C>`/`<UA>`/`<UI>`-style control-packet annotations, shown by default
via `MCOM`/`MRESP` -- meaning *no* real-hardware MONITOR line ever
matched, silently. See PROJECT.md's milestone 5 notes.)

```
20:29:03 conn-5344: connected
20:29:03 conn-5344: ping -> ok
20:29:03 conn-5344: disconnected
```

## Protocol

Newline-delimited JSON over a Unix domain socket, in both directions.

**Request** (client -> daemon):

```json
{"id": "1", "method": "get_typed", "params": {"command": "MYCALL"}}
```

**Response** (daemon -> client, matches the request's `id`):

```json
{"id": "1", "ok": true, "result": ["N5BA-10", "N5BA-10"]}
```

or, on failure:

```json
{"id": "1", "ok": false, "error": {"type": "KAMError", "message": "..."}}
```

**Event** (daemon -> client, no `id` -- only sent to connections that
have called `monitor.subscribe`):

```json
{"event": "packet", "data": {"source": "KD5EOC-10", "destination": "BEACON", "digipeaters": [], "port": 2, "payload": "...", "raw": "..."}}
```

A single connection can send multiple requests over its lifetime and,
if subscribed, will see `packet` events interleaved with its request
responses -- match on `id` (present only on responses) to tell them
apart, the way the test client in `tests/test_daemon.py` does.

Tuples (multi-port values, `Packet.digipeaters`, etc.) cross the wire
as JSON arrays; send them back as JSON arrays too (the daemon converts
list -> tuple before handing values to `kamxl.py`).

## Methods

Mirrors the `KAMXL` API from [api_reference.md](api_reference.md) --
see that page for what each one actually does. `params` keys match
the corresponding Python method's arguments; optional arguments may be
omitted to use their default.

| Method | `params` | 
| --- | --- |
| `ping` | *(none)* |
| `status` | *(none)* -- returns `{"connected": bool, "port": str, "monitor_subscribers": int}` |
| `get` | `command` |
| `set` | `command`, `value` |
| `send_command` | `command`, `timeout` (optional) -- raw pass-through: sends ``command`` verbatim, returns whatever text comes back before the next `cmd:` prompt, no assumption about response shape. Powers the web terminal (milestone 4); `get`/`get_typed` are usually the better fit for anything with known typed metadata |
| `get_typed` | `command` |
| `set_typed` | `command`, `value` |
| `get_configuration` | *(none)* |
| `connect_station` | `callsign`, `via` (optional, string or array), `timeout` (optional) |
| `send_connected` | `text`, `add_cr` (optional) |
| `read_connected` | `timeout` (optional) |
| `disconnect_station` | `timeout` (optional), `command_mode_timeout` (optional, default 5) -- separately bounds the initial Ctrl-C-back-to-Command-mode step, which runs before the DISCONNECT confirmation itself |
| `monitor.subscribe` | *(none)* -- this connection starts receiving `packet` events (the background monitor thread itself is always running -- see "Concurrency model" below -- subscribing only affects whether *this connection* gets events pushed to it) |
| `monitor.unsubscribe` | *(none)* -- stops them |
| `pbbs.list_messages` | `mypbbs` (optional), `connect_timeout` (optional, default 15), `read_timeout` (optional, default 10, worst-case ceiling -- see below) -- drives a full connect/`L`/disconnect cycle against the KAM-XL's own firmware PBBS; returns a list of `PBBSMessageSummary` dicts. Verified against real hardware for both an empty and a populated mailbox. |
| `pbbs.read_message` | `number`, `mypbbs`/`connect_timeout`/`read_timeout` (all optional, same as above) -- connect/`R n`/disconnect; returns a `PBBSMessage` dict, or `null` if the number didn't resolve to a message |
| `stations.list` | *(none, milestone 7)* -- returns every known `Station` (see `stations.py`) as a list of dicts, sorted by callsign. Built passively by the always-on monitor thread decoding APRS position reports -- no `monitor.subscribe` needed. |
| `stations.get` | `callsign` -- one `Station` dict, or `null` if never heard |
| `winlink.check_mail` | `gateway`, `password`, `mycall` (optional), `connect_timeout` (optional, default 60), `read_timeout` (optional, default 30, milestone 8) -- connects to a Winlink RMS Packet gateway, logs in, downloads up to one block of waiting mail; returns a list of `WinlinkMessage` dicts. `password` is never logged anywhere on this request's path (see kamxl_daemon.py's `_m_winlink_check_mail()`). |
| `winlink.send_message` | `gateway`, `password`, `messages` (a list of 1-5 dicts, each with `to` (list), `subject`, `body`, and optionally `cc` (list)/`msg_type`/`mid`), `mycall` (optional), `connect_timeout`/`read_timeout` (optional, same defaults as above) -- send-support extension: connects, logs in, proposes the given message(s) via B2, uploads whatever the gateway accepts, declines whatever the gateway offers back (never downloads -- that's still `winlink.check_mail`'s job). Returns a list of the MIDs actually accepted. Text-body-only, no attachments -- see `winlink.py`'s module docstring's "SEND SUPPORT" note for the full scope, and its UNVERIFIED-AGAINST-A-REAL-GATEWAY caveat. `password` is never logged (see `_m_winlink_send_message()`). |
| `winlink.account_exists` | `callsign` -- calls Winlink's HTTP web-service API (`winlink_api.py`, not the KAM-XL at all -- no serial port, no `_kam_lock`). Returns `true`/`false`. Requires `$WINLINK_API_KEY` to be set (see "Winlink API key" below); raises `KAMError` if it isn't. |
| `winlink.gateway_status` | `mode`/`history_hours`/`service_codes` (all optional, see `winlink_api.get_gateway_status()`) -- same API, same `$WINLINK_API_KEY` requirement. Returns a list of gateway dicts (callsign, location, channels). |
| `winlink.nearby_gateways` | `latitude`, `longitude`, `max_distance_km`/`limit` (optional) -- fetches the gateway listing, then sorts by client-side great-circle distance (see `winlink_api.py`'s "NOT CONFIRMED" note -- this isn't a separate Winlink API call). Returns a list of `{"gateway": {...}, "distance_km": float}`. |

`read_timeout` is a worst-case ceiling, not a fixed wait: the KAM-XL
library polls the connected-mode response in short slices and returns
as soon as the PBBS's `ENTER COMMAND:` prompt reappears, rather than
always waiting out the full duration. This fixed a real bug where a
message that took slightly longer than the old fixed 5s window to
arrive had its last line silently truncated.

Unknown methods and missing required params come back as an
`ok: false` response (`error.type` of `KAMError` or `MissingParam`
respectively) rather than closing the connection.

## Concurrency model

Every direct call into the wrapped `KAMXL` instance -- including the
background monitor loop -- goes through a single lock, since only one
Terminal Mode transaction can be in flight on the wire at a time.
Multiple client connections are handled concurrently (one thread per
connection), but their actual `KAMXL` calls are serialized behind that
lock.

Monitoring runs in its own background thread. Rather than holding the
lock for the whole loop, it polls in short bursts (~50ms), briefly
acquiring the lock each time -- so ordinary `get`/`set` requests can
still interleave with it instead of blocking for its entire duration.

**Always on, since milestone 7.** Through milestone 6, this thread
started on the first `monitor.subscribe` and stopped once the last
subscriber disconnected or unsubscribed -- fine when broadcasting
`packet` events to a live monitor pane was its only job. Milestone 7
added a station database (`stations.py`) that needs to build up
passively from whatever MONITOR traffic the KAM-XL sees, whether or
not anyone has the map page (or any monitor client) open -- so the
thread now starts the moment a `KAMDaemon` is constructed and only
stops on `shutdown()`. `monitor.subscribe`/`unsubscribe` still control
whether *that connection* receives `packet` events, but no longer have
any bearing on whether the thread itself runs.

### Known limitation

Brief lock acquisition keeps *this process's* commands and monitor
polling from stepping on each other, but it can't change what's
already true of the KAM-XL itself: unsolicited MONITOR traffic and a
command's response share one physical serial stream on real hardware.
If a monitored packet happens to arrive on the wire in the gap between
sending a command and its response finishing, no amount of
client-side locking prevents it from showing up interleaved with (or
folded into) that response -- this is a pre-existing characteristic of
`kamxl.py`'s Terminal Mode handling, not something introduced by the
daemon.

**Confirmed live**, milestone 9: typing `VERSION` through the web
terminal returned a `KAMCommandError` whose message was three
unrelated MONITOR packets (a Winlink gateway beacon and a digipeater
ID/beacon) glued in front of the KAM-XL's actual `EH?` reply. Retrying
the same command immediately afterward succeeded -- confirming this
was transient RF traffic arriving mid-command, not a lasting problem
with the command itself. `kamxl.py`'s `send_command()` now calls a new
`_strip_monitor_lines()` helper that recognizes and removes embedded
MONITOR packet headers/payloads (using the same `HEADER_RE` the
monitor thread itself uses) before the response is otherwise touched,
so a stray packet no longer corrupts the command's own response text.

This is a mitigation, not a fix for the underlying constraint: the
shared-serial-stream characteristic described above is still true, and
`_strip_monitor_lines()`'s block-boundary heuristic (a monitor block
ends at the next blank line) has a known edge case -- if a monitored
packet's payload has no trailing blank line before genuine response
text, that response text is swallowed too (see
`StripMonitorLinesTests.test_monitor_block_with_no_trailing_blank_line_consumes_to_end`
in `tests/test_typed_commands.py`). More consequentially, MONITOR
traffic that arrives during a command's execution is still **not**
recovered into `stations.py`'s `StationTracker` -- it's discarded, not
redirected, since the monitor thread is fully locked out for the
command's duration (see "Concurrency model" above). Recovering that
data was considered and deliberately deferred for this round in favor
of the simpler filter-only fix.

Milestone 7's always-on monitor thread makes this limitation more
likely to actually surface in practice than it was through milestone
6 (when it only ran while a monitor pane happened to be open) --
there's now effectively always a background reader polling between
commands. It doesn't introduce any *new* risk beyond what's described
above (the lock still fully serializes each individual command against
the polling thread), just more opportunities for real MONITOR traffic
to legitimately be waiting in the gap right before a command starts.

## Testing

`tests/test_daemon.py` runs a real `KAMDaemon` on a real (throwaway)
Unix socket against the same scripted fake serial connections used
elsewhere in the offline suite, talked to by a real socket client --
the socket and threading layers are exercised for real; only the
serial connection underneath is faked. Included in `python3
run_tests.py`.

## Scope

Single KAM-XL / serial port per daemon instance. Multi-device support
(more than one TNC managed by one daemon) isn't planned until there's
an actual need for it -- see PROJECT.md's roadmap notes for the
reasoning.
