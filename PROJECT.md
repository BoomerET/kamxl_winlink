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
- Callsign AI6K, COM8, Port 2
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
