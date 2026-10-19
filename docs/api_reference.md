# API Reference

Covers the public surface of `kamxl.py`, `packet.py`, `pbbs.py`,
`aprs.py`, and `stations.py`. Anything prefixed with `_` is internal
and not covered here (see source comments instead).

## `KAMXL`

```python
KAMXL(port: str, baudrate: int = 19200, timeout: float = 0.25)
```

`port` is passed straight to `pyserial` (e.g. `"COM8"`,
`"/dev/ttyUSB0"`). Nothing touches the wire until `connect()`.

### Connection

| Method | Description |
| --- | --- |
| `connect()` | Open the serial port. No-op if already open. |
| `disconnect()` | Close the serial port if open. Safe to call even if never connected. |
| `is_connected` (property) | `True` if the port is open. |

### Raw commands

| Method | Description |
| --- | --- |
| `send_command(command, command_timeout=10)` | Send a Terminal Mode command, wait for `cmd:`, return the response text (echo and prompt stripped). Any MONITOR traffic from other stations that arrives interleaved with the response is filtered out first (see `docs/daemon.md`'s "Known limitation" section). Raises `KAMCommandError` on `EH?`. |
| `get(command)` | Query a parameter, return its raw string value. |
| `set(command, value)` | Set a parameter to a raw value. |

### Typed commands

| Method | Description |
| --- | --- |
| `get_typed(command)` | Query a parameter known to `COMMANDS`, converted to a Python type (see [Commands](#commands) below). Unknown commands fall back to `get()`. |
| `set_typed(command, value)` | Set a parameter using a normal Python value, validated against its type/choices before writing. Read-only parameters raise `KAMError`. |

### Boolean parameters

| Method | Description |
| --- | --- |
| `get_bool(command)` | Query a single-value `ON`/`OFF` parameter as `bool`. |
| `set_bool(command, value)` | Set a single-value `ON`/`OFF` parameter from a `bool`. |

### Restricted-choice parameters

For parameters with more than two legal values (e.g. `DIGIPEAT`'s
`ON`/`UIONLY`/`OFF`).

| Method | Description |
| --- | --- |
| `get_choice(command)` | Query a single-value choice parameter, upper-cased. |
| `set_choice(command, value, choices=None)` | Set it, validating against `choices` if supplied. |

### Multi-port parameters

KAM-XL parameters that apply separately to port 1 and port 2 use a
`value1/value2` wire format. These helpers return/accept
`(port1, port2)` tuples.

| Method | Description |
| --- | --- |
| `get_multiport(command)` | Raw `(port1_str, port2_str)`. |
| `get_multiport_bool(command)` / `set_multiport_bool(command, port, value)` | Both ports as `bool`, or set just one port (leaving the other unchanged on the wire). |
| `get_multiport_choice(command)` / `set_multiport_choice(command, port, value, choices=None)` | Same, for restricted-choice multi-port parameters like `FULLDUP`. |

### Configuration / default port

| Method | Description |
| --- | --- |
| `get_configuration()` | Run `DISPLAY`, return every one-line setting as a `dict`. Multi-line `DISPLAY` entries aren't parsed here. |
| `get_default_port()` / `set_default_port(port)` | Read/write the `PORT` setting (1 or 2). Note this is the *default* port, not necessarily the active stream -- see `STATUS`/`STREAMSW` in the manual. |

### AX.25 connected mode

| Method | Description |
| --- | --- |
| `connect_station(callsign, via=None, timeout=60)` | Attempt a CONNECT, optionally via one or more digipeaters. Returns the connect banner text. Raises `KAMConnectionError` on a hard failure (retry exceeded, busy, disconnected) or `KAMTimeoutError` if nothing matched in time. |
| `send_connected(text, add_cr=True)` | Send text while in Convers mode. |
| `read_connected(timeout=5)` | Collect connected-mode data for up to `timeout` seconds. |
| `enter_command_mode(timeout=5)` | Send Ctrl-C to leave Convers mode. |
| `disconnect_station(timeout=30)` | Return to Command mode and disconnect the current link. |

### Monitoring

| Method | Description |
| --- | --- |
| `read_available()` | Return whatever bytes are currently waiting, decoded. Non-blocking. |
| `listen(seconds=60, callback=None)` | Collect raw unsolicited text for a fixed window. Returns the text, or streams it to `callback` if given. |
| `monitor(seconds=None, callback=None)` | Like `listen()`, but yields parsed `Packet` objects instead of raw text. Returns a generator if `callback` is omitted (`for packet in kam.monitor(): ...`); otherwise blocks and calls `callback(packet)` for each one. Defaults to running until interrupted (`seconds=None`), unlike `listen()`. |

### PBBS (Milestone 6)

Not a BBS this library implements -- the KAM-XL's own firmware PBBS
(mailbox) already handles message storage, forwarding, and SYSOP
access. These two methods drive it through the connected-mode methods
above (a connect to `MYPBBS`, one command, a disconnect) and hand the
raw text to `pbbs.py` for parsing. A connect from the local serial
terminal gets automatic SYSOP privilege per the manual -- no password
exchange needed.

**Verified against real hardware** -- confirmed against a real KAM-XL
PBBS for both an empty mailbox and a populated one (see `pbbs.py`'s
module docstring and `tests/test_pbbs.py`'s `RealHardwareEmptyMailboxTests`).

`read_timeout` is a worst-case ceiling, not a fixed wait: both methods
collect the connected-mode response through a private
`_collect_pbbs_response()` helper (a thin wrapper around the more
general `_poll_until()`, added for milestone 8's Winlink support --
see below) that polls in short slices and returns as soon as the
PBBS's `ENTER COMMAND:` prompt reappears (meaning it's done sending),
rather than always waiting out the full duration. This replaced an
earlier design that did a single `read_connected(timeout=read_timeout)`
call -- on real hardware, a message that took slightly longer than
that fixed window to fully arrive had its last line silently
truncated.

| Method | Description |
| --- | --- |
| `list_pbbs_messages(mypbbs=None, connect_timeout=15, read_timeout=10)` | Connect to the PBBS and list its messages. `mypbbs` defaults to the KAM-XL's current `MYPBBS` setting. Returns a list of `PBBSMessageSummary`. |
| `read_pbbs_message(number, mypbbs=None, connect_timeout=15, read_timeout=10)` | Connect to the PBBS and read one message. Returns a `PBBSMessage`, or `None` if the response didn't look like a message (e.g. the number doesn't exist). |

### Winlink (Milestone 8)

Connects to a real Winlink RMS Packet gateway over AX.25 and
downloads whatever mail is waiting, using the FBB/B2F forwarding
protocol (`winlink.py`) -- a real, separately-documented protocol
(<https://winlink.org/B2F>, <http://www.f6fbb.org/protocole.html>),
not something derived from the KAM-XL manual.

**Scope, chosen deliberately** (see PROJECT.md's milestone 8
writeup): receive-only (never proposes an outbound message of its
own) -- this can't send mail yet. Originally ASCII-only (no
compression), extended to also support Winlink's own **B2 protocol**
after a real gateway (KD5EOC-10) was found to require it and
disconnect rather than fall back to plain ASCII (see
`parse_disconnect_reason()`'s docstring). `winlink.py`'s SID now
claims `B2F$` -- a strict superset of the old `F$`-only claim, so
ASCII-only gateways still work exactly as before.

A gateway now sends either a legacy ASCII (`FB`) proposal or a B2
(`FC`) proposal, and this module handles both -- but their resulting
`WinlinkMessage` differs in richness. An ASCII message only ever
carries a plain title + body (per the B2F spec, "if a station cannot
support the B2 protocol then only the message body is transmitted and
information content of the header is lost"). A B2 message carries the
real structured Winlink header (`Mid`/`Date`/`Type`/`From`/`To`/`Cc`/
`Subject`) plus attachment metadata (name + size only -- see
"Attachment scope" below) -- exposed via `WinlinkMessage`'s optional
fields (`mid`, `date`, `msg_type`, `from_`, `to`, `cc`, `subject`,
`attachments`), all `None`/empty for an ASCII message. If a single
proposal block ever mixes both kinds, `check_winlink_mail()` raises
`winlink.WinlinkProtocolError` rather than attempting to interleave
two different wire formats -- not expected in practice, since a real
Winlink gateway that's negotiated B2 with us is expected to use `FC`
exclusively (this mirrors wl2k-go's own scope choice -- even that
mature, widely-used Winlink client library doesn't bother implementing
the legacy ASCII/B1 proposal codes at all).

**B2's wire format**, researched and implemented from the same two
sources used throughout this module: the B2F spec's "Message
Structure" section (the structured header format) and f6fbb.org's
"Binary Compressed Forward Version 1" section plus wl2k-go's
`fbb/b2f.go` (the actual binary transport -- `SOH`-prefixed header,
`STX`-chunked compressed data, `EOT` + checksum; see
`winlink.parse_b2_blocks()`). The compressed payload itself uses
LZHUF compression, implemented in the new **`lzhuf.py`** module --
see its own module docstring for the two independent reference
implementations (the official Winlink Dev Team's VB.NET source and
wl2k-go's Go source) this was cross-checked against.

**Attachment scope, chosen deliberately:** a B2 message's attachments
are parsed for name and size (`Attachment`) but their file contents
are never extracted or exposed -- avoids a file-storage design
question that hasn't been asked for yet. Extracting the bytes
themselves would be straightforward if that's ever wanted (they're
just more bytes in the same decompressed buffer, per the spec).

**Partially verified against a real RMS gateway.** Built from the
spec and cross-checked against a trusted open-source reference
implementation (`wl2k-go`) for the one genuinely security-sensitive
piece -- the secure-login response algorithm, verified against
`wl2k-go`'s own published test vectors
(`winlink.SECURE_LOGIN_TEST_VECTORS`, `tests/test_winlink.py`). A
live test against a real gateway (KD5EOC-10) confirmed the SID
exchange, the `;PQ:`/`;PR:` secure-login challenge-response, and
handshake-prompt detection all work end-to-end. That same test also
found a real bug (since fixed): the KAM-XL echoes our own
connected-mode transmission back to us -- the same behavior already
known for PBBS's `L` command -- and `has_end_of_block_marker()` used
to treat a bare `FF` as "the gateway has nothing to propose," so our
own echoed `FF` (always sent, since this module never proposes
anything of its own) could be mistaken for the gateway's reply. It
happened to still produce the right answer that day (there really
was no mail), but would have silently missed real waiting mail.
Fixed by only matching the gateway-only `F>`/`FQ` markers -- see
`has_end_of_block_marker()`'s docstring and
`tests/test_winlink.py`'s
`test_own_echoed_transmission_not_mistaken_for_gateways_reply`.
Proposal parsing and message-body extraction against an actual
populated mailbox remain unverified -- that needs an account with
real mail waiting, which hasn't happened yet. Expect the same kind
of further correction `pbbs.py` and `packet.py`'s `HEADER_RE` both
needed after their own first real tests.

A second real-hardware test against the same gateway (KD5EOC-10)
surfaced a second bug: this particular gateway printed `*** [3] Use
B2 protocol - Disconnecting (...)` and dropped the AX.25 link
entirely, rather than falling back to plain-ASCII FBB for a client
that never claims `B`/`B1`/`B2` in its own SID (this module's
deliberate scope -- see above). `check_winlink_mail()` used to have
no way to notice this: whichever poll was running when the KAM-XL's
own `*** DISCONNECTED` banner arrived just kept accumulating it as
ordinary text, and the method's `finally`-block `disconnect_station()`
call -- unaware the link was already gone and the KAM-XL had already
returned to Command mode on its own -- waited for a `cmd:` prompt
that had already come and gone, reliably timing out with a confusing
`KAMTimeoutError: Timed out returning to Command mode`. Fixed:
`check_winlink_mail()` now checks every poll's accumulated text for
that banner via `winlink.parse_disconnect_reason()`, raises a clear
`KAMConnectionError` naming the gateway and quoting its stated reason
when one's present, and skips the now-pointless `disconnect_station()`
call. See `parse_disconnect_reason()`'s docstring and
`tests/test_winlink.py`'s
`test_gateway_disconnect_mid_session_raises_clear_error` for the full
story. That was the prompt for implementing real B2 support (above) --
`check_winlink_mail()` can now actually retrieve mail from a gateway
that enforces B2, rather than only being able to name why it couldn't.

A third real-hardware test against the same gateway surfaced a
different disconnect entirely, unrelated to B2: with B2 now claimed,
the SID exchange, secure login, and B2 proposal negotiation all
succeeded, but the gateway then printed `*** Unknown client types are
not allowed on production servers -- use cms-z.winlink.org -
Disconnecting` and dropped the link. This is the production Winlink
CMS rejecting this module's own client identification (most likely
the `kamxl` app name in its SID) rather than a protocol defect --
nothing about the wire exchange was wrong. Because the earlier fix's
`KAMConnectionError` message used to hard-code "this can happen if the
gateway requires B2 protocol support" as the explanation, it went
stale and actively misleading the moment this second, unrelated cause
showed up. The message no longer guesses a cause -- it quotes the
gateway's own stated reason verbatim and points to
`winlink.py`'s module docstring's "KNOWN DISCONNECT REASONS" section,
which now lists both real causes found so far. This is not something
`check_winlink_mail()` can fix on its own: it's a registration/
gatekeeping policy on Winlink's production infrastructure, not a bug
in this module. See `winlink.py`'s module docstring for the full
writeup, including why spoofing a different, already-recognized
client's identity to bypass this was considered and deliberately
rejected.

**Still unverified: real end-to-end interop with a real gateway's own
B2-compressed bytes.** `lzhuf.py`'s codec is cross-checked against two
independent reference implementations and round-trips its own
compressed output correctly (`tests/test_lzhuf.py`), and the binary
block framing and encapsulated-header parsing are unit-tested against
hand-built fixtures -- but none of this has been exercised against a
real captured B2 message yet (that needs an account with actual mail
waiting on a B2-enforcing gateway, which hasn't happened). Expect the
same kind of correction this project's other first-drafts needed once
tested for real.

**Send support** (`send_winlink_message()`): built after Dave chose
`kamxl_winlink` as the next area of work while the client-registration
question above was still pending. Scope, confirmed directly (same
pattern as every other milestone): send-only (proposes our own
message(s), uploads whatever the gateway accepts, then declines
whatever the gateway offers back rather than also downloading in the
same call -- `check_winlink_mail()` remains the way to download),
text-body-only (no attachments -- there's no metadata-only middle
ground for something we're originating, unlike the receive side),
B2/FC only, and no persistent outbound queue or partial-resume
support. Built from the same two cross-checked sources as the rest of
B2, plus a real find specific to sending: wl2k-go appends a
two's-complement checksum after the "F>" end-of-block marker on
proposals it sends (not documented in the older ascii-only spec) --
see `winlink.build_proposal_block()`'s docstring. **Same
UNVERIFIED-AGAINST-A-REAL-GATEWAY caveat as the rest of B2** -- no
account with permission to actually deliver mail to a real RMS gateway
has confirmed this round-trips over the air yet.

| Method | Description |
| --- | --- |
| `check_winlink_mail(gateway, password, mycall=None, connect_timeout=60, read_timeout=30)` | Connect to `gateway` (an RMS Packet station's callsign), complete secure login if challenged, and download up to one proposal block (5 messages) of waiting mail -- ASCII or B2, whichever the gateway proposes. `mycall` defaults to the KAM-XL's `MYCALL` (first port value). Returns a list of `WinlinkMessage`, empty if nothing's waiting. Raises `KAMConnectionError` if the gateway hangs up mid-exchange; `winlink.WinlinkProtocolError` if a block mixes ASCII and B2 proposals, or a B2 message fails to decompress/checksum-verify. `password` is never logged. |
| `send_winlink_message(gateway, password, messages, mycall=None, connect_timeout=60, read_timeout=30)` | Connect to `gateway`, complete secure login if challenged, and send 1-5 `OutgoingMessage`s via B2. Returns the MID of each message the gateway actually accepted (in `messages` order) -- a rejected/deferred/errored message is left out, an empty return isn't itself an error. Always declines whatever the gateway offers back (send-only -- see above). Raises `ValueError` for an empty or >5-length `messages`; `KAMConnectionError` on a mid-exchange disconnect; `winlink.WinlinkProtocolError` if the gateway's `FS` answer is malformed or never arrives. `password` is never logged. |

### Exceptions

All inherit from `KAMError`.

| Exception | Raised when |
| --- | --- |
| `KAMError` | Base class; also raised directly for "not connected" and read-only-parameter errors. |
| `KAMCommandError` | The KAM-XL responded with `EH?`. |
| `KAMTimeoutError` | No expected response arrived in time. |
| `KAMConnectionError` | An AX.25 CONNECT failed (retry exceeded, busy, or an immediate disconnect), or `check_winlink_mail()`/`send_winlink_message()` detected the gateway hanging up mid-exchange (see the Winlink section above). |

`winlink.WinlinkProtocolError` (plain `Exception`, not a `KAMError` --
it's a B2F/FBB protocol-layer error, not a KAM-XL/serial one) is
raised by `check_winlink_mail()` for a genuine B2 binary-framing
violation: a checksum mismatch, an unexpected byte where `STX`/`EOT`
was expected, or a proposal block mixing ASCII and B2 messages (not
supported -- see the Winlink section above). `lzhuf.LZHUFError` (and
its `lzhuf.ChecksumError` subclass) can also surface through
`check_winlink_mail()`, wrapped in a `WinlinkProtocolError` naming the
failing message's MID.

## Commands

`COMMANDS` (a `Dict[str, CommandInfo]`) drives `get_typed`/`set_typed`.
Each entry is:

```python
CommandInfo(type: str, writable: bool = True, choices: Optional[Tuple[str, ...]] = None)
```

| Command | Type | Python shape | Notes |
| --- | --- | --- | --- |
| `MONITOR` | `multiport_bool` | `(bool, bool)` | |
| `MCON` | `multiport_bool` | `(bool, bool)` | |
| `DIGIPEAT` | `choice` | `str` | Single value, not per-port. One of `ON`, `UIONLY`, `OFF`. |
| `FULLDUP` | `multiport_choice` | `(str, str)` | Per port. One of `ON`, `OFF`, `LOOPBACK` each. |
| `MYCALL` | `multiport_string` | `(str, str)` | |
| `HBAUD` | `multiport_int` | `(int, int)` | |
| `PORT` | `int` | `int` | Default radio port, 1 or 2. |
| `VERSION` | `string` | `str` | Read-only. |

Unknown commands passed to `get_typed`/`set_typed` fall back to the
raw `get`/`set` string API rather than raising -- useful for
parameters not yet in `COMMANDS`.

To add a new one, add an entry to `COMMANDS` with the right `type`
(and `choices` if it's a `choice`/`multiport_choice`) -- no other code
changes needed for `get_typed`/`set_typed` to pick it up.

## `Packet`

*(from `packet.py`)*

```python
@dataclass(frozen=True)
class Packet:
    source: str
    destination: str
    digipeaters: Tuple[str, ...]
    port: int
    payload: str
    raw: str
    frame_type: Optional[str] = None
```

Produced by `PacketParser`/`KAMXL.monitor()` from one MONITOR header
line (`SOURCE>DESTINATION[,DIGI1,DIGI2*]/PORT: <TAG>:`, the `<TAG>:`
part present whenever `MCOM`/`MRESP` are `ON` -- the factory default)
plus the payload lines that follow until the next header.
`digipeaters` keeps the `*` suffix KAM-XL appends to any digipeater
that's already repeated the packet; `digipeated` (a `bool` property)
is `True` if any of them have.

`frame_type` is that bracketed tag (`"UI"`, `"C"`, `"UA"`, `"D"`,
`"DM"`, `"I00"`, `"rr1"`, ...) when present, `None` otherwise. `"UI"`
is an ordinary unconnected/beacon frame; most other values are AX.25
connect-session control/supervisory traffic between two *other*
stations (connect/disconnect requests and acks, numbered info frames,
receiver-ready acks, ...) and typically carry no payload of their
own -- see the manual's `MCOM`/`MRESP` entries for the full list.

`raw` is the full original text (header + payload) in case the parsed
fields are ever wrong and you need to fall back to the source text.

## `PacketParser`

*(from `packet.py`)*

Incrementally reassembles MONITOR text -- which arrives in arbitrary,
not-line-aligned chunks -- into `Packet` objects. `KAMXL.monitor()`
uses one internally; use it directly if you're feeding it text from
somewhere other than `listen()`/`read_available()` (see
[`examples/offline_packet_parsing.py`](../examples/offline_packet_parsing.py)).

| Method | Description |
| --- | --- |
| `feed(text)` | Feed newly-received text in. Returns a list of `Packet`s completed as a result (usually 0 or 1). |
| `flush()` | Finalize whatever's left buffered -- call at the end of a session to not lose the last packet, which is only known to be "done" once a following header line arrives. |

## `PBBSMessageSummary` / `PBBSMessage`

*(from `pbbs.py`)*

```python
@dataclass(frozen=True)
class PBBSMessageSummary:
    number: int
    msg_type: str            # 'B' bulletin, 'T' traffic, 'P' private
    status: Optional[str]
    size: int
    to: str
    from_call: str
    date: str
    pages: Optional[int]
    subject: str


@dataclass(frozen=True)
class PBBSMessage:
    number: int
    date: str
    from_call: str
    to: str
    routing: Optional[str]   # "@..." hierarchical routing, if present
    body: str
```

`PBBSMessageSummary` is one row of `list_pbbs_messages()`'s result;
`PBBSMessage` is `read_pbbs_message()`'s result. Both are parsed from
the KAM-XL's own PBBS command output by `pbbs.parse_message_list()`
and `pbbs.parse_message()` respectively. The message-read format
(`PBBSMessage`) has now been verified against real hardware, including
a real truncation bug found and fixed (see the PBBS methods section
above). The message-*list* row format (`PBBSMessageSummary`) is still
only built from the manual's documented example -- unverified against
a real populated mailbox listing.

## `AprsPosition`

*(from `aprs.py`, milestone 7)*

```python
@dataclass(frozen=True)
class AprsPosition:
    latitude: float
    longitude: float
    symbol_table: str
    symbol_code: str
    comment: str
    timestamp: Optional[str]   # raw APRS timestamp text, not decoded
    raw: str
```

`aprs.parse_position(payload)` decodes an AX.25 UI-frame payload
(`Packet.payload`) as an APRS uncompressed position report, returning
`None` for anything else -- any other APRS data type (status, message,
object, weather, ...), a *compressed* position report (not supported
yet), or non-APRS traffic entirely. MVP scope, chosen deliberately:
position reports are what a map needs; everything else was scoped out
for now. See the module docstring for two other known simplifications
(compressed positions unsupported, position ambiguity not fully
modeled). Built from the public APRS Protocol Reference spec, not
reverse-engineered from the KAM-XL manual, but still treated with the
project's usual caution -- unverified against a real captured APRS
session.

## `Station` / `StationTracker`

*(from `stations.py`, milestone 7)*

```python
@dataclass(frozen=True)
class Station:
    callsign: str
    latitude: float
    longitude: float
    symbol_table: str
    symbol_code: str
    comment: str
    last_heard: float    # time.time() epoch seconds
    packet_count: int
```

`StationTracker` maintains an in-memory "who's where" database, one
`Station` per source callsign (SSIDs are distinct stations), keeping
only the latest known position -- no history, no persistence across
restarts (a deliberate MVP choice; see PROJECT.md). Fed by
`update(packet, now=None)`, which only even attempts
`aprs.parse_position()` for ordinary UI frames (`packet.frame_type` is
`None` or `"UI"`) -- AX.25 connect-session control/supervisory frames
never carry APRS payloads. `tncstation_daemon.py`'s always-on monitor
thread feeds every packet it sees to a shared `StationTracker`
instance, so the database builds passively over time, whether or not
a client is actually watching.

| Method | Description |
| --- | --- |
| `update(packet, now=None)` | Feed one `Packet` in. Returns the resulting `Station` if it decoded as a position report, `None` otherwise. |
| `list_stations()` | All known stations, sorted by callsign. |
| `get_station(callsign)` | One station, or `None` if never heard. |

## `Proposal` / `B2Proposal` / `WinlinkMessage` / `OutgoingMessage`

*(from `winlink.py`, milestone 8; B2 support added afterward)*

```python
@dataclass(frozen=True)
class Proposal:                 # legacy ascii "FB" proposal
    msg_type: str   # 'P' private, 'B' bulletin
    sender: str
    via: str         # "BBS of recipient" (@) field
    recipient: str
    mid: str         # unique message ID, for dedup
    size: int
    raw: str


@dataclass(frozen=True)
class B2Proposal:               # B2 "FC" proposal (encapsulated message)
    msg_type: str   # 'EM' encapsulated message, 'CM' control message
    mid: str
    size: int             # uncompressed size
    compressed_size: int
    raw: str


@dataclass(frozen=True)
class Attachment:               # metadata only -- see "Attachment scope" above
    name: str
    size: int


@dataclass(frozen=True)
class WinlinkMessage:
    title: str
    body: str
    proposal: Union[Proposal, B2Proposal]
    raw: str

    # Populated only for a B2 message -- None/empty for legacy ascii.
    mid: Optional[str] = None
    date: Optional[str] = None
    msg_type: Optional[str] = None
    from_: Optional[str] = None
    to: List[str] = field(default_factory=list)
    cc: List[str] = field(default_factory=list)
    subject: Optional[str] = None
    attachments: List[Attachment] = field(default_factory=list)


@dataclass(frozen=True)
class OutgoingMessage:      # a message to send -- see send_winlink_message()
    to: List[str]
    subject: str
    body: str                        # text only -- no attachments
    cc: List[str] = field(default_factory=list)
    msg_type: str = "Private"
    mid: Optional[str] = None        # auto-generated (generate_mid()) if unset
```

`Proposal` is one legacy "FB ..." line. `B2Proposal` is one B2 "FC
..." line -- carries no sender/recipient itself, since that lives in
the encapsulated message's own header once decompressed.
`WinlinkMessage` pairs the downloaded content with whichever proposal
offered it; its extra fields are only meaningful for a B2 message
(see the Winlink section above for the ASCII/B2 richness difference).

| Function | Description |
| --- | --- |
| `secure_login_response(challenge, password)` | The 8-digit response to a `;PQ:` secure-login challenge. Ported from `wl2k-go` and verified against its own test vectors -- see `SECURE_LOGIN_TEST_VECTORS`. Case-sensitive on `password`, matching the reference exactly. |
| `parse_secure_challenge(line)` | Parse a `;PQ: <digits>` line, or `None`. |
| `parse_sid(line)` / `build_sid(app_name, app_version)` | Parse a remote SID line, or build our own (`B2F$` -- B2 + ASCII-basic + BID). |
| `sid_has_code(sid, code)` | Whether a parsed `RemoteSID` advertises a given feature code. |
| `build_handshake_response(mycall, app_name, app_version, secure_challenge=None, password=None)` | Build the `;FW:`/SID/`;PR:` block to send after reading the remote's handshake. Raises `ValueError` if a challenge was given with no password. |
| `parse_proposal(line)` / `parse_proposals(text)` | Parse one or all legacy `FB ...` proposal lines. |
| `parse_b2_proposal(line)` / `parse_any_proposals(text)` | Parse one `FC ...` proposal, or every proposal (either kind) out of a block of text, in order. |
| `build_fs_line(count, accept=True)` | Build an `FS ...` line accepting (or rejecting) every proposed message -- MVP scope, no per-message selection yet. |
| `has_end_of_block_marker(text)` / `has_fq_marker(text)` | Whether text contains `F>` (block done) or `FQ` (session ending). |
| `parse_disconnect_reason(text)` | Whether text contains the KAM-XL's own `*** DISCONNECTED` banner, and the gateway's stated reason if any. |
| `split_message_blocks(text, count)` / `parse_message_block(raw_text, proposal)` | Split Ctrl-Z-delimited legacy ascii message bodies out of raw text, and turn one into a `WinlinkMessage`. |
| `parse_b2_blocks(data, count)` | Parse up to `count` binary-framed (`SOH`/`STX`/`EOT`) message blocks out of raw bytes -- returns a list of `B2Block` (`title`, `offset`, `compressed_data`). Raises `WinlinkProtocolError` on a checksum mismatch or unexpected byte. |
| `parse_encapsulated_message(data)` | Parse one decompressed B2 message per the B2F spec's "Message Structure" section, returning an `EncapsulatedMessage` (`mid`, `date`, `msg_type`, `from_`, `to`, `cc`, `subject`, `mbo`, `body`, `attachments`, `extra_headers`). |
| `winlink_message_from_encapsulated(proposal, encapsulated)` | Build a `WinlinkMessage` from a `B2Proposal` and its parsed `EncapsulatedMessage`. |
| `generate_mid(callsign)` | Generate a unique <=12-character Winlink message ID. Algorithm (MD5 + base32, first 12 chars) ported from `wl2k-go`'s `fbb/mid.go` -- see the function's docstring for why exact byte-for-byte compatibility with that reference doesn't matter here. |
| `build_encapsulated_message(mid, msg, mycall)` | Build the raw bytes of an outbound B2 message from an `OutgoingMessage` -- the inverse of `parse_encapsulated_message()`. `Mbo:` is set to `mycall`, matching `wl2k-go`'s own convention. |
| `build_b2_proposal_line(mid, size, compressed_size, msg_type="EM")` | Build one outbound `FC ...` proposal line. |
| `build_proposal_block(proposal_lines)` | Join outbound proposal lines into the full block sent to propose our own mail, including the `F> XX` checksum trailer -- see the function's docstring for where that checksum convention was found (not in the older ascii-only spec). |
| `build_b2_block(title, compressed_data, offset=0, chunk_size=125)` | Build one binary-framed (`SOH`/`STX`/`EOT`) message block for transmission -- the inverse of `parse_b2_blocks()`. `chunk_size` defaults to 125, matching `wl2k-go`'s own AX.25-safety margin. |
| `parse_fs_response(line, count)` | Parse a gateway's `FS ...` answer to our own proposal into `count` answers (`"accept"`/`"reject"`/`"defer"`/`"error"`). An offset/partial-resume answer is treated as a plain accept -- see the function's docstring. Raises `WinlinkProtocolError` on a wrong count or unrecognized character. |

## `lzhuf` (LZHUF compression)

*(new module, added alongside B2 support -- see its own module
docstring for the two independent reference implementations this was
cross-checked against)*

| Function | Description |
| --- | --- |
| `compress(data)` / `decompress(data)` | Plain LZHUF compress/decompress: `[4-byte little-endian length][compressed bytes]`, no checksum. |
| `compress_b2(data)` / `decompress_b2(data)` | The wire format Winlink actually uses: `[2-byte CRC-16][4-byte length][compressed bytes]`. `decompress_b2()` raises `ChecksumError` on a mismatch. |
| `LZHUFError` | Base exception (data too short to contain its length/CRC header). |

## `winlink_api` (Winlink HTTP web-service API)

*(new module, August 2026 -- unrelated to `winlink.py`'s B2F/RF
protocol code above: this talks HTTPS/JSON directly to
`api.winlink.org`, no serial port or AX.25 session involved anywhere.
Requires an API key issued by the Winlink Development Team -- see the
module's own docstring for the full research basis and
`docs/daemon.md` for how the daemon reads it. **Endpoint/format
details were cross-checked against Pat, a real open-source Winlink
client, since the WDT's own docs page is JavaScript-rendered and
couldn't be fetched directly -- treat as correct-per-a-trusted-
reference, not yet confirmed against the live API with a real key.**
The "nearby gateways" computation specifically is client-side geometry
this project built, not a verified Winlink API operation -- see below.)*

```python
@dataclass(frozen=True)
class GatewayChannel:
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
    callsign: str
    base_callsign: str
    requested_mode: str
    comments: str
    last_status: str
    latitude: float
    longitude: float
    channels: List[GatewayChannel] = field(default_factory=list)
```

| Function | Description |
| --- | --- |
| `account_exists(callsign, api_key, timeout=15, transport=...)` | True if `callsign` has an active Winlink account (`GET /account/exists`). Raises `WinlinkAPIError` on a populated `ResponseStatus` (real API error) or a non-2xx/malformed response. |
| `get_gateway_status(api_key, mode="AnyAll", history_hours=48, service_codes=("PUBLIC",), timeout=15, transport=...)` | Fetch the current gateway/channel listing (`POST /gateway/status.json`) -- covers both the GatewayListing and GatewayChannelReport permissions, since the WDT doesn't split them across separate endpoints. `history_hours` is clamped to the API's own 48-hour maximum. Returns a list of `Gateway`. |
| `nearby_gateways(gateways, latitude, longitude, max_distance_km=None, limit=None)` | Sort `Gateway` objects (from `get_gateway_status()`) by great-circle distance from a point, nearest first -- **not a Winlink API call**, plain client-side haversine geometry over the lat/long `get_gateway_status()` already returns. See the module docstring's "NOT CONFIRMED" note on why this wasn't built as a call to a separate "GatewayProximity" endpoint. Excludes gateways with `(0, 0)` (unset) coordinates. Returns `(Gateway, distance_km)` pairs. |
| `WinlinkAPIError` | Raised on any failed call -- network failure, non-2xx status, malformed JSON, or a real API-reported error. |
| `Transport` | The injectable `(method, url, data, headers, timeout) -> (status, body)` callable every function above accepts, so tests (and any alternate HTTP stack) can swap out the real network call -- defaults to a plain `urllib.request`-based implementation, no new dependency. |
| `ChecksumError(LZHUFError)` | The B2 CRC-16 header didn't match the compressed data. |
