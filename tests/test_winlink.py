"""
Offline tests for winlink.py's parsing/building helpers and
KAMXL.check_winlink_mail()'s composition of the connected-mode
primitives.

IMPORTANT: unlike most of this project, winlink.py was NOT built from
observed real-hardware behavior -- there's no KAM-XL manual involved
at all here. It's built from the public B2F/FBB protocol specs and,
for the one genuinely security-sensitive piece (the secure-login
response algorithm), a real open-source reference implementation
(wl2k-go) -- see winlink.py's module docstring for the sources and
caveats. SECURE_LOGIN_TEST_VECTORS below are wl2k-go's own published
test vectors, so that piece is confirmed correct independent of
real-hardware testing. Everything else here is a best-effort first
draft against the spec, unverified against a real RMS gateway --
expect adjustment the same way pbbs.py's parsing needed it.
"""

import unittest

from fakes import CannedSerial, make_kam  # noqa: F401 (path + fixture reuse)

import lzhuf
from kamxl import KAMConnectionError, KAMTimeoutError
import winlink as w


def _build_encapsulated_message(
    mid="12345_N0CALL",
    date="2026/08/03 12:00",
    msg_type="Private",
    from_="N0CALL",
    to=("N5BA",),
    cc=(),
    subject="Test B2 message",
    mbo="KD5EOC",
    body="Hello from B2!\r\n",
    attachments=(),
) -> bytes:
    """
    Build one raw (decompressed) encapsulated message per the B2F
    spec's "Message Structure" section, for round-tripping through
    parse_encapsulated_message() in tests.
    """
    lines = [f"Mid: {mid}", f"Date: {date}", f"Type: {msg_type}", f"From: {from_}"]
    lines += [f"To: {addr}" for addr in to]
    lines += [f"Cc: {addr}" for addr in cc]
    lines += [f"Subject: {subject}", f"Mbo: {mbo}", f"Body: {len(body)}"]
    lines += [f"File: {len(data)} {name}" for name, data in attachments]

    header = "\r\n".join(lines) + "\r\n"
    raw = (header + "\r\n" + body).encode("latin-1")

    for _name, data in attachments:
        raw += data + b"\r\n"

    return raw


class SecureLoginResponseTests(unittest.TestCase):
    def test_matches_reference_implementation_vectors(self):
        for challenge, password, expect in w.SECURE_LOGIN_TEST_VECTORS:
            got = w.secure_login_response(challenge, password)
            self.assertEqual(got, expect)

    def test_is_case_sensitive_like_the_reference(self):
        # Not a design choice of ours -- matches wl2k-go's own
        # behavior exactly (see the two test vectors above, which
        # differ only in password case and produce different
        # responses). Documented here explicitly since it's a real
        # gotcha: a stored password with the wrong case will silently
        # fail login rather than erroring obviously.
        upper = w.secure_login_response("23753528", "FOOBAR")
        mixed = w.secure_login_response("23753528", "FooBar")

        self.assertNotEqual(upper, mixed)

    def test_response_is_always_eight_digits(self):
        response = w.secure_login_response("00000001", "X")

        self.assertEqual(len(response), 8)
        self.assertTrue(response.isdigit())


class ParseSecureChallengeTests(unittest.TestCase):
    def test_parses_challenge_line(self):
        self.assertEqual(w.parse_secure_challenge(";PQ: 425"), "425")

    def test_case_insensitive_prefix(self):
        self.assertEqual(w.parse_secure_challenge(";pq: 425"), "425")

    def test_non_challenge_line_returns_none(self):
        self.assertIsNone(w.parse_secure_challenge("Welcome to WL2K"))
        self.assertIsNone(w.parse_secure_challenge(";FW: N5BA-10"))


class SIDTests(unittest.TestCase):
    def test_parses_real_looking_sid(self):
        sid = w.parse_sid("[WL2K-5.0-B2FIHM$]")

        self.assertIsNotNone(sid)
        self.assertEqual(sid.app_name, "WL2K")
        self.assertEqual(sid.app_version, "5.0")
        self.assertEqual(sid.codes, "B2FIHM$")

    def test_sid_has_code(self):
        sid = w.parse_sid("[WL2K-5.0-B2FIHM$]")

        self.assertTrue(w.sid_has_code(sid, "F"))
        self.assertTrue(w.sid_has_code(sid, "B2"))
        self.assertFalse(w.sid_has_code(sid, "X"))

    def test_non_sid_line_returns_none(self):
        self.assertIsNone(w.parse_sid("Welcome to WL2K"))
        self.assertIsNone(w.parse_sid("FB P F6FBB FC1GHV FC1MVP 1_A 10"))

    def test_build_sid_claims_b2_f_and_bid(self):
        # Claims B2 (Winlink's own compressed/encapsulated-message
        # extension) and F (plain ascii, kept for a non-Winlink FBB
        # station) -- see winlink.py's module docstring's "B2 SUPPORT"
        # note for why this changed from an ascii-only "F$" claim.
        # "$" must be last.
        sid = w.build_sid("kamxl", "0.1")

        self.assertEqual(sid, "[kamxl-0.1-B2F$]")


class BuildHandshakeResponseTests(unittest.TestCase):
    def test_without_challenge_no_pr_line(self):
        # app_name defaults to "kamxl_winlink", not "kamxl" -- see
        # winlink.py's "KNOWN DISCONNECT REASONS" note: the production
        # Winlink CMS rejected "kamxl" as an unrecognized client type,
        # and "kamxl_winlink" is the name now being pursued for
        # registration.
        response = w.build_handshake_response("N5BA-10")

        self.assertEqual(response, ";FW: N5BA-10\r[kamxl_winlink-0.1-B2F$]")

    def test_with_challenge_includes_pr_line(self):
        response = w.build_handshake_response(
            "N5BA-10", secure_challenge="23753528", password="FOOBAR"
        )

        self.assertEqual(
            response,
            ";FW: N5BA-10\r[kamxl_winlink-0.1-B2F$]\r;PR: 72768415"
        )

    def test_challenge_without_password_raises(self):
        with self.assertRaises(ValueError):
            w.build_handshake_response("N5BA-10", secure_challenge="425")


