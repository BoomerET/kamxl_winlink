# History database backups

With `TNCSTATION_BACKUP_DIR` set, the daemon backs up the history database
(`TNCSTATION_HISTORY_DB`: packet log, messages, coverage, setups) once a day
at `TNCSTATION_BACKUP_TIME` (default 03:30, local time). If the laptop was
off or asleep at that time, the backup runs as soon as the daemon is
running again. It keeps the newest `TNCSTATION_BACKUP_KEEP` backups
(default 7) and deletes older ones; it never touches any other file
in the folder.

Each backup is a consistent snapshot taken while the daemon keeps
running (SQLite's online backup API), checked for integrity, gzipped,
and written as `kamxl-history-YYYYMMDD-HHMMSS.sqlite3.gz`. It appears
under that name only once it's complete.

The **Backups** line on the dashboard's Notifications & Backups card
shows the last good backup, how many are kept, when the next one is
due, and has a **Back up now** button.
If a backup fails you get a phone notification (`backup` event, on by
default; if you set `TNCSTATION_NTFY_EVENTS` yourself, add `backup` to it),
and another when backups work again. Failed backups are retried every
hour.

## Backing up to a network share (SMB / Windows share)

The daemon writes to an ordinary folder; Linux mounts the share there.
The share's username and password go in a root-only credentials file
for the mount, not in `.env`, so the daemon never handles them. This
example uses `\\192.168.7.246\KAM-XL` (`smb://192.168.7.246/KAM-XL`).

1. Install the SMB client tools and create the mount point:

   ```
   sudo apt install cifs-utils
   sudo mkdir -p /mnt/kamxl-backup
   ```

2. Put the share's credentials in a file only root can read:

   ```
   sudo mkdir -p /etc/kamxl
   sudo install -m 600 -o root -g root /dev/null /etc/kamxl/smb-credentials
   sudoedit /etc/kamxl/smb-credentials
   ```

   with these lines (`domain` only if the server needs one):

   ```
   username=YOUR_SHARE_USER
   password=YOUR_SHARE_PASSWORD
   domain=WORKGROUP
   ```

3. Find the user the daemon runs as -- the `User=` line in
   `/etc/systemd/system/tncstation-daemon.service` -- and add this line to
   `/etc/fstab` (one line), with that user for `uid` and `gid`:

   ```
   //192.168.7.246/KAM-XL  /mnt/kamxl-backup  cifs  credentials=/etc/kamxl/smb-credentials,uid=dave,gid=dave,file_mode=0640,dir_mode=0750,iocharset=utf8,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=30  0  0
   ```

   `x-systemd.automount` mounts the share the first time the folder is
   used (and again after the network or server comes back), and
   `nofail` keeps the laptop booting normally if the server is off.

4. Load it and check:

   ```
   sudo systemctl daemon-reload
   ls /mnt/kamxl-backup
   findmnt /mnt/kamxl-backup
   ```

   `findmnt` should show `//192.168.7.246/KAM-XL ... cifs`. If the
   mount fails with `mount error(95)`, the server only speaks an old
   SMB version: add `vers=2.0` (or, for very old devices, `vers=1.0`)
   to the options. `mount error(13)` means the username or password
   was refused.

5. Point the daemon at it in `.env` and restart it:

   ```
   TNCSTATION_BACKUP_DIR=/mnt/kamxl-backup
   #TNCSTATION_BACKUP_KEEP=7
   #TNCSTATION_BACKUP_TIME=03:30
   ```

   The first backup runs about five minutes after startup; or press
   **Back up now** on the dashboard.

### "Is the network share mounted?"

If the share isn't mounted, `/mnt/kamxl-backup` is just an empty
folder on the laptop's own disk -- and a backup there would be lost
along with the disk. So a backup is refused when the backup folder is
on the same disk as the database. To back up to another folder on the
same disk on purpose (say, a second partition that the check doesn't
recognize, or for testing), set `TNCSTATION_BACKUP_ALLOW_SAME_DISK=1`.

## Restoring

1. Stop the daemon: `sudo systemctl stop tncstation-daemon`
2. Move the current database aside, with its `-wal` and `-shm` files
   if present:

   ```
   cd /var/lib/tncstation
   sudo mkdir -p broken && sudo mv reception.sqlite3* broken/
   ```

3. Unpack the backup in its place and give it back to the daemon's
   user:

   ```
   gunzip -c /mnt/kamxl-backup/kamxl-history-20261018-033000.sqlite3.gz \
     | sudo tee /var/lib/tncstation/reception.sqlite3 > /dev/null
   sudo chown dave:dave /var/lib/tncstation/reception.sqlite3
   ```

4. Start the daemon: `sudo systemctl start tncstation-daemon`

(Use your own `TNCSTATION_HISTORY_DB` path and the daemon's user.) To look
inside a backup without restoring it, unpack it anywhere and open it
with `sqlite3`.
