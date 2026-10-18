"""Offline tests for automatic history-database backups (db_backup.py)."""

import gzip
import io
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from unittest.mock import MagicMock, patch

import db_backup
import kamxl_daemon
from db_backup import BackupManager, parse_backup_time
from fakes import ScriptedSerial, make_kam
from kamxl_daemon import KAMDaemon
from packet import Packet
from reception_history import ReceptionHistory
from test_rest import RestTestCase


def _local(y, mo, d, h, mi):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def _packet(i):
    return Packet(source="K5S%d" % i, destination="APRS", digipeaters=(),
                  port=2, frame_type="UI", payload="!3313.65N/09707.64W-%d" % i,
                  raw="")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class BackupTestCase(unittest.TestCase):

    def setUp(self):
        root = tempfile.mkdtemp()
        self.db = os.path.join(root, "data", "history.sqlite3")
        self.folder = os.path.join(root, "share")
        os.makedirs(self.folder)
        history = ReceptionHistory(self.db)
        for i in range(50):
            history.record(_packet(i), received_at=1_800_000_000 + i)
        history.close()

    def manager(self, **kwargs):
        kwargs.setdefault("allow_same_disk", True)  # temp dirs share a disk
        return BackupManager(self.db, self.folder, **kwargs)

    def restore(self, name):
        path = os.path.join(tempfile.mkdtemp(), "restored.sqlite3")
        with gzip.open(os.path.join(self.folder, name)) as src, \
                open(path, "wb") as dst:
            dst.write(src.read())
        return path


class BackupRunTests(BackupTestCase):

    def test_backup_restores_to_a_working_database(self):
        result = self.manager().run()
        self.assertTrue(result["ok"], result)
        self.assertRegex(result["file"],
                         r"^kamxl-history-\d{8}-\d{6}\.sqlite3\.gz$")

        restored = ReceptionHistory(self.restore(result["file"]))
        self.addCleanup(restored.close)
        self.assertEqual(restored.total_packets(), 50)
        mode = restored._connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertIn(mode, ("delete", "wal"))  # opens either way

        # Nothing temporary left behind, here or next to the database.
        self.assertEqual(os.listdir(self.folder), [result["file"]])
        self.assertFalse([f for f in os.listdir(os.path.dirname(self.db))
                          if f.startswith(".kamxl-backup-")])

    def test_backup_while_packets_are_being_written(self):
        stop = threading.Event()

        def writer():
            history = ReceptionHistory(self.db)
            i = 1000
            while not stop.is_set():
                history.record(_packet(i), received_at=1_800_000_000 + i)
                i += 1
            history.close()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            time.sleep(0.05)
            result = self.manager().run()
        finally:
            stop.set()
            thread.join()
        self.assertTrue(result["ok"], result)
        conn = sqlite3.connect(self.restore(result["file"]))
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertGreaterEqual(
            conn.execute("SELECT COUNT(*) FROM received_packets").fetchone()[0], 50
        )

    def test_same_disk_refused_by_default(self):
        failures = []
        m = self.manager(allow_same_disk=False, on_failure=failures.append)
        with self.assertLogs("kamxl_backup", "ERROR"):
            result = m.run()
        self.assertFalse(result["ok"])
        self.assertIn("is the network share mounted", result["error"])
        self.assertEqual(os.listdir(self.folder), [])
        self.assertEqual(len(failures), 1)

    def test_missing_folder(self):
        m = BackupManager(self.db, self.folder + "-gone", allow_same_disk=True)
        with self.assertLogs("kamxl_backup", "ERROR"):
            result = m.run()
        self.assertIn("doesn't exist", result["error"])
        self.assertFalse(m.status()["folder_reachable"])

    def test_failure_alert_once_then_recovery(self):
        failures, recoveries = [], []
        m = self.manager(on_failure=failures.append,
                         on_recovery=recoveries.append)
        os.rename(self.folder, self.folder + "-away")
        with self.assertLogs("kamxl_backup", "ERROR"):
            m.run()
            m.run()
        self.assertEqual(len(failures), 1)
        self.assertTrue(m.status()["failing"])
        self.assertIsNotNone(m.status()["last_error"])

        os.rename(self.folder + "-away", self.folder)
        self.assertTrue(m.run()["ok"])
        self.assertEqual(len(recoveries), 1)
        status = m.status()
        self.assertFalse(status["failing"])
        self.assertIsNone(status["last_error"])
        self.assertEqual(status["count"], 1)

    def test_keeps_newest_and_leaves_other_files_alone(self):
        names = ["kamxl-history-2026101%d-033000.sqlite3.gz" % d for d in range(1, 6)]
        for name in names + ["notes.txt", "kamxl-history-old.sqlite3.gz"]:
            with open(os.path.join(self.folder, name), "wb") as f:
                f.write(b"x")
        result = self.manager(keep=3).run()
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 3)
        left = sorted(os.listdir(self.folder))
        self.assertEqual(left, sorted([
            result["file"], names[4], names[3],
            "notes.txt", "kamxl-history-old.sqlite3.gz",
        ]))

    def test_one_backup_at_a_time(self):
        m = self.manager()
        m._lock.acquire()
        try:
            self.assertEqual(m.run(), {"ok": False,
                                       "error": "A backup is already running"})
        finally:
            m._lock.release()

    def test_settings_validated(self):
        with self.assertRaises(ValueError):
            BackupManager(self.db, "relative/path")
        with self.assertRaises(ValueError):
            BackupManager(self.db, self.folder, keep=0)
        self.assertEqual(parse_backup_time(None), (3, 30))
        self.assertEqual(parse_backup_time("7:05"), (7, 5))
        for bad in ("24:00", "3.30", "noon"):
            with self.assertRaises(ValueError):
                parse_backup_time(bad)


