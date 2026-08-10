"""
Small, focused helper for turning pyserial's low-level "could not
open/configure port" exceptions into an actionable message, when a
specific known cause can be identified.

Shared by exitKissMode.py and enterKissMode.py rather than duplicated
in both -- this is pure diagnostic logic, unrelated to kamxl.py's
Terminal Mode protocol machinery, so pulling it in here doesn't
compromise either script's own reasons for staying independent of
kamxl.py's connection handling (exitKissMode.py in particular is
deliberately pyserial-only; see its own module docstring).

The one cause confirmed so far -- a real report from Dave's own setup,
not a guess -- is an AX.25 KISS daemon (e.g. `kissattach`, commonly
run as a systemd service such as "kamxl-kiss") already having the
Linux kernel's N_AX25 line discipline attached to the serial device.
Once that's done, a *second* process's tcgetattr() call on the same
device fails with ENOTTY ("Inappropriate ioctl for device", errno 25)
-- not because the port is merely busy/locked in the ordinary sense
(that would normally show up as EBUSY or a permission error), but
because the device no longer behaves like a plain tty to that specific
ioctl while the AX.25 line discipline is attached to it. Stopping the
daemon (which detaches the line discipline when kissattach exits) is
what actually fixes it -- retrying without stopping it first won't
help, and the raw pyserial traceback gives no hint of any of this.
"""

import errno

from typing import Optional


def describe_serial_open_failure(exc: BaseException) -> str:
    """
    Return a human-readable explanation for a pyserial open/configure
    failure.

    Walks ``exc``'s chained cause/context (pyserial's own
    SerialException wraps the real OSError/termios.error this way --
    implicit chaining via a bare ``raise`` inside an ``except`` block,
    not an explicit ``raise ... from``, so it shows up as
    ``__context__`` rather than ``__cause__``; both are checked)
    looking for a real errno this function recognizes. Falls back to
    plain ``str(exc)`` when nothing specific is recognized, rather
    than guessing at a cause that isn't actually confirmed.
    """
    chain = []
    seen = set()
    current: Optional[BaseException] = exc

    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__

    for candidate in chain:
        errno_value = getattr(candidate, "errno", None)

        if errno_value is None:
            args = getattr(candidate, "args", None)
            if args and isinstance(args[0], int):
                errno_value = args[0]

        if errno_value == errno.ENOTTY:
            return (
                f"{exc}\n\n"
                "This specific error (ENOTTY / \"Inappropriate ioctl "
                "for device\") is a known symptom of an AX.25 KISS "
                "daemon -- e.g. a kissattach process, often run as a "
                "systemd service such as \"kamxl-kiss\" -- already "
                "having the kernel's AX.25 line discipline attached "
                "to this port. A second program can't reconfigure the "
                "port while that's attached, even though the port "
                "isn't \"busy\" in the ordinary sense.\n\n"
                "Stop the AX.25 daemon first, e.g.:\n"
                "    sudo systemctl stop kamxl-kiss\n"
                "then try again."
            )

    return str(exc)
