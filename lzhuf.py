"""
LZHUF compression/decompression -- the codec Winlink's B2F protocol
extension uses to compress message bodies before binary transfer (see
winlink.py's module docstring for how this fits into the bigger
picture). This module has no Winlink-specific knowledge at all -- it's
a general-purpose codec, kept separate the same way packet.py doesn't
know about PBBS.

WHY THIS EXISTS: the plain-ASCII FBB tier this project started with
(milestone 8's first pass) never claims "B"/"B1"/"B2" in its own SID,
so a real gateway never proposes a compressed message. That changed
when a real gateway (KD5EOC-10) was found to *require* B2 and
disconnect rather than fall back to ASCII for a non-B2 client (see
winlink.parse_disconnect_reason()'s docstring) -- supporting B2 means
actually implementing this compression.

SOURCES, cross-checked against each other before writing any code (the
same "verify against a trusted reference, not just the spec" standard
this project holds for security/correctness-critical code -- see
winlink.py's secure_login_response()):

  - The Winlink Development Team's own official reference
    implementation: https://github.com/ARSFI/Winlink-Compression
    (WinlinkSupport.vb, VB.NET, explicitly published as "source code
    for LZH compression used in Winlink B2F message forwarding").
    This is the primary reference this module is ported from.
  - wl2k-go (https://github.com/la5nta/wl2k-go), a real, widely-used
    open-source Winlink client library (already trusted elsewhere in
    this project for the secure-login algorithm) -- its lzhuf package
    is an *independently authored* Go implementation of the same
    codec.

These two implementations were compared line-by-line before writing
this port. They agree EXACTLY on every constant, every table (the
2048-byte sliding window, the 64-entry position encode/decode tables,
the 256-entry CRC-16 table -- all byte-for-byte identical between the
VB and Go sources despite being written independently, in different
languages, by different teams), and on the core LZSS-tree and adaptive
Huffman tree algorithms. That agreement is the actual verification
here -- this module is a direct, careful translation of that shared
algorithm, primarily following the VB source's structure since it's
the official one, with the Go source used throughout to cross-check
every step (particularly useful for the CRC-16 finalization, where the
two sources use superficially different-looking formulas -- VB computes
a running register and byte-swaps it at the end, Go pushes two extra
zero bytes through the update function and does not swap -- see
_crc16() below; tests/test_lzhuf.py proves both formulations agree on
arbitrary input, which is expected since a linear CRC's table-driven
update commutes with this particular table/polynomial's byte ordering).

WHAT'S VERIFIED AND WHAT ISN'T: the algorithm itself (constants, tables,
tree logic) is cross-checked against two independent sources, and this
module's own compress()/decompress() round-trip on arbitrary input
(tests/test_lzhuf.py) proves internal consistency. What's NOT yet
verified is byte-for-byte interop with a real gateway's own encoder --
that needs a real B2-compressed message captured off the air, which
hasn't happened yet (no populated mailbox tested against a B2 gateway
so far). Going in, no Go or .NET toolchain was available in this
sandbox to cross-compile either reference implementation and compare
compressed output byte-for-byte -- that remains a good follow-up
verification step if a real captured B2 message ever surfaces a parsing
bug, the same way real hardware corrected pbbs.py's and packet.py's
first-draft parsing.

WIRE FORMAT (the "B2" variant used by Winlink -- see compress_b2()/
decompress_b2()):

    [2 bytes: CRC-16 of everything that follows, little-endian]
    [4 bytes: length of the ORIGINAL uncompressed data, little-endian]
    [N bytes: LZHUF-compressed data]

The plain (non-B2) compress()/decompress() functions omit the leading
CRC-16 -- included for completeness and because it's useful on its own
for round-trip testing, but Winlink always uses the "_b2" wrapped form.
"""


class LZHUFError(Exception):
    """Base class for this module's errors."""


class ChecksumError(LZHUFError):
    """The B2 CRC-16 header didn't match the compressed data."""


# ---------------------------------------------------------------------------
# Constants (verbatim from both the VB and Go references -- they agree
# exactly on every one of these)
# ---------------------------------------------------------------------------

