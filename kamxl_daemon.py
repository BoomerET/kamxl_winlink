"""
Old name for tncstation_daemon.py, from before the project was renamed
from kamxl_winlink to tncstation. Kept so existing systemd units
(`python3 kamxl_daemon.py`) and imports keep working.

Importing this gives you the tncstation_daemon module itself -- the same
object, not a copy -- so anything set or patched on one is seen on
the other.
"""

import sys

import tncstation_daemon as _module

if __name__ == "__main__":
    _module.main()
else:
    sys.modules[__name__] = _module
