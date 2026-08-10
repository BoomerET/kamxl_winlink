"""
One-off diagnostic/recovery script: sends the standard KISS exit
sequence directly over the serial port, bypassing kamxl.py's normal
Terminal Mode protocol entirely (which won't get a response at all
if the KAM-XL is actually in KISS mode -- that's the point of this
script).

Per the KAM-XL manual ("Exiting KISS mode", ch. "Interface
Communication modes"): send FEND (0xC0), FF (0xFF), FEND (0xC0). The
TNC should reply with its normal Kantronics sign-on banner and drop
back into TERMINAL interface mode for this session.

Note this only fixes the *current* session. INTFACE is a saved
parameter that controls what mode the TNC boots into -- if it's set
to KISS, the unit will go straight back into KISS mode on its next
power-cycle/reset unless INTFACE is explicitly set back to TERMINAL
(see the follow-up instructions this script prints if it works).

If opening the port fails with "Inappropriate ioctl for device", an
AX.25 KISS daemon (e.g. kissattach, often run as a systemd service)
is likely already attached to it -- stop that first. See
serial_errors.py for the detail; this script surfaces that guidance
automatically rather than showing a raw traceback.

Usage:
    python3 exitKissMode.py /dev/ttyUSB1
"""

import sys
import time

import serial

from serial_errors import describe_serial_open_failure

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/kamxl"
BAUDRATE = 19200

KISS_EXIT = bytes([0xC0, 0xFF, 0xC0])


def main():
    print(f"Opening {PORT} at {BAUDRATE} baud...")

    try:
        ser = serial.Serial(
            port=PORT,
            baudrate=BAUDRATE,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=3,
        )
    except Exception as exc:
        print(f"\nCould not open {PORT}:\n")
        print(describe_serial_open_failure(exc))
        sys.exit(1)

    time.sleep(0.1)

    print("Sending KISS exit sequence (FEND, FF, FEND)...")
    ser.reset_input_buffer()
    ser.write(KISS_EXIT)
    ser.flush()

    time.sleep(0.5)
    response = ser.read(ser.in_waiting or 256)

    ser.close()

    print(f"\nRaw bytes received ({len(response)}):")
    print(repr(response))

    print("\nDecoded (best effort):")
    print(response.decode("ascii", errors="replace"))

    if b"cmd:" in response.lower() or b"kam" in response.lower():
        print(
            "\nLooks like it worked -- the TNC responded with what "
            "looks like a sign-on banner / command prompt.\n"
            "IMPORTANT: this only fixes the current session. To stop "
            "it from booting back into KISS mode next time, run "
            "learnKam.py or a quick script that does:\n"
            '    kam.send_command("INTFACE TERMINAL")\n'
            '    kam.send_command("RESET")\n'
            "(or power-cycle it once more after setting INTFACE, per "
            "the manual)."
        )
    else:
        print(
            "\nNo recognizable response. Either it's still stuck, or "
            "it wasn't actually in KISS mode to begin with -- worth "
            "trying a direct terminal (screen/minicom) session next "
            "to see raw behavior."
        )


if __name__ == "__main__":
    main()
