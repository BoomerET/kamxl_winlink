# tools

Scripts run by hand against real hardware -- setup, recovery, and the
live tests that verified the library over the air. None of them are
part of the daemon or the web server, and the offline test suite
(`python3 run_tests.py`) doesn't need any of them.

Run them from anywhere; they find the library in the folder above.
Each takes the serial port as its first argument (else
`TNCSTATION_PORT`, else `/dev/kamxl`):

    python3 tools/whoami.py /dev/kam-xl

Stop the tncstation daemon first -- only one program can have the port
open.

## Setup and recovery

| Script | TNC | What it does |
| --- | --- | --- |
| `whoami.py` | KAM-XL | Connects and prints MYCALL for both ports -- a quick "is it there?" |
| `learnKam.py` | KAM-XL | Prints VERSION, MYCALL, HBAUD, MONITOR, PORT, then toggles MONITOR and restores it (a typed read/write check) |
| `enterKissMode.py` | KAM-XL | Puts it in KISS mode for another KISS program (e.g. Pat). Saved in the KAM-XL -- see `exitKissMode.py` |
| `exitKissMode.py` | KAM-XL, OpenTNC | Sends the KISS exit (C0 FF C0) to get a TNC out of KISS mode, e.g. after the daemon was killed while running it in KISS. On the OpenTNC the `cmd:` prompt is back right away |
| `opentnc_capture.py` | OpenTNC | Records what an OpenTNC prints (HELP, DISPLAY, replies, monitored packets) to a transcript, read-only. `--listen 600` to also record ten minutes of traffic; `--connect CALL` records connect banners and **transmits** (asks first) |

## Live tests (KAM-XL, over the air)

These **transmit**: they connect to a station named at the top of the
file -- edit `TARGET` (and `VIA`) for your area first.

| Script | What it checks |
| --- | --- |
| `connectTest.py` | Connect to `TARGET`, print the banner and traffic, disconnect |
| `connectViaTest.py` | The same through a digipeater (`VIA`) |
| `connectFailTest.py` | Connecting to a station that isn't there fails cleanly |
| `chatTest.py` | An interactive connected-mode chat (Windows only: uses `msvcrt`) |
| `listenTest.py` | Raw monitor text with `listen()` -- receive only |
| `monitorMethodLiveTest.py` | Parsed packets with `monitor()` -- receive only |
