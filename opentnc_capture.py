#!/usr/bin/env python3
"""
Record what a real OpenTNC actually prints, so opentnc.py can be
corrected from observed behavior rather than TNC-2 convention (see
README, "Design philosophy").

    python3 opentnc_capture.py /dev/ttyACM0
    python3 opentnc_capture.py COM9 --listen 600
    python3 opentnc_capture.py /dev/ttyACM0 --connect KD5EOC-10   # transmits

Writes two files next to where it's run:

    opentnc_capture_<time>.txt    human-readable transcript
    opentnc_capture_<time>.json   the same, with exact bytes (repr)

READ-ONLY BY DEFAULT. It only sends bare parameter names (which on a
TNC-2-style unit display a value without changing it), HELP, DISPLAY,
and one deliberately unknown command (to record the rejection text),
then listens to MONITOR traffic. It never sends SAVE!, KISS, CALIBRATE,
CONVERS, BEACON, or a parameter with a value, so it never changes a
setting or keys the radio.

The one exception is --connect CALL, which attempts a real AX.25
connection (and so transmits) to record the connect/disconnect banners.
It asks for confirmation first.

This talks to pyserial directly instead of through opentnc.OpenTNC on
purpose: the point is to see the raw bytes before any of the driver's
assumptions get a chance to interpret them.
"""

import argparse
import json
import sys
import time

from datetime import datetime

import serial


PROMPT = b"cmd:"

# Bare names only: on a TNC-2-style command line these display the
# current value. Nothing here takes an argument or performs an action.
QUERIES = [
    "",                 # plain CR: should produce a fresh prompt
    "HELP",
    "VERSION",
    "DISPLAY",
    "MYCALL",
    "MONITOR",
    "ECHO",
    "MCOM",
    "MRPT",
    "MSTAMP",
    "HEADERLN",
    "CARRIER",
    "DIGIPEAT",
    "APRSDIGI",
    "FX25CONNECTION",
    "FX25ENCODING",
    "UNPROTO",
    "PACLEN",
    "MAXFRAME",
    "FRACK",
    "RETRY",
    "RESPTIME",
    "CHECK",
    "TXDELAY",
    "SLOTTIME",
    "PERSIST",
    "PPERSIST",
    "CPACTIME",
    "SENDPAC",
    "COMMAND",
    "CR",
    "STATUS",
    "XYZZYQ",           # deliberately unknown: records the error text
]


def log(transcript, kind, data, note=""):
    entry = {
        "t": round(time.monotonic() - transcript["start"], 3),
        "kind": kind,
        "repr": repr(data),
        "text": data.decode("latin-1") if isinstance(data, bytes) else data,
        "note": note,
    }
    transcript["entries"].append(entry)

    shown = entry["text"].replace("\r", "\\r").replace("\n", "\\n\n    ")
    print(f"[{entry['t']:8.3f}] {kind:>6} {note}")
    if shown:
        print(f"    {shown}")


def read_until(ser, marker, timeout):
    data = bytearray()
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        chunk = ser.read(ser.in_waiting or 1)

        if chunk:
            data.extend(chunk)

            if marker and marker in data:
                # Let any trailing bytes land too.
                time.sleep(0.2)
                data.extend(ser.read(ser.in_waiting))
                break

    return bytes(data)


def query(ser, transcript, command, timeout):
    ser.reset_input_buffer()
    out = command.encode("ascii") + b"\r"
    ser.write(out)
    ser.flush()
    log(transcript, "sent", out, command or "(CR)")

    reply = read_until(ser, PROMPT, timeout)
    note = "" if PROMPT in reply else "NO PROMPT within timeout"
    log(transcript, "recv", reply, note)

    transcript["queries"][command or "(CR)"] = reply.decode("latin-1")


def listen(ser, transcript, seconds):
    print(f"\nListening for {seconds:g}s of MONITOR traffic (Ctrl-C to stop early)...\n")
    deadline = time.monotonic() + seconds

    try:
        while time.monotonic() < deadline:
            chunk = ser.read(ser.in_waiting or 1)

            if chunk:
                log(transcript, "mon", chunk)
    except KeyboardInterrupt:
        print("\n(stopped listening)")


def connect_test(ser, transcript, callsign, timeout):
    banners = (b"CONNECTED", b"DISCONNECTED", b"retry", b"busy", b"EH?", b"?EH")

    ser.reset_input_buffer()
    out = f"CONNECT {callsign}\r".encode("ascii")
    ser.write(out)
    ser.flush()
    log(transcript, "sent", out, "connect test (TRANSMITS)")

    data = bytearray()
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        chunk = ser.read(ser.in_waiting or 1)

        if chunk:
            data.extend(chunk)
            log(transcript, "recv", chunk)

            if any(b.lower() in data.lower() for b in banners):
                time.sleep(3)  # collect the rest (e.g. a gateway's greeting)
                rest = ser.read(ser.in_waiting)
                if rest:
                    log(transcript, "recv", rest)
                break

    # Back to Command mode, then drop the link.
    for out, label, wait in (
        (b"\x03", "Ctrl-C", 3),
        (b"\r", "CR", 2),
        (b"DISCONNECT\r", "DISCONNECT", 20),
    ):
        ser.write(out)
        ser.flush()
        log(transcript, "sent", out, label)
        marker = b"DISCONNECTED" if label == "DISCONNECT" else PROMPT
        log(transcript, "recv", read_until(ser, marker, wait))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Record raw OpenTNC command and MONITOR output (read-only by default)."
    )
    parser.add_argument("port", help="Serial port, e.g. /dev/ttyACM0 or COM9")
    parser.add_argument("--listen", type=float, default=300,
                        help="Seconds of MONITOR traffic to record (default 300; 0 skips)")
    parser.add_argument("--timeout", type=float, default=5,
                        help="Seconds to wait for each command's prompt (default 5)")
    parser.add_argument("--connect", metavar="CALL",
                        help="Also attempt a real AX.25 connect to CALL (TRANSMITS)")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the --connect confirmation prompt")
    args = parser.parse_args(argv)

    if args.connect and not args.yes:
        answer = input(
            f"--connect will key your radio and attempt an AX.25 connection "
            f"to {args.connect.upper()}. Radio connected to a suitable "
            f"antenna/frequency? Type 'yes' to continue: "
        )
        if answer.strip().lower() != "yes":
            print("Not connecting.")
            args.connect = None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    transcript = {
        "port": args.port,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "start": time.monotonic(),
        "queries": {},
        "entries": [],
    }

    try:
        ser = serial.Serial(args.port, baudrate=115200, timeout=0.2)
    except serial.SerialException as exc:
        sys.exit(f"Could not open {args.port}: {exc}")

    try:
        time.sleep(0.3)
        log(transcript, "recv", ser.read(ser.in_waiting), "pending at open")

        print("Querying (read-only)...\n")
        for command in QUERIES:
            query(ser, transcript, command, args.timeout)

        if args.listen > 0:
            listen(ser, transcript, args.listen)

        if args.connect:
            connect_test(ser, transcript, args.connect.upper(), 60)
    finally:
        ser.close()

    del transcript["start"]

    with open(f"opentnc_capture_{stamp}.json", "w") as f:
        json.dump(transcript, f, indent=2)

    with open(f"opentnc_capture_{stamp}.txt", "w") as f:
        for entry in transcript["entries"]:
            f.write(f"[{entry['t']:8.3f}] {entry['kind']} {entry['note']}\n")
            f.write(f"    {entry['repr']}\n")

    print(f"\nSaved opentnc_capture_{stamp}.txt and .json")


if __name__ == "__main__":
    main()