class ProposalTests(unittest.TestCase):
    """
    Fixtures verbatim from f6fbb.org's ascii-basic protocol example --
    see winlink.py's module docstring for the source.
    """

    def test_parses_private_message_proposal(self):
        proposal = w.parse_proposal(
            "FB P F6FBB FC1GHV FC1MVP 24657_F6FBB 1345"
        )

        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.msg_type, "P")
        self.assertEqual(proposal.sender, "F6FBB")
        self.assertEqual(proposal.via, "FC1GHV")
        self.assertEqual(proposal.recipient, "FC1MVP")
        self.assertEqual(proposal.mid, "24657_F6FBB")
        self.assertEqual(proposal.size, 1345)

    def test_parses_bulletin_proposal(self):
        proposal = w.parse_proposal("FB B F6FBB FRA FBB 22_456_F6FBB 8548")

        self.assertEqual(proposal.msg_type, "B")

    def test_non_proposal_line_returns_none(self):
        self.assertIsNone(w.parse_proposal("F>"))
        self.assertIsNone(w.parse_proposal("FQ"))
        self.assertIsNone(w.parse_proposal(""))

    def test_parse_proposals_skips_non_matching_lines(self):
        text = (
            "FB P F6FBB FC1GHV FC1MVP 24657_F6FBB 1345\r\n"
            "FB B F6FBB FRA FBB 22_456_F6FBB 8548\r\n"
            "F>\r\n"
        )

        proposals = w.parse_proposals(text)

        self.assertEqual(len(proposals), 2)
        self.assertEqual(proposals[1].mid, "22_456_F6FBB")

    def test_build_fs_line_accepts_all(self):
        self.assertEqual(w.build_fs_line(3), "FS +++")

    def test_build_fs_line_rejects_all(self):
        self.assertEqual(w.build_fs_line(2, accept=False), "FS --")

    def test_has_end_of_block_marker(self):
        self.assertTrue(w.has_end_of_block_marker("FB P a b c d 5\r\nF>\r\n"))
        self.assertFalse(w.has_end_of_block_marker("FB P a b c d 5\r\n"))

    def test_has_end_of_block_marker_tolerates_checksum_suffix(self):
        # wl2k-go (a real, actively-interoperating Winlink client)
        # always appends a two-hex-digit checksum after "F>" -- see
        # build_proposal_block()'s docstring for where this was found.
        # A real gateway may do the same on a proposal block it sends
        # US, so this needs to still be recognized as the end-of-block
        # marker, not just the bare "F>" the older ascii-only doc's
        # own worked examples show.
        self.assertTrue(w.has_end_of_block_marker("FC EM a 1 2 0\r\nF> 3A\r\n"))
        self.assertTrue(w.has_end_of_block_marker("FC EM a 1 2 0\r\nF> A\r\n"))

    def test_has_end_of_block_marker_does_not_match_bare_ff(self):
        # Real bug, found live against a real gateway (KD5EOC-10):
        # KAMXL.check_winlink_mail() always sends its own "FF" right
        # after login (receive-only MVP, never proposes an outbound
        # message) -- and the KAM-XL echoes connected-mode
        # transmissions back to us (same behavior already known for
        # PBBS). A bare "FF" is deliberately NOT treated as an
        # end-of-block marker here, so our own echoed "FF" can never
        # be mistaken for the gateway's reply -- see
        # has_end_of_block_marker()'s docstring for the full story.
        self.assertFalse(w.has_end_of_block_marker("FF\r\n"))

    def test_has_fq_marker(self):
        self.assertTrue(w.has_fq_marker("FQ\r\n"))
        self.assertFalse(w.has_fq_marker("FF\r\n"))

    def test_parse_disconnect_reason_returns_none_when_not_disconnected(self):
        self.assertIsNone(
            w.parse_disconnect_reason("FB P a b c d 5\r\nF>\r\n")
        )

    def test_parse_disconnect_reason_extracts_stated_reason(self):
        # Verbatim shape of a real second bug found live against
        # KD5EOC-10: this gateway apparently requires B2 protocol
        # support and hangs up instead of falling back to plain ASCII
        # for a client (like this one) that never claims "B"/"B1"/"B2"
        # in its SID. See parse_disconnect_reason()'s docstring and
        # KAMXLWinlinkIntegrationTests's
        # test_gateway_disconnect_mid_session_raises_clear_error for
        # the full story.
        text = (
            ";FW: N5BA\r\n[kamxl-0.1-F$]\r\n;PR: 84304290\r\nFF\r\n"
            "*** [3] Use B2 protocol - Disconnecting (47.190.139.106)\r\n"
            "*** DISCONNECTED\r\ncmd:N5BA>KD5EOC-10/2: <<UA>>:\r\n"
        )

        reason = w.parse_disconnect_reason(text)

        self.assertEqual(
            reason, "*** [3] Use B2 protocol - Disconnecting (47.190.139.106)"
        )

    def test_parse_disconnect_reason_empty_string_when_no_stated_reason(self):
        reason = w.parse_disconnect_reason("*** DISCONNECTED\r\ncmd:\r\n")

        self.assertEqual(reason, "")


class B2ProposalTests(unittest.TestCase):
    def test_parses_encapsulated_message_proposal(self):
        proposal = w.parse_b2_proposal("FC EM TJKYEIMMHSRB 527 123 0")

        self.assertEqual(proposal.msg_type, "EM")
        self.assertEqual(proposal.mid, "TJKYEIMMHSRB")
        self.assertEqual(proposal.size, 527)
        self.assertEqual(proposal.compressed_size, 123)

    def test_parses_control_message_proposal(self):
        proposal = w.parse_b2_proposal("FC CM SOMEID 10 5 0")

        self.assertEqual(proposal.msg_type, "CM")

    def test_tolerates_missing_trailing_field(self):
        # The B2F spec itself only documents 4 fields (Type/ID/U-Size/
        # C-Size); the trailing "0" is an extra observed in real
        # examples (wl2k-go's own docstring) but not guaranteed.
        proposal = w.parse_b2_proposal("FC EM TJKYEIMMHSRB 527 123")

        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.compressed_size, 123)

    def test_non_b2_proposal_line_returns_none(self):
        self.assertIsNone(w.parse_b2_proposal("FB P a b c d 5"))
        self.assertIsNone(w.parse_b2_proposal("F>"))

    def test_parse_any_proposals_recognizes_both_kinds(self):
        text = (
            "FB P F6FBB FC1GHV FC1MVP 24657_F6FBB 1345\r\n"
            "FC EM TJKYEIMMHSRB 527 123 0\r\n"
            "F>\r\n"
        )

        proposals = w.parse_any_proposals(text)

        self.assertEqual(len(proposals), 2)
        self.assertIsInstance(proposals[0], w.Proposal)
        self.assertIsInstance(proposals[1], w.B2Proposal)

    def test_parse_any_proposals_only_b2(self):
        text = "FC EM AAA 10 5 0\r\nFC EM BBB 20 8 0\r\nF>\r\n"

        proposals = w.parse_any_proposals(text)

        self.assertEqual(len(proposals), 2)
        self.assertTrue(all(isinstance(p, w.B2Proposal) for p in proposals))