class ScheduleTests(BackupTestCase):

    def test_first_backup_shortly_after_startup(self):
        clock = Clock(_local(2026, 10, 18, 12, 0))
        m = self.manager(clock=clock)
        self.assertEqual(m.next_due_at(), clock.now + db_backup.STARTUP_DELAY)
        self.assertIsNone(m.tick())
        clock.now += db_backup.STARTUP_DELAY
        self.assertTrue(m.tick()["ok"])

    def test_daily_at_the_configured_time(self):
        clock = Clock(_local(2026, 10, 18, 12, 0))
        m = self.manager(clock=clock, at=(3, 30))
        m.last_success_at = _local(2026, 10, 18, 3, 31)  # done today
        self.assertEqual(m.next_due_at(), _local(2026, 10, 19, 3, 30))

        clock.now = _local(2026, 10, 19, 3, 29)
        self.assertIsNone(m.tick())
        clock.now = _local(2026, 10, 19, 3, 30)
        self.assertTrue(m.tick()["ok"])
        self.assertEqual(m.next_due_at(), _local(2026, 10, 20, 3, 30))

    def test_missed_while_asleep_runs_on_wake(self):
        clock = Clock(_local(2026, 10, 18, 12, 0))
        m = self.manager(clock=clock, at=(3, 30))
        m.last_success_at = _local(2026, 10, 18, 3, 31)
        clock.now = _local(2026, 10, 19, 8, 0)  # slept through 03:30
        self.assertLessEqual(m.next_due_at(), clock.now)
        self.assertTrue(m.tick()["ok"])

    def test_restart_with_old_backup_waits_a_few_minutes(self):
        clock = Clock(_local(2026, 10, 19, 8, 0))
        m = self.manager(clock=clock, at=(3, 30))
        m.last_success_at = _local(2026, 10, 17, 3, 31)
        self.assertEqual(m.next_due_at(), clock.now + db_backup.STARTUP_DELAY)

    def test_failure_retried_after_an_hour(self):
        clock = Clock(_local(2026, 10, 18, 12, 0))
        m = self.manager(clock=clock)
        os.rename(self.folder, self.folder + "-away")
        clock.now += db_backup.STARTUP_DELAY
        with self.assertLogs("kamxl_backup", "ERROR"):
            self.assertFalse(m.tick()["ok"])
        self.assertEqual(m.next_due_at(), clock.now + db_backup.RETRY_AFTER_FAILURE)
        self.assertIsNone(m.tick())

    def test_existing_backups_found_at_startup(self):
        first = self.manager().run()
        m = self.manager()
        self.assertEqual(m.last_file, first["file"])
        self.assertEqual(m.status()["count"], 1)


class DaemonBackupTests(BackupTestCase):

    def _daemon(self, backup=None, notifier=None):
        daemon = KAMDaemon(
            make_kam(ScriptedSerial({})), "/tmp/kamxl-backup-test.sock",
            station_callsign="N5BA", history_db=self.db, backup=backup,
            notifier=notifier,
        )
        self.addCleanup(daemon.shutdown)
        return daemon

    def test_off_by_default(self):
        daemon = self._daemon()
        self.assertEqual(daemon.dispatch("backup.status", {}), {"enabled": False})
        with self.assertRaises(ValueError):
            daemon.dispatch("backup.now", {})

    def test_backup_now_and_failure_notification(self):
        notifier = MagicMock()
        daemon = self._daemon(self.manager(), notifier)
        result = daemon.dispatch("backup.now", {})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"]["count"], 1)

        os.rename(self.folder, self.folder + "-away")
        with self.assertLogs("kamxl_backup", "ERROR"):
            self.assertFalse(daemon.dispatch("backup.now", {})["ok"])
        event, title = notifier.notify.call_args[0][:2]
        self.assertEqual((event, title), ("backup", "KAM-XL backup failed"))

        os.rename(self.folder + "-away", self.folder)
        daemon.dispatch("backup.now", {})
        self.assertEqual(notifier.notify.call_args[0][1],
                         "KAM-XL backup working again")


class CommandLineTests(unittest.TestCase):

    def _main(self, argv, env):
        with patch.dict(os.environ, env, clear=True), \
             patch.object(kamxl_daemon, "_load_dotenv"), \
             patch.object(kamxl_daemon, "KAMXL") as kamxl_cls, \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                kamxl_daemon.main(argv)
        return kamxl_cls

    def test_backup_needs_history_db(self):
        kamxl_cls = self._main(["--port", "/dev/null"], {
            "KAMXL_CALLSIGN": "N5BA", "KAMXL_BACKUP_DIR": "/mnt/kamxl-backup",
        })
        kamxl_cls.assert_not_called()

    def test_bad_backup_time_rejected_before_serial(self):
        kamxl_cls = self._main(["--port", "/dev/null"], {
            "KAMXL_CALLSIGN": "N5BA", "KAMXL_HISTORY_DB": "/tmp/x.sqlite3",
            "KAMXL_BACKUP_DIR": "/mnt/kamxl-backup", "KAMXL_BACKUP_TIME": "25:00",
        })
        kamxl_cls.assert_not_called()


class BackupRestTests(RestTestCase):

    def test_routes(self):
        _, port = self.start_stack(ScriptedSerial({}), history_db=True)
        status, payload = self.request(port, "GET", "/backup")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"], {"enabled": False})
        status, payload = self.request(port, "POST", "/backup/now", body={})
        self.assertNotEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
