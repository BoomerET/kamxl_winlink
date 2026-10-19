"""
The project was renamed from kamxl_winlink to tncstation. These check
that existing setups keep working: old KAMXL_* settings, the old
kamxl_daemon / kamxl_rest module names (and running them as scripts),
and the Winlink client name, which is registered and must NOT change.
"""

import subprocess
import sys
import unittest

from pathlib import Path
from unittest.mock import patch

import fakes  # noqa: F401  (puts the repo root on sys.path)

from envfile import ENV_PREFIX, LEGACY_ENV_PREFIX, apply_legacy_env


ROOT = Path(__file__).resolve().parent.parent


class LegacySettingsTests(unittest.TestCase):
    def test_prefixes(self):
        self.assertEqual(ENV_PREFIX, "TNCSTATION_")
        self.assertEqual(LEGACY_ENV_PREFIX, "KAMXL_")

    def test_old_name_fills_in_new_name(self):
        env = {"KAMXL_PORT": "/dev/kam-xl", "KAMXL_CALLSIGN": "N5BA"}

        used = apply_legacy_env(env)

        self.assertEqual(env["TNCSTATION_PORT"], "/dev/kam-xl")
        self.assertEqual(env["TNCSTATION_CALLSIGN"], "N5BA")
        self.assertEqual(used, ["KAMXL_CALLSIGN", "KAMXL_PORT"])

    def test_new_name_wins(self):
        env = {"KAMXL_PORT": "/dev/old", "TNCSTATION_PORT": "/dev/new"}

        used = apply_legacy_env(env)

        self.assertEqual(env["TNCSTATION_PORT"], "/dev/new")
        self.assertEqual(used, [])

    def test_unrelated_settings_untouched(self):
        env = {"WINLINK_API_KEY": "secret", "PATH": "/bin"}

        apply_legacy_env(env)

        self.assertEqual(env, {"WINLINK_API_KEY": "secret", "PATH": "/bin"})

    def test_old_env_file_reaches_the_daemon(self):
        # End to end through the daemon's real argument parsing: an
        # old-style setting only, no new names anywhere.
        import tncstation_daemon

        captured = {}

        def fake_create_tnc(model, port, baudrate=None):
            captured.update(model=model, port=port)
            raise SystemExit(0)

        with patch.dict(
            "os.environ",
            {"KAMXL_PORT": "/dev/kam-xl", "KAMXL_CALLSIGN": "N5BA-10",
             "KAMXL_TNC": "opentnc"},
            clear=True,
        ), patch.object(tncstation_daemon, "_load_dotenv"), \
                patch.object(tncstation_daemon, "create_tnc", fake_create_tnc), \
                patch("logging.basicConfig"), \
                self.assertLogs("tncstation_daemon", "INFO") as logs:
            with self.assertRaises(SystemExit):
                tncstation_daemon.main([])

        self.assertIn("KAMXL_PORT", "\n".join(logs.output))

        self.assertEqual(captured, {"model": "opentnc", "port": "/dev/kam-xl"})


class OldModuleNameTests(unittest.TestCase):
    def test_old_modules_are_the_new_modules(self):
        import kamxl_daemon
        import kamxl_rest
        import tncstation_daemon
        import tncstation_rest

        self.assertIs(kamxl_daemon, tncstation_daemon)
        self.assertIs(kamxl_rest, tncstation_rest)
        self.assertIs(kamxl_daemon.KAMDaemon, tncstation_daemon.KAMDaemon)

    def test_old_script_name_still_runs_the_daemon(self):
        result = subprocess.run(
            [sys.executable, "kamxl_daemon.py", "--help"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("tncstation background daemon", result.stdout)
        self.assertIn("--tnc", result.stdout)

    def test_old_script_name_still_runs_the_web_server(self):
        result = subprocess.run(
            [sys.executable, "kamxl_rest.py", "--help"],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )

        self.assertEqual(result.returncode, 0, result.stderr)


class WinlinkIdentityTests(unittest.TestCase):
    def test_registered_client_name_is_unchanged(self):
        # "kamxl_winlink" is the client name registered with Winlink
        # (and the one the Winlink API key was issued for). The
        # production CMS rejects unregistered names, so the repo rename
        # must not change it.
        from winlink import build_handshake_response

        self.assertIn(
            "[kamxl_winlink-",
            build_handshake_response("N5BA-10"),
        )


if __name__ == "__main__":
    unittest.main()
