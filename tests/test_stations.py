import unittest

from fakes import make_kam  # noqa: F401 (ensures kamxl/ is on sys.path)

from packet import Packet
from stations import StationTracker


def _ui_packet(source, payload, port=1):
    return Packet(
        source=source,
        destination="APRS",
        digipeaters=(),
        port=port,
        payload=payload,
        raw=f"{source}>APRS/{port}: <UI>:\n{payload}",
        frame_type="UI",
    )


def _control_packet(source, frame_type):
    return Packet(
        source=source,
        destination="N0CALL",
        digipeaters=(),
        port=1,
        payload="",
        raw="",
        frame_type=frame_type,
    )


class StationTrackerTests(unittest.TestCase):
    def test_position_packet_creates_station(self):
        tracker = StationTracker()

        station = tracker.update(
            _ui_packet("N5BA-9", "!4903.50N/07201.75W-Test"),
            now=1000.0,
        )

        self.assertIsNotNone(station)
        self.assertEqual(station.callsign, "N5BA-9")
        self.assertAlmostEqual(station.latitude, 49 + 3.50 / 60, places=6)
        self.assertEqual(station.last_heard, 1000.0)
        self.assertEqual(station.packet_count, 1)

        self.assertEqual(tracker.get_station("N5BA-9"), station)
        self.assertEqual(tracker.list_stations(), [station])

    def test_non_ui_frame_ignored(self):
        tracker = StationTracker()

        # A "C" (connect) control frame never carries an APRS
        # payload -- update() should skip attempting to parse it
        # entirely, not just fail to find a position in empty text.
        result = tracker.update(_control_packet("N0CALL", "C"))

        self.assertIsNone(result)
        self.assertEqual(tracker.list_stations(), [])

    def test_frame_type_none_still_considered(self):
        # frame_type is None when MCOM/MRESP tags were off (or a
        # header line didn't carry one) -- still treated as
        # potentially-APRS, same as an explicit "UI" tag.
        packet = _ui_packet("N5BA-9", "!4903.50N/07201.75W-")
        packet = Packet(
            source=packet.source,
            destination=packet.destination,
            digipeaters=packet.digipeaters,
            port=packet.port,
            payload=packet.payload,
            raw=packet.raw,
            frame_type=None,
        )

        tracker = StationTracker()
        station = tracker.update(packet)

        self.assertIsNotNone(station)

    def test_non_position_payload_ignored(self):
        tracker = StationTracker()

        result = tracker.update(_ui_packet("N5BA-9", "Just chatter"))

        self.assertIsNone(result)
        self.assertEqual(tracker.list_stations(), [])

    def test_second_report_updates_existing_station(self):
        tracker = StationTracker()

        tracker.update(
            _ui_packet("N5BA-9", "!4903.50N/07201.75W-"), now=1000.0
        )
        second = tracker.update(
            _ui_packet("N5BA-9", "!4900.00N/07200.00W-"), now=2000.0
        )

        self.assertEqual(second.packet_count, 2)
        self.assertEqual(second.last_heard, 2000.0)
        self.assertAlmostEqual(second.latitude, 49.0, places=6)

        # Only the latest position is kept -- one row per callsign.
        self.assertEqual(tracker.list_stations(), [second])

    def test_different_ssids_are_distinct_stations(self):
        tracker = StationTracker()

        tracker.update(_ui_packet("N5BA-1", "!4903.50N/07201.75W-"))
        tracker.update(_ui_packet("N5BA-9", "!4900.00N/07200.00W-"))

        callsigns = {s.callsign for s in tracker.list_stations()}
        self.assertEqual(callsigns, {"N5BA-1", "N5BA-9"})

    def test_list_stations_sorted_by_callsign(self):
        tracker = StationTracker()

        tracker.update(_ui_packet("WB5NZV", "!4903.50N/07201.75W-"))
        tracker.update(_ui_packet("N5BA-9", "!4900.00N/07200.00W-"))
        tracker.update(_ui_packet("KD5EOC", "!4901.00N/07200.00W-"))

        callsigns = [s.callsign for s in tracker.list_stations()]
        self.assertEqual(callsigns, ["KD5EOC", "N5BA-9", "WB5NZV"])

    def test_get_unknown_callsign_returns_none(self):
        tracker = StationTracker()

        self.assertIsNone(tracker.get_station("NOBODY"))

    def test_uses_wall_clock_when_now_not_supplied(self):
        # Doesn't stub time.time() -- just checks a real value came
        # back, proving the "now is None" default path runs at all.
        tracker = StationTracker()

        station = tracker.update(
            _ui_packet("N5BA-9", "!4903.50N/07201.75W-")
        )

        self.assertIsNotNone(station)
        self.assertGreater(station.last_heard, 0)

    def test_expire_stale_stations(self):
        tracker = StationTracker()

        tracker.update(
            _ui_packet("OLD-1", "!4903.50N/07201.75W-"),
            now=1000.0,
        )
        tracker.update(
            _ui_packet("NEW-1", "!4900.00N/07200.00W-"),
            now=4000.0,
        )

        removed = tracker.expire_stale(
            max_age_seconds=3600,
            now=5000.0,
        )

        self.assertEqual(removed, 1)
        self.assertIsNone(tracker.get_station("OLD-1"))
        self.assertIsNotNone(tracker.get_station("NEW-1"))

    def test_history_tracks_movement(self):
        tracker = StationTracker()

        tracker.update(
            _ui_packet("N5BA-9", "!4903.50N/07201.75W-"),
            now=1000.0,
        )
        station = tracker.update(
            _ui_packet("N5BA-9", "!4904.50N/07202.75W-"),
            now=2000.0,
        )

        self.assertEqual(len(station.history), 2)
        self.assertEqual(
            [p.timestamp for p in station.history],
            [1000.0, 2000.0],
        )
        self.assertNotEqual(
            station.history[0].latitude,
            station.history[1].latitude,
        )

    def test_history_ignores_duplicate_positions(self):
        tracker = StationTracker()

        tracker.update(
            _ui_packet("N5BA-9", "!4903.50N/07201.75W-"),
            now=1000.0,
        )
        station = tracker.update(
            _ui_packet("N5BA-9", "!4903.50N/07201.75W-"),
            now=2000.0,
        )

        self.assertEqual(len(station.history), 1)
        self.assertEqual(station.history[0].timestamp, 1000.0)
        self.assertEqual(station.last_heard, 2000.0)
        self.assertEqual(station.packet_count, 2)

    def test_history_limited_to_25_positions(self):
        tracker = StationTracker()

        for minute in range(30):
            tracker.update(
                _ui_packet(
                    "N5BA-9",
                    f"!49{minute:02d}.00N/07201.75W-",
                ),
                now=1000.0 + minute,
            )

        station = tracker.get_station("N5BA-9")

        self.assertEqual(len(station.history), 25)
        self.assertEqual(station.history[0].timestamp, 1005.0)
        self.assertEqual(station.history[-1].timestamp, 1029.0)
        self.assertEqual(station.packet_count, 30)

    def test_position_packet_is_retained(self):
        tracker = StationTracker()
        packet = _ui_packet(
            "N5BA-9", "!4903.50N/07201.75W-Test"
        )

        station = tracker.update(packet, now=1000.0)

        self.assertEqual(len(station.recent_packets), 1)
        self.assertEqual(station.recent_packets[0].timestamp, 1000.0)
        self.assertEqual(station.recent_packets[0].payload, packet.payload)

    def test_non_position_packet_retained_for_known_station(self):
        tracker = StationTracker()

        tracker.update(
            _ui_packet("N5BA-9", "!4903.50N/07201.75W-"),
            now=1000.0,
        )

        station = tracker.update(
            _ui_packet("N5BA-9", ">Monitoring APRS"),
            now=1010.0,
        )

        self.assertEqual(station.packet_count, 1)
        self.assertEqual(station.last_heard, 1000.0)
        self.assertEqual(len(station.recent_packets), 2)
        self.assertEqual(
            station.recent_packets[-1].payload,
            ">Monitoring APRS",
        )

    def test_recent_packets_bounded_to_25(self):
        tracker = StationTracker()

        tracker.update(
            _ui_packet("N5BA-9", "!4903.50N/07201.75W-"),
            now=1000.0,
        )

        for i in range(30):
            station = tracker.update(
                _ui_packet("N5BA-9", f">Status {i}"),
                now=1001.0 + i,
            )

        self.assertEqual(len(station.recent_packets), 25)
        self.assertEqual(
            station.recent_packets[-1].payload,
            ">Status 29",
        )
        self.assertEqual(
            station.recent_packets[0].payload,
            ">Status 5",
        )

    def test_recent_packets_preserve_digipeater_path(self):
        tracker = StationTracker()

        packet = Packet(
            source="N5BA-9",
            destination="APRS",
            digipeaters=("WIDE1-1*", "WIDE2-1"),
            port=2,
            payload="!4903.50N/07201.75W-Test",
            raw="N5BA-9>APRS,WIDE1-1*,WIDE2-1/2: <UI>:\n"
                "!4903.50N/07201.75W-Test",
            frame_type="UI",
        )

        station = tracker.update(packet, now=1000.0)
        record = station.recent_packets[0]

        self.assertEqual(record.digipeaters, ("WIDE1-1*", "WIDE2-1"))
        self.assertEqual(record.destination, "APRS")
        self.assertEqual(record.port, 2)
        self.assertEqual(record.raw, packet.raw)
        self.assertEqual(record.frame_type, "UI")

    def test_control_frames_not_retained_for_known_station(self):
        tracker = StationTracker()

        tracker.update(
            _ui_packet("N5BA-9", "!4903.50N/07201.75W-"),
            now=1000.0,
        )

        result = tracker.update(
            _control_packet("N5BA-9", "C"),
            now=1010.0,
        )

        station = tracker.get_station("N5BA-9")

        self.assertIsNone(result)
        self.assertEqual(len(station.recent_packets), 1)
        self.assertEqual(station.packet_count, 1)

if __name__ == "__main__":
    unittest.main()
