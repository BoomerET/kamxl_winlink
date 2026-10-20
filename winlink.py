"""
Parsing/building helpers for the FBB forwarding protocol used by
Winlink RMS Packet gateways (milestone 8) -- the same relationship
pbbs.py has to raw PBBS text, and packet.py to raw MONITOR text.

Winlink is a real, separately-documented protocol layered on top of
ordinary AX.25 connected-mode sessions -- not something reverse
engineered from the KAM-XL manual. Two sources were used to build
this module, both fetched and cross-checked before writing any code:

  - The official B2F spec: https://winlink.org/B2F
  - The underlying FBB forwarding protocol: http://www.f6fbb.org/protocole.html
  - The secure-login algorithm specifically was ported from
    wl2k-go (https://github.com/la5nta/wl2k-go), a real, widely-used
    open-source Winlink client library -- and verified against its
    own published test vectors (see SECURE_LOGIN_TEST_VECTORS below
    and tests/test_winlink.py) before being trusted here. That's the
    one piece of this module that's actually confirmed correct, as
    opposed to "matches the spec as written."

SCOPE (chosen deliberately, see PROJECT.md milestone 8): plain ASCII
FBB tier only -- this module never claims B/B1/B2 (compressed
protocol) support in its own SID, so a real Winlink gateway (which
must stay backward-compatible with plain FBB per its own spec) will
never propose a compressed/binary message to us. That means no LZHUF
compression, no binary YAPP-style framing, no checksums to implement
-- proposals and message bodies are all plain text.

REAL CONSEQUENCE OF THAT CHOICE, WORTH KNOWING UP FRONT: per the B2F
spec itself, "If a station cannot support the B2 protocol then only
the message body is transmitted and information content of the header
is lost." That means messages received through this module will NOT
carry Winlink's normal structured address header (Mid:/Date:/Type:/
From:/To:/Subject:/Body:/File: fields, described in the B2F spec) --
just a plain FBB title line and body text, the same reduced shape a
classic non-Winlink FBB packet BBS message would have. This is a
genuine capability tradeoff of the ascii-only scope choice, not a bug.

check_winlink_mail() (kamxl.py) remains receive-only: it always tells
the gateway it has nothing to send ("FF" immediately after login) and
only ever accepts whatever the gateway proposes back. Sending is a
separate method, KAMXL.send_winlink_message() -- see "SEND SUPPORT"
below.

SEND SUPPORT (kamxl.py's send_winlink_message()): a second real-world
finding -- the gateway (KD5EOC-10, once B2 was claimed) accepted the
handshake and B2 negotiation but rejected this module's client
identity outright (see "KNOWN DISCONNECT REASONS" below) -- means B2
send support was built and tested offline before real-gateway
interop could be confirmed either way. Scope, chosen deliberately
(asked directly, same pattern as every other milestone):

  - SEND-ONLY. send_winlink_message() proposes our own outgoing
    message(s), uploads whatever the gateway's "FS" answer accepts,
    then -- when the gateway reciprocates with its own proposal or a
    bare "FF" -- declines everything it offers back and disconnects.
    It does NOT also download mail in the same call; that's still
    check_winlink_mail()'s job, called separately if wanted. A real
    two-way single-connection exchange (propose + accept incoming too)
    was considered and deliberately deferred -- more protocol-state
    complexity for a first version than the simpler two-call approach
    justifies.
  - TEXT BODY ONLY, NO ATTACHMENTS. OutgoingMessage carries To/Cc/
    Subject/Body text only. Mirrors the receive side's metadata-only
    attachment scope, but there's no metadata-only equivalent for
    something we're originating -- either real attachment bytes get
    sent (out of scope) or none do.
  - B2/FC ONLY, no legacy-ascii ("FB") outbound proposals -- this
    module's SID already claims B2 (see build_sid()), so there's no
    reason to fall back to the ascii tier's lossy, single-recipient,
    no-subject shape for something we're composing ourselves.
  - NO PERSISTENT OUTBOUND QUEUE / NO PARTIAL-RESUME. Each call
    composes and sends whatever OutgoingMessage(s) it's given, fresh,
    in that one session -- there's no mailbox tracking a message as
    "still pending" across calls, so this module doesn't implement the
    B2F protocol's offset/partial-resume answer codes (see
    parse_fs_response()) -- an offset-resume answer is treated as a
    plain accept-from-zero, a deliberate, documented simplification.

Building blocks: generate_mid() (MID generation, cross-checked against
wl2k-go's fbb/mid.go), build_encapsulated_message() (the inverse of
parse_encapsulated_message()), build_b2_proposal_line() and
build_proposal_block() (outbound "FC ..."/"F> <checksum>" lines --
see build_proposal_block()'s docstring for where the checksum
convention itself came from, a real find not in the older ascii-only
spec), build_b2_block() (the inverse of parse_b2_blocks(), promoted
from what was originally just a test fixture builder -- it turned out
to already be exactly the production code needed once real sending
was in scope), and parse_fs_response() (decoding the gateway's "FS
..." answer to what we proposed).

UNVERIFIED AGAINST A REAL GATEWAY, same as the B2 receive path was at
first: no account with permission to actually send B2 mail to a real
RMS gateway has confirmed this round-trips correctly over the air yet
(see the "PARTIALLY VERIFIED" note below for what receive-side testing
has confirmed so far). Every piece here is built from the same two
cross-checked sources as the rest of B2 (the B2F spec and wl2k-go),
with the outbound proposal checksum specifically found only in the
latter -- but "matches two independent descriptions of the protocol"
is not the same confidence level as "confirmed against a real gateway
in the field," and this module tries to always be honest about which
one applies.

SINGLE BLOCK ONLY (MVP scope): the FBB protocol allows up to five
message proposals per block, with more blocks following if there's
more mail waiting. This module's caller (KAMXL.check_winlink_mail())
only reads one block per call -- if a real account ever has more than
five pending messages, only the first five come back on one call;
calling again should fetch the next batch, but this hasn't been
tested against real traffic where that would actually happen.

PARTIALLY VERIFIED AGAINST A REAL RMS GATEWAY. The handshake and
secure login were confirmed working end-to-end against a real
gateway (KD5EOC-10): the SID exchange and the ";PQ:"/";PR:" challenge-
response were both observed live and matched what this module
expected. That same test also surfaced a real bug (since fixed -- see
has_end_of_block_marker()'s docstring): the KAM-XL echoes our own
connected-mode transmission back to us, which our own "FF" (always
sent, since this module never proposes anything of its own) could be
mistaken for the gateway's reply if the wrong text was treated as a
stop marker. To be precise: that test's final "no mail waiting"
answer was actually correct, but the bug meant the code never
genuinely reached the gateway's real "FQ" reply before returning --
the "FQ" no-mail path itself wasn't actually exercised that day, just
coincidentally not disproven either.

A second real-hardware test against the same gateway found that it
requires B2 protocol support and disconnects the AX.25 link outright
(printing "*** [3] Use B2 protocol - Disconnecting") rather than
falling back to plain ASCII for a client, like this one, that never
claims "B"/"B1"/"B2" in its own SID -- see parse_disconnect_reason()'s
docstring for the full story and the real bug that test also
surfaced (check_winlink_mail() used to hang and raise a confusing
KAMTimeoutError instead of a clear error when this happened, since
fixed). That finding is what prompted adding real B2 support below.

B2 SUPPORT (this module now claims "B2" in its own SID -- see
build_sid()): the plain-ASCII scope note above is now historical for
the SID itself, but still describes the "FB" ascii-tier proposal path,
kept for any non-Winlink FBB station that might still use it (a B2
station must stay backward-compatible with ascii/B1 per spec). A real
Winlink gateway that negotiates B2 with us is expected to use the "FC"
proposal instead (Winlink's own "encapsulated message" extension --
see B2Proposal, parse_b2_blocks(), and EncapsulatedMessage below),
which DOES carry the full structured address header (Mid/Date/From/
To/Cc/Subject) plus attachment metadata that the ascii tier loses.
B2 messages are LZHUF-compressed (see lzhuf.py) and sent using binary
SOH/STX/EOT block framing, not the ascii tier's plain title+^Z text --
a real, separate wire format, researched from the same two sources
used for the compression codec itself: the official B2F spec's
"Binary Compressed Forward Version 1" section
(http://www.f6fbb.org/protocole.html) and wl2k-go's fbb/b2f.go, which
independently confirm the same SOH-header / STX-chunk / EOT+checksum
framing.

ATTACHMENT SCOPE (deliberately chosen, see PROJECT.md): this first B2
pass parses and exposes the real structured header and message body,
and reports each attachment's name and size (Attachment, below) -- but
does NOT extract or expose attachment file contents. Extracting
attachment bytes is mechanically simple (they're just more bytes in
the same decompressed buffer, per the B2F "Message Structure" spec)
but deliberately out of scope for now, to avoid taking on a
file-storage design question that hasn't been asked for yet.

MIXED-BATCH SCOPE LIMIT: if a single proposal block ever contains a
mix of legacy ascii ("FB") and B2 ("FC") proposals, this module doesn't
attempt to interleave the two different wire formats needed to read
them back -- check_winlink_mail() raises a clear error instead. This
is expected to be rare-to-never in practice: a real Winlink CMS/RMS
gateway that negotiates B2 with us is expected to use FC exclusively
for its own mail (this mirrors wl2k-go's own scope choice -- its
parseProposal() has a literal "// TODO: implement" for the legacy
ascii/B1 proposal codes, meaning even that mature, widely-used real
Winlink client doesn't bother with mixed-tier handling either).

Proposal parsing and message-body extraction are still unverified
against a real populated mailbox -- that needs an account with
actual mail waiting to test, which hasn't happened yet. Expect
the same kind of further correction packet.py's HEADER_RE and
pbbs.py's parsing both needed after their own first real tests. The
LZHUF codec itself (lzhuf.py) is cross-checked against two independent
reference implementations, but real end-to-end interop with a real
gateway's own B2-compressed bytes hasn't been confirmed yet either.

KNOWN DISCONNECT REASONS (real ones seen so far against KD5EOC-10 --
kept here, not baked into check_winlink_mail()'s exception message,
since guessing which one applies has already gone stale once):

  1. "*** [3] Use B2 protocol - Disconnecting" -- the gateway requires
     B2 and won't fall back to ASCII. This is what prompted adding B2
     support (above), so this specific reason shouldn't recur now that
     this module's SID claims B2.
  2. "*** Unknown client types are not allowed on production servers
     -- use cms-z.winlink.org - Disconnecting" -- seen AFTER this
     module started claiming B2 and completing the SID exchange and
     secure login successfully, so this is a different, unrelated
     cause: the production Winlink CMS (which KD5EOC-10 proxies
     sessions to -- note the earlier handshake banner's own "CMS via
     KD5EOC" line) appears to validate the connecting client's
     identity (the app name in its SID/;FW: line -- at the time, this
     module's own app name was "kamxl") against a list of recognized
     client software, and rejects anything it doesn't recognize on its
     PRODUCTION system -- pointing instead at "cms-z.winlink.org",
     which appears to be a separate test/development CMS instance for
     exactly this situation (new, not-yet-recognized client software).
     This is NOT a protocol bug in this module -- the handshake and
     secure login both completed correctly first -- and it's NOT
     something this module can code its way around: it's a real
     gatekeeping/policy decision on Winlink's infrastructure, not a
     wire-format detail. This module deliberately does NOT attempt to
     spoof a different, already-recognized client's identity to work
     around this.

     In response, the project was renamed to "kamxl_winlink" (repo and
     app identity both) and Dave is looking into registering that name
     with the Winlink Development Team. build_handshake_response()'s
     default app_name is now "kamxl_winlink" to match -- but as of this
     writing that registration hasn't been confirmed, so whether the
     production CMS will actually recognize "kamxl_winlink" remains
     unverified. If this same disconnect recurs with the new name,
     that means registration either hasn't gone through yet or needs a
     different name/process than assumed here.
"""

