"""
Old name for tncstation_rest.py, from before the project was renamed
from kamxl_winlink to tncstation. Kept so existing systemd units
(`python3 kamxl_rest.py`) and imports keep working.

Importing this gives you the tncstation_rest module itself -- the same
object, not a copy -- so anything set or patched on one is seen on
the other.
"""

import sys

import tncstation_rest as _module

if __name__ == "__main__":
    _module.main()
else:
    sys.modules[__name__] = _module
