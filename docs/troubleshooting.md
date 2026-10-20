# Troubleshooting

This project's design philosophy (see the [design history](history.md)) is
to never trust the manual over observed hardware behavior. Several
real bugs were only ever found by testing against a live KAM-XL over
the air. This page documents them, along with what to check if you
run into something similar with your own unit.

## "Connect never seems to succeed / always times out"

The KAM-XL manual's own sample transcripts disagree on the exact text
printed on a successful connect: some show `*** CONNECTED TO
callsign` (upper-case "TO"), others `*** CONNECTED to callsign`
(lower-case "to"), and the message reference appendix shows
`***CONNECTED to call` with no space after `***` at all.

`connect_station()` matches this case-insensitively and tolerates a
missing space (`CONNECT_MARKERS["connected"]` in `kamxl.py`), so this
shouldn't bite you -- but if you're writing your own matcher against
raw `send_command`/`listen()` output instead of using
`connect_station()`, don't hardcode a single exact-case string.

## "The VIA digipeat path in the connect banner is cut off"

Observed on real hardware: a marker like `*** connected to` can match
*before* the rest of the line -- including a `VIA` digipeat path --
has actually finished arriving over serial. Example seen in testing:
`connect_station("KD5EOC-10", via="RSSTN")` returning `"*** CONNECTED
to KD5EOC-10 VIA RS"` with `"STN\r\n"` trickling in a moment later as
if it were unrelated post-connect traffic.

`_read_until_any()`'s `require_line_end=True` option (used by
`connect_station()` and `disconnect_station()`) fixes this: once a
marker matches, it keeps reading for up to `line_end_grace` seconds
(default 0.5s, extended each time new bytes arrive) until the line
actually ends in a newline. If you're calling `_read_until_any()`
directly for something new, pass `require_line_end=True` any time the
text after the marker matters.

## "A stale `cmd:` shows up at the start of a connect/disconnect banner"

A plain `reset_input_buffer()` can race with a trailing `cmd:` prompt
that hasn't fully finished arriving yet -- e.g. right before issuing
`CONNECT`, a leftover fragment of the previous prompt can still land
in the buffer and glue itself to the front of the next read.

`_drain_input()` works around this (clear, wait 50ms, clear again)
and `_strip_leading_prompt()` cleans up anything that still slips
through. Both are used by `connect_station()`,
`disconnect_station()`, and `enter_command_mode()`.

## "connect_station() raises KAMTimeoutError instead of KAMConnectionError on a busy channel"

The manual's busy message is `***(callsign) busy` -- the callsign
sits *between* `***` and `busy`, so it can't be matched as a fixed
string. `CONNECT_MARKERS["busy"]` uses a regex
(`rb"\*\*\*\S*\s*busy"`) instead of a literal string for exactly this
reason. If you add your own markers, remember the KAM-XL likes to
embed variable content (callsigns, ports) inside its status messages.

## "DIGIPEAT or FULLDUP raise a KAMError / ValueError I didn't expect"

`DIGIPEAT` is a *single* value despite looking like it should be
per-port -- the manual's own "Multi-Port" tag is absent from its
entry, and it has three legal states (`ON`, `UIONLY`, `OFF`), not two.
`FULLDUP` *is* Multi-Port, but each port also has three legal states
(`ON`, `OFF`, `LOOPBACK`), not a plain boolean. Both are modeled with
the `choice`/`multiport_choice` types in `COMMANDS` -- if you're
adding a new command and it doesn't behave like a clean two-state
Multi-Port boolean, check the manual entry for its exact value list
and whether "Multi-Port" actually appears on the defaults line before
assuming `multiport_bool`.

## "monitor()/listen() only shows a couple of packets in a long window"

Not necessarily a bug -- `MONITOR` output is filtered further by
several KAM-XL sub-commands (`MALL`, `MBEACON`, `MCOM`, `MRESP`,
`MRPT`, `MXMIT`, plus `SUPLIST`/`BUDLIST`/`LLIST`). Even with
`MONITOR` `ON`, some packet types can still be suppressed. Check
`get_configuration()` for those settings if you're expecting more
traffic than you're seeing. It can also just be a quiet channel --
VHF packet traffic is often sparse outside of scheduled BBS
forwarding windows.

## "Every command times out after a firmware flash"

A firmware flash can leave the KAM-XL's host baud rate (`HBAUD`)
different from what it was before -- observed directly: flashing at
38400 left the unit answering at 38400 afterward, not back at the
usual 19200. `tncstation_daemon.py` (and `KAMXL` itself) has no way to
auto-detect this -- a baud mismatch doesn't produce garbled text or a
clean error, it just looks like the KAM-XL never responds at all,
since every byte is being misread. Check what the KAM-XL is actually
set to with a plain serial terminal (e.g. `minicom`) before assuming
anything else is wrong.

If it's not what you expected, type `ABAUD` at the KAM-XL's *current*
working baud rate, then hit `*` -- the KAM-XL uses that keystroke to
autodetect and lock to whatever baud rate `*` was just sent at,
letting you reset it to a known rate (e.g. reconnect your terminal at
19200 first, then send `ABAUD` and `*`) without needing to already
know or guess the right value. Once it's back to a known rate, either
pass `tncstation_daemon.py --baud <rate>` to match, or set it back to the
project's usual 19200 default so nothing else needs to change.

## General advice

If something behaves differently than the manual says: trust the
hardware, add a regression test in `tests/` with a scripted fake
serial connection reproducing what you saw (see
`tests/test_connect.py` for examples of this pattern), and note the
discrepancy here or in `kamxl.py`'s comments so it doesn't get
"fixed" back to matching the manual later.
