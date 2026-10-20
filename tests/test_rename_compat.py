"""
The project was renamed from kamxl_winlink to tncstation. These check
the old kamxl_daemon / kamxl_rest module names (and running them as
scripts), and the Winlink client name, which is registered and must
NOT change. Old KAMXL_* settings are no longer read.
"""

import subprocess
import sys
import unittest

from pathlib import Path

import fakes  # noqa: F401  (puts the repo root on sys.path)

import envfile


ROOT = Path(__file__).resolve().parent.parent


class SettingsPrefixTests(unittest.TestCase):
    def test_only_the_new_prefix_is_read(self):
        self.assertEqual(envfile.ENV_PREFIX, "TNCSTATION_")
        self.assertFalse(hasattr(envfile, "apply_legacy_env"))
        self.assertFalse(envfile.env_flag("VERBOSE", {"KAMXL_VERBOSE": "1"}))


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
