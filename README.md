# kamxl

<p align="center">
  <img
    src="examples/certified-95.9-ai.png"
    alt="Certified 95.9% AI"
    width="200"
  />
</p>

A Python library for controlling the Kantronics KAM-XL packet TNC in
Terminal Mode -- a clean, Pythonic API instead of hand-typed terminal
commands.

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
git clone git@github.com:BoomerET/kamxl_winlink.git
cd kamxl_winlink
pip install -e .
```

Requires Python 3.8+ and [pyserial](https://pypi.org/project/pyserial/),
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

For the background daemon, set the following in `.env`:

```dotenv
KAMXL_PORT=/dev/kam-xl
KAMXL_CALLSIGN=N0CALL                # your KAM-XL's MYCALL, SSID included
KAMXL_HISTORY_DB=/var/lib/kamxl/reception.sqlite3   # optional: inbox + history
KAMXL_AUTO_ACK=1                     # optional: auto-ack received messages
KAMXL_APRS_PATH=WIDE1-1,WIDE2-1      # optional: digipeater path (DIRECT = none)
KAMXL_LATITUDE=33.2275               # optional: position beacon
KAMXL_LONGITUDE=-97.1273
KAMXL_BEACON_COMMENT=KAM-XL
KAMXL_BEACON=1                       # optional: beacon automatically
KAMXL_WATCHDOG_MINUTES=30            # optional: reception alert after N quiet min (0 = off)
```

The library accepts an explicit serial port on all platforms.
The daemon requires `--port`/`KAMXL_PORT` and `--callsign`/`KAMXL_CALLSIGN`
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

The KAM-XL daemon can optionally record received AX.25/APRS packets in a SQLite database. This provides reception statistics that survive daemon restarts and can be displayed in the web dashboard.

Persistence is disabled by default. Enable it using the `--history-db` command-line option or the `KAMXL_HISTORY_DB` environment variable:

```bash
export KAMXL_HISTORY_DB=/var/lib/kamxl/reception.sqlite3
```

For a systemd installation, create a service override:

```bash
sudo systemctl edit kamxl-daemon.service
```

Add:

```ini
[Service]
Environment=KAMXL_HISTORY_DB=/var/lib/kamxl/reception.sqlite3
```

Ensure that the service account can write to the database directory. Apply the configuration with:

```bash
sudo systemctl daemon-reload
sudo systemctl restart kamxl-daemon.service
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

Persistent reception history is separate from the live reception statistics and the in-memory APRS station database. It does not store a historical trail of station positions.

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
