import unittest

from fakes import make_kam  # noqa: F401 (ensures kamxl/ is on sys.path)

from aprs import _base91_decode, parse_position


class ParsePositionUncompressedTests(unittest.TestCase):
    """
    APRS position-report decoding. Fixtures are the APRS Protocol
    Reference spec's own canonical example
    ("!4903.50N/07201.75W-Test") rather than real captured traffic --
    see aprs.py's module docstring for why this is treated as
    unverified against real hardware until checked against an actual
    live APRS capture, the same caution applied to pbbs.py.
    """

    def test_no_timestamp(self):
        position = parse_position("!4903.50N/07201.75W-Test comment")

        self.assertIsNotNone(position)
        self.assertAlmostEqual(position.latitude, 49 + 3.50 / 60, places=6)
        self.assertAlmostEqual(
            position.longitude, -(72 + 1.75 / 60), places=6
        )
        self.assertEqual(position.symbol_table, "/")
        self.assertEqual(position.symbol_code, "-")
        self.assertEqual(position.comment, "Test comment")
        self.assertIsNone(position.timestamp)
        self.assertEqual(position.raw, "!4903.50N/07201.75W-Test comment")

    def test_equals_data_type_same_as_bang(self):
        # '=' is '!' with the APRS messaging-capable flag set -- same
        # position shape, no bearing on parsing.
        position = parse_position("=4903.50N/07201.75W-")

        self.assertIsNotNone(position)
        self.assertEqual(position.comment, "")

    def test_with_timestamp(self):
        position = parse_position(
            "/092345z4903.50N/07201.75W-Test comment"
        )

        self.assertIsNotNone(position)
        self.assertEqual(position.timestamp, "092345z")
        self.assertAlmostEqual(position.latitude, 49 + 3.50 / 60, places=6)

    def test_at_data_type_same_as_slash(self):
        position = parse_position("@092345h4903.50N/07201.75W-")

        self.assertIsNotNone(position)
        self.assertEqual(position.timestamp, "092345h")

    def test_southern_and_western_hemisphere_negative(self):
        position = parse_position("!4903.50S/07201.75W-")

        self.assertIsNotNone(position)
        self.assertLess(position.latitude, 0)
        self.assertLess(position.longitude, 0)

    def test_northern_and_eastern_hemisphere_positive(self):
        position = parse_position("!4903.50N/07201.75E-")

        self.assertIsNotNone(position)
        self.assertGreater(position.latitude, 0)
        self.assertGreater(position.longitude, 0)

    def test_alternate_symbol_table(self):
        # Backslash table, different symbol code -- shouldn't affect
        # position decoding at all, just carried through verbatim.
        position = parse_position(r"!4903.50N\07201.75W>")

        self.assertIsNotNone(position)
        self.assertEqual(position.symbol_table, "\\")
        self.assertEqual(position.symbol_code, ">")

    def test_position_ambiguity_treated_as_zero(self):
        # Trailing minute digits replaced with spaces -- see module
        # docstring's "POSITION AMBIGUITY NOT FULLY MODELED" note.
        position = parse_position("!49  .  N/072  .  W-")

        self.assertIsNotNone(position)
        self.assertAlmostEqual(position.latitude, 49.0, places=6)
        self.assertAlmostEqual(position.longitude, -72.0, places=6)


class Base91DecodeTests(unittest.TestCase):
    def test_matches_spec_worked_example(self):
        # APRS Protocol Reference 1.0.1 sec 9's own worked example:
        # "<*e7" is derived from 190463 x (180 - 72.75) = 20427156.75,
        # truncated to the integer 20427156 before base-91 encoding.
        self.assertEqual(_base91_decode("<*e7"), 20427156)

    def test_all_zero_value_chars_decode_to_zero(self):
        # '!' (ASCII 33) is base-91 digit 0.
        self.assertEqual(_base91_decode("!!!!"), 0)