import base64
import hashlib
import re
import time

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union



# -----------------------------------------------------------------------
# Secure login (;PQ: challenge / ;PR: response)
# -----------------------------------------------------------------------

# Verbatim from wl2k-go's fbb/secure.go, which itself ported it from
# paclink-unix. This exact byte sequence is load-bearing -- it's a fixed
# salt mixed into every login response, not an arbitrary constant.
_SECURE_LOGIN_SALT = bytes([
    77, 197, 101, 206, 190, 249,
    93, 200, 51, 243, 93, 237,
    71, 94, 239, 138, 68, 108,
    70, 185, 225, 137, 217, 16,
    51, 122, 193, 48, 194, 195,
    198, 175, 172, 169, 70, 84,
    61, 62, 104, 186, 114, 52,
    61, 168, 66, 129, 192, 208,
    187, 249, 232, 193, 41, 113,
    41, 45, 240, 16, 29, 228,
    208, 228, 61, 20,
])

# wl2k-go's own published test vectors (fbb/secure_test.go) -- kept
# here, alongside tests/test_winlink.py actually asserting against
# them, so the connection between "this is the verified reference"
# and "here's the proof" isn't just a comment.
SECURE_LOGIN_TEST_VECTORS = (
    ("23753528", "FOOBAR", "72768415"),
    ("23753528", "FooBar", "95074758"),
)


