import os as _os
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # the repo root

from kamxl import KAMXL, KAMError


# Serial port: first argument, else TNCSTATION_PORT, else /dev/kamxl.
PORT = (_sys.argv[1] if len(_sys.argv) > 1
        else _os.environ.get("TNCSTATION_PORT", "/dev/kamxl"))

kam = KAMXL(PORT)

original = None


def print_packet(packet):
    header = f"{packet.source} -> {packet.destination}"

    if packet.digipeaters:
        header += f" via {','.join(packet.digipeaters)}"

    header += f" (port {packet.port})"

    print(header)

    for line in packet.payload.splitlines():
        print(f"    {line}")

    print()


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

    # This is the whole point of milestone #5 -- no manual
    # PacketParser wiring needed, unlike monitorPacketTest.py.
    for packet in kam.monitor(seconds=600):
        print_packet(packet)

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
