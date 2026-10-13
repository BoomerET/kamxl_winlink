# Quickstart

This walks through the common cases: opening a connection, reading and
writing parameters, an AX.25 connected-mode session, and passive
monitoring. All of it assumes a Kantronics KAM-XL wired to a serial
port (USB-serial adapters work fine -- `COM8` on Windows, something
like `/dev/ttyUSB0` on Linux).

## Install

```
pip install -e .
```

(from a clone of the repo -- see [README.md](../README.md#installation)).

## Connect

```python
from kamxl import KAMXL

kam = KAMXL("COM8")   # or "/dev/ttyUSB0", etc.
kam.connect()
```

`KAMXL(...)` doesn't open the port by itself -- nothing happens on the
wire until `connect()`. Always `disconnect()` when done, ideally in a
`finally:` block:

```python
kam = KAMXL("COM8")

try:
    kam.connect()
    ...
finally:
    kam.disconnect()
```

## Reading and writing parameters

Two ways to work with KAM-XL parameters: `get`/`set` for raw
string values, or `get_typed`/`set_typed` for a Pythonic type based on
the parameter's entry in `COMMANDS` (see
[api_reference.md](api_reference.md#commands)).

```python
kam.get("MYCALL")              # raw string, e.g. "N5BA-10/N5BA-10"
kam.get_typed("MYCALL")        # ("N5BA-10", "N5BA-10")

kam.get_typed("HBAUD")         # (0, 1200) -- ints, one per port
kam.get_typed("MONITOR")       # (True, False) -- bools, one per port
kam.get_typed("DIGIPEAT")      # "ON" -- single value, not per-port

kam.set_typed("MONITOR", (True, False))   # port 1 on, port 2 off
kam.set_typed("DIGIPEAT", "UIONLY")
```

`set_typed` validates before writing anything to the KAM-XL --
wrong type, wrong tuple length, or a value outside a restricted
choice's allowed set all raise before the serial port is touched.

To change just one port's setting without altering the other, use the
multi-port helpers directly instead of `set_typed` (which always
writes both ports):

```python
kam.set_multiport_bool("MONITOR", 2, True)     # port 2 only
kam.set_multiport_choice("FULLDUP", 1, "LOOPBACK", choices=("ON", "OFF", "LOOPBACK"))
```

For a full snapshot of every one-line `DISPLAY` setting:

```python
config = kam.get_configuration()   # dict, e.g. {"MYCALL": "N5BA-10/N5BA-10", ...}
```

## AX.25 connected mode

```python
kam.connect_station("KD5EOC-10")
# or, via a digipeater:
kam.connect_station("KD5EOC-10", via="RSSTN")
# or several:
kam.connect_station("TARGET", via=["DIGI1", "DIGI2"])

kam.send_connected("hello!")
print(kam.read_connected(timeout=5))

kam.disconnect_station()
```

`connect_station()` raises `KAMConnectionError` on a hard failure
(retry count exceeded, busy, disconnected) and `KAMTimeoutError` if
nothing at all comes back in time. A successful connect returns the
KAM-XL's banner text and leaves it in Convers mode, ready for
`send_connected()`/`read_connected()`.

## Monitoring

Two options depending on whether raw text or structured data is more
useful.

Raw text, fixed time window, via `listen()`:

```python
kam.set_typed("MONITOR", (True, True))
text = kam.listen(seconds=60)
print(text)
```

Structured `Packet` objects, running until you stop it, via
`monitor()`:

```python
for packet in kam.monitor():
    print(packet.source, "->", packet.destination, ":", packet.payload)
```

or with a callback (blocks for `seconds`, or forever if omitted):

```python
def on_packet(packet):
    print(packet.source, "->", packet.destination)

kam.monitor(seconds=300, callback=on_packet)
```

See [api_reference.md](api_reference.md#packet) for what's on a
`Packet`.

## Next steps

- [api_reference.md](api_reference.md) -- full method/class reference
- [troubleshooting.md](troubleshooting.md) -- real hardware quirks and
  how this library works around them
- [`examples/`](../examples/) -- runnable scripts, including one that
  needs no hardware at all
