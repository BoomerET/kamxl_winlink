# KAM-XL Python Library — Project Overview

## Roadmap (updated)

The original milestone list below (Terminal Mode robustness, Convers
Mode, packet parsing, monitoring, tests, type hints) is the
foundation -- Milestone 1 here. Direction as of now:

1. **Python library** -- done (this file's original scope, below).
2. **Background daemon** that owns the serial connection to the
   KAM-XL and manages it -- done. `kamxl_daemon.py`: newline-delimited
   JSON over a Unix domain socket, one lock serializing all KAMXL
   access (including a background monitor-broadcast thread, polled in
   short bursts so it doesn't starve ordinary commands), pub/sub
   `monitor.subscribe`/`unsubscribe` for live `Packet` events to any
   number of connected clients. See
   [docs/daemon.md](docs/daemon.md) for the protocol and a documented
   known limitation (monitor traffic and command responses still
   share one physical serial stream on real hardware -- no amount of
   client-side locking changes that). Covered by
   `tests/test_daemon.py`, a real socket/threading integration test
   against the same scripted fakes used elsewhere, and now also
   verified end-to-end against Dave's real KAM-XL (see milestone 3's
   notes below for how that hardware pass went). Single KAM-XL /
   serial port per daemon instance; multi-device support isn't in
   scope yet.
3. **REST API** exposing the daemon's capabilities over HTTP -- done.
   `kamxl_rest.py`: stdlib `http.server` only (no new dependency),
   proxies HTTP requests to the daemon's Unix socket and translates
   its JSON protocol into REST responses (see
   [docs/rest_api.md](docs/rest_api.md) for the endpoint list).
   Binds `0.0.0.0` by default (LAN-accessible, per Dave's choice),
   which meant it needed authentication before that was safe -- a
   bearer token, generated and printed at startup if not supplied,
   checked on every request; `--no-auth` is refused unless bound to
   localhost only. Live monitoring exposed as Server-Sent Events
   (`/monitor/stream`) rather than websockets, so a plain HTTP client
   (or eventually milestone 4's web terminal) can consume it directly
   -- though browser `EventSource` can't set the Authorization header
   this needs, so real browser support for that specifically waits on
   milestone 4. Covered by `tests/test_rest.py`, the same
   fakes-underneath/real-sockets-on-top approach as the daemon's own
   tests. Along the way, found and fixed a real bug in the *test
   harness itself* (not the daemon or REST code): `addCleanup`'s LIFO
   ordering meant `thread.join(2)` was firing before the matching
   `shutdown()` in both `test_daemon.py` and `test_rest.py`, silently
   wasting up to 2 seconds per test on a join that couldn't succeed
   yet -- fixing the registration order (plus tightening
   `serve_forever()`'s default poll interval from 0.5s to 0.1s) took
   the full offline suite from ~35s for 54 tests down to ~7.5s for 74.
   Now running against real hardware -- and found two real bugs doing
   it. First, `kamxl_daemon.py --port` defaulted to `COM8`, so
   starting the daemon without explicitly passing the real device
   (`/dev/ttyUSB2` on Dave's Linux box) silently tried to open a port
   that didn't exist, and every command hung for its full timeout,
   looking exactly like a genuine hardware fault. `--port` is now
   required (or `$KAMXL_PORT`), failing fast with a clear error
   instead. Second, and more subtly: `DaemonClient`'s own socket read
   timeout and the KAM-XL-side command timeout it was waiting on both
   defaulted to 10s, so the REST layer could give up (raising a raw,
   unhandled `socket.timeout` -> 500) at essentially the same moment
   the daemon was still legitimately waiting -- and for `/connect`
   (default 60s) and `/disconnect` (default 30s) specifically, the
   REST socket's fixed 10s timeout meant it would *always* give up
   long before those operations could realistically finish, every
   time. Fixed with a `DaemonTimeout` exception mapped to a proper
   `504`, and per-call socket timeout overrides on `/connect`,
   `/disconnect`, and `/connected/read` that track the caller-supplied
   `timeout` with margin, instead of relying on a single fixed
   default. Covered by `DaemonClientTimeoutTests` in
   `tests/test_rest.py`, exercising the timeout race directly with a
   deliberately slow fake Unix-socket server (no KAM-XL/daemon
   internals needed to prove the fix).

   A third real bug turned up chasing the same hardware session:
   `disconnect_station()`'s internal Ctrl-C-back-to-Command-mode step
   always ran with its own hardcoded 5s timeout, completely ignoring
   whatever `timeout` the caller passed to `disconnect_station()`
   itself. Added a separate `command_mode_timeout` param (default 5,
   unchanged for existing callers), threaded through the daemon and
   `/disconnect`'s REST body, with the REST socket-timeout budget
   updated to cover both sequential steps.

   Both `kamxl_daemon.py` and `kamxl_rest.py` are now confirmed
   working end-to-end against the real KAM-XL (`GET /params/VERSION`
   returning `KAM XL -1.24160- SERIAL NUMBER - 00001D6C2798` over a
   live `curl` call, daemon and REST as separate processes talking
   over the Unix socket exactly as designed). Getting there took a
   genuine debugging odyssey, worth recording since none of it turned
   out to be the code: after ruling out the three real bugs above,
   `/params/VERSION` and `/disconnect` were *still* timing out --
   even a bare, daemon-free `KAMXL` connection in a standalone script
   couldn't get a `cmd:` prompt back, which pointed at the TNC itself
   rather than anything in this codebase. A machine reboot didn't fix
   it either (expected, in hindsight -- that only resets the
   computer, not the separate KAM-XL unit on the other end of the USB
   cable). The leading theory became `INTFACE` being saved as `KISS`
   from earlier Winlink/APRS use, which -- per the manual -- boots
   the unit straight back into KISS mode on every power-up regardless
   of a power cycle, and would explain silence to both plain commands
   and Ctrl-C. Wrote `exitKissMode.py`, a small standalone script that
   sends the raw 3-byte KISS-exit frame (`FEND FF FEND`) outside
   `kamxl.py`'s normal protocol, as a way to test that theory directly
   against hardware. In the end, the actual cause was simpler: the
   post-reboot serial device had shifted (`/dev/ttyUSB2` before the
   reboot, briefly assumed to be `/dev/ttyUSB1` after, actually
   `/dev/ttyUSB0`), and the daemon had been correctly, faithfully
   timing out talking to a port the KAM-XL wasn't even on. `--port`
   being required now (see above) at least turns a *missing* port into
   an immediate error -- a *wrong-but-existing* one still has to fail
   the slow way, which is what happened here.
4. **Web terminal** -- browser-based Terminal Mode session -- done.
   `kamxl_rest.py` now serves it directly: `GET /` returns a single
   self-contained HTML/JS page (no build step, no external
   dependency, consistent with the REST API's own stdlib-only
   philosophy), with a terminal-like input box that POSTs to a new
   `/terminal/exec` endpoint. That endpoint -- and the daemon's new
   `send_command` method underneath it -- is a raw pass-through
   (`KAMXL.send_command()`, no assumption about response shape),
   deliberately distinct from `get`/`get_typed`: it works for any
   command, including ones `kamxl.py` has no typed metadata for at
   all (`BEACON`, `MHEARD`, ...), which is the point of a *terminal*
   as opposed to a structured params API.

   This surfaced the same browser-auth gap already flagged when the
   REST API shipped: `EventSource` (needed for milestone 5's live
   monitor) and a bare `GET /` page load can't set a custom
   `Authorization` header. Solved generally rather than just for the
   terminal page -- `_check_auth()` now also accepts the same token
   as a `?token=` query parameter, checked as a fallback after the
   header. The web terminal page reads `token` from its own URL and
   carries it forward on every request it makes. This also means
   milestone 5 shouldn't need any further backend auth changes when
   it wires up `/monitor/stream` from a browser.

   Covered by `SendCommandTests` in `tests/test_daemon.py` and
   `TerminalTests` plus new query-string-token auth tests in
   `tests/test_rest.py`.
5. **Live packet monitor** -- browser view of `kam.monitor()` traffic
   in real time -- done. Added to the *same* page as the web terminal
   (per Dave's call -- one URL, one token, rather than a separate
   route) as a scrolling feed pane above the command box: time, port,
   source -> destination (with any digipeat path), payload, driven by
   an `EventSource` against the `/monitor/stream` endpoint milestone
   3 already built. This is what the query-string `?token=` auth
   fallback added in milestone 4 was actually for -- no further
   backend auth work was needed to wire it up, as expected. A status
   indicator reflects `live` vs `reconnecting...` (`EventSource`
   retries on its own). Doesn't turn `MONITOR` on for you -- the page
   just displays whatever's already flowing, same as the daemon's
   monitor thread always has; documented in
   [docs/rest_api.md](docs/rest_api.md) so an empty feed isn't a
   surprise. Covered by new `test_page_served_at_root` assertions and
   `test_stream_accepts_query_string_token` in `tests/test_rest.py`
   (real SSE response, query-string auth specifically -- the actual
   in-browser rendering isn't unit-testable offline, same limitation
   as the terminal page's JS).

   Testing this against real RF traffic (tuning to 144.39 MHz, the
   North American APRS frequency, to get a reliable stream of real
   packets rather than relying on a single weak DigiPi link) turned
   up two more real, previously-invisible bugs -- the live monitor
   pane had actually never worked against real hardware until both
   were fixed:

   - `DaemonClient.stream_events()` crashed with
     `OSError: cannot read from timed out object` the moment it hit
     its first idle keepalive window. CPython's `socket.makefile()`
     sets a sticky flag the first time a read times out; every later
     read on that same file object then fails immediately instead of
     trying again. The SSE stream would run for one keepalive window,
     die, and get silently replaced by `EventSource`'s own
     auto-reconnect -- discarding anything that arrived during the
     reconnect gap. Fixed with `select.select()`-based polling so
     `readline()` only ever runs once data is already known to be
     waiting, never touching the socket-level timeout path (and that
     sticky flag) at all. `StreamEventsKeepaliveTests` in
     `tests/test_rest.py` reproduces multiple keepalive cycles against
     a real (idle) fake Unix-socket daemon.
   - Bigger: `packet.py`'s `HEADER_RE` never actually matched a
     real-hardware MONITOR line at all, for *any* packet, ever. `MCOM`
     and `MRESP` -- both `ON/ON` by factory default -- append a
     bracketed frame-type tag to every header
     (`K5LRK>BEACON/2: <UI>:`, `WB5NZV>KD5EOC-10,RSSTN*/2: <<C>>:`,
     `KD5EOC-10>WB5NZV,RSSTN/2: <<I00>>:`, and so on for `UA`, `D`,
     `DM`, and numbered/lowercase supervisory frames like `rr1`) that
     the original regex's strict end-of-line anchor had no allowance
     for. Since `PacketParser._process_line()` only appends
     non-matching lines to an *already-pending* packet, and nothing
     had ever matched to start one, every single line -- going all
     the way back to the daemon's original design -- was silently
     dropped. This is exactly why the monitor pane looked empty even
     with `MONITOR` confirmed on, hardware confirmed receiving
     traffic (per DCD and the DigiPi's own log), and the SSE
     connection confirmed `live`: nothing was ever wrong with any of
     those layers, `packet.py` just never recognized a single real
     header line. Found this way -- live, on hardware -- rather than
     from the manual; the manual's tag-format description was only
     actually confirmed against a real captured log once this bug
     surfaced. Fixed by extending `HEADER_RE` with an optional
     `<TAG>:`/`<<TAG>>:` suffix group, and added a new `frame_type`
     field to `Packet` (`None` for untagged/synthetic fixtures, so
     every existing offline test stayed backward compatible
     unchanged) so `"UI"` (an ordinary beacon) can be told apart from
     AX.25 control/supervisory chatter like `"C"`/`"UA"`/`"D"`/`"DM"`/
     `"I00"`/`"rr1"` at a glance -- now shown as a `<TAG>` in the web
     terminal's monitor pane. `RealHardwareFrameTypeTests` in
     `tests/test_packet_parser.py` uses fixtures taken verbatim from a
     live daemon log (single- and double-bracket tags, digipeated and
     plain, connect/disconnect/info/supervisory frames, back-to-back
     header lines with zero payload between them) to lock this in.
6. **BBS with a modern web UI** -- done, scoped down from the
   original idea after research turned up a much better path: the
   KAM-XL already has a full BBS in firmware (PBBS -- mail, bulletins,
   forwarding, SYSOP access), and neither `kamxl.py` nor the daemon
   had any support at all for incoming AX.25 connects or multiple
   simultaneous sessions, which a custom-built BBS would have needed
   from scratch. Given the choice (asked directly rather than
   assumed), the call was to build a **read-only web UI on top of the
   existing firmware PBBS** instead of a new BBS engine -- list
   messages, read one, done. Much smaller, and the actual AX.25
   session handling stays where it's already proven: the firmware.

   Turns out this fits the existing connected-mode primitives
   perfectly. Per the manual, accessing PBBS -- even your own, even
   locally -- is just an ordinary `CONNECT` to `MYPBBS`, and a local
   serial connect gets automatic SYSOP privilege (no password
   exchange). So `KAMXL.list_pbbs_messages()`/`read_pbbs_message()`
   (new in `kamxl.py`) are thin compositions of
   `connect_station()`/`send_connected()`/`read_connected()`/
   `disconnect_station()` -- all four already hardened by earlier
   milestones -- sending `L` or `R <n>` at PBBS's own
   `ENTER COMMAND:` prompt and handing the raw text to a new `pbbs.py`
   (parsing/dataclasses, the same relationship `packet.py` has to raw
   MONITOR text: `PBBSMessageSummary` for a list row, `PBBSMessage`
   for a read message). `kamxl_daemon.py` gained
   `pbbs.list_messages`/`pbbs.read_message`; `kamxl_rest.py` gained
   `GET /pbbs/messages`, `GET /pbbs/messages/<N>`, and a second
   self-contained page at `GET /pbbs` (list + click-to-read, linked
   from the terminal page's header and back).

   **Verified against real hardware**, including a real bug found and
   fixed along the way. The empty-mailbox case came first -- Dave's
   KAM-XL had PBBS enabled with no messages, and `parse_message_list()`
   correctly returned `[]` against the real raw text (added as
   `RealHardwareEmptyMailboxTests` in `tests/test_pbbs.py`). That text
   also revealed two real formatting details the manual didn't show --
   the sign-on banner reads `NNN BYTES AVAILABLE IN NN BLOCKS` (not the
   manual's plain `NNN BYTES AVAILABLE`), and an empty mailbox prints
   `THERE ARE NO MESSAGES` -- neither needed a parser change, since
   `parse_message_list()` already skips any line that doesn't look
   like a numbered message row (worked correctly by design, not luck).

   Testing a populated mailbox surfaced a real bug, though: Dave sent
   himself a message from DigiPi, read it back through the web UI, and
   the last line was missing. Root cause was `read_connected(timeout=N)`
   -- it collects serial data for a fixed wall-clock duration and
   returns whatever arrived, regardless of whether the PBBS had
   actually finished sending. The message's full text (body + closing
   `ENTER COMMAND:` prompt) took slightly longer than the old 5s window
   to arrive, so the tail got cut off. Fixed with a new private helper,
   `KAMXL._collect_pbbs_response()`, that polls `read_connected()` in
   short (max 1s) slices and stops as soon as `ENTER COMMAND` shows up
   in the accumulated text -- the reliable signal that PBBS is done and
   waiting for the next command. `list_pbbs_messages()`/
   `read_pbbs_message()` now call this instead of a single
   `read_connected()` call, and their (and the daemon's and REST API's)
   `read_timeout` default moved from `5` to `10`, since it's now a
   worst-case ceiling rather than a duration always paid in full.
   `CollectPbbsResponseTests` in `tests/test_pbbs.py` locks this in by
   stubbing `read_connected()` to return a real message's text split
   across multiple calls -- with the last line only present in a later
   chunk -- and confirming it's still correctly assembled before
   parsing, plus confirms the polling stops on the very first call when
   the prompt is already present (not always burning the full ceiling).

   Offline coverage (`tests/test_pbbs.py`, plus daemon/REST tests) uses
   the manual's own example lines as fixtures and stubs the
   connected-mode primitives directly for the call-order/argument-flow/
   error-handling behavior, rather than trying to chain a full
   connect+command+disconnect exchange through one fake serial queue
   (`read_connected()`'s "collect for N seconds" semantics doesn't
   compose cleanly that way -- found this the hard way while writing
   these tests, not while testing hardware).
7. **APRS mapping and station database** -- done. Three architecture
   questions were asked upfront (asked directly rather than assumed,
   same pattern as milestone 6): persistence (in-memory only, chosen
   over SQLite -- simplest for an MVP, rebuilds naturally as traffic
   arrives), map rendering (Leaflet + OpenStreetMap tiles from a CDN,
   chosen over a tile-free scatter plot -- worth the one external
   dependency, and it's entirely client-side, loaded by the browser
   viewing the page, not by `kamxl_rest.py` itself), and parsing scope
   (position reports only for the MVP -- status/message/object/weather
   APRS data types are out of scope for now, same "scope the MVP down"
   instinct as milestone 6's read-only PBBS choice).

   New `aprs.py`: `parse_position(payload)` decodes an AX.25 UI-frame
   payload as an APRS uncompressed position report (`AprsPosition` --
   latitude/longitude, symbol table/code, comment, raw timestamp text)
   or returns `None` for anything else, including the *compressed*
   position format (denser base-91 encoding some trackers default to)
   -- deliberately unsupported for now rather than guessed at. Built
   from the public APRS Protocol Reference spec, not the KAM-XL
   manual, since APRS is an open protocol layered on top of ordinary
   AX.25 UI frames, not a KAM-XL-specific behavior.

   New `stations.py`: `StationTracker` decodes `Packet`s into `Station`
   records (one per source callsign -- different SSIDs are distinct
   stations, per normal APRS convention), keeping only the latest
   known position per callsign, no history. Only ever attempts to
   parse ordinary UI frames (`frame_type` is `None` or `"UI"`) --
   AX.25 connect-session control/supervisory frames never carry APRS
   payloads, so those are skipped before parsing is even attempted.

   `kamxl_daemon.py` gained `stations.list`/`stations.get` and a
   shared `StationTracker` instance, fed by the existing monitor
   thread's decoded packets. This required a real, deliberate
   behavior change to that thread: through milestone 6, it only ran
   while at least one client had called `monitor.subscribe` (started
   on the first, stopped on the last disconnecting) -- fine when
   broadcasting `packet` events was its only job, but a station
   database needs to build up passively over time, whether or not
   anyone has the map open. Asked directly (third question, alongside
   persistence and map rendering) and confirmed: the thread is now
   always on, started the moment a `KAMDaemon` is constructed and
   stopped only by `shutdown()`. `kamxl_rest.py` gained `GET
   /stations`, `GET /stations/<CALLSIGN>`, and a third self-contained
   page at `GET /map` (Leaflet markers, popups with position/comment/
   last-heard, polls `/stations` every 15s), linked from the terminal
   and PBBS pages' headers, and back again.

   **Found while writing tests, not while testing hardware**: making
   the monitor thread always-on broke several existing daemon/REST
   tests that use `CannedSerial` (`ConnectStationTests`,
   `ConnectedModePassthroughTests`, and their `kamxl_rest.py`
   equivalents) -- not a production bug, but a real race in the test
   suite's own fakes. `CannedSerial` hands out its pre-queued response
   chunks to *whichever* caller reads next, with no concept of "this
   chunk is meant for a specific later operation" -- harmless when
   nothing else was reading, but the now-always-on monitor thread
   starts polling the instant a `KAMDaemon` is constructed, well
   before a test's own `connect_station()`/etc. call ever runs, and
   was winning the race to steal the first queued chunk before the
   real operation even started. On actual hardware this isn't
   possible -- nothing arrives on the wire before the command that
   provokes it, unlike a fake with everything pre-loaded up front. Fixed
   by gating `CannedSerial`'s chunks behind at least one `write()`
   having happened first (see `tests/fakes.py`), which makes the fake
   match that reality instead of assuming it's the only reader; three
   low-level `_read_until_any()` tests in `tests/test_connect.py` that
   legitimately read without any preceding write (testing that method
   in isolation) needed one added to match how it's actually invoked
   everywhere else in the codebase.

   **Unverified against a real captured APRS session.** Like PBBS
   before its own real-hardware pass, `aprs.py`'s parsing is a
   best-effort first draft against the spec, not confirmed against
   real traffic. Milestone 5's live monitor pane was already confirmed
   against real APRS traffic at 144.39 MHz, so decoded packets should
   start flowing once this is checked for real -- expect adjustment
   the same way `packet.py`'s `HEADER_RE` and `pbbs.py`'s parsing both
   needed it. 158/158 tests passing at time of writing.
8. **Plugins for Wavelog, Winlink, and Home Assistant** -- Winlink
   done (first pass), scoped down deliberately from "plugins for all
   three." Asked directly (same pattern as every prior milestone)
   which of the three to tackle first, given how different they are;
   Winlink was chosen since it builds on the connected-mode work from
   milestone 6, whereas Wavelog/Home Assistant would each be closer to
   a fresh integration. Wavelog and Home Assistant remain unstarted.

   Unlike every other milestone so far, this one is built on a real,
   separately-documented external protocol (FBB/B2F over AX.25 --
   <https://winlink.org/B2F>, <http://www.f6fbb.org/protocole.html>),
   not something derived from the KAM-XL manual -- there's no "manual
   vs. real hardware" tension here, but the same research-before-code
   discipline applied: both specs were fetched and read in full before
   any code was written, and the one genuinely security-sensitive
   piece -- the secure-login challenge/response algorithm -- was
   ported from `wl2k-go`, a real, widely-used open-source Winlink
   client, and verified against its own published test vectors before
   being trusted (`winlink.SECURE_LOGIN_TEST_VECTORS`,
   `tests/test_winlink.py`) -- not just "matches the spec as written."

   Three scope questions were asked upfront: protocol tier (**plain
   ASCII FBB**, chosen over full binary-compressed B2F -- no LZHUF
   compression codec to port, no binary YAPP-style framing/checksums,
   still genuinely interoperable since B2-capable gateways must stay
   backward-compatible with plain FBB), direction (**receive-only**,
   chosen over send+receive both -- connects, logs in, downloads
   whatever's waiting; composing/sending a new message is a followup,
   mirroring milestone 6's read-only-first PBBS scoping), and test
   target (Dave has a real registered Winlink account, so secure login
   needed to be implemented, not just connectivity).

   New `winlink.py` (parsing/building, mirroring `pbbs.py`'s
   relationship to raw text): `secure_login_response()` (verified, see
   above), SID parse/build (deliberately claiming only `F$` -- ASCII-
   basic + BID, not `B`/`B1`/`B2`, so a real gateway will only ever
   propose plain-text messages to us), `;PQ:`/`;FW:` handshake
   line handling, `FB` proposal line parsing, `FS` response building,
   and Ctrl-Z-delimited message body splitting -- no binary framing at
   the ASCII-basic tier at all. One real, non-obvious consequence of
   the ASCII-only choice, called out explicitly in the module
   docstring and worth repeating here: per the B2F spec, a station
   that can't do B2 only ever receives the plain message body, not
   Winlink's richer structured address header (Mid/Date/From/To/
   Subject/attachments) -- a real capability tradeoff of the scope
   choice, not a bug.

   `KAMXL.check_winlink_mail()` drives the exchange (connect, read
   handshake, send login response + "FF" since we never propose
   outbound mail, read the gateway's proposals or `FQ`, accept
   everything via `FS`, read the message bodies, disconnect) using the
   same connected-mode primitives PBBS uses. This needed a real
   refactor: PBBS's `_collect_pbbs_response()` (poll `read_connected()`
   in short slices until a specific string reappears) only had one
   "are we done yet" condition to check; Winlink's exchange has a
   different one at each of its three stages. Generalized into
   `_poll_until(predicate, timeout)`, with `_collect_pbbs_response()`
   becoming a one-line wrapper around it -- existing PBBS tests still
   passing confirms the refactor didn't change its behavior. MVP scope
   cut: only one proposal block (up to 5 messages) is read per call:
   a real account with more pending mail than that would need a
   second call to fetch the rest, untested since it needs an account
   with that much backlog to actually observe.

   `kamxl_daemon.py` gained `winlink.check_mail` (`password` never
   logged anywhere on this request's path); `kamxl_rest.py` gained
   `POST /winlink/check` and a fourth self-contained page at
   `GET /winlink`. The web UI specifically was held back pending a
   direct question -- asked because putting a real Winlink account
   password into a browser-rendered form over plain HTTP felt like it
   deserved an explicit okay rather than the same assumption already
   made for the API's own bearer token. Dave said yes; the page never
   persists the password (no cookie, no `localStorage`) and clears the
   field after every submit attempt.

   Also found, while implementing the always-different "am I done
   reading yet" logic Winlink's multi-stage exchange needed: nothing
   new to hardware, but a real gap in test tooling was NOT hit here
   the way milestone 7's CannedSerial race was -- this milestone's
   integration tests stub the four connected-mode primitives directly
   (`connect_station`/`send_connected`/`read_connected`/
   `disconnect_station`) from the start, the same pattern PBBS's own
   tests settled on after hitting that exact problem, so Winlink's
   test suite never attempted the shared-queue approach that caused
   it.

   **Unverified against a real RMS gateway.** Everything except the
   secure-login algorithm is a first draft against the spec, not a
   captured live session -- expect adjustment once actually tested,
   the same way `packet.py`'s `HEADER_RE`, `pbbs.py`'s parsing, and
   (still pending) `aprs.py`'s parsing all needed it. 198/198 tests
   passing at time of writing.

   **Update: first real-hardware test, and a real bug found.** Dave
   connected to a real gateway (KD5EOC-10, Denton County Texas EOC)
   with his real Winlink account and password. The SID exchange and
   the `;PQ:`/`;PR:` secure-login challenge-response worked exactly as
   expected -- confirmed live, not just against `wl2k-go`'s test
   vectors. The web app then reported "No mail waiting," which Dave
   confirmed was factually correct (no mail was waiting for his
   account that day).

   But the daemon's verbose log showed something that didn't add up:
   the "winlink proposals raw" text it logged (`;FW: AI6K\r\n
   [kamxl-0.1-F$]\r\n;PR: 14482272\r\nFF\r\n`) was byte-for-byte
   identical to what our own code had just sent as its handshake
   response. The KAM-XL was echoing our own connected-mode
   transmission back to us -- a behavior already known from PBBS's `L`
   command echo, just not yet seen on the Winlink path. And
   `has_end_of_block_marker()` treated a bare `FF` line as "the
   gateway has nothing to propose." Since `check_winlink_mail()`
   always sends its own `FF` right after logging in (receive-only
   design, never proposes anything outbound), the echo of our OWN
   `FF` satisfied that check immediately -- before the real gateway
   had said anything at all.

   To be precise about what this means: the "no mail" answer that day
   was correct, but not because the code actually detected the
   gateway's real `FQ` (no-mail) response -- the bug meant it never
   got that far. It stopped on the echo and returned empty before the
   gateway's genuine reply, whatever it would have been, ever arrived.
   Had there been real mail waiting, this would have silently reported
   "no mail" instead of downloading it -- a real false-negative, not a
   cosmetic issue.

   Fixed by removing the `FF` branch from `has_end_of_block_marker()`,
   leaving only the gateway-only `F>` marker (proposal batch ready) and
   `FQ` (genuinely nothing to send) as valid stop conditions -- per the
   B2F spec, the gateway's real reply to our initial `FF` is always one
   of those two, never another bare `FF` at that point in the exchange.
   This mirrors the same defensive principle already used for PBBS's
   `ENTER COMMAND` marker: only match strings the remote end sends,
   never something we send ourselves. Added a dedicated regression
   test (`test_own_echoed_transmission_not_mistaken_for_gateways_reply`)
   that replays Dave's exact captured handshake/echo text followed by a
   real proposal batch and message body, proving `check_winlink_mail()`
   now waits past the echo and correctly retrieves the real message.
   200/200 tests passing.

   Proposal parsing and message-body extraction against an actual
   populated mailbox remain unverified -- that needs an account with
   real mail waiting, which hasn't happened yet.

   **Update: second real-hardware test, second real bug found.** Dave
   connected to the same gateway again. This time the daemon's
   verbose log showed:

   ```
   08:47:39 conn-4464: connected
   08:47:50 winlink handshake raw: 'Welcome to the Denton County
       Texas EOC\r\n[WL2K-5.0-B2FWIHJM$]\r\n;PQ: 97759037\r\n
       CMS via KD5EOC >\r\n'
   08:48:21 winlink proposals raw: ';FW: AI6K\r\n
       [kamxl-0.1-F$]\r\n;PR: 84304290\r\nFF\r\n*** [3] Use B2
       protocol - Disconnecting (47.190.139.106)\r\n***
       DISCONNECTED\r\ncmd:AI6K>KD5EOC-10/2: <<UA>>:\r\n'
   08:48:26 conn-4464: winlink.check_mail -> KAMTimeoutError:
       Timed out returning to Command mode
   08:48:26 conn-4464: disconnected
   ```

   Two separate things going on here. First: this gateway apparently
   requires B2 protocol support and, seeing our SID claim only
   `F$` (plain ASCII, our deliberate scope choice -- see above),
   printed `*** [3] Use B2 protocol - Disconnecting` and dropped the
   AX.25 link outright, rather than falling back to plain-ASCII FBB
   the way the B2F spec's own text ("if a station cannot support the
   B2 protocol then only the message body is transmitted") seems to
   promise. That's a real limit on this milestone's scope, not a bug
   in our code: as built, this module simply cannot retrieve mail
   from a gateway that enforces B2. The 30-second gap between the
   "handshake raw" and "proposals raw" lines is the proposals poll
   running to its full `read_timeout`, since it never saw the `F>` or
   `FQ` marker it was waiting for -- just this disconnect text.

   Second, and this part *was* a real bug: by the time
   `check_winlink_mail()` reached its `finally`-block
   `disconnect_station()` call, the KAM-XL had already auto-returned
   to Command mode on its own after the remote hangup -- the `cmd:`
   prompt visible at the end of "proposals raw" above was already
   consumed by that same poll. `disconnect_station()` had no way to
   know that, so it sent its usual Ctrl-C and waited for a *new*
   `cmd:` prompt that was never going to arrive (nothing left to
   provoke one), reliably timing out 5 seconds later -- exactly the
   `08:48:21` -> `08:48:26` gap in the log, matching
   `enter_command_mode()`'s default `command_mode_timeout`. The
   result was a confusing `KAMTimeoutError: Timed out returning to
   Command mode` that gave no hint of what actually happened.

   Fixed by adding `winlink.parse_disconnect_reason()`, which checks
   any accumulated poll text for the KAM-XL's own `*** DISCONNECTED`
   banner and, if found, extracts the nearest preceding `***`-prefixed
   line as the stated reason. `check_winlink_mail()` now calls this
   after every poll stage; if it fires, the method raises a clear
   `KAMConnectionError` naming the gateway and quoting the reason
   (e.g. `"KD5EOC-10 disconnected before completing the Winlink
   exchange (\"*** [3] Use B2 protocol - Disconnecting ...\") --
   this can happen if the gateway requires B2 protocol support..."`)
   and skips the now-pointless `disconnect_station()` call. Added a
   regression test,
   `test_gateway_disconnect_mid_session_raises_clear_error`, that
   replays this exact captured exchange and confirms both the clear
   error and that `disconnect_station()` is never called. 204/204
   tests passing.

   This doesn't change the milestone's scope decision, but it does
   sharpen what "receive-only, plain-ASCII FBB tier" actually means in
   practice: it only works against a gateway willing to speak that
   tier to a non-B2 client. Actually retrieving mail from a
   B2-enforcing gateway like this one would mean implementing the B2
   protocol tier -- a real scope expansion, not something to start
   without Dave's go-ahead.

   **Update: B2 protocol tier implemented.** Dave doesn't have access
   to another Winlink gateway to test against, so rather than staying
   blocked on KD5EOC-10 requiring B2, he asked for real B2 support.

   Researched properly before writing code, same standard as the rest
   of this module: the B2F spec's own "Message Structure" and "FBB B2
   Forwarding Protocol Expansion" sections (winlink.org/B2F), the
   underlying FBB binary framing (f6fbb.org's "Binary Compressed
   Forward Version 1" section), and -- for the compression algorithm
   itself, the one piece here that's genuinely intricate bit-level
   code -- two independently-authored reference implementations
   compared line-by-line before writing a single line of the port: the
   official Winlink Development Team's own VB.NET source
   (github.com/ARSFI/Winlink-Compression) and wl2k-go's Go
   implementation (already trusted elsewhere in this project for the
   secure-login algorithm). Both agree exactly on every constant and
   table despite being written independently in different languages by
   different teams -- the same "verify against a trusted reference"
   standard this project holds for the secure-login code, extended to
   the compression codec.

   New module `lzhuf.py`: a from-scratch LZHUF compress/decompress
   port, plus the B2 wire wrapper (2-byte CRC-16 + 4-byte length
   header). One real wrinkle surfaced while cross-checking the two
   references: they compute the B2 CRC-16 in what looks like two
   different ways (VB accumulates over just the real bytes and then
   byte-swaps the result; wl2k-go pushes two extra zero bytes through
   the update function and doesn't swap). Empirically verified (not
   just assumed) that both produce the identical two wire bytes once
   each is written in its own implementation's natural byte order (this
   project's little-endian vs. VB's big-endian) -- see
   `tests/test_lzhuf.py`'s `CRC16CrossCheckTests`. `tests/test_lzhuf.py`
   otherwise round-trips compress()/decompress() across empty input,
   single bytes, highly repetitive text (exercises the LZ77
   back-reference path), all 256 byte values, random incompressible
   data, and text longer than the 2048-byte sliding window (exercises
   window wraparound) -- all pass. What this does NOT prove: real
   byte-for-byte interop with an actual gateway's own LZHUF encoder --
   no Go or .NET toolchain was available in this sandbox to
   cross-compile either reference and compare compressed output
   directly, so that remains open until a real B2 message gets
   captured and tested against.

   `winlink.py` extended: `build_sid()` now claims `B2F$` instead of
   `F$` -- a strict superset (a plain-ASCII gateway still just gets
   `FB` proposals as before, since it doesn't understand the B2 claim
   anyway). New `B2Proposal` (`FC` proposal lines), `parse_b2_blocks()`
   (the binary `SOH`/`STX`/`EOT` block framing that carries LZHUF-
   compressed message bytes -- researched from f6fbb.org and cross-
   checked against wl2k-go's `fbb/b2f.go`, which independently confirms
   the identical framing), `EncapsulatedMessage`/
   `parse_encapsulated_message()` (the real structured Winlink header:
   Mid/Date/Type/From/To/Cc/Subject/Body, per the B2F spec's "Message
   Structure" section), and `parse_any_proposals()` (recognizes both
   legacy `FB` and B2 `FC` proposals in one pass). `WinlinkMessage`
   gained optional fields (`mid`/`date`/`from_`/`to`/`cc`/`subject`/
   `attachments`) populated only for a B2 message -- `None`/empty for
   legacy ascii, preserving the existing shape for anyone already
   using it. New `WinlinkProtocolError` for genuine B2 framing
   violations (bad checksum, unexpected byte, or -- deliberately
   unsupported -- a block mixing both proposal kinds; see below).

   **Attachment scope, asked and answered:** Dave chose "metadata
   only" over "full attachment extraction" when asked -- attachments
   are parsed for name and size (`Attachment`) but their file contents
   are never extracted, avoiding a file-storage design question that
   wasn't asked for. Still a real upgrade over the ascii tier: subject,
   from, to, cc, and attachment names/sizes are all now available where
   before there was only a plain title and body.

   **Mixed-batch scope limit, deliberately not handled:** if a single
   proposal block ever contains both legacy ascii and B2 proposals,
   `check_winlink_mail()` raises `WinlinkProtocolError` rather than
   attempting to interleave two different wire formats. Not expected
   in practice -- a real Winlink gateway that's negotiated B2 with us
   is expected to use `FC` exclusively for its own mail, and even
   wl2k-go itself doesn't bother implementing the legacy ascii/B1
   proposal codes (a literal "// TODO: implement" in its own
   `proposal.go`), so a mature, widely-used real Winlink client already
   made the same call.

   `kamxl.py` extended with `read_connected_bytes()` /
   `_poll_until_bytes()` -- a real gap found while designing this, not
   from a hardware test: the existing `read_connected()` decodes
   through ASCII with `errors="replace"`, which would silently replace
   every compressed byte >= 0x80 (roughly half of all possible values)
   with U+FFFD, irreversibly corrupting LZHUF-compressed data. Every
   text-only stage of the exchange (handshake, proposal lines,
   including a `FC ...` line itself) still uses the existing
   `read_connected()` unchanged -- only the actual binary message-body
   phase after an "FS" accept switches to the new bytes-based path.
   `check_winlink_mail()` now branches: parses whichever proposal kind
   the gateway sent, and for B2 proposals, polls raw bytes, parses the
   binary blocks, decompresses each with `lzhuf.decompress_b2()`, and
   parses the encapsulated header -- for legacy ascii proposals,
   nothing changed.

   Web UI (`kamxl_rest.py`)'s winlink page updated to show the richer
   header (from/to/cc/attachments) when a message has one, falling
   back to the old proposal-based display for a legacy ascii message --
   a pre-existing bug would have shown "FROM undefined" for a B2
   message otherwise, since `B2Proposal` has no `sender` field the way
   `Proposal` does.

   Also fixed while here: `pyproject.toml`'s `py-modules` list was
   missing `aprs`, `stations`, and `winlink` entirely -- a real,
   pre-existing packaging gap from milestones 7 and 8 that running
   tests from the repo root never surfaced (Python resolves imports via
   cwd there), but a real `pip install` of a released package would
   have shipped something that couldn't even `import winlink` (and
   therefore couldn't `import kamxl`, which imports it). Added those
   three plus the new `lzhuf`.

   238/238 tests passing. Still unverified: real end-to-end interop
   with an actual B2-compressed message from a populated mailbox --
   that needs a real test against a gateway with mail actually waiting,
   which hasn't happened yet. Expect the same kind of correction this
   project's other first-drafts needed once tested for real.

   ### Update: third real disconnect cause found -- not a code bug

   A follow-up live test against KD5EOC-10, now with B2 claimed,
   completed the SID exchange, secure login, and B2 proposal
   negotiation successfully (confirming the B2 work above is functioning
   correctly) but then hit a third, different disconnect:

   ```
   *** Unknown client types are not allowed on production servers --
   use cms-z.winlink.org - Disconnecting (47.190.139.106)
   ```

   This is the production Winlink CMS (which KD5EOC-10 proxies sessions
   to) rejecting this module's own client identification -- most likely
   the `kamxl` app name in its SID/`;FW:` line -- against production,
   and pointing at `cms-z.winlink.org` as an apparent separate test/dev
   CMS instance for unrecognized client software. Nothing about the
   wire protocol was wrong here; this is a registration/gatekeeping
   policy on Winlink's infrastructure, unrelated to B2 or to anything
   `check_winlink_mail()` can fix by changing how it parses or frames
   data.

   The one real bug this surfaced: `check_winlink_mail()`'s
   `KAMConnectionError` used to hard-code "this can happen if the
   gateway requires B2 protocol support" as the explanation for any
   mid-exchange disconnect. That was accurate for the *first* real
   disconnect this project hit (B2-required), but became actively
   misleading the moment this second, unrelated cause showed up --
   especially since B2 is now claimed and clearly wasn't the issue this
   time. Fixed: the message no longer guesses a cause. It quotes the
   gateway's own stated reason verbatim (as it already did) and points
   to `winlink.py`'s module docstring, which now has a "KNOWN DISCONNECT
   REASONS" section listing both real causes found in testing so far,
   explicit about which ones code can fix (B2 support) and which one it
   can't (production client-registration gatekeeping).

   Deliberately NOT done here: making this module's SID claim to be a
   different, already-recognized Winlink client to slip past the
   production check. That would be impersonation, not a fix, and is a
   judgment call for Dave to make (e.g. contacting the Winlink
   Development Team about registering `kamxl`, or finding out whether
   `cms-z.winlink.org` is reachable/relevant for AX.25 packet testing as
   opposed to telnet-based client testing) rather than something to
   silently code around.

   238/238 tests still passing (no regressions from the message-text
   change). No commit-side change to protocol behavior -- this update
   is entirely about not misreporting *why* a real disconnect happened.

   ### Update: renamed project to kamxl_winlink, pursuing registration

   Following the finding above, Dave renamed the GitHub repo to
   `kamxl_winlink` and is looking into registering that name with the
   Winlink Development Team as a recognized client type. To match,
   `winlink.build_handshake_response()`'s default `app_name` changed
   from `"kamxl"` to `"kamxl_winlink"` -- this is the actual identity
   string sent to gateways in the `;FW:`/SID line, so it's the one
   thing that mattered for a real chance at passing the production
   CMS's client-type check. `pyproject.toml`'s project URLs, the
   `README.md` clone instructions, and this repo's own `git remote`
   were updated to the new GitHub URL.

   Deliberately NOT done: renaming the Python package/module itself
   (still `kamxl.py`, `import kamxl`, PyPI name `kamxl`, `kamxl-daemon`/
   `kamxl-rest` console scripts) -- Dave confirmed this rename is
   scoped to the Winlink client identity and repo location only, not
   the whole codebase.

   Whether the production CMS will actually accept `"kamxl_winlink"`
   is still unverified -- that depends on Dave's registration going
   through, which hasn't been confirmed as of this writing. If the
   same "Unknown client types are not allowed" disconnect recurs with
   the new name, that means registration hasn't completed yet (or
   needs a different process than assumed).

   238/238 tests passing (two `BuildHandshakeResponseTests` cases
   updated for the new default identity string).

   ### Update: Winlink send support (B2, send-only, text-body-only)

   While Dave's `kamxl_winlink` registration request is pending,
   several other pieces of the roadmap were unblocked by it. Asked
   directly which to tackle (same pattern as every prior milestone):
   Winlink send support -- the biggest remaining capability gap
   (receive-only until now).

   Research first, per this project's standard practice: re-fetched
   both prior sources (f6fbb.org's FBB forwarding protocol page,
   winlink.org/B2F) specifically for the sending/proposing direction,
   which hadn't been needed before, and fetched several more files
   from wl2k-go (`fbb/message.go`, `fbb/mid.go`, `fbb/proposal.go`,
   `fbb/wl2k.go`, `fbb/b2f.go`) to cross-check the outbound side the
   same way the receiving side was built. Two real, non-obvious finds
   came out of that research, not just theory:

   - **Outbound proposal blocks carry a checksum after "F>" that the
     older f6fbb.org ascii-only spec doesn't document at all.**
     wl2k-go's `sendOutbound()`/`handleInbound()` compute a
     two's-complement (mod 256) checksum over every proposal line's
     characters plus a trailing CR, appended as `F> XX` in hex. Since
     wl2k-go is a real, actively-interoperating Winlink client, this
     was trusted over the simpler bare-`F>` examples in the older doc.
     `winlink.build_proposal_block()` implements this for what this
     module sends; `has_end_of_block_marker()` was also updated to
     *tolerate* (though not yet verify) the same suffix on a line
     received FROM a gateway -- a real, previously-latent bug: a
     production Winlink CMS proposing mail to us the same way wl2k-go
     itself would have never matched the old strict `== "F>"` check,
     meaning `check_winlink_mail()` could have silently timed out
     waiting for a marker that had, in fact, already arrived.
   - **MID generation** was ported from wl2k-go's `fbb/mid.go`
     (`generate_mid()`): MD5 hash, base32-encode, first 12 characters.
     Documented explicitly that byte-for-byte compatibility with
     wl2k-go's own output doesn't matter (each client only ever
     recognizes its own MIDs), only the shape (<=12 chars, unique).

   Two scope questions asked directly before writing code (both
   answered, both binding):

   1. **Exchange scope**: a full two-way single-connection exchange
      (send AND receive in one call), or a simpler send-only method?
      Dave chose **send-only** -- `send_winlink_message()` proposes,
      uploads whatever's accepted, then declines whatever the gateway
      offers back rather than also downloading. `check_winlink_mail()`
      remains the way to download, called separately.
   2. **Attachments**: text body only, or real attachment bytes?
      Dave chose **text-body-only** -- no attachments at all. Unlike
      the receive side's metadata-only compromise, there's no
      equivalent middle ground for something this module originates.

   Built: `winlink.OutgoingMessage` (to/cc/subject/body/msg_type/mid),
   `generate_mid()`, `build_encapsulated_message()` (inverse of
   `parse_encapsulated_message()` -- `Mbo:` set to `mycall`, matching
   wl2k-go's own `NewMessage()`, since the B2F spec's own text for that
   field is genuinely ambiguous), `build_b2_proposal_line()`,
   `build_proposal_block()`, `build_b2_block()` (promoted from what was
   originally just `tests/test_winlink.py`'s own fixture-builder
   helper -- turned out to already be exactly the production code
   needed once sending was in scope), and `parse_fs_response()`
   (decodes a gateway's `FS ...` answer; an offset/partial-resume
   answer is treated as a plain accept, since this module has no
   persistent outbound queue to resume from -- documented as a
   deliberate simplification, not a real implementation of that
   feature). `kamxl.py` gained `send_connected_bytes()` (the send-side
   mirror of the earlier `read_connected_bytes()` fix -- `send_connected()`'s
   ASCII encode would otherwise crash outright on LZHUF-compressed
   bytes >= 0x80) and `KAMXL.send_winlink_message()` itself.

   Also worked out from first principles (not fully spelled out
   anywhere in either source for this specific single-round case): the
   session-termination tail after declining the gateway's reciprocal
   proposal. Rather than inventing a new termination convention,
   `send_winlink_message()` mirrors `check_winlink_mail()`'s own
   already-working precedent exactly -- answer with `FS` if there's a
   real proposal to answer, then just call `disconnect_station()`,
   with no separate explicit `FF`/`FQ` text needed first.

   Wired into the daemon (`winlink.send_message` method,
   `kamxl_daemon.py`) and REST API (`POST /winlink/send`,
   `kamxl_rest.py`) matching the established pattern, plus a "Send
   mail" tab on the `/winlink` web page (To/Cc/Subject/body form, same
   password-hygiene posture as the existing check-mail tab -- never
   persisted, cleared after every submit).

   276/276 tests passing, including new round-trip tests (an outbound
   block built by `build_b2_block()` decodes back through the same
   `parse_b2_blocks()`/`lzhuf.decompress_b2()`/`parse_encapsulated_message()`
   pipeline used for received mail, confirming self-consistency) and
   an independent checksum cross-check for `build_proposal_block()`
   (same "write it twice, compare" discipline as `lzhuf.py`'s own CRC-16
   cross-check).

   **UNVERIFIED AGAINST A REAL GATEWAY**, same honest caveat as the B2
   receive path started with: no account with permission to actually
   deliver mail through a real RMS gateway has confirmed this
   round-trips correctly over the air yet. Every piece here is built
   from the same two cross-checked sources as the rest of B2, with the
   outbound checksum specifically found only in wl2k-go -- but that's
   still "matches two independent descriptions of the protocol," not
   "confirmed against a real gateway in the field."

   ### Update: command responses corrupted by embedded MONITOR traffic (confirmed live)

   While still waiting on the `kamxl_winlink` registration, Dave hit a
   real bug through the web terminal: typing `VERSION` came back as a
   `KAMCommandError` whose message was three unrelated MONITOR packets
   glued onto the KAM-XL's own reply --

   ```
   KD5EOC-10>BEACON/2: <UI>:
   Winlink 2000 RMS Packet Server

   KC5GOI-1>ID/2: <UI>:
   KC5GOI-1/R RSSTN/D KC5GOI-7/N

   KC5GOI-1>BEACON/2: <UI>:
   Rosston Digi KC5GOI-1, Alias RSSTN. SW Cooke Co, Texas.

       $
   EH?
   ```

   Retrying the identical command immediately afterward succeeded,
   confirming this was transient RF traffic arriving mid-command, not
   a lasting fault. This is exactly the scenario `docs/daemon.md`'s
   "Known limitation" section had predicted since milestone 7 but
   marked "not yet hit in testing" -- unsolicited MONITOR traffic and
   a command's response share one physical serial stream on real
   hardware, and `kamxl_daemon.py`'s lock fully excludes the
   background monitor-polling thread for a command's entire duration,
   so any packet arriving in that window gets absorbed straight into
   the command's own read buffer instead of being handled separately.
   Confirmed by reading `send_command()`/`_read_until_prompt()`,
   `_monitor_loop()`'s locking, and `packet.py`'s `HEADER_RE` together
   -- not guessed at.

   A second, more consequential finding came out of that same reading:
   because the monitor thread is locked out for the command's whole
   duration, real APRS/station data arriving during that window isn't
   just cosmetically jumbled into the response -- it's permanently lost
   from `stations.py`'s `StationTracker`, never parsed as a packet at
   all. Asked Dave directly how far the fix should go: filter the noise
   out of command responses only, or also recover that traffic into
   `StationTracker` so it isn't lost. Dave chose the simpler **filter
   only** -- the `StationTracker` data-loss aspect remains a known,
   deliberately unaddressed gap.

   Implemented `KAMXL._strip_monitor_lines()`, wired into
   `send_command()` right after decoding the raw response and before
   the echo/`EH?` checks: recognizes each embedded MONITOR packet by
   its header line (the same `HEADER_RE` the monitor thread itself
   uses) and removes the header plus everything up to the next blank
   line, leaving only genuine command-response text behind. Known
   tradeoff, documented and pinned down with a dedicated test
   (`StripMonitorLinesTests.test_monitor_block_with_no_trailing_blank_line_consumes_to_end`):
   if a monitored packet's payload has no trailing blank line before
   real response text, that response text is swallowed too, since
   there's no other signal to tell them apart.

   Regression-tested against Dave's exact captured bytes via a new
   `CannedSerial`-based test in `tests/test_typed_commands.py`
   (`RawCommandTests.test_embedded_monitor_traffic_stripped_from_response`),
   plus a dedicated `StripMonitorLinesTests` class covering the
   no-monitor-traffic case, single- and multi-block removal, and the
   swallowed-response edge case above. `docs/daemon.md`'s "Known
   limitation" section and `docs/api_reference.md`'s `send_command()`
   entry updated to describe the mitigation and its remaining scope.

   282/282 tests passing.

   ### Update: Winlink Web Service API key granted -- new winlink_api.py module

   The `kamxl_winlink` registration Dave submitted earlier (see the
   "renamed project to kamxl_winlink" update above) came through. Rob,
   KM6LBU (Winlink Development Team), replied with an API key granting
   four permissions: `AccountExists`, `GatewayChannelReport`,
   `GatewayListing`, `GatewayProximity` -- explicitly for the
   web-service side (account lookups, gateway listings), not the
   telnet/B2F client-identity question, which Rob's reply said is
   handled separately by the app-name string itself (already sent as
   `kamxl_winlink`, per that earlier update) rather than needing this
   key at all. Per Rob's own instructions, the key must stay out of
   the public repo, and the endpoints "should be queried sparingly" --
   abuse risks revocation.

   This is a genuinely different Winlink surface from everything
   milestone 8 built: HTTPS/JSON to `api.winlink.org`, no AX.25, no
   serial port, no KAM-XL involvement at all. New module,
   `winlink_api.py`, rather than extending `winlink.py` or `kamxl.py`.

   **Research basis, same discipline as every external protocol here**:
   the WDT's own docs page (`https://api.winlink.org/metadata`) turned
   out to be a JavaScript-rendered ServiceStack metadata page this
   session's tooling couldn't fetch (no Chrome extension connected),
   so every endpoint/format detail was instead cross-checked against
   Pat (`https://github.com/la5nta/pat`), a real, actively-interoperating
   open-source Winlink client -- specifically its `internal/cmsapi` Go
   package, read directly from source. Confirmed real from that:
   base URL `https://api.winlink.org`; `GET /account/exists` with the
   key and callsign both in the query string; `POST /gateway/status.json`
   with the key riding in the form-urlencoded *body* instead (a
   genuinely different convention between the two endpoints, kept
   exactly as observed rather than normalized away); and that
   `/account/exists`'s errors arrive in a nested `ResponseStatus`
   object while `/gateway/status.json`'s arrive as a bare top-level
   `ErrorCode` int -- also kept as-is.

   **Could not confirm a separate `GatewayProximity` endpoint.** Pat's
   client never calls one -- `gateway/status.json`'s own response
   already carries `Latitude`/`Longitude` per gateway. Working
   assumption, stated plainly rather than silently guessed at:
   `GatewayProximity` is the permission gating access to *that*
   location data, not a distinct server operation. `nearby_gateways()`
   does the proximity computation client-side (plain haversine
   great-circle distance, independently cross-checked in
   `tests/test_winlink_api.py` against one degree of longitude at the
   equator). **This module is UNVERIFIED AGAINST THE LIVE API** --
   Dave holds the actual key value, so a real confirmation call (e.g.
   `account_exists("AI6K", ...)`, a single cheap read, respecting
   Rob's "query sparingly" ask) can only happen on his end.

   Built: `account_exists()`, `get_gateway_status()` (covers both
   GatewayListing and GatewayChannelReport -- the WDT doesn't split
   them across separate URLs), `Gateway`/`GatewayChannel` dataclasses,
   `nearby_gateways()`. Uses stdlib `urllib` only, no new dependency
   (same choice `kamxl_rest.py` made for `http.server`), with an
   injectable `Transport` callable so `tests/test_winlink_api.py` can
   exercise the real parsing/error-handling logic without ever
   touching the network -- same fakes-over-mocks discipline as
   `tests/fakes.py`'s `ScriptedSerial`/`CannedSerial`, just for HTTP.

   Wired into `kamxl_daemon.py` (`winlink.account_exists`,
   `winlink.gateway_status`, `winlink.nearby_gateways` -- none of
   which touch `self.kam`/`_kam_lock`, since there's no serial port
   involved and holding that lock for an HTTPS round-trip would
   needlessly block ordinary KAM-XL commands) and `kamxl_rest.py`
   (`GET /winlink/account/<CALLSIGN>`, `GET /winlink/gateways`,
   `GET /winlink/gateways/nearby`). The API key itself is read fresh
   from `$WINLINK_API_KEY` on every daemon call -- deliberately no
   `--winlink-api-key` CLI flag (Dave's explicit choice, over a config
   file too), so it never has to touch disk or show up in a process
   listing/shell history.

   312/312 tests passing (30 new: 17 in `tests/test_winlink_api.py`,
   6 daemon-layer, 7 REST-layer -- all against fakes, no real network
   calls in the suite).

   ### Update: optional .env file for WINLINK_API_KEY

   Dave set `WINLINK_API_KEY` for real and confirmed the "Unknown
   client types are not allowed on production servers" disconnect is
   unrelated to this key entirely -- it's a still-unresolved B2F/telnet
   client-identity whitelist issue, separate from the web-service API
   token (see `winlink.py`'s "KNOWN DISCONNECT REASONS" #2). Along the
   way, re-exporting the key by hand before every daemon start got
   old, so he created a (gitignored) `.env` file and asked for it to
   be read automatically instead.

   `kamxl_daemon.py` gained a minimal `_load_dotenv()`, called first
   thing in `main()`: reads `KEY=VALUE` lines from `.env` in the
   current directory (blank lines/`#` comments skipped, quoted values
   unquoted), filling in only whatever isn't already a real
   environment variable -- a real `export WINLINK_API_KEY=...` still
   always wins, same precedence any dotenv tool uses. No third-party
   dependency (same stdlib-only choice as `winlink_api.py`'s `urllib`
   and `kamxl_rest.py`'s `http.server`) -- this doesn't replace the
   env-var-only design from the "Key storage" decision above, just
   adds a convenience on top of it. Silently does nothing if `.env`
   doesn't exist, so nothing changes for anyone not using one.

   318/318 tests passing (6 new, `tests/test_daemon.py`'s
   `LoadDotenvTests`).

   ### Update: enterKissMode.py, to test against Pat directly

   With the B2F client-identity question still open on Winlink's end,
   Dave wants to try Pat (https://github.com/la5nta/pat -- the same
   real client `winlink_api.py`'s own research was cross-checked
   against) directly against the KAM-XL, which means putting it into
   KISS interface mode first. `exitKissMode.py` (an existing
   standalone recovery script, milestone 3) only goes one direction --
   Dave asked for the inverse.

   Per the manual's "KISS Mode" section (checked directly rather than
   guessed at, same discipline as everything else in this project):
   "type INTFACE KISS and press return. Then, send a RESET command, or
   cycle power (off/on)." New `enterKissMode.py`, at the repo root
   alongside `exitKissMode.py`, does exactly that -- but the two
   commands are handled two different ways. "INTFACE KISS" goes through
   `KAMXL.send_command()` as normal (the unit is still in ordinary
   Terminal Mode at that point, so this gets EH?-detection for free).
   "RESET" is sent as a raw write to `kam.serial` instead and read back
   raw, mirroring `exitKissMode.py`'s own reasoning for going around
   `kamxl.py`'s Terminal Mode machinery: once RESET applies the new
   INTFACE setting, the KAM-XL should stop producing a `cmd:` prompt
   entirely (now in KISS framing, not the command loop), and
   `send_command()`'s own timeout path discards whatever partial text
   it read rather than surfacing it -- exactly the sign-on-banner text
   this script wants to show as visual confirmation the switch worked.

   Same persistent-setting caveat as `exitKissMode.py`'s own follow-up
   text raises for the opposite direction: `INTFACE` is a saved
   parameter, so the KAM-XL keeps booting straight into KISS mode on
   every future power-cycle too, not just for one Pat session, until
   it's explicitly set back to `TERMINAL` (`exitKissMode.py`, then
   `INTFACE TERMINAL` + `RESET`) -- spelled out in the new script's own
   module docstring.

   Not yet run against real hardware or an actual Pat session as of
   this writing -- built directly from the manual's own instructions
   for the command sequence, same "verify against something authoritative,
   flag what's still unconfirmed" posture as every other real-hardware
   script in this project.

   ### Update: real APRS symbol icons on the map (milestone 7 extension)

   Dave asked whether the map could show correct APRS icons instead of
   Leaflet's plain default marker. `aprs.py`/`stations.py` had already
   captured `symbol_table`/`symbol_code` since milestone 7, but
   `kamxl_rest.py`'s `MAP_HTML` only ever displayed them as popup text
   -- rendering the full symbol set was explicitly scoped out of that
   MVP. Presented three options (hand-built SVG for common symbols
   only, the full ~190-symbol set from an external icon pack, or
   color-coded dots with no real icons); Dave chose the full external
   icon pack, explicitly accepting a new dependency beyond this
   project's cdnjs-only rule for browser assets, since no APRS icon
   set exists on cdnjs.

   Two real, separately licensed sources, each pinned rather than
   tracking an unpinned branch/tag:

   - **hessu/aprs-symbols** (`github.com/hessu/aprs-symbols`) supplies
     the actual sprite PNGs -- the well-known APRS icon set maintained
     by Heikki Hannikainen/OH7LZB, who also runs aprs.fi. Licensing is
     mixed per-symbol (mostly CC-BY-SA 2.0 original work, some
     public-domain-sourced elements; see that repo's `COPYRIGHT.md`).
     Pinned to commit `f2286a9cd43eb6ba4501250b4c39fff111e3796c`
     (2024-10-10) and loaded by the browser via jsdelivr's GitHub-raw
     CDN -- not vendored into this repo as binary assets.
   - **OK-DMR/aprs-symbols** (`github.com/OK-DMR/aprs-symbols`, MIT
     License, Copyright (c) 2019 Marek Sebera) supplies the lookup
     logic: a small translation table mapping the 94 printable-ASCII
     APRS symbol codes to sprite-sheet row/column, plus the
     `background-position` grid formula. Fetched and read in full
     (`aprs-symbols.js`, `aprs-symbols.css`) rather than assumed; the
     ~15-line result is ported directly into `MAP_HTML`'s own inline
     `<script>` with an attribution comment, not loaded from a CDN.

   New `aprsSymbolPosition()`/`aprsIcon()` functions in `MAP_HTML`
   build a Leaflet `L.divIcon` per station from its `symbol_table`/
   `symbol_code`, replacing the old plain `L.marker(latLng)` call in
   `refresh()`. Two real nuances handled deliberately rather than
   glossed over:

   - APRS symbol tables are literally `/` (primary, sprite table 0) or
     `\` (alternate, sprite table 1) -- any other `symbol_table`
     character means an *alternate table with overlay* (a digit/letter
     meant to be drawn over the base icon). `aprsSymbolPosition()`
     falls back to the alternate table's base icon for that case,
     matching the real APRS convention, but doesn't attempt to draw
     the overlay glyph itself -- a documented gap, not a guess.
   - A symbol code that isn't in the 94-character chart at all
     (malformed/missing data) falls back to Leaflet's plain default
     marker rather than rendering nothing -- same "skip, don't guess"
     posture as `aprs.py`'s own parser.

   The module comment above `MAP_HTML` documents both sources, their
   licenses, and the pinned commit; `docs/rest_api.md`'s "Stations /
   map" section updated to match.

   319/319 tests passing (1 new: `tests/test_rest.py`'s
   `test_map_page_renders_real_aprs_icons`, asserting the pinned
   commit hash and lookup-function names appear in the served page).

   ### Update: fixed /map black-screen regression (Python-string escaping)

   Dave reported `/map` loading to a black screen right after the
   icon commit above, with the terminal link no longer navigating
   back either. Root cause: `MAP_HTML` is a Python triple-quoted
   string, and the icon feature's JS embedded string escapes (`\"`
   for a literal quote, `\\` for a literal backslash) inside it.
   Since that string wasn't declared raw (`r"""..."""`), Python's own
   string-literal parser silently *collapsed* those escapes at import
   time -- before the page was ever served, `\"` had already become a
   bare `"` and `\\` a bare `\` -- turning the symbol lookup table
   into invalid JavaScript. A syntax error anywhere in a `<script>`
   block prevents the *entire* block from running, not just the
   broken statement, which is why it took down everything else the
   script did: `L.map()`/`tileLayer()` never ran (black screen), and
   the header links' `href`s -- also set by that same script -- never
   got assigned.

   This slipped through the previous commit's own verification
   because that check extracted the `<script>` body via regex against
   the raw `.py` *source text* and ran `node --check` on that --
   which validates what was typed, not what Python's own string-
   literal processing actually produces at import time. Confirmed the
   discrepancy directly: `python3 -c "import kamxl_rest; print(kamxl_rest.MAP_HTML)"`
   showed the corrupted array before the fix, correct after.

   Fix: `MAP_HTML = r"""..."""` instead of `"""..."""` -- the only
   backslash usage anywhere in `MAP_HTML` is the APRS lookup table, so
   this has no effect on anything else in the page.

   New regression test, `tests/test_rest.py`'s
   `test_map_page_javascript_is_syntactically_valid`: fetches the
   real *served* `/map` page (not the `.py` source text) and runs the
   actual inline `<script>` body through `node --check`, so it
   validates what a browser would actually receive. Skipped if `node`
   isn't on `PATH` (this project has no other Node/JS-tooling
   dependency); ran for real in this sandbox and passed.

   320/320 tests passing (1 new).

   ### Update: friendly error when an AX.25 KISS daemon holds the port

   Dave uses `exitKissMode.py`/`enterKissMode.py` regularly, and hit a
   real, reproducible failure: if his `kamxl-kiss` systemd service
   (running `kissattach` against the same port) is active, opening
   the port from either script raised a raw, unhelpful Python
   traceback ending in `termios.error: (25, 'Inappropriate ioctl for
   device')`.

   Root cause, not just a guess: once `kissattach` attaches the Linux
   kernel's `N_AX25` line discipline to a tty, a *second* process's
   `tcgetattr()` call on that same device fails with `ENOTTY`
   ("Inappropriate ioctl for device") -- a different failure mode
   from an ordinary "port busy" error (which would show up as
   `EBUSY`), and one pyserial's own exception message gives no hint
   about. Stopping the daemon (which detaches the line discipline
   when it exits) is what actually fixes it; retrying does not.

   New `serial_errors.py` module, shared by both scripts (deliberately
   *not* folded into `kamxl.py`, since `exitKissMode.py` stays
   pyserial-only by design -- see its own module docstring): walks a
   raised exception's chained cause/context (pyserial's
   `SerialException` wraps the real OSError/`termios.error` via
   implicit chaining -- a bare `raise` inside an `except` block, not
   `raise ... from` -- so it shows up as `__context__`, not
   `__cause__`; both are checked) looking for `errno.ENOTTY`, either
   as a real `.errno` attribute or as `args[0]` (how `termios.error`
   itself carries it). When found, returns specific guidance naming
   `kissattach` and the `kamxl-kiss` systemd service Dave described,
   with the exact `sudo systemctl stop kamxl-kiss` command; otherwise
   falls back to the exception's own message unchanged, rather than
   guessing at a cause that isn't actually confirmed.

   Both scripts now wrap their port-opening call (`serial.Serial(...)`
   in `exitKissMode.py`, `kam.connect()` in `enterKissMode.py`) in
   `try`/`except`, print `describe_serial_open_failure(exc)`, and
   exit(1) instead of letting the traceback propagate. Verified
   end-to-end (not just the isolated helper) by monkeypatching
   `serial.Serial` to raise the exact real exception shape and running
   `exitKissMode.py` through `runpy` -- confirmed the AX.25/kissattach
   guidance prints and the script exits cleanly; also confirmed an
   unrelated error (nonexistent port) still falls back to its own
   plain message rather than being misdiagnosed as the KISS scenario.

   `serial_errors` added to `pyproject.toml`'s `py-modules`, same
   packaging-gap lesson as `aprs.py`/`stations.py`/`winlink.py`/
   `winlink_api.py` before it.

   326/326 tests passing (6 new: `tests/test_serial_errors.py`).

   ### Update: dashboard as the new landing page (`GET /`)

   Dave asked where else the webapp could go next; a real home/
   dashboard page was the first pick, with the two documented APRS
   gaps (compressed positions, overlay characters) queued up after it.

   Design question up front, since it affects existing bookmarks:
   should the dashboard *replace* `/` (moving the terminal to
   `/terminal`), or live at a new path with the terminal staying put?
   Asked Dave directly rather than guessing -- he chose replacing `/`,
   accepting that his root bookmark now shows the dashboard and the
   terminal needs its own `/terminal?token=...` bookmark.

   `GET /` now serves `DASHBOARD_HTML`, a new self-contained page
   (same dark/monospace styling as the other four) with four cards:

   - **KAM-XL Link**: `GET /status`'s `connected`/`port`/
     `monitor_subscribers`, refreshed every 15s.
   - **APRS Stations**: `GET /stations`'s count and most-recently-
     heard callsign, refreshed every 15s.
   - **PBBS**: a manual **Check PBBS** button (not auto-fetched) that
     calls `GET /pbbs/messages` and shows a short inline preview.
   - **Winlink**: a static shortcut only, no auto data pull at all.

   The PBBS/Winlink restraint wasn't arbitrary: `/pbbs/messages` and
   Winlink mail checking both drive a real AX.25 connect/command/
   disconnect cycle against actual radio hardware (unlike `/status`
   and `/stations`, cheap passive reads of data the daemon already
   holds in memory) -- auto-triggering either just from loading a
   dashboard would mean surprising on-air activity and a slow page
   load every time someone opens the page. Winlink additionally needs
   a password, which has no business being entered anywhere but its
   own dedicated form, and the Winlink Development Team's own
   instructions ask that the web-service endpoints be "queried
   sparingly" -- another reason not to auto-poll `/winlink/account/...`
   or `/winlink/gateways...` just to populate a card.

   `GET /terminal` now serves what used to live at `GET /` (moved, not
   duplicated -- `TERMINAL_HTML`'s own module comment and `docs/
   rest_api.md` updated to match). All four other pages (terminal,
   PBBS, map, Winlink) gained a `home` nav link back to `/`, and their
   existing `terminal` nav links were repointed from `/` to
   `/terminal`.

   Given the exact class of bug the previous session hit (Python's
   triple-quoted string literal processing silently corrupting
   embedded JS escapes), `DASHBOARD_HTML` was syntax- and runtime-
   checked immediately after writing it, before moving on to the nav
   updates: `node --check` against the actual served `<script>` body,
   plus a small `node -e` shim faking `document`/`fetch`/`L` to
   confirm the status/station-count logic actually runs and populates
   correctly against canned responses. All five pages (dashboard +
   the four existing ones) re-verified the same way after the nav
   changes.

   New `DashboardTests` in `tests/test_rest.py`: page served at `/`,
   links to all four other pages present, a source-level check that
   PBBS/Winlink aren't auto-fetched (only `refreshStatus()`/
   `refreshStations()` run unconditionally; `/pbbs/messages` only
   appears inside the button's click handler; `/winlink/check` doesn't
   appear at all), auth-required check, and the same real
   `node --check`-on-served-content regression test `MAP_HTML` got
   after its own black-screen bug. `TerminalTests` updated: the old
   `test_page_served_at_root` split into `test_page_served_at_terminal`
   (now checks `/terminal`) and `test_root_no_longer_serves_terminal_page`.

   332/332 tests passing (6 new).

   ### Update: the two documented APRS gaps -- compressed positions, overlay icons

   Second half of "where else can we take this webapp" -- Dave's own
   pick, queued up right after the dashboard. Both gaps were already
   explicitly flagged in `aprs.py`'s and `kamxl_rest.py`'s own module
   docstrings from milestone 7, not newly discovered.

   **Compressed position parsing.** APRS has two position encodings:
   human-readable "uncompressed" (`aprs.py`'s original, only
   supported format) and a denser base-91 "compressed" form real
   trackers commonly default to. Rather than trust memory for the
   byte layout and decode math, fetched the actual APRS Protocol
   Reference 1.0.1 PDF (chapter 9) and read it directly -- confirmed
   the fixed 13-byte field layout (symbol table + 4-byte lat + 4-byte
   lon + symbol code + 2-byte course/speed/altitude + 1 compression-
   type byte) and the exact decode formula. Independently
   cross-checked against `aprslib` (github.com/rossengeorgiev/
   aprs-python, a real, widely-used open-source APRS parser) to
   confirm the "leading non-digit means compressed" detection rule
   and that a literal space in the course/speed byte really does mean
   "no data" (the PDF's own text extraction rendered this
   confusingly, as "c = V (space)" -- aprslib's actual code
   (`ord(x)-33 == -1`) confirmed it's a space, not a literal "V").

   Verified the decode math against the spec's own two worked
   examples before writing a single test: the sample compressed field
   `5L!!<*e7` hand-computed by the spec itself as 49°30'00"N/
   72°45'00"W, reproduced exactly by `_base91_decode()` +
   `_decode_compressed_position()`. (Compressed encoding is
   intentionally lossy -- the spec's own encoding example truncates
   `190463*(180-72.75)=20427156.75` to the integer `20427156` before
   base-91 encoding, so decoding it back lands about 1.6 feet off from
   exactly -72.75 -- expected quantization, not a bug, and tests use a
   1e-4-degree tolerance to reflect that rather than the uncompressed
   tests' tight 1e-6.)

   New `_base91_decode()`, `_decode_compressed_position()`,
   `_COMPRESSED_POSITION_RE`/`_COMPRESSED_POSITION_WITH_TIMESTAMP_RE`
   in `aprs.py`; `parse_position()` now tries uncompressed first
   (unchanged logic) and falls back to compressed. One real bug caught
   during implementation, not shipped: the first version of
   `_COMPRESSED_POSITION_RE` explicitly excluded digits from the
   symbol-table character position (mirroring the spec's own "leading
   digit means uncompressed" detection wording too literally) -- which
   would have silently *failed* to parse the common case of a real
   overlay digit in that exact position (e.g. a numbered station).
   Caught by testing against a payload with a `1` overlay character
   before considering the feature done, not after; fixed by relying on
   try-uncompressed-first ordering for disambiguation instead of a
   restrictive character class, matching why that ordering already
   existed.

   Course/speed, pre-calculated radio range, and altitude (the 3 bytes
   after the symbol code) are deliberately not decoded -- `AprsPosition`
   has no field for any of them, matching the uncompressed format's own
   existing scope (its comment-embedded course/speed/PHG extensions
   aren't decoded either). A real, stated boundary, not an oversight.

   **APRS overlay icon rendering.** The map already fell back to the
   alternate table's base icon for a station with an overlay character
   in `symbol_table`, but didn't draw the overlay glyph itself.
   Checked whether `hessu/aprs-symbols` (the sprite source already
   pinned for the base icons) has real overlay artwork before writing
   any code -- fetched its actual README, which says outright: "The
   aprs.fi symbol graphics set does not contain additional symbols for
   overlays yet ... Maybe later!" So loading a nonexistent/placeholder
   `aprs-symbols-SIZE-2.png` was never on the table. Instead, the
   overlay character itself (whatever real byte `symbol_table` holds)
   is drawn as a small text badge in the icon's corner -- new `.aprsOverlay`
   CSS class, `aprsSymbolPosition()` now also returns the overlay
   character, `aprsIcon()` layers the badge `<div>` over the base
   sprite `<div>` when present.

   Since the badge shows a real, over-the-air byte verbatim (not
   necessarily a well-formed digit/letter -- `aprs.py`'s parser
   doesn't validate that, by design), added a proper `escapeHtml()`
   helper to `MAP_HTML` (it didn't have one before, unlike the other
   three pages) and used it for the overlay badge -- verified against
   a deliberately malicious overlay character (`<`) in a Node.js
   runtime shim, confirming it renders as the escaped `&lt;` rather
   than breaking the marker's HTML. Also used the same new helper to
   fix `popupHtml()`'s pre-existing lack of escaping for
   `station.callsign`/`comment`/`symbol_table`/`symbol_code` while
   touching this file anyway -- those come from over-the-air text too
   and had never been escaped, a real gap noticed in passing rather
   than something Dave asked to fix, low-risk enough to just close it.

   Both `aprs.py` and `kamxl_rest.py` continued the same immediate-
   verification discipline the black-screen bug forced into practice:
   syntax- and runtime-checked (`node --check` plus a Node shim
   actually calling `aprsIcon()`/`aprsSymbolPosition()` against real
   and malicious inputs) right after writing the JS, before moving on
   to tests and docs.

   `docs/rest_api.md`'s "Stations / map" section updated (both the
   overlay-badge behavior and the "compressed positions unsupported"
   line, now removed since it's no longer true). New tests:
   `tests/test_aprs.py`'s `Base91DecodeTests` (2) and
   `ParsePositionCompressedTests` (9, covering both data-type-ID
   pairs, the timestamp variant, the no-course/speed space sentinel,
   the overlay-digit case, and two malformed-lat/lon-returns-None
   cases) replace the old, now-obsolete
   `test_compressed_position_not_supported_yet`;
   `tests/test_rest.py`'s `test_map_page_renders_overlay_characters_as_text_badge`.

   343/343 tests passing (11 net new).

---


## Purpose

Not another Winlink client. This is a Python library for controlling the
Kantronics KAM-XL in Terminal Mode, exposing a clean Pythonic API instead of
requiring users to type terminal commands.

Goal: another developer should be able to `pip install` this and:

```python
from kamxl import KAMXL
```

and never need to know how the KAM's terminal protocol works.

## Design Philosophy

Hide the terminal interface behind normal Python objects/methods.

| Terminal command | Pythonic equivalent |
| --- | --- |
| `MONITOR ON/OFF` | `kam.set_typed("MONITOR", (True, False))` |
| `PORT 2` | `kam.set_default_port(2)` |
| `DISPLAY` | `kam.get_configuration()` |

**Core principle:** never write code because the manual says the KAM
behaves a certain way — write code based on observed behavior from the
real hardware whenever the two differ. This is what got us through the
`ECHO ON` issue, prompt timing, `DISPLAY` parsing, and connected-mode
experiments, and should stay a guiding rule.

## Coding Style

Prefer: dataclasses, small methods, lots of docstrings, descriptive
exceptions, type hints (future), readability over cleverness.

Avoid: giant functions, global state.

## Test Hardware

- Kantronics KAM-XL, firmware 1.24160
- Station callsign N5BA (set via `KAMXL_CALLSIGN`), serial port
  `/dev/kamxl` on an Evolve Maestro III laptop (Linux, systemd
  services `kamxl-daemon` / `kamxl-web`). Early development used
  AI6K on COM8 (Windows).
- Port 2: VHF APRS on 144.390 MHz, 1200 baud. Port 1 (HF) is kept
  disconnected for APRS work.
- APRS messaging test partner: Kenwood TH-D75A as N5BA-7
- Live testing on 1200 baud packet
- Everything should be validated against the real unit, not just mocked

## Current Status

Implemented and working:

- Open/close serial port
- Send terminal commands, read responses
- Automatic timeout handling
- Automatic error detection (`EH?`)
- Typed getters/setters
- Multi-port parameters
- Boolean / integer / string conversion
- Automatic handling of `ECHO ON`
- Passive monitoring (`MONITOR`)
- Listening for unsolicited packet traffic (`listen()`)
- Command metadata via `CommandInfo` dataclass
- Read-only protection
- Custom exceptions (`KAMError`, `KAMCommandError`, `KAMTimeoutError`,
  `KAMConnectionError`)
- AX.25 connected mode: `connect_station()` (direct + VIA digipeat),
  `send_connected()`, `read_connected()`, `disconnect_station()`,
  hardened against real hardware races (see milestone 1 below)
- `Packet` dataclass + `PacketParser` (`packet.py`) reassembling
  chunked, non-line-aligned MONITOR text into structured packets
- `KAMXL.monitor()` -- callback and generator-based structured
  monitoring, built on `PacketParser`

## Immediate Next Milestones

1. ~~Make `connect_station()` robust~~ -- done. Fixed a real race
   condition (`_drain_input()`/`_strip_leading_prompt()`) where a
   stale "cmd:" prompt leaked into CONNECT/DISCONNECT banners, and a
   truncation bug (`require_line_end` on `_read_until_any()`) where a
   marker match returned before the rest of the line -- e.g. a VIA
   digipeat path -- had fully arrived. Validated against a direct
   connect (KD5EOC-10), a digipeated connect (via RSSTN), and a hard
   failure (RETRY COUNT EXCEEDED against N0CALL-15).
2. ~~Implement proper Convers Mode~~ -- done. `send_connected()` /
   `read_connected()` validated with a real live interactive session
   against a DigiPi AX.25 Node (URONode), including playing Zork
   over the link.
3. ~~Add packet parser classes~~ -- done. See `packet.py`.
4. ~~Build a `Packet` dataclass~~ -- done. See `packet.py`.
5. ~~Implement monitoring callbacks~~ -- done. `KAMXL.monitor()`
   supports both `callback=` and `for packet in kam.monitor():`.
6. ~~Increase test coverage~~ -- done for now. 39 offline unit tests
   in `tests/` (standard-library `unittest`, no third-party packages
   needed -- run with `python3 run_tests.py`), using a scripted fake
   serial connection to exercise typed getters/setters, echo/`EH?`
   handling, and connect/disconnect edge cases. Includes explicit
   regression tests for the two real bugs found on hardware (stray
   prompt leaking into banners, truncated VIA digipeat lines) so
   they can't silently come back. Still no substitute for real
   hardware testing per the design philosophy above -- this is a
   safety net for regressions, not a replacement.
7. ~~Add type hints throughout~~ -- done for `kamxl.py` and
   `packet.py`. Uses the `typing` module (`Optional`, `Union`, etc.)
   rather than newer `X | None` syntax, for broader Python version
   compatibility. No `mypy` available to statically verify (no
   network access in the sandbox this was built in) -- the offline
   test suite is still what actually proves behavior; re-ran it
   after adding hints and confirmed nothing regressed.
8. Package for PyPI -- scaffolding done. `pyproject.toml` added
   (PEP 621 metadata, flat layout via `py-modules = ["kamxl",
   "packet"]` so the existing `from kamxl import KAMXL` imports used
   by every `*Test.py` script keep working unchanged). Verified with
   `pip install --no-build-isolation -e .` that `kamxl` and `packet`
   import correctly from outside the repo. Not yet published --
   that needs a PyPI account/API token, and this sandbox has no
   network access to PyPI to do a full `python -m build` dry run
   (only got as far as confirming the TOML parses and the modules
   install/import correctly under local setuptools 59.6, which
   predates PEP 621 support -- worth a real `pip install build &&
   python -m build` check on a machine with a current toolchain
   before actually publishing).
9. ~~Write documentation~~ -- done. `docs/quickstart.md`,
   `docs/api_reference.md`, and `docs/troubleshooting.md` (the last
   consolidating the real-hardware quirks found so far: marker
   casing, truncated VIA banners, the stale-prompt race, DIGIPEAT/
   FULLDUP's non-obvious choice types, and MONITOR's sub-filters).
10. ~~Build examples~~ -- done. `examples/`: two `offline_*.py`
    scripts that need no hardware (packet parsing off canned text,
    typed commands off the same scripted fake serial the test suite
    uses) plus three `hardware_*.py` scripts mirroring the
    root-level `*Test.py` pattern but trimmed for reading.

## Long-Term Vision

### Terminal Mode
Complete support: every command, every parameter, typed conversions,
validation, documentation.

### Connected Mode
Behave like a socket:

```python
kam.connect_station("KD5EOC-10", via="RSSTN")
kam.send_connected(...)
kam.read_connected(...)
kam.disconnect_station()
```

### Monitoring
Callbacks and iteration, with packet decoding and filtering:

```python
kam.monitor(callback=my_function)
# or
for packet in kam.monitor():
    ...
```

### Packet Objects
Instead of returning raw text like:

```
KD5EOC-10>WB5NZV,RSSTN*/2:
```

return a structured object:

```python
Packet(
    source="KD5EOC-10",
    destination="WB5NZV",
    digipeaters=["RSSTN*"],
    port=2,
    payload="...",
)
```

Eventually parse UI frames, connected frames, APRS, Winlink, telemetry.

### Streams
Support multiple streams (A/B/C/D) exposed as objects.

### Async Support
Eventually:

```python
async with KAMXL(...) as kam:
    ...

async for packet in kam.monitor():
    ...
```

### Event Callbacks
`on_connect()`, `on_disconnect()`, `on_packet()`, `on_monitor()`,
`on_timeout()`, `on_retry()`.

### Configuration Objects
`kam.get_configuration()` should eventually return a `Configuration`
object with properties instead of a plain dict.

## Future Stretch Goals

APRS helper library, Winlink helper, BBS helper, GPS helper, Morse
helper, remote command helper, NET/ROM support, KISS mode support,
binary protocol support.

## Persistent APRS reception history (PR #10)

Implemented optional SQLite-backed reception history for received
AX.25/APRS packets.

- Disabled by default; enabled with `--history-db` or
  `KAMXL_HISTORY_DB`.
- SQLite WAL database with asynchronous packet recording.
- Persistent hourly reception counts survive daemon restarts.
- Authenticated REST endpoint: `GET /aprs/history`, with optional
  Unix timestamp `since` parameter.
- Dashboard history selector: 24 hours, 7 days, or 30 days.
- Daily aggregation uses browser-local calendar boundaries and
  handles daylight-saving transitions.
- Regression tests cover persistence, daemon integration, REST
  integration, and calendar-day aggregation.

This feature does not change milestone 7's in-memory APRS station
database or introduce historical station-position tracking.

## APRS acknowledgments for sent messages

Outgoing APRS messages from the dashboard now request and track
acknowledgments.

- `format_aprs_message(..., message_id=...)` appends `{ID`; the live
  TX path's canonical-payload check accepts it. Every send gets an ID
  from the new `aprs_outbox.OutboundAprsTracker` (4 base-36 chars,
  clock-seeded so restarts don't reuse recent IDs).
- The monitor thread matches incoming `ackID`/`rejID` -- addressed to
  this station's callsign (`KAMXL_CALLSIGN`, see below), from the
  original addressee, with our ID --
  and marks the message `acked`/`rejected`. Passive only: no serial
  I/O, wrapped so a bookkeeping error can't kill the monitor thread.
- Also handled: third-party (`}`) packets from iGates (how replies
  from APRS-IS services and phone apps reach RF -- also now unwrapped
  for the received-messages inbox, which previously dropped them),
  APRS 1.1 reply-acks (`{MM}AA`), and `ackID}`.
- `aprs.outbox` daemon method / `GET /aprs/outbox`; manual resend via
  `resend_id` on `aprs.send_message` / `POST /aprs/send`, reusing the
  same ID and still requiring all three per-call confirmations. No
  automatic retries.
- Dashboard "Sent Messages" list with Awaiting ack / Acknowledged /
  Rejected badges and a Resend button.
- Sent-message state is in memory only (lost on daemon restart);
  acks themselves are still persisted in `aprs_messages` when
  `--history-db` is on.
- **Verified over the air 2026-10-17** against a Kenwood TH-D75A
  (N5BA-7) on 144.390: dashboard -> D75 message, D75's automatic ack
  matched back to it (status Acknowledged); D75 -> N5BA message shown
  in the inbox and auto-acked by the daemon, D75 marked it
  acknowledged. Messages and ACKs confirmed in both directions.
- Automatic ACKs for messages gated from APRS-IS were initially left
  out; Dave asked for them and they were added later (see "Station
  callsign from .env, and auto-ACK for iGated messages" below).

Tests: `tests/test_aprs_outbox.py` (45).

### No more separate "initialize with radios disconnected" step

Before, every daemon restart (and any terminal command, setting
change, PBBS/Winlink use) left APRS sending locked until the operator
ran `aprs.tx_initialize` with `confirm_radio_disconnected`, which had
no web UI and meant physically disconnecting radios. Dave found that
unworkable. The verification itself only ever writes Ctrl-C and
`STATUS`, nothing that can transmit, so:

- `aprs.send_message` and `aprs.ack_enable` now run that verification
  themselves (`_verify_aprs_session_locked()`) when the session is
  unverified or latched unsafe, then continue. Exclusive-serial and
  monitor-thread checks still come first; the three per-send
  confirmations are unchanged. If STATUS shows A/2 connected (or
  anything unexpected), the send is refused before CONVERS, nothing
  is transmitted, and the state stays unsafe.
- `aprs.tx_initialize` still exists but no longer needs
  `confirm_radio_disconnected`.
- At this point the automatic-ACK worker did not auto-verify (its
  Ctrl-C could knock an operator out of a live connected session).
  Superseded by the next section: it now auto-verifies except when a
  session may be open, in which case it pauses.

### Dashboard auto-ACK switch and Clear Inbox

- Auto-ACK can now be turned on/off from the dashboard (`GET/POST
  /aprs/ack`) or at startup (`--auto-ack`, `KAMXL_AUTO_ACK=1`), with a
  status line (on / paused / off + why, ACKs sent, last ACK).
- The ACK worker now re-verifies the TNC itself when its state is
  unknown -- unless `_aprs_link_may_be_active` says a terminal,
  connected-mode, PBBS or Winlink session may still be open, in which
  case it pauses rather than Ctrl-C'ing into it. A successful
  verification, disconnect, or self-contained PBBS/Winlink cycle
  clears that flag; a failed verification sets it.
- `POST /aprs/messages/clear` / `aprs.clear_messages` deletes stored
  received messages for this station. Inbox auto-loads, refreshes
  every 15 s, and hides ack/rej rows.

- Verified on the air the same night: auto-ACK on, D75 messages to
  N5BA acked and marked acknowledged on the radio. History enabled via
  `KAMXL_HISTORY_DB=/var/lib/kamxl/reception.sqlite3` in `.env`.

Tests: `tests/test_auto_ack_and_inbox.py` (13).

### Station callsign from .env, and auto-ACK for iGated messages

- The station callsign is no longer hard-coded (`N5BA` used to be a
  constant in `kamxl_daemon.py` and the dashboard's inbox URL). It now
  comes from `KAMXL_CALLSIGN` (normally in `.env`) or `--callsign`,
  and is **required**, same fail-fast reasoning as `--port`: a missing
  or wrong callsign stops the daemon at startup, before the serial
  port is opened, instead of silently never matching anything. It
  must equal the KAM-XL's MYCALL exactly, SSID included (`N5BA` and
  `N5BA-10` are different stations to APRS). `KAMDaemon(...,
  station_callsign=None)` -- the library default -- disables the
  inbox, ack matching and auto-ACKs rather than guessing.
- The dashboard inbox and `GET /aprs/messages` now default to the
  daemon's configured callsign instead of a hard-coded `N5BA`.
- Automatic ACKs now also answer messages gated to RF from APRS-IS
  (third-party `}` packets from an iGate -- phone apps, ANSRVR,
  aprs.fi messaging). The ACK is addressed to the originator inside
  the third-party header, not the iGate, and goes out on RF like any
  other ACK; an iGate in range gates it back to APRS-IS. Our own
  packets gated back to RF are never acked. Duplicate copies (iGate +
  digipeaters) are still suppressed by the existing 10 s dedupe.
- Limitation: ACKs, like all our transmissions, go out direct (no
  digipeater path), so they only reach APRS-IS if an iGate hears the
  KAM-XL's radio directly.

Tests: `tests/test_callsign_and_igate.py` (11).

### Digipeater path for transmissions, and a position beacon

Prompted by the 2026-10-18 reception log: nearly everything N5BA
hears arrives via the W5NGU-3 digipeater, yet everything we sent went
out direct (the original "first live transmission must be direct"
rule), so messages and ACKs only reached stations/iGates in direct
range.

- `KAMXL_APRS_PATH` / `--aprs-path` (default `WIDE1-1,WIDE2-1`,
  `DIRECT` for none) now applies to messages, resends, auto-ACKs and
  beacons; `aprs.send_message` accepts a per-request `path` override.
  `aprs_tx.validate_aprs_path()` validates it (max 8 digis, no
  injection); digis without an SSID are now allowed.
- `validate_unproto()` now accepts a `VIA` path on either side of the
  multi-port `/` (e.g. `APRS VIA WIDE1-1,WIDE2-1/APRS VIA ...`). Before,
  a VIA read-back on both ports would have been rejected *as a
  read-back failure*, which skips UNPROTO restoration.
- **Verified on hardware 2026-10-18:** the KAM-XL accepted
  `UNPROTO APRS VIA WIDE1-1,WIDE2-1` and its read-back matched the
  expected two-port form, so path transmissions go out normally. (If
  a future firmware ever shows it differently, the transaction refuses
  to transmit and restores the original UNPROTO -- covered by a test
  with a one-port fake.)
- Position beacon: `aprs.format_aprs_position()` builds an
  uncompressed `=DDMM.mmN/DDDMM.mmW<sym>comment` report (`=` = can
  receive messages, which also lets iGates know to gate messages to
  N5BA). Configured by `KAMXL_LATITUDE`, `KAMXL_LONGITUDE`,
  `KAMXL_BEACON_SYMBOL` (`/-`), `KAMXL_BEACON_COMMENT`,
  `KAMXL_BEACON_INTERVAL` (30 min, min 10), `KAMXL_BEACON=1` to start
  at boot. Daemon methods `aprs.beacon_status/enable/disable/now`,
  REST `GET/POST /aprs/beacon`, `POST /aprs/beacon/now`, and a
  dashboard "Position Beacon" card. The TX path now accepts this one
  position format alongside messages.
- Scheduler thread `kamxl-aprs-beacon`: same pause rule as auto-ACK
  (skip and retry in 60 s while a session may be open), fail-closed
  on a failed transmission.

- **Verified over the air 2026-10-18** with the TH-D75A (N5BA-7):
  - Message `39OR` to N5BA-7 via WIDE1-1,WIDE2-1 was acked. It was
    also iGated to APRS-IS and gated back to RF by W5CGC-10 as a
    third-party packet, reaching the D75 a second time; the D75
    acked that copy too.
  - The D75's ack came back digipeated via KC5JMD-2 and W5NGU-3, and
    again from APRS-IS through W5CGC-10 as a third-party packet; the
    duplicate was ignored and the message stayed Acknowledged.
  - "Beacon now" showed up on aprs.fi as N5BA (house symbol,
    comment "N5BA KAM-XL"), path `WIDE1-1,WIDE2-1,qAR,W5NGU-3` --
    heard on RF and gated to APRS-IS by W5NGU-3.

Tests: `tests/test_path_and_beacon.py` (30).

### Reception watchdog

Prompted by the 2026-10-17/18 night when the IC-91AD sat on top of
the amplifier: for about two hours the KAM-XL decoded only the nearby
TH-D75A (N5BA-7) and nothing from the network, which looked like a
software fault until the log was read.

- The daemon now tracks, for Port 2, when it last heard any station
  other than its own callsigns (any SSID of `KAMXL_CALLSIGN`) and
  when it last heard its own.
- `diagnostics.watchdog` in `status`: `ok` / `starting` /
  `only_own` (own stations heard, nobody else -- desense, local
  noise, or a digipeater outage) / `silent` (nothing at all) /
  `disabled`, with a plain-language `message`.
- Threshold `KAMXL_WATCHDOG_MINUTES` / `--watchdog-minutes`, default
  30, `0` disables, minimum 5.
- A `kamxl-watchdog` thread logs a warning on entering an alert state
  and an info line on recovery (once each, not every check).
- Dashboard: an amber (only own) or red (silent) banner above the
  cards, plus the KAM-XL Link card's reception line.

Tests: `tests/test_reception_watchdog.py` (12).

### Conversations, saved sent messages, automatic resend

**Sent messages persist.** With `--history-db`, every change to a sent
message (send, resend, ack/rej, give-up) is saved to a new
`aprs_outbound` table (one JSON record per message ID, upserted, so
new fields need no migration) through the tracker's `on_change` hook,
and the list is restored at startup. Restored records keep their
status and still match late acks, and ID allocation continues after
the newest restored ID. Deliberately, any resend schedule is dropped
on restore: a restart must never make the radio transmit on its own
for old messages.

**Optional automatic resend.** `KAMXL_AUTO_RETRY=1` / `--auto-retry`
or the dashboard toggle (off by default). For messages sent while it
is on: resend with the same ID after `KAMXL_RETRY_INTERVAL` (60 s),
then 2x, 4x... up to `KAMXL_RETRY_COUNT` (3) resends; after the last
one, wait once more and mark the message `gave_up` ("No ack") while
keeping it pending so a late ack still counts. A matching ack or rej
cancels the schedule. Resends go through a new shared
`_guarded_transmit()` (now also used by the beacon) with the same
rules as the other automatic transmissions: re-verify the TNC when
its state is unknown, pause for 60 s instead while a terminal /
connected / PBBS / Winlink session may be open, and turn auto-resend
off if a transmission fails after touching the TNC.

**Conversation view.** The dashboard's separate Sent and Received
lists are replaced by threads per station (received + sent, sorted by
time) with unread counts, a Reply button that fills the send form,
per-message ack badges and Resend, "Clear all messages", and opt-in
alerts (two-tone chime plus a desktop notification when the browser
grants permission). Read state and the alerts preference are
per-browser conveniences in `localStorage`. Checked in headless
Chromium at desktop and phone widths.

Tests: `tests/test_conversations_persist_retry.py` (23).

### One .env for both programs

`kamxl_rest.py` now reads the project's `.env` too, through a shared
`envfile.load_dotenv()` (moved out of `kamxl_daemon.py`, which keeps
`_load_dotenv` as an alias). The web settings (`KAMXL_REST_API_KEY`,
`KAMXL_REST_HOST`, `KAMXL_REST_PORT`, `KAMXL_SOCKET`) now live in the
same file as the daemon's, so the separate `/etc/kamxl/web.env` and
`web.env.sample` are no longer needed. Real environment variables
still win, so an existing `EnvironmentFile=` keeps working. Also
fixed a packaging gap found on the way: `pyproject.toml`'s
`py-modules` was missing modules the daemon imports (`aprs_tx`,
`aprs_outbox`, `reception_*`) plus the new `envfile`.

### Packet history retention

`received_packets` gained a row per packet heard and never shrank.
`KAMXL_HISTORY_DAYS` / `--history-days` (command-line default 90; `0`
keeps everything; otherwise at least 31 so the 30-day chart is never
truncated) now bounds it. Pruning runs on the reception writer thread
-- the one that owns the inserting connection -- once right after
startup and then every 24 h, deleting in batches of 5,000 so inserts
never wait long. Messages (`aprs_messages`, `aprs_outbound`) are
deliberately never pruned. `aprs.history` reports `retention_days`,
`oldest_packet_at`, `last_pruned_at`, `last_pruned_rows`; the
dashboard history card shows the retention. The `KAMDaemon` library
default stays 0 (keep all); only the command line defaults to 90.

Also: `requires-python` corrected to `>=3.9` (the code uses
`str.removeprefix`; CI proves 3.9, 3.11 and 3.13).

Tests: `tests/test_history_retention.py` (7).

### Web pages moved out of kamxl_rest.py

The five pages (`DASHBOARD_HTML`, `TERMINAL_HTML`, `PBBS_HTML`,
`WINLINK_HTML`, `MAP_HTML`) were ~4,000 lines of HTML/CSS/JS inside
Python string literals, which made them hard to edit (no syntax
highlighting, Python string escaping such as `"\\25CF"` in CSS) and
made `kamxl_rest.py` 5,400 lines long. They now live as real files in
a small `kamxl_web` package:

    kamxl_web/<page>/index.html  style.css  app.js

`index.html` refers to the other two with
`<link rel="stylesheet" href="style.css">` and
`<script src="app.js"></script>`; `kamxl_web.load_page()` replaces
those two tags with the files' contents. The constants keep their
names (`DASHBOARD_HTML = load_page("dashboard")`), so routes and tests
are unchanged, and pages are still single authenticated responses --
separate `/static/` requests wouldn't carry the `?token=`.

The extraction was mechanical and checked byte-for-byte: every
reassembled page was identical to the original string before the
literals were removed. Then all 679 existing tests passed unchanged,
all five pages loaded in headless Chromium with no script errors (the
map's "L is not defined" is only because the sandbox can't reach the
Leaflet CDN), and a real `pip install .` into a fresh venv shipped and
loaded the pages (`packages = ["kamxl_web"]` + package-data in
pyproject). `kamxl_rest.py` is now ~1,400 lines.

Tests: `tests/test_web_pages.py` (6).

### Weather stations on the map

`aprs.parse_weather()` decodes APRS 1.0.1 chapter 12 weather into a
`WeatherReport`: positionless `_MMDDHHMMcDDDsSSS...` reports and
position reports with the `_` symbol (`DDD/SSS` wind then fields).
Fields g/t/r/p/P/h/b/L/l/s are fixed-width, `...`/spaces mean "not
reported", `h00` = 100 %, `b` is tenths of mb, `l` adds 1000 W/m2.
Parsing stops at the first thing that isn't a valid field, so the
unit tag in KC5TIL-3's real `...b10278tRSW` isn't misread as a second
temperature (regression test uses that captured packet). A report
must contain at least one real measurement, so stations that merely
use the WX symbol with a text comment don't decode as empty weather.
Out-of-range wind direction/temperature/humidity are dropped.

`StationTracker` keeps `weather`/`weather_at` per station. Weather
from a positionless report for a station with no position yet is held
(up to an hour, 500 stations) until its position beacon arrives;
weather-only packets update `weather_at` but not `last_heard`, so
position age and expiry behave as before.

Map: weather block in pop-ups and the details panel (°F with °C, mb
with inHg, compass wind direction), a temperature label beside each
weather station, and a "Show Temperatures" toggle remembered in
localStorage. A position-WX station's comment line shows only the text
after the weather fields. Checked in headless Chromium with Leaflet
served locally (the sandbox can't reach the CDN).

Tests: `tests/test_weather.py` (13).

### Bulletins and announcements

- `aprs.bulletin_kind()` classifies addressees per APRS 1.0.1 ch. 14:
  `BLN0-9` bulletin, `BLNA-Z` announcement, `BLN<digit><group>` group
  bulletin, plus `NWS-*`/`NWS_*`.
- `parse_aprs_message()` now reads any 9-character addressee of
  letters/digits/`-`/`_` (needed for `BLN1HOTG`, `NWS-WARN`); sending
  stays strict. Bulletins decode with `kind="bulletin"`, no message
  ID (a `{` in their text is text), so they are never acked or
  matched as acks. The exact-case addressee rule is unchanged.
- They were already being stored in `aprs_messages` (the writer keeps
  every decoded message). `list_bulletins()` groups by (source,
  addressee) with latest text, first/last heard and count -- latest
  text via an explicit subquery, because SQLite's bare-column
  shortcut is undefined with both `MIN()` and `MAX()` (a test caught
  this). `aprs.bulletins` / `GET /aprs/bulletins?days=`,
  `aprs.clear_bulletins` / `POST /aprs/bulletins/clear`.
- Retention: bulletins repeat every few minutes all day, so their old
  copies are now pruned along with packet history (an exception to
  "messages are never pruned", which still holds for messages to or
  from you).
- Dashboard: Bulletins card grouped by sender with type tags, "x N"
  repeat counts, "new" markers (per-browser read state), Mark all
  read, Clear, 1/7/30-day range, and the conversation alerts' chime
  and notification for newly heard bulletins.

Tests: `tests/test_bulletins.py` (13).

### APRS Services card

A dashboard card with templates for WXBOT (weather by ZIP, City,ST,
airport; today/tonight/tomorrow/current/metar), SMSGTE (SMS, phone
normalized to 10 digits or an alias), EMAIL-2 (address or shortcut),
ANSRVR (CQ/join/leave a group) and WHO-IS (callsign lookup). Syntax
checked against the services' own documentation (KI6WJP's WXBOT page,
aprs-is.net's email page, and the "APRS Queries" reference). It only
fills the existing send form -- no new transmit path -- with a live
preview and 67-character limit. Writing its test exposed a real gap:
`format_aprs_message()` rejected `WHO-IS` (it only allowed numeric
SSIDs), so addressees may now carry a 1-2 character alphanumeric
suffix; malformed addressees are still rejected and the live TX
transaction accepts the result.

Tests: `tests/test_aprs_services.py` (3).

### Objects/items on the map, and "Message" from the map

- `aprs.parse_object()` decodes objects (`;` + 9-char name + `*`/`_` +
  timestamp + position) and items (`)` + 3-9 char name + `!`/`_` +
  position), reusing `parse_position()` for both position forms.
  `parse_frequency_info()` reads repeater details from the name and
  comment per the APRS frequency conventions (`147.000-T`, `T110`
  -> 110.9 Hz via the standard CTCSS table, `-060` = -600 kHz, `R30m`),
  limited to amateur repeater bands so stray numbers don't read as
  frequencies.
- `StationTracker` keeps objects in their own table keyed by name
  (`MapObject`, with the current `owner`), drops killed ones, and
  expires them on the same 1-hour rule. They never move the sending
  station's marker but still appear in its recent packets.
  `objects.list` / `GET /objects`; `/stations` is unchanged.
- Map: object/item markers with their APRS symbol, hover name label,
  pop-up with a highlighted frequency line, "Show Objects" toggle.
- "Message" links in station pop-ups, the details panel and object
  pop-ups (to the owner) open `/?to=CALL`; the dashboard pre-fills
  the recipient, opens that conversation, focuses the text box, and
  removes `to` from the address bar.
- Checked in headless Chromium with Leaflet served locally, including
  following a Message link from the map to the filled-in form.

Tests: `tests/test_objects_and_message_links.py` (14).

### "Did it get out?" and phone notifications

**Heard back.** Our own transmissions often come back to us: a
digipeater repeats them (the copy's source is our callsign and its
path has a used `*` hop -- the last starred hop is the digipeater that
sent that copy), or an iGate gates them to APRS-IS and another iGate
sends them back to RF as a third-party packet originated by us (as
W5CGC-10 did on 2026-10-18). The daemon now records both: on sent
messages (`digipeated_by/at`, `igated_by/at`, persisted with the
record, matched by message ID and addressee) and for the beacon
(`heard_back`). Our own packet with no used hop (not digipeated) is
ignored, as is anyone else's copy of the same text. Dashboard: "via
W5NGU-3" and "on APRS-IS" chips on sent messages, a "Heard back" line
on the beacon card. This automates the check done by hand while
troubleshooting the amplifier desense.

**Phone notifications** (`notify.py`). Posts JSON to an ntfy server
(`KAMXL_NTFY_URL` + `KAMXL_NTFY_TOPIC`) from a background thread with
a bounded queue, so a slow server never delays packet handling.
Events: `messages` (to us, including via iGate), `bulletins` (new
ones), `watchdog` (alert and recovery); default messages,watchdog.
Auth: ntfy bearer token and/or Cloudflare Access service-token headers
(Dave's server, notify.n5ba.net, is behind Cloudflare). Secrets come
only from the environment/.env, are never logged or returned by
`notify.status`. Duplicates (same message heard directly and via an
iGate; repeated bulletins) are suppressed for 15 minutes. HTTP
401/403 errors mention the Cloudflare token as the likely cause.
`notify.status` / `GET /notify`, `notify.test` / `POST /notify/test`,
and a dashboard card with a "Send test notification" button.

**Verified 2026-10-18** against Dave's self-hosted ntfy server
(notify.n5ba.net) behind Cloudflare Access, using a Cloudflare Access
service token (`KAMXL_NTFY_CF_ACCESS_CLIENT_ID` / `_SECRET` in
`.env`): the dashboard's "Send test notification" was delivered to his
phone. The same day a real APRS message sent over the air from the
TH-D75A (N5BA-7) triggered the incoming-message alert on his phone
within seconds: the complete RF -> KAM-XL -> daemon -> ntfy ->
phone path works, with exactly one alert. (Duplicate copies of a message, such as a digipeated
or iGated repeat, are suppressed by the 15-minute dedupe; covered by
unit tests.) Heard-back also confirmed on the air the same day: the
beacon card showed "Heard back: repeated by W5NGU-3" after an
automatic beacon.

Tests: `tests/test_heard_back_and_notify.py` (20).

### Map survives a restart; phone-sized pages

**Map restore.** The history database now stores each packet's payload
(new nullable `payload` column on `received_packets`, added to
existing databases automatically at startup; older rows stay NULL).
When the daemon starts with a history database it replays the last
hour of stored packets -- the same `STATION_MAX_AGE` (3600 s) after
which stations and objects drop off the map -- through
`StationTracker.update()` with their original receive times. Stations,
trails, weather, objects and recent-packet lists come back as they
were, and still expire on schedule. Only the map is rebuilt: replayed
packets never queue ACKs, send notifications, count toward reception
stats, or touch sent-message state. `raw` isn't stored (nothing shows
it), so replayed packets have it empty. A failure here is logged and
startup continues with an empty map. Storage cost: roughly one
payload (typically under 100 bytes) per stored packet, pruned with the
rest by `KAMXL_HISTORY_DAYS`. This supersedes the "in-memory only"
choice from milestone 7.

**Phone-sized pages.** No page had a viewport tag, so phones drew them
at desktop width and shrank them. All five pages now have one; the
dashboard gets full-width cards and tighter margins under 600 px, the
map shows station details as a sheet along the bottom of the screen,
and form fields use 16 px text so iOS doesn't zoom in on tap. Checked
in headless Chromium at 390 px: no sideways scrolling on any page.
Kept deliberately small (no installable app/PWA); Dave prefers RF.

Tests: `tests/test_station_restore.py` (8), viewport check in
`tests/test_web_pages.py`.

### Coverage by station setup

Dave runs different radios and antennas (a mag-mount on a cookie sheet
in the garage, a J-pole at 16 ft in the backyard) and wants to compare
them. `aprs_coverage.py` (named so it can't clash with the `coverage`
test tool) plus history-database tables:

- `settings` holds the current **station setup** label, set from the
  dashboard and kept across restarts. Every received packet gets it
  (new `received_packets.setup` column, stamped when the packet is
  queued) and so does every transmission (`own_tx`: beacons,
  messages, resends).
- `own_heard`: each time a transmission comes back -- repeated by a
  digipeater or sent back from APRS-IS by an iGate -- it is credited
  to the latest matching transmission within 15 minutes (beacon
  payload / message ID), once per digipeater per transmission.
- The digipeater credited is the **first hop** -- the one that heard
  us directly. The KAM-XL (like most TNCs) stars only the last
  repeater, so every hop before the last `*` is used too, and the
  first path entry is the first hop; an alias there (WIDE1*) means a
  digipeater that didn't insert its callsign, recorded as "". The
  existing "Heard back" display still shows the last repeater.
- `coverage_report()` per setup: sent, % digipeated, % reached
  APRS-IS, which digipeaters heard us (with % and distance from their
  last stored position), and the receive side -- packets/hour over
  hours with traffic, stations heard, stations heard direct, and the
  farthest direct stations (distance from stored payloads; needs
  `KAMXL_LATITUDE`/`LONGITUDE`). Old rows with no setup group as
  "(no setup)".

RPCs `coverage.status`, `coverage.set_setup`, `coverage.report`; REST
`GET /coverage?days=N`, `POST /coverage/setup`. Dashboard Coverage
card: setup box (with past setups suggested), period, and up to four
setups side by side. Map: `/map?coverage=<setup>&days=N` draws lines
from home to each digipeater, green 75%+, amber 25%+, red below.

Caveats worth remembering when reading the numbers: a digipeater that
only heard another digipeater's copy is not counted (on purpose); a
transmission nobody repeated may still have been heard by stations
that don't digipeat; and the receive numbers depend on band activity
as much as the antenna, so compare setups over similar periods.

Tests: `tests/test_coverage.py` (13).

### Packet log search

A sixth page, `kamxl_web/log/` at `/log`, linked from every header and
from a station's details on the map ("Packet log").
`ReceptionHistory.search_packets()` filters stored packets by sender
(exact, any SSID, or `W5N*` prefix; also the originator inside a
gated `}` packet), digipeater anywhere in the path (`json_each` over
the stored path), literal payload text, period and setup; newest
first, paged by row id. `station_summary()` answers "when did I last
hear X and through which digipeater": first/last heard, count, how
many direct, SSIDs seen, and the transmitting digipeater of each copy.

"Transmitting digipeater" is now `aprs_coverage.last_hop()`: the last
*callsign* among the used hops, because in `W5NGU-3,WIDE1*,WIDE2-1`
the starred WIDE1 is an alias W5NGU-3 used up. The beacon/message
"Heard back" display uses it too (falling back to the alias when no
callsign was inserted); before, that path would have said "WIDE1".

The page keeps the search in the URL (back button, bookmarks, links
from other pages), makes callsigns and digipeaters clickable to
search for them, highlights the matched text, and lays each packet out
as two lines so it reads on a phone. RPC `packets.search`, REST
`GET /packets/search`.

Sizing, measured: about 150 bytes per stored packet including indexes
(100,000 packets = 15 MB), and a text search over 100,000 rows takes
~15 ms, so SQLite is nowhere near its limits here. Considered and
rejected: moving to PostgreSQL/MariaDB (one writer, one machine,
millions of rows at most -- a server adds administration and a
failure point for no gain) and SQLite FTS5 full-text search
(tokenizes words, which suits prose but not substring searches on
callsigns and APRS payloads; LIKE is fast enough).

Tests: `tests/test_packet_log.py` (14), `last_hop` cases in
`tests/test_coverage.py`.

### Daily history backups

Dave switched to keeping all history (`KAMXL_HISTORY_DAYS=0`), so the
one SQLite file is now worth protecting. `db_backup.py`:
`BackupManager` makes a backup with SQLite's online backup API (the
daemon keeps writing), runs `PRAGMA quick_check` on the copy, switches
it to rollback-journal mode for simple restores, gzips it into the
backup folder as `kamxl-history-YYYYMMDD-HHMMSS.sqlite3.gz.partial`,
fsyncs and renames. Only files matching that name pattern are ever
pruned (newest `KAMXL_BACKUP_KEEP` kept).

Schedule: daily at `KAMXL_BACKUP_TIME`; a missed slot (laptop asleep)
runs on the next minute check; no backup for 26 h at startup -> five
minutes after startup; a failure retries hourly. The last good backup
is read back from the folder at startup, so no state is stored.

Destination: Dave's SMB share (`\\192.168.7.246\KAM-XL`). Decided to
let Linux mount it (cifs, fstab with `x-systemd.automount`, `nofail`,
root-only credentials file) rather than speak SMB in the daemon: no
third-party package, the OS handles reconnects, and the share
password stays out of the daemon's environment. Because an unmounted
share is just an empty local folder, a backup onto the same filesystem
as the database is refused unless `KAMXL_BACKUP_ALLOW_SAME_DISK=1`.

Notifications: new `backup` ntfy event (added to the defaults) on the
first failure and on recovery. Dashboard Backups card with Back up
now. RPCs `backup.status`, `backup.now`; REST `GET /backup`,
`POST /backup/now`. Setup and restore steps: `docs/backup.md`.

Tests: `tests/test_db_backup.py` (19), including a backup taken while
another connection keeps inserting packets.

