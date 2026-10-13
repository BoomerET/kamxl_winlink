import unittest

from fakes import CannedSerial, ScriptedSerial, make_kam

from kamxl import KAMError, KAMCommandError


class ConnectionRequiredTests(unittest.TestCase):
    def test_raises_without_a_connection(self):
        kam = make_kam(None)

        with self.assertRaises(KAMError):
            kam.get("MONITOR")


class RawCommandTests(unittest.TestCase):
    def test_get_bool_and_set_bool(self):
        kam = make_kam(
            ScriptedSerial(script={"FOO": "FOO ON"})
        )

        self.assertTrue(kam.get_bool("FOO"))

        # SET commands don't need a scripted response body -- just
        # confirm it sends the right wire format.
        kam.set_bool("FOO", False)

        self.assertEqual(
            kam.serial.written[-1],
            b"FOO OFF\r"
        )

    def test_eh_response_raises_command_error(self):
        kam = make_kam(
            ScriptedSerial(script={"BADCMD": "EH?"})
        )

        with self.assertRaises(KAMCommandError):
            kam.get("BADCMD")

    def test_echo_on_and_off_parse_identically(self):
        for echo in (True, False):
            with self.subTest(echo=echo):
                kam = make_kam(
                    ScriptedSerial(
                        script={"MONITOR": "MONITOR ON/OFF"},
                        echo=echo
                    )
                )

                self.assertEqual(
                    kam.get_typed("MONITOR"),
                    (True, False)
                )

    def test_embedded_monitor_traffic_stripped_from_response(self):
        """
        Regression test for a real bug found live through the web
        terminal: typing "VERSION" got its response jumbled with
        MONITOR packets from other stations. Verbatim shape of what
        was actually observed in the daemon log:

            08:14:52 conn-3696: send_command -> KAMCommandError:
            KAM-XL rejected command 'VERSION':
            'KD5EOC-10>BEACON/2: <UI>:\\r\\nWinlink 2000 RMS Packet
            Server\\r\\n\\r\\nKC5GOI-1>ID/2: <UI>:\\r\\nKC5GOI-1/R
            RSSTN/D KC5GOI-7/N\\r\\n\\r\\nKC5GOI-1>BEACON/2: <UI>:\\r\\n
            Rosston Digi KC5GOI-1, Alias RSSTN. SW Cooke Co,
            Texas.\\r\\n\\r\\n    $\\r\\nEH?'

        Unsolicited MONITOR traffic from two other stations (three
        packets total) arrived in the gap between sending "VERSION"
        and its response completing -- a limitation previously flagged
        as "not yet hit in testing" in docs/daemon.md, now confirmed
        live. Retrying the identical command immediately afterward
        succeeded, confirming this was transient RF interference, not
        a lasting problem with the command itself.

        This confirms the fix (_strip_monitor_lines(), called from
        send_command()): the three embedded monitor packets are
        removed, leaving only the KAM-XL's own genuine "    $\\r\\nEH?"
        reply in the raised exception -- not the whole unreadable dump.
        """
        raw_response = (
            b"KD5EOC-10>BEACON/2: <UI>:\r\n"
            b"Winlink 2000 RMS Packet Server\r\n\r\n"
            b"KC5GOI-1>ID/2: <UI>:\r\n"
            b"KC5GOI-1/R RSSTN/D KC5GOI-7/N\r\n\r\n"
            b"KC5GOI-1>BEACON/2: <UI>:\r\n"
            b"Rosston Digi KC5GOI-1, Alias RSSTN. SW Cooke Co, Texas.\r\n\r\n"
            b"    $\r\nEH?cmd:"
        )

        kam = make_kam(CannedSerial([raw_response]))

        with self.assertRaises(KAMCommandError) as ctx:
            kam.send_command("VERSION")

        message = str(ctx.exception)

        self.assertNotIn("KD5EOC-10", message)
        self.assertNotIn("KC5GOI-1", message)
        self.assertNotIn("Winlink 2000 RMS Packet Server", message)
        self.assertNotIn("Rosston Digi", message)
        self.assertIn("EH?", message)