_N = 2048                        # Sliding window (dictionary) size.
_F = 60                          # Look-ahead buffer size.
_THRESHOLD = 2
_NIL = _N                        # "Null" tree pointer.
_NCHAR = 256 - _THRESHOLD + _F   # 314 -- character codes 0..NCHAR-1.
_T = (_NCHAR * 2) - 1            # 627 -- Huffman tree table size.
_R = _T - 1                      # 626 -- Huffman tree root position.
_MAX_FREQ = 0x8000

# Position encode/decode tables -- upper 6 bits of a match position are
# Huffman-coded via these fixed tables (the lower 6 bits are sent
# verbatim). 64 entries each, one per possible 6-bit value.
_P_CODE = [
    0x00, 0x20, 0x30, 0x40, 0x50, 0x58, 0x60, 0x68,
    0x70, 0x78, 0x80, 0x88, 0x90, 0x94, 0x98, 0x9C,
    0xA0, 0xA4, 0xA8, 0xAC, 0xB0, 0xB4, 0xB8, 0xBC,
    0xC0, 0xC2, 0xC4, 0xC6, 0xC8, 0xCA, 0xCC, 0xCE,
    0xD0, 0xD2, 0xD4, 0xD6, 0xD8, 0xDA, 0xDC, 0xDE,
    0xE0, 0xE2, 0xE4, 0xE6, 0xE8, 0xEA, 0xEC, 0xEE,
    0xF0, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7,
    0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD, 0xFE, 0xFF,
]
_P_LEN = [
    0x03, 0x04, 0x04, 0x04, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x06, 0x06, 0x06, 0x06,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08,
    0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08,
]
_D_CODE = [
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02,
    0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02, 0x02,
    0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03,
    0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08,
    0x09, 0x09, 0x09, 0x09, 0x09, 0x09, 0x09, 0x09,
    0x0A, 0x0A, 0x0A, 0x0A, 0x0A, 0x0A, 0x0A, 0x0A,
    0x0B, 0x0B, 0x0B, 0x0B, 0x0B, 0x0B, 0x0B, 0x0B,
    0x0C, 0x0C, 0x0C, 0x0C, 0x0D, 0x0D, 0x0D, 0x0D,
    0x0E, 0x0E, 0x0E, 0x0E, 0x0F, 0x0F, 0x0F, 0x0F,
    0x10, 0x10, 0x10, 0x10, 0x11, 0x11, 0x11, 0x11,
    0x12, 0x12, 0x12, 0x12, 0x13, 0x13, 0x13, 0x13,
    0x14, 0x14, 0x14, 0x14, 0x15, 0x15, 0x15, 0x15,
    0x16, 0x16, 0x16, 0x16, 0x17, 0x17, 0x17, 0x17,
    0x18, 0x18, 0x19, 0x19, 0x1A, 0x1A, 0x1B, 0x1B,
    0x1C, 0x1C, 0x1D, 0x1D, 0x1E, 0x1E, 0x1F, 0x1F,
    0x20, 0x20, 0x21, 0x21, 0x22, 0x22, 0x23, 0x23,
    0x24, 0x24, 0x25, 0x25, 0x26, 0x26, 0x27, 0x27,
    0x28, 0x28, 0x29, 0x29, 0x2A, 0x2A, 0x2B, 0x2B,
    0x2C, 0x2C, 0x2D, 0x2D, 0x2E, 0x2E, 0x2F, 0x2F,
    0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37,
    0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F,
]
_D_LEN = [
    0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03,
    0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03,
    0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03,
    0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03, 0x03,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05, 0x05,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06, 0x06,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07, 0x07,
    0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08,
    0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08, 0x08,
]

