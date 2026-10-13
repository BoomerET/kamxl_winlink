import sys
import time

import msvcrt

from kamxl import (
    KAMXL,
    KAMError,
    KAMTimeoutError,
    KAMConnectionError,
)


# DigiPi's AX.25 Node service defaults to YOURCALL-4 unless it's been
# renamed to a tactical callsign (e.g. "COOL"). If this doesn't
# connect, try running listenTest.py for a bit to catch its actual
# node beacon and confirm the real callsign/SSID.
TARGET = "N5BA-4"

QUIT_COMMAND = "/quit"


def read_line_nonblocking(buffer):
    """
    Poll the keyboard for a single keystroke (Windows-only, via
    msvcrt) and fold it into buffer.

    Returns:
        (buffer, line_ready)

        line_ready is the completed line (without the newline) once
        Enter is pressed, otherwise None.
    """
    if not msvcrt.kbhit():
        return buffer, None

    char = msvcrt.getwch()

    if char in ("\r", "\n"):
        print()
        return "", buffer

    if char == "\x08":  # Backspace
        if buffer:
            buffer = buffer[:-1]
            sys.stdout.write("\b \b")
            sys.stdout.flush()

        return buffer, None

    if char == "\x03":  # Ctrl-C delivered through the console buffer
        raise KeyboardInterrupt

    # Ignore the lead byte of extended keys (arrows, F-keys, etc.) --
    # not worth handling for a quick test harness.
    if char in ("\x00", "\xe0"):
        msvcrt.getwch()
        return buffer, None

    sys.stdout.write(char)
    sys.stdout.flush()

    return buffer + char, None


kam = KAMXL("COM8")

connected_station = False

try:
    kam.connect()

    print(f"Connecting to {TARGET}...")
    print()

    banner = kam.connect_station(TARGET)
    connected_station = True

    print("Connected:")
    print(banner)
    print()
    print(
        f"Type messages and press Enter to send. "
        f"Type {QUIT_COMMAND} to disconnect."
    )
    print()

    buffer = ""

    while True:
        incoming = kam.read_available()

        if incoming:
            print(incoming, end="", flush=True)

        buffer, line = read_line_nonblocking(buffer)

        if line is not None:
            if line.strip().lower() == QUIT_COMMAND:
                break

            kam.send_connected(line)

        time.sleep(0.05)

except KeyboardInterrupt:
    print()
    print("Stopped by user (Ctrl-C).")

except KAMTimeoutError as exc:
    print()
    print("KAMTimeoutError (our own timeout fired first):")
    print(exc)

except KAMConnectionError as exc:
    print()
    print("KAMConnectionError (KAM-XL itself reported failure):")
    print(exc)

except KAMError as exc:
    print()
    print("KAM-XL error:", exc)

except Exception as exc:
    print()
    print("Unexpected error:", exc)

finally:
    if connected_station:
        print()
        print("Disconnecting from station...")

        try:
            confirmation = kam.disconnect_station()
            print(repr(confirmation))
        except Exception as exc:
            print("Could not cleanly disconnect station:", exc)

    kam.disconnect()