class ParsePositionCompressedTests(unittest.TestCase):
    """
    Compressed-format position decoding -- see module docstring's
    "COMPRESSED POSITIONS" note. Fixtures are the APRS Protocol
    Reference 1.0.1 spec's own worked examples (chapter 9), each
    independently re-derived and cross-checked against aprslib
    (github.com/rossengeorgiev/aprs-python)'s real, proven decode
    logic while implementing this -- not guessed at.
    """

    # Compressed encoding is inherently lossy (spec: "position to 1
    # foot worldwide", not exact) -- the spec's own worked example
    # truncates 190463*(180-72.75)=20427156.75 down to the integer
    # 20427156 before encoding, so decoding it back lands on
    # -72.75000393776..., about 1.6 feet off from -72.75 exactly.
    # That's expected quantization, not a bug -- checked with a loose
    # tolerance (1e-4 degrees, ~36 feet, comfortably covering it),
    # unlike the uncompressed tests' tight 1e-6 tolerance.
    _COMPRESSED_TOLERANCE = 1e-4

    def test_no_timestamp(self):
        position = parse_position("=/5L!!<*e7>7P[with course/speed")

        self.assertIsNotNone(position)
        self.assertAlmostEqual(
            position.latitude, 49.5, delta=self._COMPRESSED_TOLERANCE
        )
        self.assertAlmostEqual(
            position.longitude, -72.75, delta=self._COMPRESSED_TOLERANCE
        )
        self.assertEqual(position.symbol_table, "/")
        self.assertEqual(position.symbol_code, ">")
        self.assertEqual(position.comment, "with course/speed")
        self.assertIsNone(position.timestamp)
        self.assertEqual(
            position.raw, "=/5L!!<*e7>7P[with course/speed"
        )

    def test_bang_data_type_same_as_equals(self):
        position = parse_position("!/5L!!<*e7>7P[")

        self.assertIsNotNone(position)
        self.assertAlmostEqual(
            position.latitude, 49.5, delta=self._COMPRESSED_TOLERANCE
        )

    def test_with_timestamp(self):
        position = parse_position(
            "@092345z/5L!!<*e7>{?!with radio range"
        )

        self.assertIsNotNone(position)
        self.assertEqual(position.timestamp, "092345z")
        self.assertAlmostEqual(
            position.latitude, 49.5, delta=self._COMPRESSED_TOLERANCE
        )
        self.assertAlmostEqual(
            position.longitude, -72.75, delta=self._COMPRESSED_TOLERANCE
        )
        self.assertEqual(position.comment, "with radio range")

    def test_slash_data_type_same_as_at(self):
        position = parse_position("/092345h/5L!!<*e7>{?!")

        self.assertIsNotNone(position)
        self.assertEqual(position.timestamp, "092345h")

    def test_no_course_speed_range_sentinel(self):
        # A literal space as the first "cs" byte means "no course,
        # speed, or range data" per the spec -- this module doesn't
        # decode cs/T at all regardless, but the space character still
        # has to parse without error since it's a real, valid byte
        # value in that position (outside the strict base-91 range,
        # but this module never validates cs against that range --
        # only lat/lon).
        position = parse_position("=/5L!!<*e7>  Tno course or speed")

        self.assertIsNotNone(position)
        self.assertEqual(position.comment, "no course or speed")

    def test_overlay_symbol_table_digit(self):
        # Real APRS traffic uses a digit or uppercase letter here for
        # "alternate table with overlay" instead of the plain "/" or
        # "\" table selector -- must still parse (see the comment
        # above _COMPRESSED_POSITION_RE for why a restrictive
        # character class here would be wrong).
        position = parse_position("=15L!!<*e7>7P[numbered station")

        self.assertIsNotNone(position)
        self.assertEqual(position.symbol_table, "1")

    def test_malformed_latitude_returns_none(self):
        # A space inside the latitude field is outside the strict
        # base-91 range ('!'..'{') -- degrades to None rather than
        # decoding a wrong position.
        position = parse_position("=/5L! <*e7>7P[")

        self.assertIsNone(position)

    def test_malformed_longitude_returns_none(self):
        position = parse_position("=/5L!!<* 7>7P[")

        self.assertIsNone(position)

    def test_too_short_does_not_match(self):
        # Fewer than the fixed 12 bytes after the symbol table char --
        # not a valid compressed field, and (being short and mostly
        # digit-free) not a valid uncompressed one either.
        self.assertIsNone(parse_position("=/5L!!<*"))


class ParsePositionNonPositionTests(unittest.TestCase):
    def test_empty_payload(self):
        self.assertIsNone(parse_position(""))

    def test_status_packet_not_a_position(self):
        self.assertIsNone(parse_position(">Status text here"))

    def test_message_packet_not_a_position(self):
        self.assertIsNone(
            parse_position(":N0CALL   :Hello there{001")
        )

    def test_object_packet_not_a_position(self):
        self.assertIsNone(
            parse_position(";LEADER   *111111z4903.50N/07201.75W-")
        )

    def test_plain_text_not_a_position(self):
        self.assertIsNone(parse_position("Just some plain text"))


if __name__ == "__main__":
    unittest.main()