class B2BlockFramingTests(unittest.TestCase):
    def test_parses_single_complete_block(self):
        compressed = lzhuf.compress_b2(b"Hello, Winlink!")
        raw_bytes = w.build_b2_block("Test Subject", compressed)

        blocks = w.parse_b2_blocks(raw_bytes, 1)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].title, "Test Subject")
        self.assertEqual(blocks[0].offset, 0)
        self.assertEqual(blocks[0].compressed_data, compressed)

    def test_parses_multiple_blocks(self):
        c1 = lzhuf.compress_b2(b"Message one")
        c2 = lzhuf.compress_b2(b"Message two")
        raw_bytes = w.build_b2_block("First", c1) + w.build_b2_block("Second", c2)

        blocks = w.parse_b2_blocks(raw_bytes, 2)

        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].title, "First")
        self.assertEqual(blocks[1].title, "Second")

    def test_returns_fewer_than_count_if_not_all_arrived_yet(self):
        compressed = lzhuf.compress_b2(b"Only one message so far")
        raw_bytes = w.build_b2_block("Only One", compressed)

        blocks = w.parse_b2_blocks(raw_bytes, 2)

        self.assertEqual(len(blocks), 1)

    def test_returns_nothing_for_incomplete_header(self):
        blocks = w.parse_b2_blocks(bytes([0x01, 0x05, ord("a")]), 1)

        self.assertEqual(blocks, [])

    def test_returns_nothing_for_incomplete_data_chunk(self):
        compressed = lzhuf.compress_b2(b"Hello, Winlink!")
        raw_bytes = w.build_b2_block("Test Subject", compressed)
        # Truncate mid-chunk, before the EOT+checksum ever arrives.
        truncated = raw_bytes[:-5]

        blocks = w.parse_b2_blocks(truncated, 1)

        self.assertEqual(blocks, [])

    def test_checksum_mismatch_raises_protocol_error(self):
        compressed = lzhuf.compress_b2(b"Hello, Winlink!")
        raw_bytes = bytearray(w.build_b2_block("Test Subject", compressed))
        raw_bytes[-1] ^= 0xFF  # Corrupt just the checksum byte itself.

        with self.assertRaises(w.WinlinkProtocolError):
            w.parse_b2_blocks(bytes(raw_bytes), 1)

    def test_unexpected_byte_raises_protocol_error(self):
        compressed = lzhuf.compress_b2(b"Hi")
        raw_bytes = bytearray(w.build_b2_block("Test", compressed))
        # Replace the STX marker right after the header with garbage.
        header_len = raw_bytes[1]
        stx_index = 2 + header_len
        self.assertEqual(raw_bytes[stx_index], 0x02)
        raw_bytes[stx_index] = 0x99

        with self.assertRaises(w.WinlinkProtocolError):
            w.parse_b2_blocks(bytes(raw_bytes), 1)


class EncapsulatedMessageTests(unittest.TestCase):
    def test_parses_full_header_and_body(self):
        raw = _build_encapsulated_message(
            to=("N5BA", "N0CALL"),
            cc=("W1AW",),
            attachments=[("test.txt", b"HELLO")],
        )

        message = w.parse_encapsulated_message(raw)

        self.assertEqual(message.mid, "12345_N0CALL")
        self.assertEqual(message.date, "2026/08/03 12:00")
        self.assertEqual(message.msg_type, "Private")
        self.assertEqual(message.from_, "N0CALL")
        self.assertEqual(message.to, ["N5BA", "N0CALL"])
        self.assertEqual(message.cc, ["W1AW"])
        self.assertEqual(message.subject, "Test B2 message")
        self.assertEqual(message.mbo, "KD5EOC")
        self.assertEqual(message.body, "Hello from B2!\r\n")
        self.assertEqual(len(message.attachments), 1)
        self.assertEqual(message.attachments[0].name, "test.txt")
        self.assertEqual(message.attachments[0].size, 5)

    def test_does_not_extract_attachment_bytes(self):
        # Deliberate scope choice -- see winlink.py's "ATTACHMENT
        # SCOPE" note. Attachment carries only name/size.
        raw = _build_encapsulated_message(attachments=[("f.bin", b"\x00\x01\x02")])

        message = w.parse_encapsulated_message(raw)

        self.assertEqual(message.attachments[0].size, 3)
        self.assertFalse(hasattr(message.attachments[0], "data"))

    def test_minimal_header_no_attachments(self):
        raw = _build_encapsulated_message()

        message = w.parse_encapsulated_message(raw)

        self.assertEqual(message.attachments, [])
        self.assertEqual(message.body, "Hello from B2!\r\n")

    def test_unrecognized_header_fields_preserved_not_dropped(self):
        raw = (
            b"Mid: 1_A\r\nDate: 2026/08/03 12:00\r\nType: Private\r\n"
            b"From: A\r\nTo: B\r\nSubject: S\r\nMbo: M\r\nBody: 5\r\n"
            b"X-Custom: something\r\n\r\nhello"
        )

        message = w.parse_encapsulated_message(raw)

        self.assertIn("X-Custom", message.extra_headers)
        self.assertEqual(message.extra_headers["X-Custom"], ["something"])

    def test_winlink_message_from_encapsulated(self):
        raw = _build_encapsulated_message()
        encapsulated = w.parse_encapsulated_message(raw)
        proposal = w.parse_b2_proposal("FC EM 12345_N0CALL 100 50 0")

        message = w.winlink_message_from_encapsulated(proposal, encapsulated)

        self.assertEqual(message.title, "Test B2 message")
        self.assertEqual(message.subject, "Test B2 message")
        self.assertEqual(message.from_, "N0CALL")
        self.assertEqual(message.to, ["N5BA"])
        self.assertEqual(message.body, "Hello from B2!\r\n")
        self.assertIs(message.proposal, proposal)

    def test_falls_back_to_mid_when_subject_missing(self):
        raw = _build_encapsulated_message(subject="")
        encapsulated = w.parse_encapsulated_message(raw)
        proposal = w.parse_b2_proposal("FC EM 12345_N0CALL 100 50 0")

        message = w.winlink_message_from_encapsulated(proposal, encapsulated)

        self.assertEqual(message.title, "12345_N0CALL")


