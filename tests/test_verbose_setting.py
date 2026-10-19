"""
TNCSTATION_VERBOSE: --verbose from .env, for both programs.
"""

import logging
import unittest

from unittest.mock import patch

import fakes  # noqa: F401  (puts the repo root on sys.path)

from envfile import env_flag


class EnvFlagTests(unittest.TestCase):
    def test_true_values(self):
        for value in ("1", "true", "TRUE", "yes", "On", " on "):
            self.assertTrue(env_flag("VERBOSE", {"TNCSTATION_VERBOSE": value}), value)

    def test_false_values(self):
        for value in ("", "0", "false", "no", "off", "maybe"):
            self.assertFalse(env_flag("VERBOSE", {"TNCSTATION_VERBOSE": value}), value)

        self.assertFalse(env_flag("VERBOSE", {}))


class _Stop(Exception):
    pass


def _daemon_log_level(env, argv=()):
    """Run the daemon's main() up to logging setup; return the level."""
    import tncstation_daemon

    levels = []

    def fake_create_tnc(*args, **kwargs):
        raise _Stop

    with patch.dict("os.environ", dict(env, TNCSTATION_PORT="/dev/null",
                                       TNCSTATION_CALLSIGN="N5BA"), clear=True), \
            patch.object(tncstation_daemon, "_load_dotenv"), \
            patch.object(tncstation_daemon, "create_tnc", fake_create_tnc), \
            patch("logging.basicConfig", lambda **kw: levels.append(kw["level"])):
        try:
            tncstation_daemon.main(list(argv))
        except _Stop:
            pass

    return levels[0]


def _rest_log_level(env, argv=()):
    """Run the web server's main() up to logging setup; return the level."""
    import tncstation_rest

    levels = []

    def fake_basic_config(**kw):
        levels.append(kw["level"])
        raise _Stop

    with patch.dict("os.environ", dict(env, TNCSTATION_REST_API_KEY="k"), clear=True), \
            patch.object(tncstation_rest, "_load_dotenv"), \
            patch("logging.basicConfig", fake_basic_config):
        try:
            tncstation_rest.main(list(argv))
        except _Stop:
            pass

    return levels[0]


class DaemonVerboseTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual(_daemon_log_level({}), logging.INFO)

    def test_setting_turns_it_on(self):
        self.assertEqual(_daemon_log_level({"TNCSTATION_VERBOSE": "1"}), logging.DEBUG)

    def test_old_setting_name_works(self):
        self.assertEqual(_daemon_log_level({"KAMXL_VERBOSE": "yes"}), logging.DEBUG)

    def test_flag_still_works(self):
        self.assertEqual(_daemon_log_level({}, ["--verbose"]), logging.DEBUG)
        self.assertEqual(_daemon_log_level({}, ["-v"]), logging.DEBUG)

    def test_no_verbose_overrides_the_setting(self):
        self.assertEqual(
            _daemon_log_level({"TNCSTATION_VERBOSE": "1"}, ["--no-verbose"]),
            logging.INFO,
        )


class RestVerboseTests(unittest.TestCase):
    def test_off_by_default(self):
        self.assertEqual(_rest_log_level({}), logging.INFO)

    def test_setting_turns_it_on(self):
        self.assertEqual(_rest_log_level({"TNCSTATION_VERBOSE": "true"}), logging.DEBUG)

    def test_no_verbose_overrides_the_setting(self):
        self.assertEqual(
            _rest_log_level({"TNCSTATION_VERBOSE": "1"}, ["--no-verbose"]),
            logging.INFO,
        )


if __name__ == "__main__":
    unittest.main()
