import unittest

from fakes import make_kam  # noqa: F401 (ensures kamxl/ is on sys.path)

from packet import PacketParser


class PacketParserTests(unittest.TestCase):
    """
    Uses real chunked text captured from the KAM-XL over the air
    during live testing (see docs/history.md) -- fed in the same
    non-line-aligned pieces actually observed on real hardware, not
    clean synthetic lines.
    """

    def test_aprs_message_completes_without_next_header(self):
        parser = PacketParser()

        packets = parser.feed(
            "N5BA-7>APRS/2: <UI>:\r\n"
            ":N5BA     :Hello from TH-D75A{01\r\n\r\n"
        )

        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].source, "N5BA-7")
        self.assertEqual(packets[0].port, 2)
        self.assertEqual(packets[0].frame_type, "UI")
        self.assertEqual(
            packets[0].payload,
            ":N5BA     :Hello from TH-D75A{01",
        )

    def test_aprs_message_waits_for_complete_terminator(self):
        parser = PacketParser()

        packets = parser.feed(
            "N5BA-7>APRS/2: <UI>:\r\n"
            ":N5BA     :Hello from TH-D75A{"
        )
        self.assertEqual(packets, [])

        packets = parser.feed("01\r\n")
        self.assertEqual(packets, [])

        packets = parser.feed("\r\n")
        self.assertEqual(len(packets), 1)
        self.assertEqual(
            packets[0].payload,
            ":N5BA     :Hello from TH-D75A{01",
        )

    def test_multiline_packet_not_finalized_by_blank_line(self):
        parser = PacketParser()

        packets = parser.feed(
            "KD5EOC-10>BEACON/2: <UI>:\r\n"
            "First line\r\n"
            "\r\n"
            "Second line\r\n"
        )

        self.assertEqual(packets, [])

        packets = parser.flush()

        self.assertEqual(len(packets), 1)
        self.assertEqual(
            packets[0].payload,
            "First line\n\nSecond line",
        )

    def test_single_clean_chunk(self):
        parser = PacketParser()

        packets = parser.feed(
            "KD5EOC-10>BEACON/2:\r\n"
            "Winlink 2000 RMS Packet Server\r\n"
        )
        packets += parser.flush()

        self.assertEqual(len(packets), 1)

        packet = packets[0]

        self.assertEqual(packet.source, "KD5EOC-10")
        self.assertEqual(packet.destination, "BEACON")
        self.assertEqual(packet.digipeaters, ())
        self.assertFalse(packet.digipeated)
        self.assertEqual(packet.port, 2)
        self.assertEqual(
            packet.payload,
            "Winlink 2000 RMS Packet Server"
        )

    def test_multiline_with_digipeat_path_split_across_reads(self):
        parser = PacketParser()

        raw = (
            "KR8X-7>BEACON,K5LRK*/2:\r\n"
            "KR8X-7 Node alias TXDBSN:\r\n"
            "KR8X-10 Winlink RMS GW,\r\n"
            "-11 BBS,\r\n"
            "-12 CHAT"
        )

        # Feed it in small, deliberately misaligned pieces (not on
        # line boundaries) to mirror what read_available() actually
        # handed back during live testing.
        chunk_size = 7
        packets = []

        for i in range(0, len(raw), chunk_size):
            packets += parser.feed(raw[i:i + chunk_size])

        packets += parser.flush()

        self.assertEqual(len(packets), 1)

        packet = packets[0]

        self.assertEqual(packet.source, "KR8X-7")
        self.assertEqual(packet.destination, "BEACON")
        self.assertEqual(packet.digipeaters, ("K5LRK*",))
        self.assertTrue(packet.digipeated)
        self.assertEqual(packet.port, 2)
        self.assertEqual(
            packet.payload,
            "KR8X-7 Node alias TXDBSN:\n"
            "KR8X-10 Winlink RMS GW,\n"
            "-11 BBS,\n"
            "-12 CHAT"
        )

    def test_two_back_to_back_packets(self):
        parser = PacketParser()

        packets = parser.feed(
            "KD5EOC-10>BEACON/2:\r\n"
            "Winlink 2000 RMS Packet Server\r\n"
            "N5BA-4>BEACON/2:\r\n"
            "N5BA-4 Linux Node http://digipi.org/\r\n"
        )
        packets += parser.flush()

        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[0].source, "KD5EOC-10")
        self.assertEqual(
            packets[0].payload,
            "Winlink 2000 RMS Packet Server"
        )
        self.assertEqual(packets[1].source, "N5BA-4")
        self.assertEqual(
            packets[1].payload,
            "N5BA-4 Linux Node http://digipi.org/"
        )

    def test_flush_catches_a_line_with_no_trailing_newline(self):
        # The KAM-XL can cut off mid-payload-line when a listen()
        # window closes -- flush() needs to catch this, not silently
        # drop it.
        parser = PacketParser()

        packets = parser.feed(
            "N5BA-4>BEACON/2:\r\n"
            "N5BA-4 Linux Node http://digipi.org/"
        )
        packets += parser.flush()

        self.assertEqual(len(packets), 1)
        self.assertEqual(
            packets[0].payload,
            "N5BA-4 Linux Node http://digipi.org/"
        )

    def test_non_monitor_text_is_ignored_not_fabricated(self):
        # Convers-mode / non-MONITOR text (e.g. a Winlink handshake)
        # should never be mistaken for MONITOR packets -- there's no
        # header line, so it should be silently ignored rather than
        # raising or fabricating a bogus packet.
        parser = PacketParser()

        packets = parser.feed(
            "Welcome to the Denton County Texas EOC\r\n"
            "[WL2K-5.0-B2FWIHJM$]\r\n"
            ";PQ: 78358301\r\n"
            "CMS via KD5EOC >\r\n"
        )
        packets += parser.flush()

        self.assertEqual(packets, [])

    def test_destination_field_generalizes_beyond_beacon(self):
        # Real traffic includes non-BEACON destinations too, e.g. an
        # ID frame.
        parser = PacketParser()

        packets = parser.feed(
            "K5LRK>ID/2:\r\n"
            "K5LRK/R LAARK/D\r\n"
        )
        packets += parser.flush()

        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].destination, "ID")


