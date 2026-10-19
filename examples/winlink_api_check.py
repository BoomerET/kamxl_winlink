"""
One cheap, real call to Winlink's HTTP web-service API (winlink_api.py)
to confirm an API key actually works -- no KAM-XL, no serial port, no
daemon or REST server needed, just network access and a real key.

Per the Winlink Development Team's own instructions when they issue a
key ("the API endpoints should be queried sparingly"), this makes
exactly one call: account_exists() for a single callsign -- the
cheapest read available, and enough to prove the key is valid and
account_exists()'s parsing/auth are working end-to-end. It deliberately
does NOT also call get_gateway_status()/nearby_gateways() here; if you
want to try those too, do it separately and sparingly, not as part of
routinely re-running this script.

Reads WINLINK_API_KEY the same way tncstation_daemon.py does: a real
exported environment variable, or a .env file in the current directory
(reuses tncstation_daemon.py's own _load_dotenv() rather than duplicating
that parsing logic) -- see docs/daemon.md's "Winlink API key" section.

Usage:

    python3 examples/winlink_api_check.py [CALLSIGN]

CALLSIGN defaults to "N5BA" if not given.
"""

import os
import sys
from pathlib import Path

# tncstation_daemon.py and winlink_api.py (repo root) need to be importable
# even when this script is run directly (not via `pip install -e .`)
# -- same reasoning, same fix, as offline_typed_commands.py's own
# sys.path bootstrap. Without this, running `python
# examples/winlink_api_check.py` puts only examples/ itself on
# sys.path, not the repo root one level up -- a real gap found the
# first time this was actually run outside this project's own sandbox
# (which had PYTHONPATH set already, masking it).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tncstation_daemon import _load_dotenv
from winlink_api import WinlinkAPIError, account_exists


def main():
    _load_dotenv()

    api_key = os.environ.get("WINLINK_API_KEY")

    if not api_key:
        print(
            "WINLINK_API_KEY is not set (checked the real environment "
            "and a .env file in the current directory). See "
            "docs/daemon.md's \"Winlink API key\" section."
        )
        sys.exit(1)

    callsign = sys.argv[1] if len(sys.argv) > 1 else "N5BA"

    print(f"Checking account_exists({callsign!r})...")

    try:
        exists = account_exists(callsign, api_key)
    except WinlinkAPIError as exc:
        print(f"API call failed: {exc}")
        print(
            "This means the key, network path, or account/exists "
            "parsing has a real problem -- not just that the callsign "
            "has no account (that would print False, not raise)."
        )
        sys.exit(1)

    print(f"CallsignExists: {exists}")
    print("Key and account_exists() are both working end-to-end.")


if __name__ == "__main__":
    main()
