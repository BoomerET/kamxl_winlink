import unittest

from aprs_tx import (
    APRSTransmitError,
    validate_disconnected_status,
    validate_aprs_port,
    validate_unproto,
    validate_aprs_active_stream,
    build_aprs_tx_plan
)


class APRSStatusTests(unittest.TestCase):

    def test_disconnected_port_2(self):
        validate_disconnected_status(
            "FREE BYTES 2685\r\n"
            "A/2  stream - IO DISCONNECTED\r\n"
        )

    def test_connected_stream_rejected(self):
        with self.assertRaises(APRSTransmitError):
            validate_disconnected_status(
                "FREE BYTES 2685\r\n"
                "A/2  stream - IO CONNECTED"
            )

    def test_missing_stream_rejected(self):
        with self.assertRaises(APRSTransmitError):
            validate_disconnected_status(
                "FREE BYTES 2685"
            )

    def test_unknown_state_rejected(self):
        with self.assertRaises(APRSTransmitError):
            validate_disconnected_status(
                "FREE BYTES 2685\r\n"
                "A/2  stream - UNKNOWN"
            )

    def test_multiple_streams_with_connection_rejected(self):
        with self.assertRaises(APRSTransmitError):
            validate_disconnected_status(
                "FREE BYTES 2685\r\n"
                "A/2  stream - IO DISCONNECTED\r\n"
                "B/2  stream - IO CONNECTED"
            )

    def test_unexpected_extra_line_rejected(self):
        with self.assertRaises(APRSTransmitError):
            validate_disconnected_status(
                "FREE BYTES 2685\r\n"
                "A/2  stream - IO DISCONNECTED\r\n"
                "UNEXPECTED DATA"
            )

    def test_missing_free_bytes_rejected(self):
        with self.assertRaises(APRSTransmitError):
            validate_disconnected_status(
                "A/2  stream - IO DISCONNECTED"
            )

    def test_status_rejects_missing_vhf_stream(self):
        status = (
            "FREE BYTES 2685\r\n"
            "A/1 stream - IO DISCONNECTED\r\n"
        )

        with self.assertRaisesRegex(
            APRSTransmitError,
            "does not confirm disconnected A/2 stream",
        ):
            validate_disconnected_status(status)

    def test_port_2_accepted(self):
        validate_aprs_port("PORT     2")

    def test_port_1_rejected(self):
        with self.assertRaises(APRSTransmitError):
            validate_aprs_port("PORT     1")

    def test_unrecognized_port_rejected(self):
        with self.assertRaises(APRSTransmitError):
            validate_aprs_port("PORT UNKNOWN")

    def test_default_unproto(self):
        self.assertEqual(
            validate_unproto("CQ/CQ"),
            "CQ/CQ",
        )

    def test_unproto_with_path(self):
        self.assertEqual(
            validate_unproto("APRS VIA WIDE1-1,WIDE2-1"),
            "APRS VIA WIDE1-1,WIDE2-1",
        )

    def test_invalid_unproto_rejected(self):
        with self.assertRaises(APRSTransmitError):
            validate_unproto("APRS\r\nPORT 1")

    def test_active_stream_a2_accepted(self):
        validate_aprs_active_stream(
            "FREE BYTES 2685\r\n"
            "A/2  stream - IO DISCONNECTED"
        )

    def test_active_stream_a1_rejected(self):
        with self.assertRaises(APRSTransmitError):
            validate_aprs_active_stream(
                "FREE BYTES 2685\r\n"
                "A/1  stream - IO DISCONNECTED"
            )

    def test_active_stream_b2_rejected(self):
        with self.assertRaises(APRSTransmitError):
            validate_aprs_active_stream(
                "FREE BYTES 2685\r\n"
                "B/2  stream - IO DISCONNECTED"
            )

    def test_multiple_streams_rejected(self):
        with self.assertRaises(APRSTransmitError):
            validate_aprs_active_stream(
                "FREE BYTES 2685\r\n"
                "A/2  stream - IO DISCONNECTED\r\n"
                "B/2  stream - IO DISCONNECTED"
            )

    def test_tx_plan_without_digipeaters(self):
        plan = build_aprs_tx_plan(
            ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX",
            "CQ/CQ",
        )

        self.assertEqual(
            plan,
            (
                "UNPROTO APRS",
                "CONVERS",
                ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX\r",
                "\x03",
                "UNPROTO CQ/CQ",
            ),
        )

    def test_tx_plan_with_digipeaters(self):
        plan = build_aprs_tx_plan(
            ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX",
            "CQ/CQ",
            path="WIDE1-1,WIDE2-1",
        )

        self.assertEqual(plan[0], "UNPROTO APRS VIA WIDE1-1,WIDE2-1")
        self.assertEqual(plan[-1], "UNPROTO CQ/CQ")

    def test_tx_plan_rejects_invalid_path(self):
        with self.assertRaises(APRSTransmitError):
            build_aprs_tx_plan(
                ":ANSRVR   :CQ #APRSTHURSDAY Howdy from Prosper TX",
                "CQ/CQ",
                path="WIDE1-1\rRESET",
            )

if __name__ == "__main__":
    unittest.main()
