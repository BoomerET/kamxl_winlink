import time

import os as _os
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # the repo root

from kamxl import (
    KAMXL,
    KAMError,
    KAMTimeoutError,
    KAMConnectionError,
)


TARGET = "KD5EOC-10"

# How long to sit in Convers mode capturing whatever the Winlink RMS
# gateway sends before we disconnect, if it doesn't disconnect first.
SESSION_SECONDS = 60

# If nothing arrives for this many seconds, assume the exchange is over
# and wrap up early instead of waiting out the full SESSION_SECONDS.
IDLE_TIMEOUT = 15


# Serial port: first argument, else TNCSTATION_PORT, else /dev/kamxl.
PORT = (_sys.argv[1] if len(_sys.argv) > 1
        else _os.environ.get("TNCSTATION_PORT", "/dev/kamxl"))

kam = KAMXL(PORT)

# Tracks whether connect_station() actually succeeded, so the finally
# block below only tries to disconnect the *station* (not just the
# serial port) if there's really an AX.25 link up.
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
        f"Capturing traffic for up to {SESSION_SECONDS}s "
        f"(idle timeout {IDLE_TIMEOUT}s)..."
    )
    print()

    deadline = time.monotonic() + SESSION_SECONDS
    last_data = time.monotonic()

    while time.monotonic() < deadline:
        text = kam.read_available()

        if text:
            last_data = time.monotonic()

            # Winlink's B2F handshake is largely binary/control-code
            # data, not friendly text -- repr() so nothing gets lost
            # or misread as terminal control codes.
            print(f"[{len(text)} bytes] {text!r}")

        if time.monotonic() - last_data > IDLE_TIMEOUT:
            print()
            print("No traffic for a while, wrapping up.")
            break

        time.sleep(0.05)
    else:
        print()
        print("Reached the session time limit, wrapping up.")

except KeyboardInterrupt:
    print()
    print("Stopped by user (Ctrl-C).")

except KAMTimeoutError as exc:
    print()
    print("Timed out:", exc)

except KAMConnectionError as exc:
    print()
    print("Connection failed:", exc)

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
