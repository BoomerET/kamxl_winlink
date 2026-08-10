# kamxl

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
