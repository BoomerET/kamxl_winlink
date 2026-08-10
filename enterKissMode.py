"""
One-off diagnostic/setup script: the inverse of exitKissMode.py --
puts the KAM-XL into KISS interface mode so a real KISS-speaking
client (e.g. Pat, https://github.com/la5nta/pat) can talk to it
directly, bypassing this project's own Terminal Mode protocol
entirely.

Per the KAM-XL manual ("KISS Mode", ch. "Interface Communication
Modes"): "To place the KAM XL in KISS Mode, at the command prompt
(cmd:), type INTFACE KISS and press return. Then, send a RESET
command, or cycle power (off/on) to the KAM."

Two real steps, deliberately handled two different ways:

  1. "INTFACE KISS" is sent through kamxl.py's normal KAMXL.send_command()
     -- the KAM-XL is still in ordinary Terminal Mode at this point, so
     this gets the usual EH?-detection/echo-handling safety net for
     free (fails loudly, with a clear error, if the command wasn't
     accepted -- e.g. a typo, or firmware that rejects it for some
     other reason -- rather than plowing ahead into RESET regardless).
  2. "RESET" is sent as a raw write directly to kam.serial, and the
     reply is read back raw rather than through send_command(). This
     mirrors exitKissMode.py's own reasoning for going around
     kamxl.py's normal protocol: once RESET applies the new INTFACE
     setting, the KAM-XL will very likely stop producing a "cmd:"
     prompt at all (it's now in KISS framing, not Terminal Mode's
     command loop) -- send_command()'s _read_until_prompt() would just
     time out waiting for a prompt that's never coming, and -- more
     importantly for a diagnostic script -- discards whatever partial
     text it *did* read on that timeout, which is exactly the
     sign-on-banner text this script wants to show you as visual
     confirmation that the switch actually happened.

IMPORTANT, same caveat exitKissMode.py's own follow-up text raises for
the opposite direction: INTFACE is a *saved* parameter, not just a
this-session setting. Per the manual, INTFACE "Sets the mode of
operation for the RS232 port upon power-up or after a reset" -- so
after this script runs, the KAM-XL will keep booting straight into
KISS mode on every future power-cycle too, not just for this one Pat
session, until INTFACE is explicitly set back. When you're done
testing with Pat: run exitKissMode.py to get a "cmd:" prompt back for
this session, then send INTFACE TERMINAL and RESET (or power-cycle
again) to make that change stick -- exactly the same follow-up
exitKissMode.py itself already prints.

If opening the port fails with "Inappropriate ioctl for device", an
AX.25 KISS daemon (e.g. kissattach, often run as a systemd service)
is likely already attached to it -- stop that first. See
serial_errors.py for the detail; this script surfaces that guidance
automatically rather than showing a raw traceback.

Usage:
    python3 enterKissMode.py /dev/ttyUSB1
"""

import sys
import time

from kamxl import KAMXL, KAMError
from serial_errors import describe_serial_open_failure

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
BAUDRATE = 19200


def main():
    print(f"Connecting to KAM-XL on {PORT} at {BAUDRATE} baud...")
    kam = KAMXL(PORT, baudrate=BAUDRATE)

    try:
        kam.connect()
    except Exception as exc:
        print(f"\nCould not open {PORT}:\n")
        print(describe_serial_open_failure(exc))
        sys.exit(1)

    try:
        print("Sending INTFACE KISS...")
        response = kam.send_command("INTFACE KISS")
        print("Response:", response or "(empty -- likely just echoed OK)")

        print(
            "\nSending RESET to apply it -- the KAM-XL will very likely "
            "stop responding to Terminal Mode commands immediately after "
            "this, since it should now be in KISS framing. That's the "
            "expected/successful outcome here, not a failure."
        )
        kam.serial.reset_input_buffer()
        kam.serial.write(b"RESET\r")
        kam.serial.flush()

        time.sleep(1.5)
        raw = kam.serial.read(kam.serial.in_waiting or 512)

        print(f"\nRaw bytes received after RESET ({len(raw)}):")
        print(repr(raw))

        print("\nDecoded (best effort):")
        print(raw.decode("ascii", errors="replace"))

        if b"cmd:" in raw.lower():
            print(
                "\nStill seeing a 'cmd:' prompt -- INTFACE KISS/RESET "
                "may not have taken effect. Try running this script "
                "again, or check INTFACE with 'GET INTFACE' style "
                "tooling first."
            )
        else:
            print(
                "\nNo 'cmd:' prompt came back -- looks like the switch "
                "to KISS mode worked. Point Pat (or another KISS client) "
                f"at {PORT} at {BAUDRATE} baud now.\n"
                "Remember: this is a saved setting, not just for this "
                "session -- see this script's own module docstring for "
                "how to switch back to TERMINAL mode when you're done."
            )

    except KAMError as exc:
        print(f"\nKAM-XL error: {exc}")
        sys.exit(1)

    finally:
        # Deliberately not kam.disconnect_station()/any Terminal Mode
        # cleanup here -- if the switch to KISS worked, there's no
        # "cmd:" prompt left to clean up with, and trying would just
        # time out. Just close the serial port itself.
        kam.disconnect()


if __name__ == "__main__":
    main()
