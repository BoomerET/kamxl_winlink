import json
import unittest
import urllib.parse

import winlink_api as api


def _fake_transport(status=200, body=b""):
    """
    Records every call and always returns the given (status, body) --
    same fakes-over-mocks discipline as tests/fakes.py's
    ScriptedSerial/CannedSerial, just for the injectable HTTP
    Transport instead of the serial port.
    """
    calls = []

    def transport(method, url, data, headers, timeout):
        calls.append({
            "method": method,
            "url": url,
            "data": data,
            "headers": headers,
            "timeout": timeout,
        })
        return status, body

    return transport, calls


class AccountExistsTests(unittest.TestCase):
    def test_true_when_account_exists(self):
        transport, calls = _fake_transport(200, json.dumps({
            "CallsignExists": True,
            "ResponseStatus": {},
        }).encode("utf-8"))

        result = api.account_exists("N5BA", "KEY123", transport=transport)

        self.assertTrue(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "GET")

        parsed = urllib.parse.urlparse(calls[0]["url"])
        self.assertEqual(parsed.path, "/account/exists")
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["callsign"], ["N5BA"])
        self.assertEqual(query["key"], ["KEY123"])

    def test_false_when_account_does_not_exist(self):
        transport, _ = _fake_transport(200, json.dumps({
            "CallsignExists": False,
            "ResponseStatus": {},
        }).encode("utf-8"))

        self.assertFalse(
            api.account_exists("N0CALL", "KEY123", transport=transport)
        )

    def test_populated_response_status_raises(self):
        transport, _ = _fake_transport(200, json.dumps({
            "CallsignExists": False,
            "ResponseStatus": {
                "ErrorCode": "InvalidKey",
                "Message": "The access key is not valid.",
            },
        }).encode("utf-8"))

        with self.assertRaises(api.WinlinkAPIError):
            api.account_exists("N5BA", "BADKEY", transport=transport)

    def test_non_2xx_status_raises(self):
        transport, _ = _fake_transport(403, b'{"error": "forbidden"}')

        with self.assertRaises(api.WinlinkAPIError):
            api.account_exists("N5BA", "KEY123", transport=transport)

    def test_malformed_json_raises(self):
        transport, _ = _fake_transport(200, b"not json")

        with self.assertRaises(api.WinlinkAPIError):
            api.account_exists("N5BA", "KEY123", transport=transport)


class GetGatewayStatusTests(unittest.TestCase):
    RAW_RESPONSE = {
        "ServerName": "CMS-B",
        "ErrorCode": 0,
        "Gateways": [
            {
                "Callsign": "KD5EOC-10",
                "BaseCallsign": "KD5EOC",
                "RequestedMode": "Packet",
                "Comments": "",
                "LastStatus": "Thu, 06 Aug 2026 12:00:00 GMT",
                "Latitude": 33.21,
                "Longitude": -97.13,
                "GatewayChannels": [
                    {
                        "OperatingHours": "24/7",
                        "SupportedModes": "PACKET",
                        "Frequency": 145.09,
                        "ServiceCode": "PUBLIC",
                        "Baud": "1200",
                        "RadioRange": "25",
                        "Mode": 1,
                        "Gridsquare": "EM13ov",
                        "Antenna": "Vertical",
                    },
                ],
            },
            {
                "Callsign": "W5ABC-10",
                "BaseCallsign": "W5ABC",
                "RequestedMode": "Packet",
                "Comments": "No location on file",
                "LastStatus": "Thu, 06 Aug 2026 11:00:00 GMT",
                "Latitude": 0,
                "Longitude": 0,
                "GatewayChannels": [],
            },
        ],
    }

    def test_parses_gateways_and_channels(self):
        transport, _ = _fake_transport(
            200, json.dumps(self.RAW_RESPONSE).encode("utf-8")
        )

        gateways = api.get_gateway_status("KEY123", transport=transport)

        self.assertEqual(len(gateways), 2)

        first = gateways[0]
        self.assertEqual(first.callsign, "KD5EOC-10")
        self.assertEqual(first.base_callsign, "KD5EOC")
        self.assertEqual(first.latitude, 33.21)
        self.assertEqual(first.longitude, -97.13)
        self.assertEqual(len(first.channels), 1)
        self.assertEqual(first.channels[0].frequency, 145.09)
        self.assertEqual(first.channels[0].gridsquare, "EM13ov")

        second = gateways[1]
        self.assertEqual(second.channels, [])

    def test_key_rides_in_body_not_query_string(self):
        # Genuinely different from account/exists's convention -- see
        # winlink_api.py's module docstring. Pinned down here so a
        # future refactor can't silently "normalize" it back to the
        # wrong place.
        transport, calls = _fake_transport(
            200, json.dumps(self.RAW_RESPONSE).encode("utf-8")
        )

        api.get_gateway_status("KEY123", transport=transport)

        parsed_url = urllib.parse.urlparse(calls[0]["url"])
        self.assertEqual(parsed_url.query, "")

        body = urllib.parse.parse_qs(calls[0]["data"].decode("ascii"))
        self.assertEqual(body["key"], ["KEY123"])

    def test_mode_and_history_hours_and_service_codes_in_body(self):
        transport, calls = _fake_transport(
            200, json.dumps(self.RAW_RESPONSE).encode("utf-8")
        )

        api.get_gateway_status(
            "KEY123",
            mode="packet",
            history_hours=48,
            service_codes=("PUBLIC", "EMCOMM"),
            transport=transport,
        )

        body = urllib.parse.parse_qs(calls[0]["data"].decode("ascii"))
        self.assertEqual(body["Mode"], ["packet"])
        self.assertEqual(body["HistoryHours"], ["48"])
        # Repeated field -- both service codes must survive, not just
        # the last one a plain dict would have overwritten.
        self.assertEqual(body["ServiceCodes"], ["PUBLIC", "EMCOMM"])

    def test_history_hours_clamped_to_48(self):
        transport, calls = _fake_transport(
            200, json.dumps(self.RAW_RESPONSE).encode("utf-8")
        )

        api.get_gateway_status("KEY123", history_hours=999, transport=transport)

        body = urllib.parse.parse_qs(calls[0]["data"].decode("ascii"))
        self.assertEqual(body["HistoryHours"], ["48"])

    def test_nonzero_error_code_raises(self):
        transport, _ = _fake_transport(200, json.dumps({
            "ServerName": "CMS-B",
            "ErrorCode": 7,
            "Gateways": [],
        }).encode("utf-8"))

        with self.assertRaises(api.WinlinkAPIError):
            api.get_gateway_status("KEY123", transport=transport)

    def test_empty_gateway_list(self):
        transport, _ = _fake_transport(200, json.dumps({
            "ServerName": "CMS-B",
            "ErrorCode": 0,
            "Gateways": [],
        }).encode("utf-8"))

        self.assertEqual(
            api.get_gateway_status("KEY123", transport=transport), []
        )


