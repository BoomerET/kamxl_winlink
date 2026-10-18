"""
Automatic backups of the history database.

Once a day (at KAMXL_BACKUP_TIME, local time) -- or as soon as the
daemon is running again after a missed day -- the database is copied
with SQLite's online backup API, so the daemon keeps receiving and
writing while it runs. The copy is checked (PRAGMA quick_check),
gzip-compressed into the backup folder under a temporary name, then
renamed into place, so a half-written file never looks like a backup.
Only the newest ``keep`` backups are kept.

The backup folder is normally a network share mounted by the operating
system (e.g. CIFS/SMB via /etc/fstab). If that share isn't mounted,
the folder is just an empty directory on the local disk -- so by
default a backup is refused when the folder is on the same filesystem
as the database ("is the share mounted?"). Set allow_same_disk to
back up to another folder on the same disk on purpose.

No network code here and no third-party packages: the OS handles the
share and its credentials.
"""

import gzip
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("kamxl_backup")
logger.addHandler(logging.NullHandler())

FILE_PREFIX = "kamxl-history-"
FILE_SUFFIX = ".sqlite3.gz"
_FILE_RE = re.compile(
    r"^" + re.escape(FILE_PREFIX) + r"(\d{8}-\d{6})" + re.escape(FILE_SUFFIX) + r"$"
)
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# After a failed attempt, try again this much later.
RETRY_AFTER_FAILURE = 3600
# If the newest backup is older than this, back up soon after startup
# rather than waiting for the daily time (laptop was off/asleep).
OVERDUE_AFTER = 26 * 3600
STARTUP_DELAY = 300


class BackupError(Exception):
    pass


def parse_backup_time(text: Optional[str]) -> tuple:
    """"HH:MM" (24 h, local time) -> (hour, minute). Default 03:30."""
    if text is None or not str(text).strip():
        return (3, 30)
    match = _TIME_RE.match(str(text).strip())
    if not match:
        raise ValueError("Backup time must be HH:MM (24-hour), e.g. 03:30")
    return (int(match.group(1)), int(match.group(2)))