def secure_login_response(challenge: str, password: str) -> str:
    """
    Compute the 8-digit response to a Winlink ";PQ:" secure-login
    challenge for the given account password.

    NOT case-normalizing -- matches the reference implementation
    exactly (see SECURE_LOGIN_TEST_VECTORS: "FOOBAR" and "FooBar"
    produce different responses). Winlink account passwords are
    canonically upper-case only (the system upper-cases them if you
    somehow enter lower-case) -- pass ``password`` exactly as your
    account has it configured. A login failure against a password
    you're sure is correct is worth double-checking for stray case
    differences before looking anywhere else.
    """
    payload = (
        challenge.encode("ascii")
        + password.encode("ascii")
        + _SECURE_LOGIN_SALT
    )
    digest = hashlib.md5(payload).digest()

    packed = (
        ((digest[3] & 0x3F) << 24)
        | (digest[2] << 16)
        | (digest[1] << 8)
        | digest[0]
    )

    return f"{packed:08d}"[-8:]


_CHALLENGE_RE = re.compile(r"^;PQ:\s*(?P<challenge>\S+)\s*$", re.IGNORECASE)


def parse_secure_challenge(line: str) -> Optional[str]:
    """
    Parse a ";PQ: <digits>" secure-login challenge line. Returns None
    if this line isn't one (most lines during handshake aren't --
    callers should just skip it and keep reading, not treat a
    non-match as an error).
    """
    match = _CHALLENGE_RE.match(line.strip())

    return match.group("challenge") if match else None


# -----------------------------------------------------------------------
# SID (System Identification) line
# -----------------------------------------------------------------------

@dataclass(frozen=True)
class RemoteSID:
    app_name: str
    app_version: str
    codes: str   # e.g. "B2FIHM$" -- see has_code()


_SID_RE = re.compile(r"^\[(?P<app>[^-\]]+)-(?P<version>[^-\]]+)-(?P<codes>[^\]]+)\]\s*$")


def parse_sid(line: str) -> Optional[RemoteSID]:
    """
    Parse a gateway's own SID line (e.g. "[WL2K-5.0-B2FIHM$]"). Returns
    None if this line isn't a SID line at all.
    """
    match = _SID_RE.match(line.strip())

    if match is None:
        return None

    return RemoteSID(
        app_name=match.group("app"),
        app_version=match.group("version"),
        codes=match.group("codes").upper(),
    )


def sid_has_code(sid: RemoteSID, code: str) -> bool:
    """
    Whether the remote's SID advertises a given single/multi-character
    feature code (e.g. "F" for plain FBB ASCII proposals, "B2" for the
    compressed protocol this module doesn't implement).
    """
    return code.upper() in sid.codes


def build_sid(app_name: str, app_version: str) -> str:
    """
    Our own outgoing SID line.

    Claims "B2" (Winlink's own compressed/encapsulated-message
    extension -- see B2Proposal and parse_b2_blocks()), "F" (plain FBB
    ASCII-basic proposals, kept for a non-Winlink FBB station that
    isn't B2-aware), and "$" (BID/MID field, always present in every
    proposal line this module sends or expects -- must be the last
    character per the SID format).

    This used to claim only "F$" (see PROJECT.md's milestone 8
    writeup) until a real gateway (KD5EOC-10) was found to require B2
    and disconnect rather than fall back to ASCII for a client that
    doesn't advertise it -- see parse_disconnect_reason()'s docstring.
    Claiming B2 is a strict superset of the old F-only behavior: a
    gateway that only understands plain ASCII will simply propose "FB"
    messages to us the same as before (a B2-capable station must stay
    backward-compatible with ascii/B1 clients per spec), so there's no
    real downside to always claiming it now.
    """
    return f"[{app_name}-{app_version}-B2F$]"


# -----------------------------------------------------------------------
# Handshake response (;FW: / SID / ;PR:)
# -----------------------------------------------------------------------

def build_handshake_response(
    mycall: str,
    app_name: str = "kamxl_winlink",
    app_version: str = "0.1",
    secure_challenge: Optional[str] = None,
    password: Optional[str] = None,
) -> str:
    """
    Build our side of the handshake: the ";FW:" line requesting mail
    for ``mycall``, our own SID line, and (if the gateway sent a
    ";PQ:" challenge) the ";PR:" secure-login response -- joined with
    "\\r" as a single block, ready to hand to KAMXL.send_connected().

    ``password`` is required if ``secure_challenge`` is given (raises
    ValueError otherwise, rather than silently skipping login and
    likely getting disconnected by the gateway).

    ``app_name`` defaults to "kamxl_winlink", not "kamxl" -- see the
    "KNOWN DISCONNECT REASONS" note above: the production Winlink CMS
    rejected "kamxl" as an unrecognized client type, and this new name
    is the one being pursued for registration with the Winlink
    Development Team. Until/unless that registration completes, this
    default is a real, unverified guess -- the same kind of thing this
    project always flags rather than silently assumes works.
    """
    if secure_challenge and not password:
        raise ValueError(
            "Gateway sent a secure-login challenge but no password "
            "was provided"
        )

    lines = [
        f";FW: {mycall}",
        build_sid(app_name, app_version),
    ]

    if secure_challenge:
        response = secure_login_response(secure_challenge, password)
        lines.append(f";PR: {response}")

    return "\r".join(lines)


# -----------------------------------------------------------------------
# Proposals (FB lines -- plain ASCII FBB tier)
# -----------------------------------------------------------------------

@dataclass(frozen=True)
class Proposal:
    """
    One "FB" proposal line -- the gateway offering us one message.

    ``msg_type`` is 'P' (private) or 'B' (bulletin). ``via`` is the
    "BBS of recipient" (@) field -- often the gateway itself when a
    message is addressed directly to us. ``mid`` is the message's
    unique ID (BID/MID) used for cross-network deduplication.
    """

    msg_type: str
    sender: str
    via: str
    recipient: str
    mid: str
    size: int
    raw: str