# CRC-16 table -- used only for the B2 wrapper's 2-byte checksum of the
# compressed data (compress_b2()/decompress_b2()), NOT part of the core
# LZHUF codec itself. Matches both the VB reference's "Compression"
# class's internal CRCTable and wl2k-go's crc16tab exactly.
_CRC16_TABLE = [
    0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7,
    0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF,
    0x1231, 0x0210, 0x3273, 0x2252, 0x52B5, 0x4294, 0x72F7, 0x62D6,
    0x9339, 0x8318, 0xB37B, 0xA35A, 0xD3BD, 0xC39C, 0xF3FF, 0xE3DE,
    0x2462, 0x3443, 0x0420, 0x1401, 0x64E6, 0x74C7, 0x44A4, 0x5485,
    0xA56A, 0xB54B, 0x8528, 0x9509, 0xE5EE, 0xF5CF, 0xC5AC, 0xD58D,
    0x3653, 0x2672, 0x1611, 0x0630, 0x76D7, 0x66F6, 0x5695, 0x46B4,
    0xB75B, 0xA77A, 0x9719, 0x8738, 0xF7DF, 0xE7FE, 0xD79D, 0xC7BC,
    0x48C4, 0x58E5, 0x6886, 0x78A7, 0x0840, 0x1861, 0x2802, 0x3823,
    0xC9CC, 0xD9ED, 0xE98E, 0xF9AF, 0x8948, 0x9969, 0xA90A, 0xB92B,
    0x5AF5, 0x4AD4, 0x7AB7, 0x6A96, 0x1A71, 0x0A50, 0x3A33, 0x2A12,
    0xDBFD, 0xCBDC, 0xFBBF, 0xEB9E, 0x9B79, 0x8B58, 0xBB3B, 0xAB1A,
    0x6CA6, 0x7C87, 0x4CE4, 0x5CC5, 0x2C22, 0x3C03, 0x0C60, 0x1C41,
    0xEDAE, 0xFD8F, 0xCDEC, 0xDDCD, 0xAD2A, 0xBD0B, 0x8D68, 0x9D49,
    0x7E97, 0x6EB6, 0x5ED5, 0x4EF4, 0x3E13, 0x2E32, 0x1E51, 0x0E70,
    0xFF9F, 0xEFBE, 0xDFDD, 0xCFFC, 0xBF1B, 0xAF3A, 0x9F59, 0x8F78,
    0x9188, 0x81A9, 0xB1CA, 0xA1EB, 0xD10C, 0xC12D, 0xF14E, 0xE16F,
    0x1080, 0x00A1, 0x30C2, 0x20E3, 0x5004, 0x4025, 0x7046, 0x6067,
    0x83B9, 0x9398, 0xA3FB, 0xB3DA, 0xC33D, 0xD31C, 0xE37F, 0xF35E,
    0x02B1, 0x1290, 0x22F3, 0x32D2, 0x4235, 0x5214, 0x6277, 0x7256,
    0xB5EA, 0xA5CB, 0x95A8, 0x8589, 0xF56E, 0xE54F, 0xD52C, 0xC50D,
    0x34E2, 0x24C3, 0x14A0, 0x0481, 0x7466, 0x6447, 0x5424, 0x4405,
    0xA7DB, 0xB7FA, 0x8799, 0x97B8, 0xE75F, 0xF77E, 0xC71D, 0xD73C,
    0x26D3, 0x36F2, 0x0691, 0x16B0, 0x6657, 0x7676, 0x4615, 0x5634,
    0xD94C, 0xC96D, 0xF90E, 0xE92F, 0x99C8, 0x89E9, 0xB98A, 0xA9AB,
    0x5844, 0x4865, 0x7806, 0x6827, 0x18C0, 0x08E1, 0x3882, 0x28A3,
    0xCB7D, 0xDB5C, 0xEB3F, 0xFB1E, 0x8BF9, 0x9BD8, 0xABBB, 0xBB9A,
    0x4A75, 0x5A54, 0x6A37, 0x7A16, 0x0AF1, 0x1AD0, 0x2AB3, 0x3A92,
    0xFD2E, 0xED0F, 0xDD6C, 0xCD4D, 0xBDAA, 0xAD8B, 0x9DE8, 0x8DC9,
    0x7C26, 0x6C07, 0x5C64, 0x4C45, 0x3CA2, 0x2C83, 0x1CE0, 0x0CC1,
    0xEF1F, 0xFF3E, 0xCF5D, 0xDF7C, 0xAF9B, 0xBFBA, 0x8FD9, 0x9FF8,
    0x6E17, 0x7E36, 0x4E55, 0x5E74, 0x2E93, 0x3EB2, 0x0ED1, 0x1EF0,
]