class GenerateMidTests(unittest.TestCase):
    def test_length_is_twelve(self):
        mid = w.generate_mid("N5BA-10")

        self.assertEqual(len(mid), 12)

    def test_alphanumeric_base32_charset(self):
        mid = w.generate_mid("N5BA-10")

        # base32's alphabet is A-Z and 2-7 -- no 0/1/8/9 (avoids
        # visual confusion with O/I/B/g), matching Python's own
        # base64.b32encode().
        self.assertRegex(mid, r"^[A-Z2-7]{12}$")

    def test_consecutive_calls_differ(self):
        mid1 = w.generate_mid("N5BA-10")
        mid2 = w.generate_mid("N5BA-10")

        self.assertNotEqual(mid1, mid2)


class BuildEncapsulatedMessageTests(unittest.TestCase):
    def test_round_trips_through_parse(self):
        msg = w.OutgoingMessage(
            to=["N0CALL"],
            cc=["N1CALL"],
            subject="Test outbound message",
            body="Line one\nLine two\n",
        )

        raw = w.build_encapsulated_message("12345_N5BA", msg, "N5BA-10")
        parsed = w.parse_encapsulated_message(raw)

        self.assertEqual(parsed.mid, "12345_N5BA")
        self.assertEqual(parsed.msg_type, "Private")
        self.assertEqual(parsed.from_, "N5BA-10")
        self.assertEqual(parsed.to, ["N0CALL"])
        self.assertEqual(parsed.cc, ["N1CALL"])
        self.assertEqual(parsed.subject, "Test outbound message")
        self.assertEqual(parsed.mbo, "N5BA-10")
        # Line endings normalized to CRLF -- see the function's
        # docstring for why (Body:'s byte count must match exactly).
        self.assertEqual(parsed.body, "Line one\r\nLine two\r\n")

    def test_mbo_defaults_to_mycall(self):
        # Matches wl2k-go's own NewMessage(), not a guess -- see the
        # function's docstring for why this follows the reference
        # rather than the B2F spec's more ambiguous text.
        msg = w.OutgoingMessage(to=["N0CALL"], subject="Hi", body="Hello")

        raw = w.build_encapsulated_message("MID1", msg, "N5BA-10")
        parsed = w.parse_encapsulated_message(raw)

        self.assertEqual(parsed.mbo, "N5BA-10")

    def test_body_length_header_matches_actual_body(self):
        msg = w.OutgoingMessage(to=["N0CALL"], subject="Hi", body="Hi\nthere")

        raw = w.build_encapsulated_message("MID1", msg, "N5BA-10")
        text = raw.decode("latin-1")
        header_text, _, rest = text.partition("\r\n\r\n")

        body_len = next(
            int(line.split(":", 1)[1].strip())
            for line in header_text.splitlines()
            if line.startswith("Body:")
        )

        self.assertEqual(rest[:body_len], "Hi\r\nthere")


class BuildB2ProposalLineTests(unittest.TestCase):
    def test_format(self):
        line = w.build_b2_proposal_line("12345_N5BA", 527, 123)

        self.assertEqual(line, "FC EM 12345_N5BA 527 123 0")

    def test_round_trips_through_parse(self):
        line = w.build_b2_proposal_line("12345_N5BA", 527, 123)
        proposal = w.parse_b2_proposal(line)

        self.assertEqual(proposal.mid, "12345_N5BA")
        self.assertEqual(proposal.size, 527)
        self.assertEqual(proposal.compressed_size, 123)


class BuildProposalBlockTests(unittest.TestCase):
    def test_ends_with_recognized_end_of_block_marker(self):
        block = w.build_proposal_block(["FC EM 12345_N5BA 10 5 0"])

        self.assertTrue(w.has_end_of_block_marker(block))

    def test_checksum_matches_independent_calculation(self):
        # Cross-check the checksum against an independently-written
        # calculation of the same wl2k-go algorithm (two's-complement,
        # mod 256, of every line's characters plus a trailing "\r"
        # each) -- same "write it twice, compare" discipline used for
        # lzhuf's CRC-16 cross-check in tests/test_lzhuf.py, rather
        # than just asserting against build_proposal_block()'s own
        # output.
        lines = ["FC EM AAA 10 5 0", "FC EM BBB 20 8 0"]

        block = w.build_proposal_block(lines)

        expected = 0
        for line in lines:
            for c in line:
                expected += ord(c)
            expected += ord("\r")
        expected = (-expected) & 0xFF

        self.assertEqual(block, "\r".join(lines) + f"\rF> {expected:02X}")

    def test_single_line_checksum(self):
        block = w.build_proposal_block(["FC EM AAA 1 1 0"])

        self.assertTrue(block.startswith("FC EM AAA 1 1 0\rF> "))
        # 2 uppercase hex digits.
        checksum_part = block.rsplit(" ", 1)[1]
        self.assertRegex(checksum_part, r"^[0-9A-F]{2}$")