class HaversineTests(unittest.TestCase):
    def test_same_point_is_zero_distance(self):
        self.assertEqual(api._haversine_km(33.0, -97.0, 33.0, -97.0), 0.0)

    def test_one_degree_of_longitude_at_equator(self):
        # Independent cross-check, same "compute it a second way and
        # compare" discipline as lzhuf.py's CRC-16 test and
        # winlink.py's proposal-checksum test: one degree of longitude
        # at the equator is (2 * pi * R) / 360, using this module's
        # own Earth radius constant.
        expected = (2 * 3.141592653589793 * api._EARTH_RADIUS_KM) / 360

        distance = api._haversine_km(0.0, 0.0, 0.0, 1.0)

        self.assertAlmostEqual(distance, expected, places=6)


class NearbyGatewaysTests(unittest.TestCase):
    def _gateway(self, callsign, lat, lon):
        return api.Gateway(
            callsign=callsign,
            base_callsign=callsign.split("-")[0],
            requested_mode="Packet",
            comments="",
            last_status="",
            latitude=lat,
            longitude=lon,
        )

    def test_sorted_nearest_first(self):
        far = self._gateway("FAR-10", 40.0, -100.0)
        near = self._gateway("NEAR-10", 33.05, -97.05)
        origin = (33.0, -97.0)

        results = api.nearby_gateways([far, near], *origin)

        self.assertEqual([gw.callsign for gw, _ in results], ["NEAR-10", "FAR-10"])
        # Distances themselves must actually be ascending, not just
        # coincidentally in the right order for this fixture.
        self.assertLess(results[0][1], results[1][1])

    def test_zero_zero_coordinates_excluded(self):
        unset = self._gateway("UNSET-10", 0.0, 0.0)
        real = self._gateway("REAL-10", 33.05, -97.05)

        results = api.nearby_gateways([unset, real], 33.0, -97.0)

        self.assertEqual([gw.callsign for gw, _ in results], ["REAL-10"])

    def test_max_distance_km_filters(self):
        near = self._gateway("NEAR-10", 33.05, -97.05)
        far = self._gateway("FAR-10", 45.0, -110.0)

        results = api.nearby_gateways(
            [near, far], 33.0, -97.0, max_distance_km=50
        )

        self.assertEqual([gw.callsign for gw, _ in results], ["NEAR-10"])

    def test_limit_truncates(self):
        gateways = [
            self._gateway(f"GW{i}-10", 33.0 + i * 0.1, -97.0)
            for i in range(5)
        ]

        results = api.nearby_gateways(gateways, 33.0, -97.0, limit=2)

        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