def _crc16_update(crc: int, byte: int) -> int:
    """
    One step of the CRC-16 used by the B2 wrapper. Matches wl2k-go's
    udpCRC16() exactly: the table is indexed by the running register's
    high byte alone, with the input byte XORed onto the *result* of
    that lookup rather than folded into the index (as the VB reference
    does it instead: ``table[(crc>>8) XOR byte]``, with no trailing
    padding). tests/test_lzhuf.py computes both ways across a range of
    inputs and confirms this function's two-trailing-zero-byte "flush"
    (see _crc16() below) makes its result equal the VB formulation's
    plain unpadded result, exactly, for every input tried -- not just
    assumed from theory. VB then applies a byte-swap on top of its
    result that this module deliberately does not replicate -- see
    _crc16()'s docstring for why that's correct, not an oversight.
    """
    return ((crc << 8) & 0xFF00) ^ _CRC16_TABLE[(crc >> 8) & 0xFF] ^ byte


def _crc16(data: bytes) -> int:
    """
    The B2 wrapper's checksum over ``data``, written to the wire as 2
    little-endian bytes (see compress_b2()/decompress_b2()).

    Two trailing zero bytes are pushed through the update function
    before returning -- matches wl2k-go's crc()/Sum() exactly. The VB
    reference computes its version of this value differently: no
    trailing zero bytes, but a byte-swap of the final register. Both
    approaches produce the same two wire bytes once each is written in
    its own implementation's natural byte order (this module's
    little-endian vs. VB's big-endian) -- swap-then-big-endian and
    plain-then-little-endian come out identical, confirmed empirically
    in tests/test_lzhuf.py rather than just assumed.
    """
    crc = 0

    for byte in data:
        crc = _crc16_update(crc, byte)

    crc = _crc16_update(crc, 0)
    crc = _crc16_update(crc, 0)

    return crc


# ---------------------------------------------------------------------------
# Shared Huffman/LZSS tree state
# ---------------------------------------------------------------------------