class BuildB2BlockTests(unittest.TestCase):
    def test_round_trips_through_parse(self):
        compressed = lzhuf.compress_b2(b"Hello, Winlink!")

        block = w.build_b2_block("Test Subject", compressed)
        parsed = w.parse_b2_blocks(block, 1)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].title, "Test Subject")
        self.assertEqual(parsed[0].compressed_data, compressed)

    def test_chunks_data_larger_than_chunk_size(self):
        compressed = lzhuf.compress_b2(b"x" * 1000)

        block = w.build_b2_block("Big", compressed, chunk_size=10)
        parsed = w.parse_b2_blocks(block, 1)

        self.assertEqual(parsed[0].compressed_data, compressed)


class ParseFsResponseTests(unittest.TestCase):
    def test_plain_accept_and_reject_symbols(self):
        self.assertEqual(w.parse_fs_response("FS +-", 2), ["accept", "reject"])

    def test_letter_codes(self):
        self.assertEqual(
            w.parse_fs_response("FS YNLHRE", 6),
            ["accept", "reject", "defer", "defer", "reject", "error"]
        )

    def test_offset_answer_treated_as_accept(self):
        # No persistent outbound queue to resume from -- see the
        # function's docstring. "!3350" and "A3350" both mean "accept,
        # resume from offset 3350" per spec; this module always sends
        # from the start instead.
        self.assertEqual(w.parse_fs_response("FS !3350", 1), ["accept"])
        self.assertEqual(w.parse_fs_response("FS A3350", 1), ["accept"])

    def test_bare_fs_prefix_without_space(self):
        self.assertEqual(w.parse_fs_response("FS", 0), [])

    def test_wrong_count_raises(self):
        with self.assertRaises(w.WinlinkProtocolError):
            w.parse_fs_response("FS +", 2)

    def test_unrecognized_character_raises(self):
        with self.assertRaises(w.WinlinkProtocolError):
            w.parse_fs_response("FS Z", 1)


class MessageBlockTests(unittest.TestCase):
    def test_splits_multiple_ctrl_z_terminated_blocks(self):
        text = "Title one\r\nBody one\r\n\x1aTitle two\r\nBody two\x1a"

        blocks = w.split_message_blocks(text, 2)

        self.assertEqual(len(blocks), 2)
        self.assertTrue(blocks[0].startswith("Title one"))
        self.assertTrue(blocks[1].startswith("Title two"))

    def test_trailing_remainder_after_last_ctrl_z_is_dropped(self):
        # Whatever follows the final ^Z (next proposal batch, FQ,
        # etc.) isn't a message body.
        text = "Title\r\nBody\x1aFQ\r\n"

        blocks = w.split_message_blocks(text, 1)

        self.assertEqual(blocks, ["Title\r\nBody"])

    def test_returns_fewer_than_count_if_not_all_arrived_yet(self):
        text = "Title one\r\nBody one\x1a"

        blocks = w.split_message_blocks(text, 2)

        self.assertEqual(len(blocks), 1)

    def test_parse_message_block_splits_title_and_body(self):
        proposal = w.parse_proposal(
            "FB P F6FBB FC1GHV FC1MVP 24657_F6FBB 1345"
        )

        message = w.parse_message_block(
            "Test Subject\r\nLine one\r\nLine two\r\n", proposal
        )

        self.assertEqual(message.title, "Test Subject")
        self.assertEqual(message.body, "Line one\nLine two")
        self.assertEqual(message.proposal, proposal)