class RealHardwareFrameTypeTests(unittest.TestCase):
    """
    Regression coverage for a real bug found live on hardware: with
    MCOM and MRESP at their factory defaults (both ON/ON), every
    MONITOR header line carries a bracketed frame-type tag ("<UI>",
    "<<C>>", "<<I00>>", ...) that the original HEADER_RE had no
    allowance for at all -- meaning it never matched a single
    real-hardware header line. Since _process_line() only appends
    non-matching lines to an *already pending* packet, and nothing
    ever matched to start one, every line was silently dropped from
    the very first header onward -- the live monitor pane stayed
    empty no matter how much traffic the KAM-XL was actually
    receiving. These fixtures are taken verbatim from a live daemon
    log (see docs/history.md's milestone 5 notes).
    """

    def test_single_bracket_ui_frame(self):
        parser = PacketParser()

        packets = parser.feed(
            "K5LRK>BEACON/2: <UI>:\r\n"
            "LAARK Digipeater, The Colony\r\n\r\n"
        )
        packets += parser.flush()

        self.assertEqual(len(packets), 1)
        packet = packets[0]

        self.assertEqual(packet.source, "K5LRK")
        self.assertEqual(packet.destination, "BEACON")
        self.assertEqual(packet.frame_type, "UI")
        self.assertEqual(
            packet.payload,
            "LAARK Digipeater, The Colony\n"
        )

    def test_double_bracket_digipeated_ui_frame(self):
        parser = PacketParser()

        packets = parser.feed(
            "KR8X-7>BEACON,K5LRK*/2: <<UI>>:\r\n"
            "KR8X-7 Node alias TXDBSN:\r\n"
            "KR8X-10 Winlink RMS GW,\r\n"
            "-11 BBS,\r\n"
            "-12 CHAT\r\n"
        )
        packets += parser.flush()

        self.assertEqual(len(packets), 1)
        packet = packets[0]

        self.assertEqual(packet.source, "KR8X-7")
        self.assertEqual(packet.digipeaters, ("K5LRK*",))
        self.assertEqual(packet.frame_type, "UI")

    def test_connect_request_control_frame_has_no_payload(self):
        parser = PacketParser()

        packets = parser.feed(
            "WB5NZV>KD5EOC-10,RSSTN*/2: <<C>>:\r\n"
            "KD5EOC-10>WB5NZV,RSSTN/2: <<UA>>:\r\n"
        )
        packets += parser.flush()

        self.assertEqual(len(packets), 2)

        connect_request = packets[0]
        self.assertEqual(connect_request.source, "WB5NZV")
        self.assertEqual(connect_request.destination, "KD5EOC-10")
        self.assertEqual(connect_request.digipeaters, ("RSSTN*",))
        self.assertEqual(connect_request.frame_type, "C")
        self.assertEqual(connect_request.payload, "")

        ack = packets[1]
        self.assertEqual(ack.frame_type, "UA")

    def test_numbered_information_frame_with_payload(self):
        parser = PacketParser()

        packets = parser.feed(
            "KD5EOC-10>WB5NZV,RSSTN/2: <<I00>>:\r\n"
            "Welcome to the Denton County Texas EOC\r\n\r\n"
        )
        packets += parser.flush()

        self.assertEqual(len(packets), 1)
        packet = packets[0]

        self.assertEqual(packet.frame_type, "I00")
        self.assertEqual(
            packet.payload,
            "Welcome to the Denton County Texas EOC\n"
        )

    def test_back_to_back_supervisory_frames_no_payload(self):
        # Real capture: two RR (receiver-ready) acks with literally
        # nothing between their header lines.
        parser = PacketParser()

        packets = parser.feed(
            "KD5EOC-10>WB5NZV,RSSTN/2: <<RR0>>:\r\n"
            "KD5EOC-10>WB5NZV,RSSTN*/2: <<RR0>>:\r\n"
        )
        packets += parser.flush()

        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[0].frame_type, "RR0")
        self.assertEqual(packets[0].payload, "")
        self.assertEqual(packets[1].frame_type, "RR0")

    def test_lowercase_response_supervisory_frame(self):
        parser = PacketParser()

        packets = parser.feed(
            "WB5NZV>KD5EOC-10,RSSTN*/2: <<rr1>>:\r\n"
        )
        packets += parser.flush()

        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].frame_type, "rr1")

    def test_disconnect_and_disconnected_mode_frames(self):
        parser = PacketParser()

        packets = parser.feed(
            "KD5EOC-10>WB5NZV,RSSTN/2: <<D>>:\r\n"
            "WB5NZV>KD5EOC-10,RSSTN*/2: <<DM>>:\r\n"
        )
        packets += parser.flush()

        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[0].frame_type, "D")
        self.assertEqual(packets[1].frame_type, "DM")

    def test_untagged_header_still_parses_with_frame_type_none(self):
        # Backward compatibility: plain fixtures used throughout the
        # rest of this suite (and the offline daemon/REST tests) have
        # no tag at all -- frame_type should just be None, not an
        # empty string or a parse failure.
        parser = PacketParser()

        packets = parser.feed(
            "KD5EOC-10>BEACON/2:\r\n"
            "Winlink 2000 RMS Packet Server\r\n"
        )
        packets += parser.flush()

        self.assertEqual(len(packets), 1)
        self.assertIsNone(packets[0].frame_type)


if __name__ == "__main__":
    unittest.main()