class _Tree:
    """
    The adaptive Huffman tree + LZSS dictionary-search tree shared by
    both compress() and decompress(). One fresh instance per call --
    unlike the VB reference (which uses process-wide ``Shared`` fields
    guarded by a lock), so this is naturally safe to call concurrently
    without any synchronization.

    Array sizes match wl2k-go's Go port exactly (a statically-sized,
    compiled language's array bounds are a stronger proof of the real
    required bounds than the VB source's slightly looser allocations).
    """

    __slots__ = (
        "text_buf", "dad", "lson", "rson",
        "freq", "son", "prnt",
        "match_length", "match_position",
    )

    def __init__(self) -> None:
        self.text_buf = bytearray(_N + _F)
        self.dad = [0] * (_N + 1)
        self.lson = [0] * (_N + 1)
        self.rson = [0] * (_N + 257)
        self.freq = [0] * (_T + 1)
        self.son = [0] * _T
        self.prnt = [0] * (_T + _NCHAR)
        self.match_length = 0
        self.match_position = 0

    def init_tree(self) -> None:
        for i in range(_N + 1, _N + 257):
            self.rson[i] = _NIL

        for i in range(_N):
            self.dad[i] = _NIL

    def start_huff(self) -> None:
        for i in range(_NCHAR):
            self.freq[i] = 1
            self.son[i] = i + _T
            self.prnt[i + _T] = i

        i = 0
        j = _NCHAR

        while j <= _R:
            self.freq[j] = self.freq[i] + self.freq[i + 1]
            self.son[j] = i
            self.prnt[i] = j
            self.prnt[i + 1] = j
            i += 2
            j += 1

        self.freq[_T] = 0xFFFF
        self.prnt[_R] = 0

    def insert_node(self, r: int) -> None:
        cmp = 1
        p = _N + 1 + self.text_buf[r]
        self.rson[r] = _NIL
        self.lson[r] = _NIL
        self.match_length = 0

        while True:
            if cmp >= 0:
                if self.rson[p] != _NIL:
                    p = self.rson[p]
                else:
                    self.rson[p] = r
                    self.dad[r] = p
                    return
            else:
                if self.lson[p] != _NIL:
                    p = self.lson[p]
                else:
                    self.lson[p] = r
                    self.dad[r] = p
                    return

            i = 1

            while i < _F:
                cmp = self.text_buf[r + i] - self.text_buf[p + i]

                if cmp != 0:
                    break

                i += 1

            if i > _THRESHOLD:
                if i > self.match_length:
                    self.match_position = ((r - p) & (_N - 1)) - 1
                    self.match_length = i

                    if self.match_length >= _F:
                        break

                if i == self.match_length:
                    c = ((r - p) & (_N - 1)) - 1

                    if c < self.match_position:
                        self.match_position = c

        self.dad[r] = self.dad[p]
        self.lson[r] = self.lson[p]
        self.rson[r] = self.rson[p]
        self.dad[self.lson[p]] = r
        self.dad[self.rson[p]] = r

        if self.rson[self.dad[p]] == p:
            self.rson[self.dad[p]] = r
        else:
            self.lson[self.dad[p]] = r

        self.dad[p] = _NIL

    def delete_node(self, p: int) -> None:
        if self.dad[p] == _NIL:
            return

        if self.rson[p] == _NIL:
            q = self.lson[p]
        elif self.lson[p] == _NIL:
            q = self.rson[p]
        else:
            q = self.lson[p]

            if self.rson[q] != _NIL:
                while self.rson[q] != _NIL:
                    q = self.rson[q]

                self.rson[self.dad[q]] = self.lson[q]
                self.dad[self.lson[q]] = self.dad[q]
                self.lson[q] = self.lson[p]
                self.dad[self.lson[p]] = q

            self.rson[q] = self.rson[p]
            self.dad[self.rson[p]] = q

        self.dad[q] = self.dad[p]

        if self.rson[self.dad[p]] == p:
            self.rson[self.dad[p]] = q
        else:
            self.lson[self.dad[p]] = q

        self.dad[p] = _NIL

    def reconst(self) -> None:
        j = 0

        for i in range(_T):
            if self.son[i] >= _T:
                self.freq[j] = (self.freq[i] + 1) // 2
                self.son[j] = self.son[i]
                j += 1

        i = 0
        j = _NCHAR

        while j < _T:
            k = i + 1
            f = self.freq[i] + self.freq[k]
            self.freq[j] = f
            k = j - 1

            while f < self.freq[k]:
                k -= 1

            k += 1
            n = j

            while n >= k + 1:
                self.freq[n] = self.freq[n - 1]
                self.son[n] = self.son[n - 1]
                n -= 1

            self.freq[k] = f
            self.son[k] = i
            i += 2
            j += 1

        for i in range(_T):
            k = self.son[i]
            self.prnt[k] = i

            if k < _T:
                self.prnt[k + 1] = i

    def update(self, c: int) -> None:
        if self.freq[_R] == _MAX_FREQ:
            self.reconst()

        c = self.prnt[c + _T]

        while True:
            self.freq[c] += 1
            k = self.freq[c]
            n = c + 1

            if k > self.freq[n]:
                while k > self.freq[n + 1]:
                    n += 1

                self.freq[c] = self.freq[n]
                self.freq[n] = k

                i = self.son[c]
                self.prnt[i] = n

                if i < _T:
                    self.prnt[i + 1] = n

                j = self.son[n]
                self.son[n] = i

                self.prnt[j] = c

                if j < _T:
                    self.prnt[j + 1] = c

                self.son[c] = j
                c = n

            c = self.prnt[c]

            if c == 0:
                break


# ---------------------------------------------------------------------------
# Bit I/O
# ---------------------------------------------------------------------------

