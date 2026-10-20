"""deploy/: the systemd units and the script that installs them."""

import os
import shutil
import subprocess
import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
UNITS = ("tncstation-daemon.service", "tncstation-web.service")


class InstallScriptTests(unittest.TestCase):
    def dry_run(self):
        sh = shutil.which("sh")
        if not sh:
            self.skipTest("no sh")
        env = dict(os.environ, SUDO_USER="n0call")
        result = subprocess.run(
            [sh, str(DEPLOY / "install.sh"), "--dry-run"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_dry_run_fills_in_every_placeholder(self):
        out = self.dry_run()

        self.assertNotIn("@", out)
        for unit in UNITS:
            self.assertIn(f"### /etc/systemd/system/{unit}", out)
        self.assertIn("User=n0call", out)
        self.assertIn(f"WorkingDirectory={ROOT}", out)
        self.assertIn(f"{ROOT}/tncstation_daemon.py", out)
        self.assertIn(f"{ROOT}/tncstation_rest.py", out)

    def test_install_writes_units_and_reloads_systemd(self):
        import tempfile

        sh = shutil.which("sh")
        if not sh:
            self.skipTest("no sh")
        tmp = Path(tempfile.mkdtemp())
        (tmp / "bin").mkdir()
        fake = tmp / "bin" / "systemctl"
        fake.write_text(f'#!/bin/sh\necho "$*" >> {tmp}/calls\n')
        fake.chmod(0o755)
        env = dict(os.environ, SUDO_USER="nobody",
                   TNCSTATION_UNIT_DIR=str(tmp),
                   PATH=f"{tmp}/bin:" + os.environ["PATH"])

        result = subprocess.run([sh, str(DEPLOY / "install.sh")],
                                capture_output=True, text=True,
                                timeout=30, env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        for unit in UNITS:
            text = (tmp / unit).read_text()
            self.assertNotIn("@", text, unit)
            self.assertIn(f"WorkingDirectory={ROOT}", text)
        self.assertEqual((tmp / "calls").read_text().split(), ["daemon-reload"])
        self.assertIn("systemctl enable --now", result.stdout)

    def test_units_run_scripts_that_exist(self):
        for unit in UNITS:
            text = (DEPLOY / unit).read_text()
            exec_line = next(l for l in text.splitlines()
                             if l.startswith("ExecStart="))
            script = exec_line.split()[-1].replace("@DIR@/", "")
            self.assertTrue((ROOT / script).exists(), script)

    def test_daemon_unit_basics(self):
        text = (DEPLOY / "tncstation-daemon.service").read_text()
        for line in ("WorkingDirectory=@DIR@", "SupplementaryGroups=dialout",
                     "StateDirectory=tncstation", "Restart=always"):
            self.assertIn(line, text)

    def test_web_unit_follows_the_daemon(self):
        text = (DEPLOY / "tncstation-web.service").read_text()
        self.assertIn("After=tncstation-daemon.service", text)
        self.assertIn("WorkingDirectory=@DIR@", text)

    def test_script_is_executable(self):
        self.assertTrue(os.access(DEPLOY / "install.sh", os.X_OK))


if __name__ == "__main__":
    unittest.main()