# "FB P F6FBB FC1GHV FC1MVP 24657_F6FBB 1345" -- ascii-basic tier
# proposal line (see f6fbb.org's "Ascii Basic Protocol" section). All
# seven fields (including the leading "FB") are mandatory per spec.
_PROPOSAL_RE = re.compile(
    r"^FB\s+(?P<type>[PB])\s+"
    r"(?P<sender>\S+)\s+"
    r"(?P<via>\S+)\s+"
    r"(?P<recipient>\S+)\s+"
    r"(?P<mid>\S+)\s+"
    r"(?P<size>\d+)\s*$"
)


def parse_proposal(line: str) -> Optional[Proposal]:
    """
    Parse one "FB ..." proposal line. Returns None if this line isn't
    a proposal (callers should skip it, not treat it as an error --
    e.g. it might be the "F>" end-of-block marker, or an unrelated
    comment line).
    """
    match = _PROPOSAL_RE.match(line.strip())

    if match is None:
        return None

    return Proposal(
        msg_type=match.group("type"),
        sender=match.group("sender"),
        via=match.group("via"),
        recipient=match.group("recipient"),
        mid=match.group("mid"),
        size=int(match.group("size")),
        raw=line.strip(),
    )


def parse_proposals(text: str) -> List[Proposal]:
    """
    Parse every "FB ..." proposal line out of a block of text (e.g.
    everything received up to and including the "F>" end-of-block
    marker). Non-matching lines are silently skipped.
    """
    proposals = []

    for line in text.splitlines():
        proposal = parse_proposal(line)

        if proposal is not None:
            proposals.append(proposal)

    return proposals


class WinlinkProtocolError(Exception):
    """
    A genuine B2 binary-framing protocol violation -- a bad checksum
    or an unexpected byte where SOH/STX/EOT was expected. NOT raised
    for "not enough data has arrived yet" (that's handled by returning
    fewer parsed blocks/messages than asked for, the same pattern
    split_message_blocks() already uses for the ascii tier), only for
    data that's actually malformed once it's fully in hand.
    """


# -----------------------------------------------------------------------
# B2 proposals (FC lines -- Winlink's own encapsulated-message
# extension, see the module docstring's "B2 SUPPORT" section)
# -----------------------------------------------------------------------

@dataclass(frozen=True)
class B2Proposal:
    """
    One "FC" proposal line -- the gateway offering us one Winlink
    encapsulated message. Unlike the plain-ascii Proposal, this line
    carries no sender/recipient itself -- that lives inside the
    encapsulated message's own structured header once decompressed
    (see EncapsulatedMessage), which is the whole point of B2.

    ``msg_type`` is "EM" (encapsulated message) or "CM" (Winlink
    control message -- not a real user message; this module doesn't
    do anything special with these beyond parsing the proposal line).
    ``size`` is the uncompressed message size; ``compressed_size`` is
    how many bytes to expect over the wire for it.
    """

    msg_type: str
    mid: str
    size: int
    compressed_size: int
    raw: str


# "FC EM TJKYEIMMHSRB 527 123 0" -- Type, MID, U-Size, C-Size, and a
# trailing field observed in real captured examples (wl2k-go's own
# proposal.go docstring shows this exact shape) that's always "0" and
# not documented in the B2F spec itself -- tolerated but ignored.
_B2_PROPOSAL_RE = re.compile(
    r"^FC\s+(?P<type>EM|CM)\s+"
    r"(?P<mid>\S+)\s+"
    r"(?P<size>\d+)\s+"
    r"(?P<csize>\d+)"
    r"(?:\s+\d+)?\s*$"
)


def parse_b2_proposal(line: str) -> Optional[B2Proposal]:
    """
    Parse one "FC ..." proposal line. Returns None if this line isn't
    a B2 proposal (callers should skip it, not treat it as an error).
    """
    match = _B2_PROPOSAL_RE.match(line.strip())

    if match is None:
        return None

    return B2Proposal(
        msg_type=match.group("type"),
        mid=match.group("mid"),
        size=int(match.group("size")),
        compressed_size=int(match.group("csize")),
        raw=line.strip(),
    )


def parse_any_proposals(text: str) -> List[Union[Proposal, B2Proposal]]:
    """
    Parse every proposal line -- legacy ascii "FB ..." or B2
    encapsulated "FC ..." -- out of a block of text, in the order they
    appear. A gateway that negotiated B2 with us (this module's SID
    now claims it -- see build_sid()) is expected to use "FC" for real
    Winlink mail; "FB" support is kept only for a non-Winlink FBB
    station that might still propose the old way (a B2 station must
    stay backward-compatible with ascii/B1 clients per spec).
    """
    proposals: List[Union[Proposal, B2Proposal]] = []

    for line in text.splitlines():
        line = line.strip()

        proposal = parse_proposal(line)

        if proposal is not None:
            proposals.append(proposal)
            continue

        b2_proposal = parse_b2_proposal(line)

        if b2_proposal is not None:
            proposals.append(b2_proposal)

    return proposals


def build_fs_line(count: int, accept: bool = True) -> str:
    """
    Build an "FS ..." response line accepting (or rejecting) every
    proposed message, in order.

    MVP scope: always all-accept or all-reject -- no per-message
    accept/defer/reject selection yet (real FS lines support '+'/'-'/
    '=' per message; see f6fbb.org's spec for the fuller vocabulary).
    """
    code = "+" if accept else "-"

    return "FS " + (code * count)


_END_OF_BLOCK_RE = re.compile(r"^F>(\s+[0-9A-Fa-f]{1,2})?$")