class KAMXLWinlinkIntegrationTests(unittest.TestCase):
    """
    Exercises check_winlink_mail()'s composition of connect_station()/
    send_connected()/read_connected()/disconnect_station() -- call
    order, argument flow, and that disconnect_station() always runs
    even if a step in between raises. Same reasoning and pattern as
    test_pbbs.py's KAMXLPbbsIntegrationTests: connected-mode primitives
    are stubbed directly rather than driven through a full CannedSerial
    round trip, since chaining a multi-stage exchange (handshake,
    proposals, messages) through one shared canned-chunk queue doesn't
    compose cleanly with read_connected()'s "collect for N seconds"
    semantics.
    """

    def _kam_with_stubs(
        self,
        read_connected_chunks,
        mycall_lookup="N5BA-10/N5BA-10",
        read_connected_bytes_chunks=(),
    ):
        kam = make_kam(CannedSerial([]))
        calls = []
        remaining = list(read_connected_chunks)
        remaining_bytes = list(read_connected_bytes_chunks)

        kam.get = lambda command: calls.append(("get", command)) or mycall_lookup
        kam.connect_station = lambda callsign, **kwargs: calls.append(
            ("connect_station", callsign)
        )
        kam.send_connected = lambda text, **kwargs: calls.append(
            ("send_connected", text)
        )
        # Only exercised by outbound B2 message bodies (send_winlink_message())
        # -- mirrors send_connected() above, just for raw bytes (see
        # send_connected_bytes()'s docstring in kamxl.py).
        kam.send_connected_bytes = lambda data, **kwargs: calls.append(
            ("send_connected_bytes", data)
        )
        kam.read_connected = lambda **kwargs: (
            calls.append(("read_connected",))
            or (remaining.pop(0) if remaining else "")
        )
        # Only exercised by B2 ("FC") message bodies -- see
        # read_connected_bytes()'s docstring in kamxl.py for why the
        # binary message-body phase can't reuse the str-based
        # read_connected() stub above.
        kam.read_connected_bytes = lambda **kwargs: (
            calls.append(("read_connected_bytes",))
            or (remaining_bytes.pop(0) if remaining_bytes else b"")
        )
        kam.disconnect_station = lambda **kwargs: calls.append(
            ("disconnect_station",)
        )

        return kam, calls

    def test_no_mail_waiting_returns_empty_list(self):
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-FHM$]\r\n;PQ: 425\r\n>\r\n",
            "FQ\r\n",
        ])

        messages = kam.check_winlink_mail("N5BA-10", "FOOBAR")

        self.assertEqual(messages, [])
        # get MYCALL, connect, read (handshake), send (login+FF),
        # read (proposals -> FQ), disconnect.
        self.assertEqual(
            [call[0] for call in calls],
            [
                "get", "connect_station", "read_connected",
                "send_connected", "read_connected", "disconnect_station",
            ]
        )
        self.assertEqual(calls[0], ("get", "MYCALL"))
        self.assertEqual(calls[1], ("connect_station", "N5BA-10"))

        # Login response computed from the real challenge + password.
        sent = calls[3]
        self.assertEqual(sent[0], "send_connected")
        self.assertIn(";PR: " + w.secure_login_response("425", "FOOBAR"), sent[1])
        self.assertIn("FF", sent[1])

    def test_no_login_challenge_skips_pr_line(self):
        # Some gateways (e.g. a private/local RMS Packet setup) might
        # not require secure login at all.
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-FHM$]\r\nWelcome\r\n>\r\n",
            "FQ\r\n",
        ])

        kam.check_winlink_mail("N5BA-10", "FOOBAR")

        sent = calls[3]
        self.assertEqual(sent[0], "send_connected")
        self.assertNotIn(";PR:", sent[1])

    def test_own_echoed_transmission_not_mistaken_for_gateways_reply(self):
        """
        Regression test for a real bug found live against a real
        gateway (KD5EOC-10, Denton County Texas EOC). Verbatim shape
        of what was actually observed via daemon -v logging:

            08:29:22 conn-6720: connected
            08:29:33 winlink handshake raw: 'Welcome to the Denton
                County Texas EOC\\r\\n[WL2K-5.0-B2FWIHJM$]\\r\\n
                ;PQ: 20914129\\r\\nCMS via KD5EOC >\\r\\n'
            08:29:34 winlink proposals raw: ';FW: N5BA\\r\\n
                [kamxl-0.1-F$]\\r\\n;PR: 14482272\\r\\nFF\\r\\n'
            08:29:37 conn-6720: winlink.check_mail -> ok

            (webapp: "No mail waiting.")

            That "proposals raw" text is EXACTLY what we ourselves
            sent (;FW:/SID/;PR:/FF) -- the KAM-XL echoes connected-
            mode transmissions back to us, same as PBBS's already-
            known echo of its own "L" command. The old
            has_end_of_block_marker() treated a bare "FF" as "the
            gateway has nothing", so it stopped and returned "no
            mail" on seeing our OWN echo -- before the real gateway
            had said anything at all. It happened to still be the
            right answer that day (there really was no mail), but
            would have silently under-reported real waiting mail.

        This test feeds exactly that echoed chunk first, followed by
        the gateway's real (delayed) reply in a later read_connected()
        call, and confirms the real reply is what gets used --
        proving the fix, not just changing behavior.
        """
        kam, calls = self._kam_with_stubs([
            "Welcome to the Denton County Texas EOC\r\n"
            "[WL2K-5.0-B2FWIHJM$]\r\n;PQ: 20914129\r\nCMS via KD5EOC >\r\n",
            # Our own echoed transmission -- NOT the gateway's reply.
            ";FW: N5BA\r\n[kamxl-0.1-F$]\r\n;PR: 14482272\r\nFF\r\n",
            # The gateway's real, delayed reply.
            "FB P N0CALL N5BA N5BA 12345_N0CALL 42\r\nF>\r\n",
            "Test Subject\r\nHello there.\r\n\x1a",
        ])

        messages = kam.check_winlink_mail(
            "KD5EOC-10", "REALPASSWORD", mycall="N5BA"
        )

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].title, "Test Subject")
        self.assertEqual(messages[0].proposal.sender, "N0CALL")

    def test_gateway_disconnect_mid_session_raises_clear_error(self):
        """
        Regression test for a second real bug found live against
        KD5EOC-10 (Denton County Texas EOC). Verbatim shape of what
        was actually observed via daemon -v logging:

            08:47:39 conn-4464: connected
            08:47:50 winlink handshake raw: 'Welcome to the Denton
                County Texas EOC\\r\\n[WL2K-5.0-B2FWIHJM$]\\r\\n
                ;PQ: 97759037\\r\\nCMS via KD5EOC >\\r\\n'
            08:48:21 winlink proposals raw: ';FW: N5BA\\r\\n
                [kamxl-0.1-F$]\\r\\n;PR: 84304290\\r\\nFF\\r\\n***
                [3] Use B2 protocol - Disconnecting
                (47.190.139.106)\\r\\n*** DISCONNECTED\\r\\n
                cmd:N5BA>KD5EOC-10/2: <<UA>>:\\r\\n'
            08:48:26 conn-4464: winlink.check_mail -> KAMTimeoutError:
                Timed out returning to Command mode
            08:48:26 conn-4464: disconnected

        This gateway apparently requires B2 protocol support and
        disconnects rather than falling back to plain ASCII for a
        client, like this one, that never claims "B"/"B1"/"B2" in its
        own SID -- it printed "*** [3] Use B2 protocol -
        Disconnecting" and dropped the AX.25 link. The ~30 second gap
        between the "handshake raw" and "proposals raw" log lines
        matches the proposals-detection poll running to its full
        read_timeout (it never saw "F>"/"FQ", just our own echo
        followed by the disconnect banner). By the time
        check_winlink_mail() reached its finally-block
        disconnect_station() call, the KAM-XL had already
        auto-returned to Command mode on its own -- the "cmd:" prompt
        visible in "proposals raw" above was already consumed by that
        same poll, so disconnect_station()'s Ctrl-C had no fresh
        prompt left to wait for and reliably timed out 5 seconds
        later (matching the observed 08:48:21 -> 08:48:26 gap
        exactly).

        This test confirms the fix: check_winlink_mail() now detects
        the KAM-XL's own "*** DISCONNECTED" banner in the accumulated
        text, raises a clear KAMConnectionError describing what
        happened (quoting the gateway's stated reason) instead of the
        old confusing KAMTimeoutError, and skips the doomed
        disconnect_station() call entirely -- see
        parse_disconnect_reason()'s and check_winlink_mail()'s
        docstrings for the full story.
        """
        kam, calls = self._kam_with_stubs([
            "Welcome to the Denton County Texas EOC\r\n"
            "[WL2K-5.0-B2FWIHJM$]\r\n;PQ: 97759037\r\nCMS via KD5EOC >\r\n",
            ";FW: N5BA\r\n[kamxl-0.1-F$]\r\n;PR: 84304290\r\nFF\r\n"
            "*** [3] Use B2 protocol - Disconnecting (47.190.139.106)\r\n"
            "*** DISCONNECTED\r\ncmd:N5BA>KD5EOC-10/2: <<UA>>:\r\n",
        ])

        with self.assertRaises(KAMConnectionError) as ctx:
            kam.check_winlink_mail(
                "KD5EOC-10", "REALPASSWORD", mycall="N5BA",
                read_timeout=0.05
            )

        self.assertIn("KD5EOC-10", str(ctx.exception))
        self.assertIn("Use B2 protocol", str(ctx.exception))

        # The doomed disconnect_station() call must never fire -- the
        # KAM-XL already returned to Command mode on its own, and
        # calling it anyway is exactly what produced the old
        # confusing timeout.
        self.assertNotIn(
            "disconnect_station", [call[0] for call in calls]
        )

    def test_pending_mail_downloaded_and_parsed(self):
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-FHM$]\r\n;PQ: 425\r\n>\r\n",
            "FB P N0CALL N5BA-10 N5BA-10 12345_N0CALL 42\r\nF>\r\n",
            "Test Subject\r\nHello there.\r\n\x1a",
        ])

        messages = kam.check_winlink_mail("N5BA-10", "FOOBAR")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].title, "Test Subject")
        self.assertEqual(messages[0].body, "Hello there.")
        self.assertEqual(messages[0].proposal.sender, "N0CALL")

        # get, connect, read (handshake), send (login+FF),
        # read (proposals), send (FS), read (messages), disconnect.
        self.assertEqual(
            [call[0] for call in calls],
            [
                "get", "connect_station", "read_connected",
                "send_connected", "read_connected", "send_connected",
                "read_connected", "disconnect_station",
            ]
        )
        fs_call = calls[5]
        self.assertEqual(fs_call, ("send_connected", "FS +"))

    def test_b2_mail_downloaded_and_parsed(self):
        raw_message = _build_encapsulated_message(
            subject="Test B2 message",
            body="Hello from B2!\r\n",
        )
        compressed = lzhuf.compress_b2(raw_message)
        b2_block = w.build_b2_block("Test B2 message", compressed)

        proposal_line = (
            f"FC EM 12345_N0CALL {len(raw_message)} {len(compressed)} 0"
        )

        kam, calls = self._kam_with_stubs(
            read_connected_chunks=[
                "[WL2K-5.0-B2FWIHJM$]\r\n;PQ: 425\r\n>\r\n",
                f"{proposal_line}\r\nF>\r\n",
            ],
            read_connected_bytes_chunks=[b2_block],
        )

        messages = kam.check_winlink_mail("N5BA-10", "FOOBAR")

        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message.title, "Test B2 message")
        self.assertEqual(message.subject, "Test B2 message")
        self.assertEqual(message.from_, "N0CALL")
        self.assertEqual(message.to, ["N5BA"])
        self.assertEqual(message.body, "Hello from B2!\r\n")
        self.assertIsInstance(message.proposal, w.B2Proposal)

        # get, connect, read (handshake), send (login+FF), read
        # (proposals), send (FS), read_connected_bytes (b2 messages),
        # disconnect.
        self.assertEqual(
            [call[0] for call in calls],
            [
                "get", "connect_station", "read_connected",
                "send_connected", "read_connected", "send_connected",
                "read_connected_bytes", "disconnect_station",
            ]
        )

    def test_mixed_legacy_and_b2_proposals_raises(self):
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-B2FWIHJM$]\r\n>\r\n",
            (
                "FB P N0CALL N5BA-10 N5BA-10 12345_N0CALL 42\r\n"
                "FC EM 99999_N0CALL 100 50 0\r\n"
                "F>\r\n"
            ),
        ])

        with self.assertRaises(w.WinlinkProtocolError):
            kam.check_winlink_mail("N5BA-10", "FOOBAR")

        # Must still disconnect cleanly even though it raised.
        self.assertIn(("disconnect_station",), calls)

    def test_explicit_mycall_skips_mycall_lookup(self):
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-FHM$]\r\n>\r\n",
            "FQ\r\n",
        ])

        kam.check_winlink_mail("N5BA-10", "FOOBAR", mycall="N5BA-2")

        self.assertNotIn("get", [call[0] for call in calls])

        # calls: connect, read (handshake), send (login+FF), ...
        sent = calls[2]
        self.assertEqual(sent[0], "send_connected")
        self.assertIn("N5BA-2", sent[1])

    def test_disconnect_always_runs_even_if_read_fails(self):
        kam, calls = self._kam_with_stubs([])

        def failing_read(**kwargs):
            calls.append(("read_connected",))
            raise KAMTimeoutError("boom")

        kam.read_connected = failing_read

        with self.assertRaises(KAMTimeoutError):
            kam.check_winlink_mail("N5BA-10", "FOOBAR")

        self.assertIn(("disconnect_station",), calls)