class _BitWriter:
    __slots__ = ("out", "putbuf", "putlen")

    def __init__(self) -> None:
        self.out = bytearray()
        self.putbuf = 0
        self.putlen = 0

    def put_code(self, length: int, code: int) -> None:
        self.putbuf = (self.putbuf | (code >> self.putlen)) & 0xFFFF
        self.putlen += length

        if self.putlen < 8:
            return

        self.out.append((self.putbuf >> 8) & 0xFF)
        self.putlen -= 8

        if self.putlen >= 8:
            self.out.append(self.putbuf & 0xFF)
            self.putlen -= 8
            self.putbuf = (code << (length - self.putlen)) & 0xFFFF
        else:
            self.putbuf = (self.putbuf << 8) & 0xFFFF

    def flush(self) -> None:
        if self.putlen > 0:
            self.out.append((self.putbuf >> 8) & 0xFF)


class _BitReader:
    """
    Deliberately matches the reference implementations' behavior of
    silently synthesizing zero bytes once ``data`` runs out (needed
    because the bit buffer routinely "looks ahead" by a byte or two
    past the logically-last symbol it will actually decode -- a real,
    harmless quirk of both the VB and Go references, not unique to
    this port). Truncated/corrupt input is caught one layer up, before
    any bit-level decoding is attempted: decompress_b2() verifies the
    B2 CRC-16 over the raw compressed bytes first, so a truncated
    message never reaches this class in the first place when arriving
    through the normal B2 path.
    """

    __slots__ = ("data", "pos", "getbuf", "getlen")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.getbuf = 0
        self.getlen = 0

    def _getc(self) -> int:
        if self.pos < len(self.data):
            c = self.data[self.pos]
            self.pos += 1
            return c

        return 0

    def get_bit(self) -> int:
        while self.getlen <= 8:
            self.getbuf = (self.getbuf | (self._getc() << (8 - self.getlen))) & 0xFFFF
            self.getlen += 8

        val = (self.getbuf >> 15) & 1
        self.getbuf = (self.getbuf << 1) & 0xFFFF
        self.getlen -= 1
        return val

    def get_byte(self) -> int:
        while self.getlen <= 8:
            self.getbuf = (self.getbuf | (self._getc() << (8 - self.getlen))) & 0xFFFF
            self.getlen += 8

        val = (self.getbuf >> 8) & 0xFF
        self.getbuf = (self.getbuf << 8) & 0xFFFF
        self.getlen -= 8
        return val


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------

def _encode_char(tree: _Tree, writer: _BitWriter, c: int) -> None:
    code = 0
    length = 0
    k = tree.prnt[c + _T]

    while True:
        code >>= 1

        if k & 1:
            code += 0x8000

        length += 1
        k = tree.prnt[k]

        if k == _R:
            break

    writer.put_code(length, code)
    tree.update(c)


def _encode_position(tree: _Tree, writer: _BitWriter, c: int) -> None:
    i = c >> 6
    writer.put_code(_P_LEN[i], _P_CODE[i] << 8)
    writer.put_code(6, (c & 0x3F) << 10)


def compress(data: bytes) -> bytes:
    """
    LZHUF-compress ``data``, returning [4-byte little-endian original
    length][compressed bytes] -- no CRC-16 header (see compress_b2()
    for the wrapped form Winlink actually uses on the wire).
    """
    size = len(data)

    if size == 0:
        return b"\x00\x00\x00\x00"

    tree = _Tree()
    tree.init_tree()
    tree.start_huff()
    writer = _BitWriter()

    s = 0
    r = _N - _F

    for i in range(r):
        tree.text_buf[i] = 0x20

    pos = 0
    length = 0

    while length < _F and pos < size:
        tree.text_buf[r + length] = data[pos]
        pos += 1
        length += 1

    for i in range(1, _F + 1):
        tree.insert_node(r - i)

    tree.insert_node(r)

    while True:
        if tree.match_length > length:
            tree.match_length = length

        if tree.match_length <= _THRESHOLD:
            tree.match_length = 1
            _encode_char(tree, writer, tree.text_buf[r])
        else:
            _encode_char(tree, writer, 255 - _THRESHOLD + tree.match_length)
            _encode_position(tree, writer, tree.match_position)

        last_match_length = tree.match_length
        i = 0

        while i < last_match_length and pos < size:
            i += 1
            tree.delete_node(s)
            c = data[pos]
            pos += 1
            tree.text_buf[s] = c

            if s < _F - 1:
                tree.text_buf[s + _N] = c

            s = (s + 1) & (_N - 1)
            r = (r + 1) & (_N - 1)
            tree.insert_node(r)

        while i < last_match_length:
            i += 1
            tree.delete_node(s)
            s = (s + 1) & (_N - 1)
            r = (r + 1) & (_N - 1)
            length -= 1

            if length > 0:
                tree.insert_node(r)

        if not length > 0:
            break

    writer.flush()

    return size.to_bytes(4, "little") + bytes(writer.out)