class StripMonitorLinesTests(unittest.TestCase):
    """
    Direct tests of _strip_monitor_lines() -- see its docstring (and
    RawCommandTests.test_embedded_monitor_traffic_stripped_from_response
    above, the real-hardware capture this was built from) for the full
    story.
    """

    def test_no_monitor_traffic_unchanged(self):
        kam = make_kam(None)

        text = "VERSION  KAM XL -1.25199-  SERIAL NUMBER - 00001D6C2798"

        self.assertEqual(kam._strip_monitor_lines(text), text)

    def test_single_monitor_block_removed(self):
        kam = make_kam(None)

        text = (
            "K5LRK>BEACON/2: <UI>:\r\n"
            "Some beacon text\r\n"
            "\r\n"
            "Real response line"
        )

        self.assertEqual(
            kam._strip_monitor_lines(text), "Real response line"
        )

    def test_multiple_monitor_blocks_removed_response_preserved(self):
        kam = make_kam(None)

        text = (
            "K5LRK>BEACON/2: <UI>:\r\n"
            "First beacon\r\n"
            "\r\n"
            "Real response line\r\n"
            "WB5NZV>ID/2: <UI>:\r\n"
            "Second beacon\r\n"
            "\r\n"
            "More real response"
        )

        self.assertEqual(
            kam._strip_monitor_lines(text),
            "Real response line\r\nMore real response"
        )

    def test_monitor_block_with_no_trailing_blank_line_consumes_to_end(self):
        # Documented limitation (see _strip_monitor_lines()'s
        # docstring): without a blank line to signal where a monitored
        # packet's payload ends, this can't tell it apart from
        # genuine response text glued directly after it, and swallows
        # both. Pinned here as a known, explicit tradeoff rather than
        # left as undefined behavior -- same spirit as packet.py's own
        # HEADER_RE needing a real-hardware correction after its first
        # live test.
        kam = make_kam(None)

        text = (
            "K5LRK>BEACON/2: <UI>:\r\n"
            "Beacon text\r\n"
            "Real response line that gets swallowed too"
        )

        self.assertEqual(kam._strip_monitor_lines(text), "")


class TypedMultiportBoolTests(unittest.TestCase):
    def test_get_typed(self):
        kam = make_kam(
            ScriptedSerial(script={"MONITOR": "MONITOR OFF/OFF"})
        )

        self.assertEqual(
            kam.get_typed("MONITOR"),
            (False, False)
        )

    def test_set_typed_round_trip(self):
        kam = make_kam(
            ScriptedSerial(script={"MONITOR": "MONITOR ON/OFF"})
        )

        result = kam.set_typed("MONITOR", (True, False))

        self.assertEqual(result, (True, False))
        # The actual SET command sent should use the multi-port
        # "value/value" format, not e.g. two separate commands.
        self.assertIn(
            b"MONITOR ON/OFF\r",
            kam.serial.written
        )


class TypedChoiceTests(unittest.TestCase):
    def test_get_and_set_typed(self):
        kam = make_kam(
            ScriptedSerial(script={"DIGIPEAT": "DIGIPEAT UIONLY"})
        )

        self.assertEqual(
            kam.set_typed("DIGIPEAT", "uionly"),
            "UIONLY"
        )

    def test_invalid_choice_never_touches_serial(self):
        kam = make_kam(ScriptedSerial())

        with self.assertRaises(ValueError):
            kam.set_typed("DIGIPEAT", "BOGUS")

        self.assertEqual(kam.serial.written, [])


class TypedMultiportChoiceTests(unittest.TestCase):
    def test_get_and_set_typed(self):
        kam = make_kam(
            ScriptedSerial(script={"FULLDUP": "FULLDUP ON/LOOPBACK"})
        )

        self.assertEqual(
            kam.set_typed("FULLDUP", ("on", "loopback")),
            ("ON", "LOOPBACK")
        )

    def test_invalid_choice_raises_before_writing(self):
        kam = make_kam(ScriptedSerial())

        with self.assertRaises(ValueError):
            kam.set_typed("FULLDUP", ("ON", "BOGUS"))

        self.assertEqual(kam.serial.written, [])


