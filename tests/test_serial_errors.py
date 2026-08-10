"""
Offline tests for serial_errors.py's describe_serial_open_failure().

No real serial hardware or AX.25 daemon involved -- these build
synthetic exception objects/chains that mimic what pyserial and
termios actually raise (per the real traceback Dave hit:
termios.error: (25, 'Inappropriate ioctl for device'), wrapped by
pyserial's own SerialException via implicit chaining, not an explicit
"raise ... from") and check the function recognizes -- or correctly
does *not* recognize -- the AX.25 KISS daemon scenario.
"""

import errno
import unittest

from serial_errors import describe_serial_open_failure


class DescribeSerialOpenFailureTests(unittest.TestCase):
    def test_plain_exception_falls_back_to_str(self):
        exc = FileNotFoundError("No such file or directory: '/dev/kamxl'")

        result = describe_serial_open_failure(exc)

        self.assertEqual(result, str(exc))
        self.assertNotIn("kissattach", result)

    def test_direct_errno_attribute_enotty_triggers_guidance(self):
        # Some OSError subclasses carry a real .errno attribute
        # directly, without needing to walk a chained cause/context.
        exc = OSError(errno.ENOTTY, "Inappropriate ioctl for device")

        result = describe_serial_open_failure(exc)

        self.assertIn("kissattach", result)
        self.assertIn("kamxl-kiss", result)
        self.assertIn("systemctl stop", result)

    def test_chained_termios_style_error_triggers_guidance(self):
        # Mirrors the real traceback exactly: pyserial's
        # SerialException is raised from inside an "except
        # termios.error as msg:" block with a bare `raise` (implicit
        # chaining -> __context__, not __cause__), and termios.error
        # itself carries the errno as args[0] rather than a real
        # .errno attribute.
        class FakeTermiosError(Exception):
            pass

        try:
            try:
                raise FakeTermiosError(25, "Inappropriate ioctl for device")
            except FakeTermiosError:
                raise RuntimeError(
                    "Could not configure port: (25, 'Inappropriate "
                    "ioctl for device')"
                )
        except RuntimeError as exc:
            # Python 3 implicitly deletes the "as exc" binding once
            # the except block exits, so capture what's needed here.
            exc_str = str(exc)
            result = describe_serial_open_failure(exc)

        self.assertIn("kissattach", result)
        self.assertIn("kamxl-kiss", result)
        self.assertIn(exc_str, result)

    def test_explicit_cause_is_also_checked(self):
        # Belt-and-suspenders: an explicit "raise ... from" (__cause__)
        # should be recognized the same way as implicit chaining
        # (__context__).
        cause = OSError(errno.ENOTTY, "Inappropriate ioctl for device")
        exc = RuntimeError("Could not configure port")
        exc.__cause__ = cause

        result = describe_serial_open_failure(exc)

        self.assertIn("kissattach", result)

    def test_unrelated_errno_does_not_trigger_kiss_guidance(self):
        # Permission denied -- a real, different failure mode (wrong
        # user/group on the device node) that must not be misdiagnosed
        # as the AX.25 daemon scenario.
        exc = OSError(errno.EACCES, "Permission denied")

        result = describe_serial_open_failure(exc)

        self.assertEqual(result, str(exc))
        self.assertNotIn("kissattach", result)

    def test_self_referential_chain_does_not_infinite_loop(self):
        # Defensive: a pathological exception chain that cycles back
        # on itself must not hang the caller.
        exc = RuntimeError("boom")
        exc.__context__ = exc

        result = describe_serial_open_failure(exc)

        self.assertEqual(result, str(exc))


if __name__ == "__main__":
    unittest.main()