def has_end_of_block_marker(text: str) -> bool:
    """
    Whether ``text`` contains the "F>" line that ends a proposal
    block -- the gateway is done sending proposals for now.

    Tolerates (but does not verify) a trailing checksum after "F>"
    (e.g. "F> 3A") -- found while researching outbound send support
    (see build_proposal_block()'s docstring): wl2k-go, a real,
    actively-interoperating Winlink client, always appends one to the
    proposal blocks IT sends, and reads one back the same way when
    receiving. The older f6fbb.org ascii-only doc's own worked
    examples show a bare "F>" with nothing after it, so it's not
    certain every gateway sends the checksummed form -- but a real
    Winlink CMS very well might, and the original strict `== "F>"`
    match here would have silently never matched that line, meaning
    check_winlink_mail() would poll until timeout instead of ever
    detecting the end of a real proposal block. Verifying the checksum
    (not just tolerating it) would mean reconstructing the exact raw
    proposal-line bytes the gateway sent from what parse_any_proposals()
    parsed back out, which isn't preserved today -- accepted as a real,
    documented gap rather than silently ignored.

    Deliberately does NOT also match a bare "FF" line, even though
    "FF" means "I have nothing to propose" and can legitimately come
    from either side. Real bug found live against a real gateway
    (KD5EOC-10): the KAM-XL echoes our own connected-mode transmission
    back to us -- the same behavior already known for PBBS (see
    pbbs.py's RealHardwareEmptyMailboxTests, whose captured raw text
    starts with the echoed "L" command). KAMXL.check_winlink_mail()
    always sends "FF" itself right after logging in (this module's
    receive-only MVP never proposes an outbound message) -- so if a
    bare "FF" were treated as a valid "the gateway has nothing"
    signal, our OWN echoed "FF" would satisfy it immediately, before
    the real gateway had answered at all. In the live test that
    surfaced this, it happened to still produce the correct answer
    (there really was no mail waiting) purely by coincidence -- had
    there been real mail, this would have silently reported "no mail"
    instead. Per the spec, the gateway's genuine reply to our initial
    "FF" is either a real "FB ... F>" proposal batch, or a bare "FQ"
    if it truly has nothing (see has_fq_marker()) -- never a bare
    "FF" at that specific point in the exchange, so relying on "F>"/
    "FQ" only (things the far end sends and we never do) sidesteps
    the echo ambiguity entirely, the same defensive principle PBBS's
    "ENTER COMMAND" marker already relies on.
    """
    return any(_END_OF_BLOCK_RE.match(line.strip()) for line in text.splitlines())


def has_fq_marker(text: str) -> bool:
    """Whether text contains a bare "FQ" line -- session ending."""
    return any(line.strip() == "FQ" for line in text.splitlines())


_DISCONNECT_BANNER_RE = re.compile(r"\*\*\*\s*disconnected", re.IGNORECASE)


def parse_disconnect_reason(text: str) -> Optional[str]:
    """
    Whether ``text`` contains the KAM-XL's own "*** DISCONNECTED"
    banner -- meaning the AX.25 link is already gone, not just idle.
    Returns ``None`` if there's no such banner. If there is, returns
    the KAM-XL's stated reason (the nearest preceding "***"-prefixed
    line, if any -- e.g. "*** [3] Use B2 protocol - Disconnecting
    (47.190.139.106)") or "" if the banner appeared with no such line.

    Real bug found live against a real gateway (KD5EOC-10, Denton
    County Texas EOC): our SID only ever claims "F$" (plain ASCII,
    receive-only -- see this module's docstring), never "B"/"B1"/"B2".
    This particular gateway responded to that by printing "*** [3] Use
    B2 protocol - Disconnecting (...)" and dropping the AX.25 link
    entirely, rather than falling back to plain-ASCII FBB the way the
    B2F spec's text ("if a station cannot support the B2 protocol then
    only the message body is transmitted") seems to promise. Whatever
    poll was running when that arrived (in the observed case, the
    proposals-detection poll -- it never saw "F>"/"FQ", so ran to its
    full read_timeout and captured the disconnect banner and the
    KAM-XL's own "cmd:" prompt along with it) ends up swallowing the
    "cmd:" prompt that a *subsequent* disconnect_station() call would
    otherwise wait for. Since the KAM-XL doesn't print a new one until
    provoked, and there is nothing left to provoke it with, that
    disconnect_station() call has nothing to wait for and reliably
    times out (observed: "KAMTimeoutError: Timed out returning to
    Command mode", 5 seconds later -- enter_command_mode()'s default
    command_mode_timeout). KAMXL.check_winlink_mail() calls this after
    every poll stage so it can raise a clear, specific error and skip
    the doomed disconnect_station() call instead.
    """
    lines = [line.strip() for line in text.splitlines()]

    for index, line in enumerate(lines):
        if _DISCONNECT_BANNER_RE.search(line):
            for candidate in reversed(lines[:index]):
                if candidate.startswith("***"):
                    return candidate

            return ""

    return None


# -----------------------------------------------------------------------
# B2 binary block framing (SOH/STX/EOT) -- carries LZHUF-compressed
# encapsulated messages, see the module docstring's "B2 SUPPORT" note
# -----------------------------------------------------------------------

_SOH = 0x01
_STX = 0x02
_EOT = 0x04


@dataclass(frozen=True)
class B2Block:
    """
    One binary-framed message block, still LZHUF-compressed -- the
    wire format a B2Proposal's message arrives in once accepted (see
    parse_b2_blocks()). ``title`` comes from the SOH header itself
    (distinct from -- and sometimes identical to -- the encapsulated
    message's own Subject: header field once decompressed).
    ``offset`` is always 0 for a fresh (non-resumed) transfer, which is
    all this module supports.
    """

    title: str
    offset: int
    compressed_data: bytes


