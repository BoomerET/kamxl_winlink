"""
The project was renamed from kamxl_winlink to tncstation, with no
compatibility layer left: old KAMXL_* settings aren't read, and the
kamxl_daemon / kamxl_rest aliases are gone. What must NOT change is the
Winlink client name, which is registered.
"""

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
    def test_old_script_names_are_gone(self):
        for name in ("kamxl_daemon.py", "kamxl_rest.py"):
            self.assertFalse((ROOT / name).exists(), name)


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
