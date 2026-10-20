"""The web server explains, in one line, why it can't listen."""

import errno
import socket
import unittest
from unittest.mock import patch

import fakes  # noqa: F401  (puts the repo root on sys.path)

import tncstation_rest


class BindFailureTests(unittest.TestCase):
    def run_main(self, argv):
        with patch.object(tncstation_rest, "_load_dotenv"), \
                patch.dict("os.environ", {"TNCSTATION_REST_API_KEY": "k" * 32}, clear=True), \
                patch("logging.basicConfig"), \
                self.assertLogs("tncstation_rest", "ERROR") as logs, \
                self.assertRaises(SystemExit) as ctx:
            tncstation_rest.main(argv)
        return ctx.exception.code, "\n".join(logs.output)

    def test_port_in_use(self):
        # Seen on a fresh install: Pat already had 8080.
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        self.addCleanup(taken.close)
        port = taken.getsockname()[1]

        code, log = self.run_main(["--host", "127.0.0.1", "--port", str(port)])

        self.assertEqual(code, 2)
        self.assertIn(f"Port {port} is already in use", log)
        self.assertIn("TNCSTATION_REST_PORT", log)

    def test_other_hints(self):
        hint = tncstation_rest._bind_failure_hint
        self.assertIn("below 1024", hint(OSError(errno.EACCES, "x"), "0.0.0.0", 80))
        self.assertIn("TNCSTATION_REST_HOST",
                      hint(OSError(errno.EADDRNOTAVAIL, "x"), "10.9.9.9", 8090))
        self.assertIsNone(hint(OSError(errno.ENOENT, "x"), "0.0.0.0", 8090))


if __name__ == "__main__":
    unittest.main()
