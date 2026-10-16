"""Tests for outgoing APRS message formatting."""

import unittest

from aprs import format_aprs_message


class TestAprsMessageFormatting(unittest.TestCase):

    def test_aprs_thursday_message(self):
        payload = format_aprs_message(
            "ANSRVR",
            "CQ #APRSTHURSDAY Howdy from Prosper TX",
        )

        self.assertEqual(
            payload,
            ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX",
        )

    def test_callsign_normalization(self):
        payload = format_aprs_message(" n5ba-7 ", "Hello")

        self.assertEqual(payload, ":N5BA-7   :Hello")

    def test_invalid_addressee(self):
        for addressee in (
            "",
            "TOOLONGCALL",
            "N5BA-123",
            "BAD CALL",
            "N5BA!",
        ):
            with self.subTest(addressee=addressee):
                with self.assertRaises(ValueError):
                    format_aprs_message(addressee, "Hello")

    def test_invalid_message(self):
        for message in (
            "",
            "Hello\nWorld",
            "Hello\rWorld",
            "Hello\x03World",
            "Hello|World",
            "Hello~World",
            "Hello{World",
            "Café",
        ):
            with self.subTest(message=message):
                with self.assertRaises(ValueError):
                    format_aprs_message("ANSRVR", message)

    def test_paclen_limit(self):
        with self.assertRaisesRegex(ValueError, "PACLEN"):
            format_aprs_message("ANSRVR", "X" * 120, paclen=128)

        payload = format_aprs_message(
            "ANSRVR", "X" * 114, paclen=128
        )
        self.assertEqual(len(payload), 125)


if __name__ == "__main__":
    unittest.main()