def parse_b2_blocks(data: bytes, count: int) -> List[B2Block]:
    """
    Parse up to ``count`` binary-framed (SOH/STX/EOT) message blocks
    out of raw bytes received after sending an "FS" line accepting one
    or more B2Proposals. Sources (independently cross-checked before
    writing this, same standard as lzhuf.py): f6fbb.org's "Binary
    Compressed Forward Version 1" section, and wl2k-go's
    fbb/b2f.go:readCompressed() -- both describe the identical framing:

        <SOH><header length><title>\\0<offset>\\0
        (<STX><chunk length><compressed bytes>)*
        <EOT><8-bit two's-complement checksum of all chunk bytes>

    Returns fewer than ``count`` blocks if ``data`` doesn't contain
    that many complete blocks yet -- same "poll until you have enough"
    contract as split_message_blocks(), so callers can feed this a
    growing buffer as more arrives. Raises WinlinkProtocolError on a
    checksum mismatch or an unexpected byte within a block that IS
    otherwise fully present -- that's real corruption, not "not here
    yet" (per the B2F spec, a real gateway disconnects on a checksum
    failure; this module surfaces it as an exception instead so the
    caller can decide what to do).
    """
    blocks: List[B2Block] = []
    pos = 0

    while len(blocks) < count:
        if pos >= len(data) or data[pos] != _SOH:
            break

        if pos + 1 >= len(data):
            break

        header_len = data[pos + 1]
        header_start = pos + 2
        header_end = header_start + header_len

        if header_end > len(data):
            break

        header_bytes = data[header_start:header_end]
        parts = header_bytes.split(b"\x00")

        if len(parts) < 2:
            break

        title = parts[0].decode("latin-1")

        try:
            offset = int(parts[1])
        except ValueError:
            break

        cursor = header_end
        payload = bytearray()
        block_done = False

        while not block_done:
            if cursor >= len(data):
                return blocks

            marker = data[cursor]

            if marker == _STX:
                if cursor + 1 >= len(data):
                    return blocks

                length = data[cursor + 1]

                if length == 0:
                    length = 256

                chunk_start = cursor + 2
                chunk_end = chunk_start + length

                if chunk_end > len(data):
                    return blocks

                payload.extend(data[chunk_start:chunk_end])
                cursor = chunk_end
            elif marker == _EOT:
                if cursor + 1 >= len(data):
                    return blocks

                checksum = data[cursor + 1]
                computed = (-sum(payload)) & 0xFF

                if computed != checksum:
                    raise WinlinkProtocolError(
                        f"B2 binary block {title!r}: checksum mismatch "
                        f"(header says {checksum:#04x}, computed "
                        f"{computed:#04x} over {len(payload)} bytes)"
                    )

                cursor += 2
                pos = cursor
                block_done = True
            else:
                raise WinlinkProtocolError(
                    f"B2 binary block {title!r}: unexpected byte "
                    f"{marker:#04x} (expected STX or EOT) at offset "
                    f"{cursor}"
                )

        blocks.append(B2Block(title=title, offset=offset, compressed_data=bytes(payload)))

    return blocks


# -----------------------------------------------------------------------
# Encapsulated messages (the structured header B2/FC gives us access
# to -- see the B2F spec's "Message Structure" section)
# -----------------------------------------------------------------------

@dataclass(frozen=True)
class Attachment:
    """
    Metadata for one attachment on an encapsulated message -- name and
    size only, per this module's deliberate attachment scope (see the
    module docstring's "ATTACHMENT SCOPE" note). The attachment's
    actual bytes are not extracted or exposed.
    """

    name: str
    size: int


@dataclass(frozen=True)
class EncapsulatedMessage:
    """
    One decompressed B2/FC message, parsed per the B2F spec's "Message
    Structure" section: an ASCII address header (CRLF-separated,
    case-insensitive field names), a blank line, then the message body
    (exactly ``Body:``'s declared length), then any number of
    attachments (not extracted -- see Attachment).

    Field names mirror the spec's own header names (``mid``, ``date``,
    ``msg_type`` for "Type:", ``from_`` for "From:" -- "from" is a
    Python keyword, ``to``/``cc`` as lists since either may repeat).
    Any header field not recognized is preserved in ``extra_headers``
    rather than silently dropped, in case a real gateway sends
    something this parser doesn't yet know about -- the spec itself
    says unrecognized fields "will be ignored and will not cause an
    error," so preserving rather than validating is the deliberately
    tolerant choice here (same philosophy as pbbs.py's message parsing
    silently skipping lines it doesn't recognize).
    """

    mid: str
    date: str
    msg_type: str
    from_: str
    to: List[str]
    cc: List[str]
    subject: str
    mbo: str
    body: str
    attachments: List[Attachment]
    extra_headers: Dict[str, List[str]]


def parse_encapsulated_message(data: bytes) -> EncapsulatedMessage:
    """
    Parse one decompressed B2/FC message (the output of
    lzhuf.decompress_b2() on a B2Block's compressed_data).

    Per the spec, the body is "limited to ASCII characters," but
    wl2k-go's own header.go notes that real gateways (RMS Express, CMS)
    routinely send ISO-8859-1 in practice -- decoded here as latin-1,
    which maps every byte 0-255 to a unique code point and therefore
    never raises on real-world 8-bit content (matching wl2k-go's
    documented default charset).
    """
    text = data.decode("latin-1")
    header_text, _, rest = text.partition("\r\n\r\n")

    headers: Dict[str, List[str]] = {}

    for line in header_text.splitlines():
        if ":" not in line:
            continue

        key, _, value = line.partition(":")
        headers.setdefault(key.strip().title(), []).append(value.strip())

    def first(key: str, default: str = "") -> str:
        values = headers.pop(key, None)
        return values[0] if values else default

    def all_values(key: str) -> List[str]:
        return headers.pop(key, [])

    mid = first("Mid")
    date = first("Date")
    msg_type = first("Type")
    from_ = first("From")
    to = all_values("To")
    cc = all_values("Cc")
    subject = first("Subject")
    mbo = first("Mbo")

    body_len_str = first("Body", "0")

    try:
        body_len = int(body_len_str)
    except ValueError:
        body_len = 0

    body = rest[:body_len]

    attachments = []

    for file_entry in all_values("File"):
        size_str, _, name = file_entry.partition(" ")

        try:
            size = int(size_str)
        except ValueError:
            continue

        attachments.append(Attachment(name=name.strip(), size=size))

    return EncapsulatedMessage(
        mid=mid,
        date=date,
        msg_type=msg_type,
        from_=from_,
        to=to,
        cc=cc,
        subject=subject,
        mbo=mbo,
        body=body,
        attachments=attachments,
        extra_headers=headers,
    )


# -----------------------------------------------------------------------
# Message bodies
# -----------------------------------------------------------------------

@dataclass(frozen=True)
class WinlinkMessage:
    """
    One received message. For a legacy ascii ("FB") message, only
    ``title``/``body``/``proposal``/``raw`` are meaningful -- the rest
    default to None/empty, since the ascii tier never carries a
    structured header (see the module docstring's "REAL CONSEQUENCE OF
    THAT CHOICE" note). For a B2 ("FC") encapsulated message, all
    fields are populated from the real structured header (see
    EncapsulatedMessage) -- this is the whole benefit of B2 over the
    ascii tier. ``proposal`` is whichever proposal (Proposal or
    B2Proposal) offered this message.
    """

    title: str
    body: str
    proposal: Union[Proposal, B2Proposal]
    raw: str

    mid: Optional[str] = None
    date: Optional[str] = None
    msg_type: Optional[str] = None
    from_: Optional[str] = None
    to: List[str] = field(default_factory=list)
    cc: List[str] = field(default_factory=list)
    subject: Optional[str] = None
    attachments: List[Attachment] = field(default_factory=list)


