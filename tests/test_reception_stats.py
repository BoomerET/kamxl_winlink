import unittest
from types import SimpleNamespace

from reception_stats import ReceptionStats


class ReceptionStatsTests(unittest.TestCase):

    def packet(self, source, digipeaters=()):
        return SimpleNamespace(
            source=source,
            digipeaters=digipeaters,
        )

    def test_empty_statistics(self):
        stats = ReceptionStats()
        result = stats.snapshot()

        self.assertEqual(result["total_packets"], 0)
        self.assertEqual(result["unique_sources"], 0)
        self.assertEqual(result["top_sources"], [])

    def test_counts_sources(self):
        stats = ReceptionStats()

        stats.update(self.packet("N5BA"))
        stats.update(self.packet("N5BA"))
        stats.update(self.packet("K5TEST"))

        result = stats.snapshot()

        self.assertEqual(result["total_packets"], 3)
        self.assertEqual(result["unique_sources"], 2)
        self.assertEqual(
            result["top_sources"][0],
            {"callsign": "N5BA", "packets": 2},
        )

    def test_counts_used_digipeaters(self):
        stats = ReceptionStats()

        stats.update(self.packet(
            "N5BA",
            ("W5NGU-3*", "WIDE2-1"),
        ))

        result = stats.snapshot()

        self.assertEqual(result["used_digipeater_packets"], 1)
        self.assertEqual(
            result["top_digipeaters"],
            [{"callsign": "W5NGU-3", "packets": 1}],
        )

    def test_ignores_unused_digipeaters(self):
        stats = ReceptionStats()

        stats.update(self.packet(
            "N5BA",
            ("W5NGU-3", "WIDE2-1"),
        ))

        result = stats.snapshot()

        self.assertEqual(result["used_digipeater_packets"], 0)
        self.assertEqual(
            result["no_identified_digipeater_packets"], 1,
        )

    def test_deduplicates_digipeaters_per_packet(self):
        stats = ReceptionStats()

        stats.update(self.packet(
            "N5BA",
            ("W5NGU-3*", "W5NGU-3*"),
        ))

        result = stats.snapshot()

        self.assertEqual(
            result["top_digipeaters"][0]["packets"],
            1,
        )
