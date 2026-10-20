import os as _os
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # the repo root

from kamxl import KAMXL, KAMError


# Serial port: first argument, else TNCSTATION_PORT, else /dev/kamxl.
PORT = (_sys.argv[1] if len(_sys.argv) > 1
        else _os.environ.get("TNCSTATION_PORT", "/dev/kamxl"))

kam = KAMXL(PORT)

# Set before the try block so the restore-on-exit logic below can safely
# check "did we actually read an original value" without risking a
# NameError if kam.connect() (or the very first get_typed call) fails.
original = None

try:
    kam.connect()

    original = kam.get_typed("MONITOR")

    print("Original MONITOR:", original)

    kam.set_multiport_bool(
        "MONITOR",
        2,
        True
    )

    print("Listening for 10 minutes...")
    print()

    kam.listen(
        seconds=600,
        callback=lambda text: print(
            text,
            end="",
            flush=True
        )
    )

except KeyboardInterrupt:
    print()
    print("Stopped listening (Ctrl-C).")

except KAMError as exc:
    print()
    print("KAM-XL error:", exc)

except Exception as exc:
    print()
    print("Unexpected error:", exc)

finally:
    if original is not None:
        print()
        print("Restoring MONITOR...")

        try:
            kam.set_typed(
                "MONITOR",
                original
            )
        except Exception as exc:
            print(
                "Could not restore MONITOR:",
                exc
            )

    kam.disconnect()