def winlink_message_from_encapsulated(
    proposal: B2Proposal,
    encapsulated: EncapsulatedMessage,
) -> WinlinkMessage:
    """
    Build a WinlinkMessage from a decompressed, parsed B2/FC message
    (see parse_encapsulated_message()), pairing it with the B2Proposal
    that offered it. ``title`` is the Subject header when present
    (falling back to the raw MID if a message genuinely has no subject
    -- better than an empty string for something meant to be shown to
    a person).
    """
    return WinlinkMessage(
        title=encapsulated.subject or encapsulated.mid,
        body=encapsulated.body,
        proposal=proposal,
        raw=encapsulated.body,
        mid=encapsulated.mid,
        date=encapsulated.date,
        msg_type=encapsulated.msg_type,
        from_=encapsulated.from_,
        to=encapsulated.to,
        cc=encapsulated.cc,
        subject=encapsulated.subject,
        attachments=encapsulated.attachments,
    )


def split_message_blocks(text: str, count: int) -> List[str]:
    """
    Split raw received text into up to ``count`` Ctrl-Z-terminated
    message blocks (title + body, per f6fbb.org's ascii-basic
    protocol -- no binary framing at this tier). Each returned string
    still contains its own title as the first line, with the
    terminating Ctrl-Z itself stripped. Returns fewer than ``count``
    entries if the text doesn't contain that many yet -- callers
    polling incrementally should keep reading until they get exactly
    ``count`` back.
    """
    parts = text.split("\x1a")

    # Whatever follows the last Ctrl-Z (the gateway's next proposal
    # batch, FQ, etc.) isn't a message body -- only keep the parts
    # that were actually terminated by a Ctrl-Z, i.e. all but the
    # trailing remainder.
    blocks = [part.lstrip("\r\n") for part in parts[:-1]]

    return blocks[:count]


def parse_message_block(raw_text: str, proposal: Proposal) -> WinlinkMessage:
    """
    Turn one Ctrl-Z-delimited block (from split_message_blocks()) into
    a WinlinkMessage, pairing it with the Proposal that offered it.
    """
    lines = raw_text.splitlines()
    title = lines[0] if lines else ""
    body = "\n".join(lines[1:]).strip("\r\n")

    return WinlinkMessage(
        title=title,
        body=body,
        proposal=proposal,
        raw=raw_text,
    )


# -----------------------------------------------------------------------
# Outbound messages (sending) -- see the module docstring's "SEND
# SUPPORT" section for the deliberate scope: send-only, text-body-only,
# B2/FC only, no persistent outbound queue/resume.
# -----------------------------------------------------------------------

@dataclass(frozen=True)
class OutgoingMessage:
    """
    A message this station wants to send -- text body only, see the
    module docstring's "SEND SUPPORT" note for why there's no
    attachment support here at all (unlike the receive side's
    metadata-only compromise, there's no equivalent middle ground for
    something we're originating).

    ``mid`` is optional -- KAMXL.send_winlink_message() generates one
    via generate_mid() if left unset. Only set it yourself if you have
    a real reason to (e.g. resending under the same ID for dedup).
    """

    to: List[str]
    subject: str
    body: str
    cc: List[str] = field(default_factory=list)
    msg_type: str = "Private"
    mid: Optional[str] = None


def generate_mid(callsign: str) -> str:
    """
    Generate a unique Winlink message ID -- the "Mid:" header field,
    and the "ID" field of an outgoing FC proposal line. Max 12
    characters per the B2F spec.

    Algorithm ported from wl2k-go's fbb/mid.go (GenerateMid): MD5 of
    "<something>-<callsign>", base32-encoded, first 12 characters. Only
    the *approach* is ported, not a byte-for-byte-compatible format --
    wl2k-go hashes its own Go time.Time string representation, which
    this module doesn't try to reproduce, because it doesn't need to:
    each client only ever generates and later recognizes its own MIDs
    (for dedup against messages it has already sent/received), never
    reconstructs or validates another client's MID format. What matters
    for real interop is that the result is <=12 characters and
    effectively unique -- both true here, cross-checked against a real
    reference implementation's approach rather than invented from
    scratch.
    """
    payload = f"{time.time_ns()}-{callsign}".encode("ascii", errors="replace")
    digest = hashlib.md5(payload).digest()

    return base64.b32encode(digest).decode("ascii")[:12]


def build_encapsulated_message(mid: str, msg: OutgoingMessage, mycall: str) -> bytes:
    """
    Build the raw (uncompressed) bytes of an encapsulated B2/FC
    message from an OutgoingMessage -- the inverse of
    parse_encapsulated_message(), round-tripped through it in
    tests/test_winlink.py. Per the B2F spec's "Message Structure"
    section: an ASCII address header (Mid/Date/Type/From/To/Cc/
    Subject/Mbo/Body, CRLF-separated), a blank line, then the body.

    ``mid`` is a separate parameter rather than pulled from
    ``msg.mid`` so callers resolve it exactly once (see
    generate_mid()) and reuse the same value for both this function
    and the outbound proposal line (build_b2_proposal_line()) --
    calling generate_mid() twice for the same message would produce
    two different IDs.

    ``Mbo:`` is set to ``mycall``, matching wl2k-go's own
    NewMessage() (which sets Mbo to the sending station's own
    callsign, not the relaying gateway's) -- the B2F spec's own text
    ("call of the originating RMS") is genuinely ambiguous here, so
    this follows the reference rather than guessing independently.

    No ``File:`` fields are ever written -- text-body-only by design
    (see the module docstring).

    Body line endings are normalized to CRLF regardless of how the
    caller wrote ``msg.body`` (a plain Python string typically only
    has "\\n"), since the spec requires CRLF throughout and the
    ``Body:`` field's byte count must match exactly what's actually
    sent. Per spec, a CRLF terminator is appended after the body and
    is deliberately NOT counted in ``Body:`` (this module's own
    parse_encapsulated_message() doesn't care either way, since it
    slices exactly ``Body:`` characters and ignores whatever follows,
    but a real gateway parsing our own output might, so this follows
    the spec precisely rather than relying on that generosity).
    """
    date = datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M")
    body = msg.body.replace("\r\n", "\n").replace("\n", "\r\n")

    lines = [
        f"Mid: {mid}",
        f"Date: {date}",
        f"Type: {msg.msg_type}",
        f"From: {mycall}",
    ]
    lines += [f"To: {addr}" for addr in msg.to]
    lines += [f"Cc: {addr}" for addr in msg.cc]
    lines += [
        f"Subject: {msg.subject}",
        f"Mbo: {mycall}",
        f"Body: {len(body)}",
    ]

    header = "\r\n".join(lines)

    return (header + "\r\n\r\n" + body + "\r\n").encode("latin-1")


