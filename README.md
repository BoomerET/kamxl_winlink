# tncstation

[![Tests](https://github.com/BoomerET/tncstation/actions/workflows/tests.yml/badge.svg)](https://github.com/BoomerET/tncstation/actions/workflows/tests.yml)

<p align="center">
  <img
    src="examples/certified-95.9-ai.png"
    alt="Certified 95.9% AI"
    width="200"
  />
</p>

A packet radio station for hardware TNCs -- the Kantronics KAM-XL and
the NA6D OpenTNC. Three layers, each usable on its own:

- **A Python library** that drives a TNC in Terminal Mode -- a clean,
  Pythonic API instead of hand-typed terminal commands (`tnc.py`, with
  one small driver per model).
- **A background daemon** (`tncstation_daemon.py`) that owns the serial
  port: continuous monitoring, an APRS station database, messaging
  with acks and retries, beacons, persistent reception history.
- **A web dashboard** (`tncstation_rest.py`): live map, packet log,
  telemetry graphs, coverage comparison, a web terminal, phone
  notifications.

Formerly `kamxl_winlink`; see [Upgrading from
kamxl_winlink](#upgrading-from-kamxl_winlink).

![The dashboard: TNC link, stations, APRS messaging and services](docs/images/dashboard.png)

![The live APRS map around Dallas-Fort Worth](docs/images/map.jpg)

```python
from kamxl import KAMXL

kam = KAMXL("COM8")
kam.connect()

kam.set_typed("MONITOR", (True, False))
kam.connect_station("KD5EOC-10", via="RSSTN")

for packet in kam.monitor():
    print(packet.source, "->", packet.destination, ":", packet.payload)
```

This is not a Winlink client -- it's meant to be the general-purpose
layer underneath one (or a BBS, a chat tool, an APRS decoder, ...).

## Installation

Not yet published to PyPI (see [PROJECT.md](PROJECT.md)). For now, install
from a local clone:

```
git clone git@github.com:BoomerET/tncstation.git
cd tncstation
pip install -e .
```

Requires Python 3.9+ (tested on 3.9, 3.13 -- e.g. a Raspberry Pi -- and 3.14) and [pyserial](https://pypi.org/project/pyserial/),
which `pip install -e .` pulls in automatically.

### Persistent Serial Device (Linux)

Linux may assign different device names to USB serial adapters
after reconnecting or rebooting. A udev rule provides a stable
device name such as `/dev/kam-xl`.

Identify your USB serial adapter:

```bash
ls -l /dev/serial/by-id/
udevadm info -q property -n /dev/ttyUSB0 | grep -E 'ID_VENDOR_ID|ID_MODEL_ID|ID_SERIAL_SHORT'
```

Create `/etc/udev/rules.d/99-kam-xl.rules`:

```udev
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", ATTRS{serial}=="A10LK78T", SYMLINK+="kam-xl"
```

Replace the identifiers with those reported for your own adapter.
The values above identify a specific FTDI FT232R USB serial adapter.

Reload udev and verify the device:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=tty
ls -l /dev/kam-xl
```

Use the stable device name in Python:

```python
from kamxl import KAMXL

kam = KAMXL("/dev/kam-xl")
try:
    kam.connect()
    print(kam.get("MYCALL"))
finally:
    kam.disconnect()
```

For the background daemon and the web dashboard, set the following in
`.env` (one file for both; see `.env.sample` for every setting):

```dotenv
TNCSTATION_PORT=/dev/kam-xl
TNCSTATION_CALLSIGN=N0CALL                # your KAM-XL's MYCALL, SSID included
TNCSTATION_HISTORY_DB=/var/lib/tncstation/reception.sqlite3   # optional: inbox + history
TNCSTATION_AUTO_ACK=1                     # optional: auto-ack received messages
TNCSTATION_APRS_PATH=WIDE1-1,WIDE2-1      # optional: digipeater path (DIRECT = none)
TNCSTATION_LATITUDE=33.2275               # optional: position beacon
TNCSTATION_LONGITUDE=-97.1273
TNCSTATION_BEACON_COMMENT=KAM-XL
TNCSTATION_BEACON=1                       # optional: beacon automatically
TNCSTATION_WATCHDOG_MINUTES=30            # optional: reception alert after N quiet min (0 = off)
TNCSTATION_AUTO_RETRY=1                   # optional: resend unacknowledged messages
TNCSTATION_REST_API_KEY=change-me         # web dashboard / API token
TNCSTATION_VERBOSE=0                     # optional: 1 = detailed (DEBUG) logging, same as --verbose
TNCSTATION_NTFY_URL=https://ntfy.sh       # optional: phone notifications
TNCSTATION_NTFY_TOPIC=your-topic
```

The library accepts an explicit serial port on all platforms.
The daemon requires `--port`/`TNCSTATION_PORT` and `--callsign`/`TNCSTATION_CALLSIGN`
rather than silently guessing either.

## Status

Actively developed and tested against real hardware (Kantronics
KAM-XL, firmware 1.24160, over live 1200-baud packet). Not yet on
PyPI.

Implemented so far:

- Serial connection management, command/response handling, automatic
  `EH?` error detection and `ECHO ON` handling
- Typed getters/setters for KAM-XL parameters (booleans, integers,
  strings, multi-port and restricted-choice values)
- AX.25 connected mode: `connect_station()` (direct and VIA
  digipeat), `send_connected()`, `read_connected()`,
  `disconnect_station()`
- Passive monitoring: `listen()` for raw text, `monitor()` for
  structured `Packet` objects, callback- or generator-style

See [PROJECT.md](PROJECT.md) for the full design philosophy, roadmap,
and what's coming next (async support, APRS/Winlink/BBS helpers, and
more).

## Upgrading from kamxl_winlink

The project was renamed when it grew past the KAM-XL and Winlink.
Existing setups keep working without changes:

- **Settings:** `KAMXL_*` names in `.env` (or the environment) are
  still read. The new names are `TNCSTATION_*` -- same suffixes, e.g.
  `KAMXL_PORT` -> `TNCSTATION_PORT`. If both are set, the new one
  wins. The daemon logs which old names it found, at startup.
- **Programs:** `kamxl_daemon.py` and `kamxl_rest.py` still run (they're
  now aliases), so existing systemd units don't need editing. The new
  names are `tncstation_daemon.py` and `tncstation_rest.py`.
- **Library:** `from kamxl import KAMXL` and the `KAMError` family are
  unchanged -- `kamxl.py` is still the KAM-XL driver.
- **Git:** GitHub redirects the old URL. To update a clone:
  `git remote set-url origin https://github.com/BoomerET/tncstation.git`
- **Socket:** the default socket is now `/tmp/tncstation.sock`
  (was `/tmp/kamxl.sock`). The daemon and web server share the
  default, so restart both after pulling -- or set `TNCSTATION_SOCKET`.
- **Winlink:** unchanged. The client still identifies itself as
  `kamxl_winlink`, the name registered with Winlink -- renaming the
  repo doesn't change that.

When convenient: rename `KAMXL_` to `TNCSTATION_` in `.env`, and point
the systemd units at the new script names (e.g. rename
`kamxl-daemon.service` to `tncstation-daemon.service`). Docs use the
new names throughout.

## Supported TNCs

The library is split into a generic driver layer (`tnc.py`'s
`TerminalTNC`: serial I/O, commands, typed parameters, connected
mode, monitoring, Winlink) and one small subclass per TNC model. The
daemon, REST API and web pages work with any of them.

| Model | Module | Status |
| --- | --- | --- |
| Kantronics KAM-XL | `kamxl.KAMXL` | Verified over the air; everything works |
| NA6D / AE6EO OpenTNC | `opentnc.OpenTNC` | Unverified -- receive, map, terminal, connected mode and Winlink built from TNC-2 convention; APRS transmit disabled until checked against real output |

Pick one with `create_tnc()` in code, or `TNCSTATION_TNC` / `--tnc` for the
daemon:

```python
from tnc import create_tnc

tnc = create_tnc("opentnc", "/dev/ttyACM0")
tnc.connect()
print(tnc.get_typed("MYCALL"), tnc.get_fx25())
for packet in tnc.monitor():
    print(packet.source, "->", packet.destination, ":", packet.payload)
```

Any of them can also run in **KISS mode** (`TNCSTATION_MODE=kiss`),
and Direwolf works as a TNC over KISS TCP (`TNCSTATION_TNC=kiss`,
`TNCSTATION_PORT=tcp://localhost:8001`): monitoring and APRS with raw
AX.25 frames, no display-format parsing. See [docs/kiss.md](docs/kiss.md).

`from kamxl import KAMXL` and the `KAMError` exception family work
exactly as before. See [docs/opentnc.md](docs/opentnc.md) for the
OpenTNC: setup, what's verified, and how to capture real output with
`opentnc_capture.py`.

## Themes

Every web page has a theme picker beside its title:

- **Terminal** -- green on black, the original look (default)
- **Light** -- dark on light, for daylight and phones outdoors
- **Night (red)** -- dim red on black, including the map, to keep your
  night vision at the operating position
- **Auto** -- Light or Terminal, following the device's setting

The choice is remembered per browser, so a phone and a laptop can
differ. All colors live in `tncstation_web/_shared/theme.css` as named
tokens, one block per theme; page stylesheets only refer to tokens.

## Documentation

- [docs/quickstart.md](docs/quickstart.md) -- connect, read/write
  parameters, connected mode, monitoring
- [docs/api_reference.md](docs/api_reference.md) -- full method/class
  reference
- [docs/troubleshooting.md](docs/troubleshooting.md) -- real hardware
  quirks and how this library works around them
- [docs/daemon.md](docs/daemon.md) -- the background daemon: protocol,
  methods, concurrency model
- [docs/rest_api.md](docs/rest_api.md) -- the REST API: endpoints,
  auth, live monitoring over Server-Sent Events, the dashboard it
  serves at `GET /`, the self-contained web terminal at
  `GET /terminal`, the read-only PBBS browser at `GET /pbbs`, the live
  APRS station map at `GET /map`, the Winlink mail check/send pages at
  `GET /winlink`, and the Winlink web-service API endpoints (account
  lookup, gateway listings -- needs `$WINLINK_API_KEY`, see
  [docs/daemon.md](docs/daemon.md#winlink-api-key))
- [examples/](examples/) -- runnable scripts, including two that need
  no hardware at all

## Persistent APRS Reception History

The daemon can optionally record received AX.25/APRS packets in a SQLite database. This provides reception statistics that survive daemon restarts and can be displayed in the web dashboard.

Persistence is disabled by default. Enable it using the `--history-db` command-line option or the `TNCSTATION_HISTORY_DB` environment variable:

```bash
export TNCSTATION_HISTORY_DB=/var/lib/tncstation/reception.sqlite3
```

For a systemd installation, create a service override:

```bash
sudo systemctl edit tncstation-daemon.service
```

Add:

```ini
[Service]
Environment=TNCSTATION_HISTORY_DB=/var/lib/tncstation/reception.sqlite3
```

Ensure that the service account can write to the database directory. Apply the configuration with:

```bash
sudo systemctl daemon-reload
sudo systemctl restart tncstation-daemon.service
```

The SQLite database uses write-ahead logging (WAL). Packet recording is handled by a background writer so database operations do not block the serial reception path.

### Reception history API

The authenticated REST API provides:

```http
GET /aprs/history
GET /aprs/history?since=UNIX_TIMESTAMP
```

The optional `since` parameter is a Unix timestamp in seconds. The response includes the persistent packet total, hourly reception counts, and whether history recording is enabled.

### Dashboard history charts

The web dashboard provides three reception-history display periods:

- **Last 24 hours:** Hourly reception counts, including the current partial hour.
- **Last 7 days:** Daily reception counts.
- **Last 30 days:** Daily reception counts.

The 7-day and 30-day views aggregate hourly records into calendar days using the browser's local timezone. Day boundaries account for daylight-saving transitions, including 23-hour and 25-hour days.

**Backups:** set `TNCSTATION_BACKUP_DIR` and the daemon backs the database up every night, for example to a network share, keeping the last week. See [docs/backup.md](docs/backup.md) for setting up an SMB/Windows share and for restoring.

On the **map**, the Tracks picker shows where stations went over the last 6 hours to 7 days (from the history database), not just the live last hour.

The **telemetry** page graphs what digipeaters and weather stations report in APRS telemetry -- battery voltage, temperatures, on/off bits -- named and scaled with each station's own PARM/UNIT/EQNS definitions.

The **packet log** page (`/log`, "log" in every page's header) searches everything stored: by sender, digipeater, text and period -- e.g. "when did I last hear K5XYZ, and through which digipeater?" Packet contents are stored from the version that added map restore onward; older rows show who and when only.

The history database also powers the dashboard's **Coverage** card: set your current station setup (e.g. "Mag-mount, garage" or "J-pole 16 ft") and it compares setups side by side -- how often your beacons and messages were digipeated or reached APRS-IS, which digipeaters heard you, and how many stations you heard. See [docs/rest_api.md](docs/rest_api.md#coverage-by-station-setup).

Persistent reception history is separate from the live reception statistics. It also stores each packet's payload, so when the daemon restarts it replays the last hour into the APRS station map: stations, trails, weather and objects reappear immediately instead of the map starting empty. Without `TNCSTATION_HISTORY_DB` the map starts empty after a restart.

## Testing

```
python3 run_tests.py
```

Runs the offline unit test suite (standard-library `unittest`, no
third-party packages required) against a scripted fake serial
connection -- no KAM-XL hardware needed. Files ending in `Test.py`
at the repo root (`listenTest.py`, `connectTest.py`, `chatTest.py`,
etc.) are the live hardware test scripts actually used to validate
this library against a real KAM-XL over the air.

## Design philosophy

Never write code because the manual says the KAM-XL behaves a
certain way -- write code based on observed behavior from real
hardware whenever the two differ. Several bugs were only ever found
by testing against a live KAM-XL over the air, not by reading the
manual: a stale command prompt leaking into connect/disconnect
banners, a VIA digipeat path getting truncated because a marker
matched before the rest of the line arrived, and a race between an
async link-failure notice and an unrelated command's response.

## License

MIT -- see [LICENSE](LICENSE).
