#!/usr/bin/env python3
import os as _os
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # the repo root

from kamxl import KAMXL

# Serial port: first argument, else TNCSTATION_PORT, else /dev/kamxl.
PORT = (_sys.argv[1] if len(_sys.argv) > 1
        else _os.environ.get("TNCSTATION_PORT", "/dev/kamxl"))

kam = KAMXL(PORT)

try:
    kam.connect()

    mycall = kam.get_typed("MYCALL")

    print(f"Port 1 MYCALL: {mycall[0]!r}")
    print(f"Port 2 MYCALL: {mycall[1]!r}")

finally:
    kam.disconnect()