def build_b2_proposal_line(mid: str, size: int, compressed_size: int, msg_type: str = "EM") -> str:
    """
    Build one outbound "FC ..." proposal line for a message we want to
    send -- mirrors parse_b2_proposal()'s format exactly. The trailing
    "0" matches what's consistently observed in real captured examples
    and wl2k-go's own output (see B2Proposal's docstring); its meaning
    isn't documented anywhere found so far, but every real example
    available has it, so this includes it too rather than omitting an
    apparently-expected field.
    """
    return f"FC {msg_type} {mid} {size} {compressed_size} 0"


def build_proposal_block(proposal_lines: List[str]) -> str:
    """
    Join one or more outbound proposal lines (see
    build_b2_proposal_line()) into the full block this module sends to
    propose its own outgoing mail, including the trailing "F> XX"
    end-of-block marker with its checksum.

    The checksum itself is NOT in the f6fbb.org ascii-basic spec (whose
    own worked examples show a bare "F>" with nothing after it) -- it
    was found only by reading wl2k-go's fbb/b2f.go (sendOutbound()),
    a real, actively-interoperating Winlink client: a two's-complement
    (mod 256) checksum, uppercase hex, of every proposal line's
    characters plus a trailing "\\r" for each line, summed across the
    whole block. Trusted over the older doc's simpler examples because
    wl2k-go is confirmed working against real production Winlink CMS
    systems today. See has_end_of_block_marker()'s docstring, updated
    alongside this to tolerate (though not yet verify) the same
    checksum suffix on a line we *receive*.
    """
    checksum = 0

    for line in proposal_lines:
        checksum += sum(ord(c) for c in line) + ord("\r")

    checksum = (-checksum) & 0xFF

    lines = list(proposal_lines) + [f"F> {checksum:02X}"]

    return "\r".join(lines)


def build_b2_block(title: str, compressed_data: bytes, offset: int = 0, chunk_size: int = 125) -> bytes:
    """
    Build one binary-framed (SOH/STX/EOT) message block for
    transmission -- the wire format expected after a gateway's "FS"
    answer accepts one of our own outbound B2Proposals. Inverse of
    parse_b2_blocks(); see that function's docstring for the framing
    itself and its two independently cross-checked sources.

    ``chunk_size`` defaults to 125, matching wl2k-go's own
    fbb/b2f.go:writeCompressed() (MaxMsgLength) -- its comment notes
    the protocol's actual maximum is 255, but 125 is used "to allow use
    of AX.25 links with a paclen of 128," the exact transport this
    module also targets, so the same conservative default is used here
    rather than the higher protocol ceiling.
    """
    title_bytes = title.encode("latin-1")
    offset_bytes = str(offset).encode("ascii")
    header = title_bytes + b"\x00" + offset_bytes + b"\x00"

    if len(header) > 255:
        raise WinlinkProtocolError(
            f"B2 block header too long ({len(header)} bytes, max 255) "
            f"for title {title!r}"
        )

    block = bytes([_SOH, len(header)]) + header

    pos = 0

    while pos < len(compressed_data):
        chunk = compressed_data[pos:pos + chunk_size]
        length_byte = len(chunk) if len(chunk) < 256 else 0
        block += bytes([_STX, length_byte]) + chunk
        pos += len(chunk)

    checksum = (-sum(compressed_data)) & 0xFF
    block += bytes([_EOT, checksum])

    return block


# f6fbb.org's Binary Compressed Forward Version 1 answer vocabulary,
# mapped to wl2k-go's own three-way classification (fbb/b2f.go's
# parseProposalAnswer()) -- 'R'(ejected) groups with 'N'/'-', while
# 'H'(eld) groups with 'L'/'='(defer), NOT with reject, matching that
# reference exactly (a naive reading of "accepted but will be held"
# could plausibly be grouped either way; this follows the real,
# interoperating client's own choice rather than picking independently).
_FS_ANSWER_CODES = {
    "Y": "accept", "+": "accept",
    "N": "reject", "-": "reject", "R": "reject",
    "L": "defer", "=": "defer", "H": "defer",
    "E": "error",
}


def parse_fs_response(line: str, count: int) -> List[str]:
    """
    Parse a gateway's "FS ..." response to one of our own outbound
    proposals into a list of ``count`` answers -- one of "accept",
    "reject", "defer", or "error" apiece, in proposal order.

    Per f6fbb.org's Binary Compressed Forward Version 1 section, an
    answer can also request a byte offset ("!3350" or "A3350" --
    accept, but resume from offset 3350 of a previously interrupted
    transfer). This module has no persistent outbound queue to resume
    FROM (see the module docstring's "SEND SUPPORT" note -- every
    OutgoingMessage is composed and sent fresh, never partially
    retried across sessions), so an offset answer is treated as a
    plain accept, always sending from the beginning -- a deliberate,
    documented simplification, not a real implementation of partial
    resume.

    Raises WinlinkProtocolError if the line doesn't contain exactly
    ``count`` answers, or contains a character this module doesn't
    recognize.
    """
    text = line.strip()

    if text.startswith("FS "):
        text = text[3:]
    elif text == "FS":
        text = ""

    answers: List[str] = []
    i = 0

    while i < len(text):
        c = text[i]
        i += 1

        if c in ("A", "a", "!"):
            while i < len(text) and text[i].isdigit():
                i += 1

            answers.append("accept")
            continue

        answer = _FS_ANSWER_CODES.get(c.upper())

        if answer is None:
            raise WinlinkProtocolError(
                f"Unrecognized character {c!r} in FS response line {line!r}"
            )

        answers.append(answer)

    if len(answers) != count:
        raise WinlinkProtocolError(
            f"FS response has {len(answers)} answer(s), expected "
            f"{count} (one per proposed message): {line!r}"
        )

    return answers
