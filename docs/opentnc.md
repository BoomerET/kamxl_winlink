# OpenTNC support

The [NA6D OpenTNC](https://na6d.com/products/na6d-opentnc) is an
RP2040 / Zephyr RTOS hardware TNC built on Dave Platt's (AE6EO)
open-source [OpenTNC](https://gitlab.com/davidplatt/opentnc) design.
It has its own AX.25 stack and modems (1200 AFSK, 2400 V.26, 9600
G3RUH, FX.25 forward error correction), a USB serial port, and a
TNC-2-style command line at a `cmd:` prompt.

tncstation runs it in **KISS mode** for APRS -- monitoring, the map,
messages with ACKs, beacons -- which is verified over the air.
Terminal Mode works for settings and monitoring; see [Status](#status).

## Setting it up

This is the order that worked on a Raspberry Pi 3B+ with an Anytone
D878UV II Plus on 144.390.

### 1. Configure the OpenTNC

Open its USB serial port in a terminal (`screen /dev/ttyACM0`; quit
with Ctrl-A, K, y) and, per NA6D's guide:

    MYCALL N0CALL-11
    MONITOR ON
    CARRIER SQUELCH      (or AUDIO if you run the radio's squelch open)
    SAVE!

**`SAVE!` matters**: without it every setting is lost at the next power
cycle. Unplug and replug the OpenTNC, then type `MYCALL`, `MONITOR` and
`CARRIER` to check they stuck.

### 2. Set up the radio

The OpenTNC plugs into the radio's mic/speaker jack. On the radio:

- **Turn off its own APRS or TNC** (the TH-D75's `APRS12`, the D878's
  analog APRS). Two TNCs on one radio double up beacons and collide.
  On the D878 also set **Analog APRS PTT mode: Off** -- otherwise the
  radio adds its own APRS burst after every transmission.
- **Wide (25 kHz)** channel, **no CTCSS/DCS** on receive, squelch as low
  as it goes without opening on noise.
- **Off:** battery saver / power save (it clips the start of packets),
  busy-channel lockout, VOX, key beeps and voice prompts (they go out
  the jack too).
- On a dual-band radio, single-band display with 144.390 on the PTT
  band.
- **Low power** for the first tests (see [Pi power](#raspberry-pi-power)).

### 3. Levels

There's nothing in alsamixer -- the OpenTNC has its own modem.

Two LEDs worth knowing: the green one blinking once a second is the
firmware's heartbeat, and the white **CON** LED is lit while the
OpenTNC is in KISS mode (it's dark at the `cmd:` prompt). With the
daemon running, CON should be on.

**Receive** is the radio's volume. LED 1's once-a-second blink is the
idle heartbeat, not a level reading. The real test is a packet: with
`MONITOR ON`, every packet it decodes prints a line in the terminal,
such as

    0 (???): <[To APRS from N5BA via WIDE1-1 WIDE2-1: 03(UI) PID ^F0 =3314.34N/09650.70W-N5BA KAM-XL ^0D ]

If the PKT LED flashes but nothing prints, the packet failed its
checksum: change the volume and try again. A test transmitter a few
feet away at full power overloads the receiver, so use low power, or
another room. If no LED reacts at all, no audio is reaching the
OpenTNC: unplug the cable and check you can hear the radio, then check
the plug is fully seated.

**Transmit** is factory-set to 50 mV for HT/mic inputs. If it ever needs
adjusting: `TXLEVEL` / `TXATTEN` or trimmer RV202, against `CALIBRATE`
tones (which key the transmitter -- do that by hand, into a dummy load).

### 4. A stable device name (Linux)

The OpenTNC shows up as `/dev/ttyACM*`, and the number can change. Its
USB ID is `1209:ae6e`; create `/etc/udev/rules.d/99-opentnc.rules`:

    SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="ae6e", SYMLINK+="opentnc"

then `sudo udevadm control --reload-rules && sudo udevadm trigger`, or
replug it. `ls -l /dev/opentnc` should point at the `ttyACM` device.

### 5. Configure tncstation

Close the terminal first -- only one program can have the port open.
In `.env` (copy `.env.sample`):

    TNCSTATION_TNC=opentnc
    TNCSTATION_MODE=kiss
    TNCSTATION_PORT=/dev/opentnc
    TNCSTATION_CALLSIGN=N0CALL-11
    TNCSTATION_HISTORY_DB=/var/lib/tncstation/reception.sqlite3
    TNCSTATION_REST_API_KEY=pick-a-long-random-token
    # Automatic transmissions off until the first tests pass:
    TNCSTATION_BEACON=0
    TNCSTATION_AUTO_ACK=0
    TNCSTATION_AUTO_RETRY=0

Then install and start the services (README, "Running as a service"):

    sudo deploy/install.sh
    sudo systemctl enable --now tncstation-daemon tncstation-web
    journalctl -u tncstation-daemon -n 30

The log should show `OpenTNC: entered KISS mode (KISS NOW)` and
`tncstation daemon (OpenTNC, kiss mode)`. The daemon puts the OpenTNC
in KISS mode at startup with `KISS NOW`, checks that it really stopped
answering at `cmd:`, and takes it out again (C0 FF C0) when it stops,
so a terminal works whenever the daemon isn't running.

Don't put the OpenTNC in KISS yourself with `KISS ON`: on this firmware
that's a *saved* setting meaning "enter KISS at the next restart", not
"now" -- it leaves the TNC at `cmd:` (deaf to the daemon) until a power
cycle. Earlier versions of tncstation sent it; the daemon now clears
it (`KISS OFF`) the first time it finds it set.

### 6. First on-air tests

1. **Receive:** beacon from another station; it should appear on the
   dashboard's map and packet log.
2. **Transmit:** at low power, press **Beacon now**. Check another
   station (or aprs.fi) hears it, and on a Pi run
   `vcgencmd get_throttled` -- it should still say `0x0`.
3. **Messages:** send one each way. The OpenTNC station's sent message
   should turn **Acknowledged**.

Then set `TNCSTATION_BEACON`, `TNCSTATION_AUTO_ACK` and
`TNCSTATION_AUTO_RETRY` to `1` and `sudo systemctl restart
tncstation-daemon`. Raise the power a step at a time, checking
`get_throttled` after each.

## What happens if...

Tested on the station: it looks after itself.

- **The OpenTNC is unplugged** (or its USB cable is bumped): the daemon
  notices the port vanish, exits, and systemd restarts it every 5
  seconds until `/dev/opentnc` is back -- any USB port will do -- then
  it puts the OpenTNC back in KISS mode and carries on.
- **The Pi reboots or loses power:** both services start at boot, the
  map and inbox come back from the history database, and beacon /
  auto-ACK / auto-resend follow `.env` (anything switched on the
  dashboard alone resets to the `.env` setting at every restart).
- **The radio is switched off:** nothing breaks; the station just hears
  nothing. After `TNCSTATION_WATCHDOG_MINUTES` of silence the dashboard
  shows a reception warning, and phone notifications send one if the
  `watchdog` event is on.
- **The daemon restarts** (after a `git pull`, a crash, or a clean
  stop): it puts the OpenTNC back in KISS mode at startup and checks.
  If the OpenTNC won't go, the daemon stops with "OpenTNC didn't enter
  KISS mode" in its log (and systemd retries) instead of running deaf.
- **Its receiver locks up:** seen on firmware 4.04, about an hour into a
  KISS session -- no receive LEDs on signals the radio clearly hears,
  while the heartbeat, CON (KISS mode) and transmit carry on. A power
  cycle or a firmware `RESTART` cures it, so after
  `TNCSTATION_TNC_RESET_MINUTES` (default 10) with nothing heard, the
  daemon sends `RESTART`, waits for the OpenTNC to come back on USB, and
  puts it in KISS mode again -- at most every 30 minutes. The log says
  "reset the OpenTNC", and a phone notification goes out if the
  `watchdog` event is on. On a quiet channel that's just a harmless
  few-second reset now and then; set it to 0 to turn it off.
- **It hears nothing and the packet log shows only your own beacons:**
  the OpenTNC is at `cmd:` rather than in KISS -- our frames come back
  as the command line's echo. Unplug and replug it, and please report
  it with the daemon log.

## Raspberry Pi power

If the Pi reboots the moment the radio transmits, look at power before
anything else. `vcgencmd get_throttled` reporting `0x50000` (or
`dmesg` saying "Undervoltage detected") means the supply sagged. On the
test station the original supply already dipped during boot; a
stronger 5.1 V supply with a short, heavy cable fixed the transmit
reboots completely, without any ferrites. Don't power
the radio or a USB SSD from the Pi.

To see whether dips line up with transmissions, put the two logs side
by side -- both in clock time:

    journalctl -k -b | grep -i voltage          # undervoltage warnings
    journalctl -u tncstation-daemon -b | grep -E ':[0-9]{2} TX '   # every beacon, message, ack

If the supply is solid (`0x0` after boot) and the Pi still resets on
transmit, it's RF getting in over the cables: clip-on ferrites (mix 31)
on the radio cable and the USB cable near the OpenTNC, and the antenna
away from the Pi -- an HT's rubber duck next to it is the worst case.

## Status

Checked against a real NA6D OpenTNC (October 2026): its command
replies with `tools/opentnc_capture.py` (the transcript is
`tests/opentnc_capture_20261019.txt`, replayed byte for byte by the
tests), its monitor output on the air, and the firmware source.

| Feature | State |
| --- | --- |
| KISS mode: monitoring, map, APRS messages with ACKs, beacons | **Verified** over the air -- see [kiss.md](kiss.md) |
| Settings: get/set, typed values, `DISPLAY` | **Verified** -- replies are `is VALUE` / `KEY is VALUE` |
| Rejected commands detected | **Verified** -- `Unknown command: X` |
| `SAVE!`, `CARRIERDETECT`, FX.25 helpers | **Verified** commands; set_* round trips not yet run on the unit |
| Terminal Mode MONITOR decoding | **Verified** on the air and against the firmware source (`DumpPacket()` in `src/packetutil.c`) |
| Terminal Mode connect / disconnect banners | Unverified |
| Terminal Mode APRS transmit | Off: the safety checks need a `STATUS` command, which this firmware lacks -- use KISS mode |
| PBBS | Not applicable (no built-in mailbox) -- the web pages hide it |
| Winlink | Untried over its connected mode -- hidden for now |

In a monitor line the leading number is a packet counter and `(???)`
the AX.25 version (`2.0`, `2.2`, or unknown); a `*` on the destination
or source is the command/response bit, on a digipeater "has repeated".
Bytes that aren't printable ASCII appear as ` ^XX ` (a space each side;
the beacon's trailing CR is ` ^0D `), and the driver turns them back
into bytes.

Not on this firmware: `VERSION`, `STATUS`, `ECHO`, `MCOM`, `MRPT`,
`MSTAMP`, `HEADERLN`, `PPERSIST`, `APRSDIGI`, `BEACON`. Commands accept
any abbreviation down to the capitals `HELP` shows (`CARrierdetect` ->
`CAR`).

## Port numbering

The KAM-XL has two radio ports (1 = HF, 2 = VHF), and this application
treats port 2 as APRS everywhere: the daemon, reception history,
statistics and dashboards. The OpenTNC has a single radio port, and
its traffic is reported as port 2 (`packet.APRS_RADIO_PORT`), so all of
that works unchanged, and a history database recorded with one TNC
can be read with the other.

## Running both TNCs

Each daemon owns one serial port, so a KAM-XL and an OpenTNC side by
side means two daemons, each with its own socket, history database
and web server port -- e.g. two systemd units with different `.env`
files (`TNCSTATION_TNC`, `TNCSTATION_PORT`, `TNCSTATION_SOCKET`,
`TNCSTATION_HISTORY_DB`, `TNCSTATION_REST_PORT`). Or simply two
computers, as on the test station. That's also a good way to compare
FX.25 on and off: the Coverage card compares station setups within one
database.

## Never sent by this library

`mode expert` / `plugh!` (reboots into the USB bootloader for firmware
updates) and `CALIBRATE` (keys the transmitter with test tones). Do
those by hand in a terminal.