class BackupManager:

    def __init__(
        self,
        database_path: str,
        backup_dir: str,
        keep: int = 7,
        at: tuple = (3, 30),
        allow_same_disk: bool = False,
        clock: Callable[[], float] = time.time,
        on_failure: Optional[Callable[[str], None]] = None,
        on_recovery: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        if not os.path.isabs(backup_dir):
            raise ValueError("Backup folder must be an absolute path")
        if not 1 <= int(keep) <= 365:
            raise ValueError("Backups to keep must be from 1 to 365")
        self.database_path = os.path.abspath(database_path)
        self.backup_dir = backup_dir
        self.keep = int(keep)
        self.at = at
        self.allow_same_disk = bool(allow_same_disk)
        self._clock = clock
        self._on_failure = on_failure
        self._on_recovery = on_recovery

        self._lock = threading.Lock()      # one backup at a time
        self._state_lock = threading.Lock()
        self._running = False
        self._started_at = clock()
        self.last_attempt_at: Optional[float] = None
        self.last_success_at: Optional[float] = None
        self.last_file: Optional[str] = None
        self.last_size: Optional[int] = None
        self.last_error: Optional[str] = None
        self.last_duration: Optional[float] = None
        self._failing = False

        # Pick up where we left off: the newest backup already there.
        try:
            existing = self.list_backups()
        except OSError:
            existing = []
        if existing:
            newest = existing[0]
            self.last_success_at = newest["created_at"]
            self.last_file = newest["name"]
            self.last_size = newest["size"]

    # -- scheduling -----------------------------------------------------

    def _daily_time_on_or_before(self, now: float) -> float:
        """Today's backup time if it has passed, else yesterday's."""
        local = time.localtime(now)
        target = time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                              self.at[0], self.at[1], 0, 0, 0, -1))
        if target > now:
            local = time.localtime(now - 86400)
            target = time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                                  self.at[0], self.at[1], 0, 0, 0, -1))
        return target

    def next_due_at(self, now: Optional[float] = None) -> float:
        now = self._clock() if now is None else now
        with self._state_lock:
            last_ok, last_try = self.last_success_at, self.last_attempt_at
            failing = self._failing

        if failing and last_try is not None:
            return last_try + RETRY_AFTER_FAILURE

        if last_ok is None or now - last_ok > OVERDUE_AFTER:
            return max(now, self._started_at + STARTUP_DELAY) \
                if last_try is None else now

        latest_slot = self._daily_time_on_or_before(now)
        if last_ok < latest_slot:
            return latest_slot  # missed or due now
        # Done for this slot: next one is tomorrow's.
        return self._daily_time_on_or_before(now + 86400)

    def tick(self, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Run a backup if one is due. Returns its result, else None."""
        now = self._clock() if now is None else now
        if now < self.next_due_at(now):
            return None
        return self.run()

    # -- the backup itself ---------------------------------------------

    def _check_destination(self) -> None:
        if not os.path.isdir(self.backup_dir):
            raise BackupError(
                f"Backup folder {self.backup_dir} doesn't exist or isn't "
                f"reachable (is the network share mounted?)"
            )
        if not os.access(self.backup_dir, os.W_OK):
            raise BackupError(f"Can't write to backup folder {self.backup_dir}")
        if not self.allow_same_disk:
            db_dev = os.stat(os.path.dirname(self.database_path)).st_dev
            if os.stat(self.backup_dir).st_dev == db_dev:
                raise BackupError(
                    f"Backup folder {self.backup_dir} is on the same disk as "
                    f"the database -- is the network share mounted? "
                    f"(KAMXL_BACKUP_ALLOW_SAME_DISK=1 to allow this)"
                )

    def run(self) -> Dict[str, Any]:
        """Make one backup now. Never raises; returns the outcome."""
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "A backup is already running"}
        started = self._clock()
        with self._state_lock:
            self._running = True
            self.last_attempt_at = started
        tmp_path = partial = None
        try:
            self._check_destination()
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(started))
            name = f"{FILE_PREFIX}{stamp}{FILE_SUFFIX}"
            final = os.path.join(self.backup_dir, name)
            partial = final + ".partial"

            # 1. Consistent snapshot next to the database (fast, local).
            tmp_path = os.path.join(
                os.path.dirname(self.database_path),
                f".kamxl-backup-{stamp}.sqlite3",
            )
            source = sqlite3.connect(self.database_path, timeout=30)
            try:
                target = sqlite3.connect(tmp_path)
                try:
                    source.backup(target)
                    check = target.execute("PRAGMA quick_check").fetchone()
                    if not check or check[0] != "ok":
                        raise BackupError(
                            f"Backup copy failed its integrity check: {check}"
                        )
                    # A plain rollback-journal file restores more simply.
                    target.execute("PRAGMA journal_mode = DELETE")
                finally:
                    target.close()
            finally:
                source.close()

            # 2. Compress onto the share under a temporary name, then
            #    rename, so a partial file is never taken for a backup.
            with open(tmp_path, "rb") as raw, open(partial, "wb") as out:
                with gzip.GzipFile(filename=name[:-3], mode="wb",
                                   fileobj=out, compresslevel=6) as gz:
                    shutil.copyfileobj(raw, gz, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            os.replace(partial, final)
            size = os.path.getsize(final)

            removed = self._prune()
            finished = self._clock()
            with self._state_lock:
                recovered = self._failing
                self._failing = False
                self.last_success_at = finished
                self.last_file = name
                self.last_size = size
                self.last_error = None
                self.last_duration = finished - started
            logger.info(
                "History backed up to %s (%d bytes, %.1f s; %d old removed)",
                final, size, finished - started, removed,
            )
            result = {"ok": True, "file": name, "size": size,
                      "removed": removed, "seconds": round(finished - started, 1)}
            if recovered and self._on_recovery is not None:
                self._on_recovery(result)
            return result
        except Exception as exc:
            message = str(exc) if isinstance(exc, (BackupError, OSError)) \
                else f"{type(exc).__name__}: {exc}"
            with self._state_lock:
                first_failure = not self._failing
                self._failing = True
                self.last_error = message
            logger.error("History backup failed: %s", message)
            if first_failure and self._on_failure is not None:
                self._on_failure(message)
            try:
                if partial and os.path.exists(partial):
                    os.remove(partial)
            except OSError:
                pass
            return {"ok": False, "error": message}
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    logger.warning("Could not remove %s", tmp_path)
            with self._state_lock:
                self._running = False
            self._lock.release()

    def list_backups(self) -> List[Dict[str, Any]]:
        """Our backup files in the folder, newest first."""
        found = []
        for entry in os.scandir(self.backup_dir):
            match = _FILE_RE.match(entry.name)
            if not match or not entry.is_file():
                continue
            created = time.mktime(time.strptime(match.group(1), "%Y%m%d-%H%M%S"))
            found.append({"name": entry.name, "size": entry.stat().st_size,
                          "created_at": created})
        found.sort(key=lambda b: b["name"], reverse=True)
        return found

    def _prune(self) -> int:
        """Delete our backups beyond the newest ``keep``. Only ever
        touches files named like ours."""
        removed = 0
        for old in self.list_backups()[self.keep:]:
            try:
                os.remove(os.path.join(self.backup_dir, old["name"]))
                removed += 1
            except OSError as exc:
                logger.warning("Could not remove old backup %s: %s",
                               old["name"], exc)
        return removed

    def status(self) -> Dict[str, Any]:
        try:
            backups = self.list_backups()
            reachable = True
        except OSError:
            backups, reachable = [], False
        next_due = self.next_due_at()
        with self._state_lock:
            return {
                "enabled": True,
                "folder": self.backup_dir,
                "folder_reachable": reachable,
                "keep": self.keep,
                "time": "%02d:%02d" % self.at,
                "running": self._running,
                "last_attempt_at": self.last_attempt_at,
                "last_success_at": self.last_success_at,
                "last_file": self.last_file,
                "last_size": self.last_size,
                "last_duration": self.last_duration,
                "last_error": self.last_error if self._failing else None,
                "failing": self._failing,
                "next_due_at": next_due,
                "count": len(backups),
                "total_size": sum(b["size"] for b in backups),
            }
