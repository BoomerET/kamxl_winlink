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
        self.assertEqual(result["all_sources"], [])

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

    def test_all_sources_includes_more_than_top_ten(self):
        stats = ReceptionStats()

        # Create 15 different stations.
        for index in range(15):
            stats.update(self.packet(f"N5TEST-{index}"))

        result = stats.snapshot()

        self.assertEqual(result["unique_sources"], 15)
        self.assertEqual(len(result["top_sources"]), 10)
        self.assertEqual(len(result["all_sources"]), 15)

        callsigns = {
            entry["callsign"]
            for entry in result["all_sources"]
        }

        self.assertIn("N5TEST-0", callsigns)
        self.assertIn("N5TEST-14", callsigns)

    def test_all_sources_sorted_by_count_then_callsign(self):
        stats = ReceptionStats()

        for callsign in (
            "W5ZZZ",
            "N5BA",
            "K5AAA",
            "N5BA",
            "K5AAA",
            "W5ZZZ",
            "N5BA",
        ):
            stats.update(self.packet(callsign))

        result = stats.snapshot()

        self.assertEqual(
            result["all_sources"],
            [
                {"callsign": "N5BA", "packets": 3},
                {"callsign": "K5AAA", "packets": 2},
                {"callsign": "W5ZZZ", "packets": 2},
            ],
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

    def test_direct_and_digipeated(self):
        # Same rule as the map's "heard direct": any "*" means a
        # digipeater repeated it -- an alias-only WIDE1* included.
        stats = ReceptionStats()
        stats.update(self.packet("N5BA", ()))
        stats.update(self.packet("N5BA", ("WIDE1-1", "WIDE2-1")))   # unused path
        stats.update(self.packet("N5BA", ("W5NGU-3*", "WIDE2-1")))
        stats.update(self.packet("N5BA", ("WIDE1*", "WIDE2-1")))

        result = stats.snapshot()

        self.assertEqual(result["direct_packets"], 2)
        self.assertEqual(result["digipeated_packets"], 2)
        # The older field still counts only callsign-marked digipeaters.
        self.assertEqual(result["used_digipeater_packets"], 1)

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
