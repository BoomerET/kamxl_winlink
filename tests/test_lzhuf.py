"""
Offline tests for lzhuf.py.

IMPORTANT, same caveat as tests/test_winlink.py: this codec is ported
from two independently-authored reference implementations (the
official Winlink Dev Team's VB.NET source and wl2k-go's Go source --
see lzhuf.py's module docstring) that agree exactly on every constant
and table, which is the verification this project relies on for
security/correctness-critical code it can't test against real hardware
directly. What these tests actually prove is internal consistency:
compress() and decompress() invert each other across a range of
inputs, and the two CRC-16 formulations described in the two reference
sources agree with each other. What they do NOT prove is byte-for-byte
interop with a real gateway's own LZHUF encoder -- that needs a real
B2-compressed message captured off the air, which hasn't happened yet.
"""

import random
import unittest

import lzhuf


def _crc16_vb_style(data: bytes) -> int:
    """
    Alternative CRC-16 formulation, matching the VB reference's literal
    accumulation: fold the byte into the table index rather than XOR
    it onto the lookup result, with no trailing zero-byte flush.

    lzhuf._crc16() uses wl2k-go's formulation instead (table indexed by
    the high byte alone, input byte XORed onto the result, with two
    trailing zero bytes pushed through before returning). Empirically
    (verified below across a range of inputs, not just asserted from
    theory) these two REGISTER values are identical -- Go's "push two
    zero bytes" step turns out to produce exactly the same running
    value as VB's plain no-padding accumulation would, for this
    particular table/polynomial.

    VB's own GetCRC() applies a byte-swap on top of this before
    returning -- but that swap isn't needed to match Go's register
    value (confirmed here); it exists because VB then writes the
    result in big-endian order, whereas this codebase (like Go) writes
    it little-endian. Swap-then-big-endian and plain-then-little-endian
    produce the same two wire bytes either way -- so the swap is a
    serialization-order detail, not a different checksum value, and
    intentionally isn't replicated here since it would make this
    cross-check compare the wrong thing.
    """
    crc = 0

    for byte in data:
        crc = ((crc << 8) ^ lzhuf._CRC16_TABLE[(crc >> 8) ^ byte]) & 0xFFFF

    return crc


class CRC16CrossCheckTests(unittest.TestCase):
    def test_two_independent_formulations_agree(self):
        samples = [
            b"",
            b"A",
            b"\x00\x00\x00\x00",
            b"Hello, Winlink!",
            bytes(range(256)),
            bytes([0xFF] * 37),
            bytes(random.Random(42).randrange(256) for _ in range(500)),
        ]

        for data in samples:
            self.assertEqual(
                lzhuf._crc16(data),
                _crc16_vb_style(data),
                msg=f"CRC formulations disagree for {data[:20]!r}...",
            )


class RoundTripTests(unittest.TestCase):
    def _assert_round_trips(self, data: bytes):
        compressed = lzhuf.compress(data)
        restored = lzhuf.decompress(compressed)
        self.assertEqual(restored, data)

        wrapped = lzhuf.compress_b2(data)
        restored_b2 = lzhuf.decompress_b2(wrapped)
        self.assertEqual(restored_b2, data)

    def test_empty(self):
        self._assert_round_trips(b"")

    def test_single_byte(self):
        self._assert_round_trips(b"X")

    def test_short_ascii(self):
        self._assert_round_trips(b"Hello, Winlink!")

    def test_highly_repetitive_text(self):
        # Exercises the LZSS back-reference path heavily.
        self._assert_round_trips(b"abcabcabcabcabcabcabcabcabcabc" * 50)

    def test_realistic_message_body(self):
        body = (
            b"Mid: 12345_N5BA\r\n"
            b"Date: 2026/08/03 12:00\r\n"
            b"Type: Private\r\n"
            b"From: N5BA\r\n"
            b"To: N0CALL\r\n"
            b"Subject: Test message via B2 protocol\r\n"
            b"Body: 26\r\n"
            b"\r\n"
            b"Hello there, this is a test.\r\n"
        )
        self._assert_round_trips(body)

    def test_all_256_byte_values(self):
        self._assert_round_trips(bytes(range(256)))

    def test_random_incompressible_data(self):
        data = bytes(random.Random(7).randrange(256) for _ in range(2000))
        self._assert_round_trips(data)

    def test_long_text_with_repeats_spanning_window(self):
        # Bigger than the 2048-byte sliding window, so this exercises
        # window wraparound in both the encoder's dictionary and the
        # decoder's sliding history buffer.
        chunk = b"The quick brown fox jumps over the lazy dog. " * 20
        data = chunk * 10
        self.assertGreater(len(data), 2048)
        self._assert_round_trips(data)


class ErrorHandlingTests(unittest.TestCase):
    def test_decompress_too_short_for_header(self):
        with self.assertRaises(lzhuf.LZHUFError):
            lzhuf.decompress(b"\x00\x00")

    def test_decompress_b2_too_short_for_crc_header(self):
        with self.assertRaises(lzhuf.LZHUFError):
            lzhuf.decompress_b2(b"\x00")

    def test_decompress_b2_detects_corruption(self):
        wrapped = bytearray(lzhuf.compress_b2(b"Hello, Winlink!"))
        # Flip a bit deep in the compressed payload (past the CRC and
        # length header).
        wrapped[-1] ^= 0xFF

        with self.assertRaises(lzhuf.ChecksumError):
            lzhuf.decompress_b2(bytes(wrapped))

    def test_decompress_b2_detects_corrupted_crc_header_itself(self):
        wrapped = bytearray(lzhuf.compress_b2(b"Hello, Winlink!"))
        wrapped[0] ^= 0xFF

        with self.assertRaises(lzhuf.ChecksumError):
            lzhuf.decompress_b2(bytes(wrapped))


if __name__ == "__main__":
    unittest.main()