class TypedMultiportIntTests(unittest.TestCase):
    def test_get_and_set_typed(self):
        kam = make_kam(
            ScriptedSerial(script={"HBAUD": "HBAUD 1200/9600"})
        )

        self.assertEqual(
            kam.get_typed("HBAUD"),
            (1200, 9600)
        )
        self.assertEqual(
            kam.set_typed("HBAUD", (1200, 9600)),
            (1200, 9600)
        )


class TypedMultiportStringTests(unittest.TestCase):
    def test_get_typed_matches_real_hardware_shape(self):
        # Matches what whoami.py actually returned from the real
        # KAM-XL: the same callsign on both ports, no SSID.
        kam = make_kam(
            ScriptedSerial(script={"MYCALL": "MYCALL N5BA/N5BA"})
        )

        self.assertEqual(
            kam.get_typed("MYCALL"),
            ("N5BA", "N5BA")
        )


class TypedIntTests(unittest.TestCase):
    def test_get_typed(self):
        kam = make_kam(
            ScriptedSerial(script={"PORT": "PORT 2"})
        )

        self.assertEqual(kam.get_typed("PORT"), 2)

    def test_set_default_port_success(self):
        kam = make_kam(
            ScriptedSerial(script={"PORT": "PORT 2"})
        )

        self.assertEqual(kam.set_default_port(2), 2)

    def test_set_default_port_mismatch_raises(self):
        # KAM-XL claims port 1 even though we asked for port 2 --
        # set_default_port() should catch that instead of silently
        # trusting the write.
        kam = make_kam(
            ScriptedSerial(script={"PORT": "PORT 1"})
        )

        with self.assertRaises(KAMError):
            kam.set_default_port(2)

    def test_set_default_port_rejects_bad_port_number(self):
        kam = make_kam(ScriptedSerial())

        with self.assertRaises(ValueError):
            kam.set_default_port(3)

        self.assertEqual(kam.serial.written, [])


class ReadOnlyAndUnknownCommandTests(unittest.TestCase):
    def test_version_is_read_only(self):
        kam = make_kam(
            ScriptedSerial(script={"VERSION": "VERSION 1.24160"})
        )

        self.assertEqual(kam.get_typed("VERSION"), "1.24160")

        written_before_set_attempt = list(kam.serial.written)

        with self.assertRaises(KAMError):
            kam.set_typed("VERSION", "9.99999")

        # The rejection must happen before ever writing to the
        # serial port -- no new writes beyond the earlier get_typed().
        self.assertEqual(
            kam.serial.written,
            written_before_set_attempt
        )

    def test_unknown_command_falls_back_to_raw_get_set(self):
        kam = make_kam(
            ScriptedSerial(script={"FOOBAR": "FOOBAR 42"})
        )

        self.assertEqual(kam.get_typed("FOOBAR"), "42")

        # set_typed() on an unknown command should still work (raw
        # passthrough) rather than raising.
        kam.set_typed("FOOBAR", "43")

        self.assertIn(
            b"FOOBAR 43\r",
            kam.serial.written
        )


class GetConfigurationTests(unittest.TestCase):
    def test_parses_multiline_display_output(self):
        kam = make_kam(
            ScriptedSerial(script={
                "DISPLAY": (
                    "MYCALL   N5BA/N5BA\r\n"
                    "MONITOR  OFF/OFF\r\n"
                    "BLANKKEY"
                )
            })
        )

        config = kam.get_configuration()

        self.assertEqual(config["MYCALL"], "N5BA/N5BA")
        self.assertEqual(config["MONITOR"], "OFF/OFF")
        # A key with no trailing value at all (e.g. an unset MYCALL
        # on real hardware) should show up as an empty string, not
        # get silently dropped.
        self.assertEqual(config["BLANKKEY"], "")


if __name__ == "__main__":
    unittest.main()
