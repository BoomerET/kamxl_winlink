import unittest

from fakes import ChunkSerial, make_kam


CHUNKS = [
    "KD5EOC-10>BEACON/2:\r\n",
    "Winlink 2000 RMS Packet Server\r\n",
    "N5BA-4>BEACON/2:\r\n",
    "N5BA-4 Linux Node http://digipi.org/\r\n",
]


class MonitorMethodTests(unittest.TestCase):
    """
    Exercises KAMXL.monitor()'s own wiring (PacketParser
    integration, generator vs. callback modes, deadline handling).
    PacketParser's text reassembly itself is covered separately in
    test_packet_parser.py.
    """

    def test_generator_mode(self):
        kam = make_kam(ChunkSerial(CHUNKS))

        packets = list(kam.monitor(seconds=0.5))

        self.assertEqual(len(packets), 2)
        self.assertEqual(packets[0].source, "KD5EOC-10")
        self.assertEqual(packets[1].source, "N5BA-4")
        self.assertEqual(
            packets[1].payload,
            "N5BA-4 Linux Node http://digipi.org/"
        )

    def test_callback_mode(self):
        kam = make_kam(ChunkSerial(CHUNKS))

        collected = []

        kam.monitor(seconds=0.5, callback=collected.append)

        self.assertEqual(len(collected), 2)
        self.assertEqual(collected[0].source, "KD5EOC-10")
        self.assertEqual(collected[1].source, "N5BA-4")

    def test_generator_mode_with_no_traffic(self):
        kam = make_kam(ChunkSerial([]))

        self.assertEqual(list(kam.monitor(seconds=0.2)), [])


if __name__ == "__main__":
    unittest.main()
