"""OpenTNC receiver lockup: the daemon resets it with RESTART."""

import itertools
import time
import unittest
from unittest.mock import Mock, patch

import fakes  # noqa: F401  (puts the repo root on sys.path)
from fakes import ScriptedSerial, make_kam

import opentnc
from kiss import KISS_EXIT
from opentnc import OpenTNCKISS
from test_kiss import FakeOpenTNC
from tnc import TNCError
from tncstation_daemon import KAMDaemon


def quick(tnc):
    tnc.PROMPT_WAIT = tnc.ENTER_WAIT = tnc.BOOT_QUIET = 0.01
    tnc.RESET_GONE_TIMEOUT, tnc.RESET_BACK_TIMEOUT, tnc.RESET_POLL = 0.2, 0.2, 0.01
    return tnc


class OpenTNCResetTests(unittest.TestCase):
    def test_restart_and_back_in_kiss(self):
        tnc = quick(OpenTNCKISS("/dev/opentnc", mycall="N5BA-1"))
        old = tnc.serial = FakeOpenTNC(kiss=True)
        rebooted = FakeOpenTNC()           # comes back at cmd:
        # Port still there, then gone (reboot), then back.
        exists = itertools.chain([True, False, False, True], itertools.repeat(True))

        with patch.object(opentnc.os.path, "exists", lambda p: next(exists)), \
                patch("kiss.serial.Serial", return_value=rebooted):
            tnc.reset_tnc()

        self.assertEqual(old.written[0], KISS_EXIT)
        self.assertIn(b"RESTART\r", old.written)
        self.assertFalse(old.is_open)
        self.assertIs(tnc.serial, rebooted)
        self.assertTrue(rebooted.kiss)
        self.assertIn(b"KISS NOW\r", rebooted.written)

    def test_not_coming_back_is_an_error(self):
        tnc = quick(OpenTNCKISS("/dev/opentnc"))
        tnc.serial = FakeOpenTNC(kiss=True)
        with patch.object(opentnc.os.path, "exists", lambda p: False), \
                self.assertRaises(TNCError):
            tnc.reset_tnc()


class DaemonResetTests(unittest.TestCase):
    def daemon(self, minutes=10, supported=True):
        kam = make_kam(ScriptedSerial({}))
        d = KAMDaemon(kam, "/tmp/tncstation-reset-test.sock",
                      station_callsign="N5BA-1", tnc_reset_minutes=minutes)
        self.addCleanup(d.shutdown)
        kam.RESET_SUPPORTED = supported
        kam.reset_tnc = Mock()
        return d

    def heard(self, d, at):
        with d._stats_lock:
            d._last_other_packet_at = at

    def test_resets_after_quiet_then_rate_limited(self):
        d = self.daemon()
        now = time.time()
        self.heard(d, now - 9 * 60)
        self.assertFalse(d._tnc_reset_tick(now))           # not quiet long enough

        self.heard(d, now - 11 * 60)
        with self.assertLogs("tncstation_daemon", "WARNING") as logs:
            self.assertTrue(d._tnc_reset_tick(now))
        self.assertIn("reset the KAM-XL", "\n".join(logs.output))
        self.assertEqual(d.kam.reset_tnc.call_count, 1)

        # Still nothing heard: not again within 30 minutes...
        self.assertFalse(d._tnc_reset_tick(now + 20 * 60))
        # ...but again after that.
        self.assertTrue(d._tnc_reset_tick(now + 31 * 60))
        self.assertEqual(d.kam.reset_tnc.call_count, 2)
        self.assertEqual(d.dispatch("status", {})["diagnostics"]["tnc_resets"], 2)

    def test_hearing_again_after_a_reset_counts_from_then(self):
        d = self.daemon()
        now = time.time()
        self.heard(d, now - 11 * 60)
        d._tnc_reset_tick(now)
        self.heard(d, now + 25 * 60)                       # receiving again
        # 34 min after the reset (past the 30-min limit), but only 9 min
        # since the last packet: no reset.
        self.assertFalse(d._tnc_reset_tick(now + 34 * 60))
        # 11 min since the last packet: reset.
        self.assertTrue(d._tnc_reset_tick(now + 36 * 60))

    def test_off_and_unsupported(self):
        for d in (self.daemon(minutes=0), self.daemon(supported=False)):
            self.heard(d, time.time() - 3600)
            self.assertFalse(d._tnc_reset_tick())
            d.kam.reset_tnc.assert_not_called()

    def test_minimum(self):
        with self.assertRaises(ValueError):
            KAMDaemon(make_kam(ScriptedSerial({})), "/tmp/x.sock", tnc_reset_minutes=2)


if __name__ == "__main__":
    unittest.main()