class KAMXLSendWinlinkMessageIntegrationTests(unittest.TestCase):
    """
    Exercises send_winlink_message()'s composition of connect_station()/
    send_connected()/send_connected_bytes()/read_connected()/
    disconnect_station() -- same stubbing approach as
    KAMXLWinlinkIntegrationTests above, for the same reasons.

    UNVERIFIED AGAINST A REAL GATEWAY -- see winlink.py's module
    docstring's "SEND SUPPORT" section. These tests confirm the
    method's own internal logic and wire-format construction are
    self-consistent (e.g. a block it builds round-trips back through
    the same parsing this module uses for received mail), not that a
    real Winlink CMS accepts it.
    """

    def _kam_with_stubs(self, read_connected_chunks, mycall_lookup="N5BA-10/N5BA-10"):
        kam = make_kam(CannedSerial([]))
        calls = []
        remaining = list(read_connected_chunks)

        kam.get = lambda command: calls.append(("get", command)) or mycall_lookup
        kam.connect_station = lambda callsign, **kwargs: calls.append(
            ("connect_station", callsign)
        )
        kam.send_connected = lambda text, **kwargs: calls.append(
            ("send_connected", text)
        )
        kam.send_connected_bytes = lambda data, **kwargs: calls.append(
            ("send_connected_bytes", data)
        )
        kam.read_connected = lambda **kwargs: (
            calls.append(("read_connected",))
            or (remaining.pop(0) if remaining else "")
        )
        kam.disconnect_station = lambda **kwargs: calls.append(
            ("disconnect_station",)
        )

        return kam, calls

    def test_sends_single_message_and_declines_reciprocal_mail(self):
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-B2FWIHJM$]\r\n;PQ: 425\r\n>\r\n",
            "FS +\r\n",
            "FF\r\n",
        ])

        outgoing = w.OutgoingMessage(
            to=["N0CALL"], subject="Test outbound", body="Hello there\n"
        )

        accepted = kam.send_winlink_message(
            "KD5EOC-10", "FOOBAR", [outgoing], mycall="N5BA-10"
        )

        self.assertEqual(len(accepted), 1)

        self.assertEqual(
            [call[0] for call in calls],
            [
                "connect_station", "read_connected", "send_connected",
                "read_connected", "send_connected_bytes", "read_connected",
                "disconnect_station",
            ]
        )

        proposal_call = calls[2]
        self.assertEqual(proposal_call[0], "send_connected")
        self.assertIn(f"FC EM {accepted[0]}", proposal_call[1])
        self.assertIn(
            ";PR: " + w.secure_login_response("425", "FOOBAR"),
            proposal_call[1]
        )

        # Round-trip the actual bytes transmitted for the message body
        # through the same parsing this module uses for RECEIVED mail
        # -- confirms the block we build to send really does decode
        # back into what we asked to send, not just that it "looks
        # right" by inspection.
        block_call = calls[4]
        self.assertEqual(block_call[0], "send_connected_bytes")

        parsed_blocks = w.parse_b2_blocks(block_call[1], 1)
        self.assertEqual(len(parsed_blocks), 1)

        raw = lzhuf.decompress_b2(parsed_blocks[0].compressed_data)
        encapsulated = w.parse_encapsulated_message(raw)

        self.assertEqual(encapsulated.mid, accepted[0])
        self.assertEqual(encapsulated.subject, "Test outbound")
        self.assertEqual(encapsulated.to, ["N0CALL"])
        self.assertEqual(encapsulated.from_, "N5BA-10")
        self.assertEqual(encapsulated.body, "Hello there\r\n")

    def test_gateway_rejects_message_nothing_uploaded(self):
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-B2FWIHJM$]\r\n>\r\n",
            "FS -\r\n",
            "FF\r\n",
        ])

        outgoing = w.OutgoingMessage(to=["N0CALL"], subject="Hi", body="Hello")

        accepted = kam.send_winlink_message(
            "KD5EOC-10", "FOOBAR", [outgoing], mycall="N5BA-10"
        )

        self.assertEqual(accepted, [])
        self.assertNotIn("send_connected_bytes", [c[0] for c in calls])

    def test_declines_gateways_reciprocal_proposal(self):
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-B2FWIHJM$]\r\n>\r\n",
            "FS +\r\n",
            # Gateway's own reciprocal proposal -- with a checksum
            # suffix on "F>", exercising has_end_of_block_marker()'s
            # new tolerance for it (see that function's docstring).
            "FC EM 99999_N0CALL 100 50 0\r\nF> 3A\r\n",
        ])

        outgoing = w.OutgoingMessage(to=["N0CALL"], subject="Hi", body="Hello")

        kam.send_winlink_message(
            "KD5EOC-10", "FOOBAR", [outgoing], mycall="N5BA-10"
        )

        # Send-only scope -- always decline whatever the gateway
        # offers back (see winlink.py's module docstring). The last
        # send_connected call (after our own message block) should be
        # our FS reject line answering their one proposed message.
        send_calls = [c for c in calls if c[0] == "send_connected"]
        self.assertEqual(send_calls[-1][1], "FS -")

    def test_no_messages_raises_value_error(self):
        kam, calls = self._kam_with_stubs([])

        with self.assertRaises(ValueError):
            kam.send_winlink_message("KD5EOC-10", "FOOBAR", [])

        self.assertNotIn("connect_station", [c[0] for c in calls])

    def test_too_many_messages_raises_value_error(self):
        kam, calls = self._kam_with_stubs([])
        messages = [
            w.OutgoingMessage(to=["N0CALL"], subject="x", body="x")
            for _ in range(6)
        ]

        with self.assertRaises(ValueError):
            kam.send_winlink_message("KD5EOC-10", "FOOBAR", messages)

        self.assertNotIn("connect_station", [c[0] for c in calls])

    def test_multiple_messages_partial_accept(self):
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-B2FWIHJM$]\r\n>\r\n",
            "FS +-\r\n",
            "FQ\r\n",
        ])

        messages = [
            w.OutgoingMessage(to=["N0CALL"], subject="First", body="One"),
            w.OutgoingMessage(to=["N1CALL"], subject="Second", body="Two"),
        ]

        accepted = kam.send_winlink_message(
            "KD5EOC-10", "FOOBAR", messages, mycall="N5BA-10"
        )

        self.assertEqual(len(accepted), 1)

        block_calls = [c for c in calls if c[0] == "send_connected_bytes"]
        self.assertEqual(len(block_calls), 1)

    def test_gateway_disconnect_mid_send_raises_clear_error(self):
        kam, calls = self._kam_with_stubs([
            "[WL2K-5.0-B2FWIHJM$]\r\n>\r\n",
            "*** Unknown client types are not allowed on production "
            "servers -- use cms-z.winlink.org - Disconnecting "
            "(1.2.3.4)\r\n*** DISCONNECTED\r\n"
            "cmd:N5BA-10>KD5EOC-10: <<UA>>:\r\n",
        ])

        outgoing = w.OutgoingMessage(to=["N0CALL"], subject="Hi", body="Hello")

        with self.assertRaises(KAMConnectionError) as ctx:
            kam.send_winlink_message(
                "KD5EOC-10", "FOOBAR", [outgoing], mycall="N5BA-10",
                read_timeout=0.05
            )

        self.assertIn("KD5EOC-10", str(ctx.exception))

        # Same reasoning as check_winlink_mail()'s own regression test
        # -- the KAM-XL already returned to Command mode on its own,
        # so the doomed disconnect_station() call must be skipped.
        self.assertNotIn(
            "disconnect_station", [call[0] for call in calls]
        )


if __name__ == "__main__":
    unittest.main()
