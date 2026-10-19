# OpenTNC support

The [NA6D OpenTNC](https://na6d.com/products/na6d-opentnc) is an
RP2040 / Zephyr RTOS hardware TNC built on Dave Platt's (AE6EO)
open-source [OpenTNC](https://gitlab.com/davidplatt/opentnc) design.
It has its own AX.25 stack and modems (1200 AFSK, 2400 V.26, 9600
G3RUH, FX.25 forward error correction) and a TNC-2-style command line
at a `cmd:` prompt over USB serial. That last part is why it fits this
project: the same Terminal Mode code that drives the KAM-XL drives it,
with a small driver (`opentnc.py`) for the differences.

## Status

**Unverified.** `opentnc.py` was written before the hardware arrived,
from TNC-2 convention and NA6D's getting-started guide. Per this
project's design philosophy, the first job with a real unit is to
record what it actually prints and correct the driver from that.

| Feature | State |
| --- | --- |
| Typed get/set (MYCALL, MONITOR, FX.25, timing parameters) | Built, unverified |
| MONITOR decoding (map, stations, history, telemetry, log) | Built, unverified |
| Web terminal | Should just work (raw commands) |
| AX.25 connect / disconnect | Built, unverified |
| Winlink over packet (check / send mail) | Built, unverified |
| `SAVE!`, `CARRIER`, FX.25 helpers, `KISS ON` | Built, unverified |
| APRS transmit (messages, ACKs, resends, beacons) | **Disabled** -- fails closed until STATUS/UNPROTO output is checked |
| PBBS | Not applicable (the OpenTNC has no built-in mailbox) |

## KISS mode: APRS without the guesswork

Everything above about unverified display formats applies to Terminal
Mode. In KISS mode (`TNCSTATION_MODE=kiss`, see [kiss.md](kiss.md)) the
OpenTNC passes raw AX.25 frames, so monitoring *and APRS transmit*
work from day one -- nothing depends on how the firmware formats text.
What KISS can't do is connected mode (PBBS, Winlink, the web
terminal). If the OpenTNC's job is APRS, KISS is the easier start; the
capture below is still worth doing for Terminal Mode later.

## Day-one checklist

1. **Configure it** in a terminal (PuTTY, `screen /dev/ttyACM0`, ...),
   per NA6D's guide:

       MYCALL N0CALL-1
       CARRIER SQUELCH      (or AUDIO if your squelch is open)
       SAVE!

2. **Capture its output** -- read-only, nothing is changed or
   transmitted:

       python3 opentnc_capture.py /dev/ttyACM0 --listen 600

   With `MONITOR` on and the radio on 144.390, ten minutes usually
   catches plenty of APRS. Optionally add `--connect KD5EOC-10` (or
   another node or gateway) to record the connect/disconnect banners;
   that one *does* transmit and asks first.

3. **Compare the capture with the driver's assumptions** (all class
   attributes at the top of `opentnc.OpenTNC`):

   | What to look for in the capture | Driver attribute | Assumed |
   | --- | --- | --- |
   | A monitored packet's header line | `HEADER_RE` (packet.`TNC2_HEADER_RE`) | `SRC>DST,DIGI*:payload`, optional ` <UI>` tag before the colon |
   | Whether packets ever span lines / end with a blank line | `_strip_monitor_lines()` | one line per packet |
   | Reply to the unknown command `XYZZYQ` | `ERROR_MARKERS` | `EH?` or `?EH` |
   | Reply to a bare `MYCALL` | `get()` | `MYCALL N0CALL` (also `:`, `=`, `is`) |
   | Connect / disconnect banners (`--connect`) | `CONNECT_MARKERS`, `DISCONNECT_MARKERS` | `*** CONNECTED to X`, `*** DISCONNECTED` |
   | Whether a stray `cmd:` shows up between monitored packets | -- | not expected |
| How quickly a burst's last packet is followed by silence | `MONITOR_IDLE_FLUSH` | 0.5 s of quiet means the packet is complete |

   Fix what differs, put the real lines into `tests/test_opentnc.py`
   in place of the TNC-2 guesses, and run `python3 run_tests.py`.

4. **Run the daemon** against it:

       TNCSTATION_TNC=opentnc TNCSTATION_PORT=/dev/ttyACM0 TNCSTATION_CALLSIGN=N0CALL-1 \
           python3 tncstation_daemon.py -v

   `-v` logs every raw monitor read (`monitor raw: ...`), so a header
   the parser doesn't recognize is still visible. The dashboard's
   status now reports `tnc_model` and `aprs_tx_supported`.

5. **APRS transmit, later.** `aprs_tx.py` checks the TNC's `STATUS`,
   `PORT`, `PACLEN`, `CPACTIME`, `SENDPAC`, `COMMAND` and `UNPROTO`
   replies line by line before it ever keys the radio, and those were
   written against the KAM-XL's two-port output. The capture records
   all of them. Once there's an OpenTNC version of those checks,
   set `OpenTNC.APRS_TX_SUPPORTED = True`.

## Port numbering

The KAM-XL has two radio ports (1 = HF, 2 = VHF), and this application
treats port 2 as APRS everywhere: the daemon, reception history,
statistics and dashboards. The OpenTNC has a single radio port, and
its traffic is reported as port 2 (`packet.APRS_RADIO_PORT`), so all of
that works unchanged, and a history database recorded with one TNC
can be read with the other.

## Linux: a stable device name

The OpenTNC is a USB CDC-ACM device, so it shows up as `/dev/ttyACM*`
rather than `/dev/ttyUSB*`. Get its IDs with:

    udevadm info -q property -n /dev/ttyACM0 | grep -E 'ID_VENDOR_ID|ID_MODEL_ID|ID_SERIAL_SHORT'

and add a rule like the KAM-XL one in the README, e.g.
`/etc/udev/rules.d/99-opentnc.rules`:

    SUBSYSTEM=="tty", ATTRS{idVendor}=="XXXX", ATTRS{idProduct}=="YYYY", ATTRS{serial}=="ZZZZ", SYMLINK+="opentnc"

## Running both TNCs

Each daemon owns one serial port, so a KAM-XL and an OpenTNC side by
side means two daemons, each with its own socket, history database
and web server port -- e.g. two systemd units with different `.env`
files (`TNCSTATION_TNC`, `TNCSTATION_PORT`, `TNCSTATION_SOCKET`, `TNCSTATION_HISTORY_DB`).
That's also a good way to compare FX.25 on and off: the Coverage card
compares station setups within one database.

## Never sent by this library

`mode expert` / `plugh!` (reboots into the USB bootloader for firmware
updates) and `CALIBRATE` (keys the transmitter with test tones). Do
those by hand in a terminal.
