"""
Demonstrates the typed get/set API against a scripted fake serial
connection -- no KAM-XL hardware required.

Uses the same ScriptedSerial fake as the offline unit test suite
(tests/fakes.py) to simulate a KAM-XL's command/response protocol.
Swap that fake for a real port and this is exactly what talking to
actual hardware looks like -- see hardware_basic_terminal.py.
"""

import sys
from pathlib import Path

# tests/fakes.py (and kamxl.py, one level up from that) need to be
# importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fakes import ScriptedSerial, make_kam


def main():
    # Canned responses, keyed by the exact command text the KAM-XL
    # would receive (see ScriptedSerial's docstring in tests/fakes.py).
    # ScriptedSerial is stateless -- it always answers a given command
    # with the same fixed text -- so each demo section below gets its
    # own instance scripted for what it needs, rather than one shared
    # instance that would have to somehow track state across calls.
    kam = make_kam(ScriptedSerial({
        "VERSION": "KAM-XL VERSION 1.24160",
        "MYCALL": "MYCALL   N5BA-10/N5BA-10",
        "HBAUD": "HBAUD    0/1200",
        "MONITOR": "MONITOR  ON/OFF",
        "DIGIPEAT": "DIGIPEAT UIONLY",
        "FULLDUP": "FULLDUP  OFF/OFF",
    }))

    print("VERSION :", kam.get_typed("VERSION"))
    print("MYCALL  :", kam.get_typed("MYCALL"))
    print("HBAUD   :", kam.get_typed("HBAUD"))
    print("MONITOR :", kam.get_typed("MONITOR"))
    print("DIGIPEAT:", kam.get_typed("DIGIPEAT"))
    print("FULLDUP :", kam.get_typed("FULLDUP"))

    print()
    print("Setting FULLDUP port 1 to LOOPBACK...")
    # A fresh fake scripted to already reflect the post-write state --
    # set_multiport_choice() reads the value back after writing it,
    # and a real KAM-XL would report LOOPBACK/OFF at that point.
    set_demo_kam = make_kam(ScriptedSerial({
        "FULLDUP": "FULLDUP  LOOPBACK/OFF",
    }))
    result = set_demo_kam.set_multiport_choice(
        "FULLDUP",
        1,
        "LOOPBACK",
        choices=("ON", "OFF", "LOOPBACK")
    )
    print("  wire command:", set_demo_kam.serial.written[0])
    print("  result      :", result)

    print()
    print("Read-only protection:")
    try:
        kam.set_typed("VERSION", "TEST")
    except Exception as exc:
        print(" ", type(exc).__name__, "-", exc)

    print()
    print("Validation before writing (invalid DIGIPEAT choice):")
    try:
        kam.set_typed("DIGIPEAT", "NOT_A_REAL_VALUE")
    except Exception as exc:
        print(" ", type(exc).__name__, "-", exc)


if __name__ == "__main__":
    main()
