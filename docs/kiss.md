# KISS mode

In KISS mode the TNC stops formatting packets as text and just passes
raw AX.25 frames: every received packet arrives as its exact bytes, and
transmitting means handing the TNC a frame tncstation built. That
makes a KISS station independent of any one TNC's display format, and
it removes the Terminal Mode problem of monitored packets and command
replies sharing one serial stream (see [daemon.md](daemon.md), "Known
limitation").

## What works in KISS mode

| Feature | KISS mode |
| --- | --- |
| Monitoring: map, stations, history, telemetry, packet log, coverage | Yes |
| APRS messages, auto-ACK, resends, beacons | Yes -- sent as UI frames from your station callsign |
| Web terminal (TNC commands) | No |
| AX.25 connected mode, PBBS, Winlink | No |

Connected mode needs the TNC's own AX.25 link layer, which only
Terminal Mode exposes; in KISS those features answer with a clear
error, and the web pages hide their links. A natural split for a two-TNC station: one TNC in KISS as the
full-time APRS radio, the other in Terminal Mode for connected work
(two daemons -- see [opentnc.md](opentnc.md), "Running both TNCs").

## Setting it up

Two settings in `.env`: which TNC (`TNCSTATION_TNC`) and the mode
(`TNCSTATION_MODE`). `TNCSTATION_PORT` can be a serial port or
`tcp://host:port`.

**OpenTNC:**

    TNCSTATION_TNC=opentnc
    TNCSTATION_MODE=kiss
    TNCSTATION_PORT=/dev/opentnc

The daemon enters KISS with `KISS NOW` at startup (and checks it took),
and leaves it with C0 FF C0 at shutdown -- not `KISS ON`, which on the
OpenTNC is a saved "enter KISS at the next restart" setting (see
[opentnc.md](opentnc.md)). Set `MYCALL` and `SAVE!` on the OpenTNC once beforehand (see
[opentnc.md](opentnc.md), which walks through the whole setup) -- in KISS mode the frames carry
`TNCSTATION_CALLSIGN` as the source, but the OpenTNC's own digipeater
still uses MYCALL.

**Direwolf** (soundcard modem, or any KISS-over-TCP server):

    TNCSTATION_TNC=kiss
    TNCSTATION_PORT=tcp://localhost:8001
    TNCSTATION_TNC_LABEL=Direwolf

`kiss` means "already speaking KISS": nothing is sent to enter or
leave it. It also fits a hardware TNC you've put in KISS yourself
(`TNCSTATION_PORT=/dev/ttyUSB0`). The `kiss` driver can't tell what's
at the other end, so the web pages call it "KISS TNC" unless
`TNCSTATION_TNC_LABEL` names it; the name goes in each browser tab's
title ("tncstation dashboard · Direwolf").

**KAM-XL (unverified):**

    TNCSTATION_TNC=kamxl
    TNCSTATION_MODE=kiss

The daemon sends `INTFACE KISS` and `RESET` at startup. `INTFACE` is
saved in the KAM-XL, so at a clean shutdown the daemon sends the KISS
exit, `INTFACE TERMINAL` and `RESET` to put it back. If the daemon is
killed instead, the KAM-XL keeps booting into KISS: run
`tools/exitKissMode.py`, then `INTFACE TERMINAL` and `RESET` by hand.
**APRS transmit is off for the KAM-XL in KISS** until its port mapping
(KISS channel 0 = Port 1 HF, channel 1 = Port 2 VHF, from the manual)
has been confirmed on the air -- a wrong guess would key the HF port.
Its Terminal Mode driver transmits APRS normally, so there's no reason
to run the KAM-XL in KISS today except to test that.

## Ports

Packets are recorded with the radio port numbers the rest of the app
uses (port 2 = APRS):

| TNC | KISS channel -> port | APRS transmitted on |
| --- | --- | --- |
| OpenTNC, generic/Direwolf | 0 -> 2 | channel 0 |
| KAM-XL | 0 -> 1 (HF), 1 -> 2 (VHF) | channel 1 (transmit off, see above) |

Other channels are recorded as port 10 + channel, so they never mix
with APRS.

## Transmitting

A message or beacon goes out as one AX.25 UI frame: source
`TNCSTATION_CALLSIGN`, destination `TNCSTATION_TOCALL` (see below), path
`TNCSTATION_APRS_PATH`.
The same per-send confirmations and safety checks apply as in Terminal
Mode; the Terminal-only steps (Ctrl-C to command mode, STATUS, setting
and restoring UNPROTO, CONVERS) simply aren't needed. Payloads are
validated exactly as before -- length, line breaks, message format,
path -- before anything is written.

## The tocall (destination)

APRS software identifies itself in the AX.25 destination field with an
identifier from the [aprs-deviceid](https://github.com/aprsorg/aprs-deviceid)
registry; a bare `APRS` is obsolete (Direwolf points it out on every
packet). tncstation uses `TNCSTATION_TOCALL`, default `APZTNC` -- from
the `APZ` range the registry sets aside for software in development,
checked unallocated -- until it has its own identifier. The same
setting applies to Terminal Mode, where it becomes the `UNPROTO`
destination. Set `TNCSTATION_TOCALL=APRS` for the old behavior.

## Library use

    from tnc import create_tnc

    tnc = create_tnc("kiss", "tcp://localhost:8001", mycall="N5BA-10")
    tnc.connect()

    for packet in tnc.monitor():
        print(packet.source, packet.digipeaters, packet.payload)

    tnc.send_ui(b">Testing from tncstation", path=["WIDE1-1"])

The framing pieces are in `kiss.py` for other uses: `kiss_frame()` /
`KISSDeframer`, `encode_ui_frame()` / `decode_ax25()`.