def compress_b2(data: bytes) -> bytes:
    """
    LZHUF-compress ``data`` in the "B2" wire format Winlink actually
    uses: [2-byte CRC-16][4-byte length][compressed bytes]. See this
    module's docstring for the format and how it was verified.
    """
    body = compress(data)
    checksum = _crc16(body)

    return checksum.to_bytes(2, "little") + body


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------

def _decode_char(tree: _Tree, reader: _BitReader) -> int:
    c = tree.son[_R]

    while c < _T:
        c = tree.son[c + reader.get_bit()]

    c -= _T
    tree.update(c)
    return c


def _decode_position(reader: _BitReader) -> int:
    i = reader.get_byte()
    c = (_D_CODE[i] << 6) & 0xFFFF
    j = _D_LEN[i]
    j -= 2

    while j > 0:
        j -= 1
        i = ((i << 1) | reader.get_bit()) & 0xFFFF

    return c | (i & 0x3F)


def decompress(data: bytes) -> bytes:
    """
    Inverse of compress(): reads the 4-byte little-endian length header
    followed by LZHUF-compressed bytes, and returns the original data.

    Raises LZHUFError if ``data`` is too short to even contain the
    4-byte length header. Does NOT otherwise detect truncated/corrupt
    compressed data on its own (matching the reference implementations
    -- see _BitReader's docstring); decompress_b2() catches that one
    layer up, via the B2 CRC-16 check, before any bit-level decoding
    is attempted.
    """
    if len(data) < 4:
        raise LZHUFError("truncated LZHUF data: missing 4-byte length header")

    size = int.from_bytes(data[:4], "little")

    if size == 0:
        return b""

    tree = _Tree()
    tree.start_huff()
    reader = _BitReader(data[4:])

    text_buf = bytearray(_N)

    for i in range(_N - _F):
        text_buf[i] = 0x20

    r = _N - _F
    count = 0
    output = bytearray()

    while count < size:
        c = _decode_char(tree, reader)

        if c < 256:
            output.append(c)
            text_buf[r] = c
            r = (r + 1) & (_N - 1)
            count += 1
        else:
            i = (r - _decode_position(reader) - 1) & (_N - 1)
            j = (c - 255) + _THRESHOLD

            for _ in range(j):
                if count >= size:
                    break

                c2 = text_buf[(i) & (_N - 1)]
                output.append(c2)
                text_buf[r] = c2
                r = (r + 1) & (_N - 1)
                i += 1
                count += 1

    return bytes(output)


def decompress_b2(data: bytes) -> bytes:
    """
    Inverse of compress_b2(): verifies the leading 2-byte CRC-16
    against the rest of ``data``, then decompresses it.

    Raises ChecksumError if the CRC doesn't match (real corruption --
    per f6fbb.org's spec, a real gateway disconnects on a checksum
    failure, so this should be treated the same way: don't trust the
    message). Raises LZHUFError if ``data`` is too short to contain
    even the CRC header.
    """
    if len(data) < 2:
        raise LZHUFError("truncated B2 LZHUF data: missing 2-byte CRC header")

    expected = int.from_bytes(data[:2], "little")
    body = data[2:]
    actual = _crc16(body)

    if actual != expected:
        raise ChecksumError(
            f"LZHUF CRC-16 mismatch: header says {expected:#06x}, "
            f"computed {actual:#06x} over {len(body)} bytes"
        )

    return decompress(body)
