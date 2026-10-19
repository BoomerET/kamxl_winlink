"""
Tests for tncstation_web: the dashboard and other pages now live in
tncstation_web/<page>/{index.html,style.css,app.js} and are assembled into
one self-contained response by load_page().
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import tncstation_rest
import tncstation_web


class LoadPageTests(unittest.TestCase):

    def test_every_page_assembles_self_contained(self):
        for name in tncstation_web.PAGE_NAMES:
            html = tncstation_web.load_page(name)
            with self.subTest(page=name):
                self.assertTrue(html.lower().startswith("<!doctype html>"))
                self.assertNotIn(tncstation_web.STYLE_TAG, html)
                self.assertNotIn(tncstation_web.SCRIPT_TAG, html)
                # The shared theme (theme.css / theme.js) and features.js,
                # plus the page's own.
                self.assertEqual(len(re.findall(r"<style>", html)), 2)
                self.assertEqual(len(re.findall(r"<script>", html)), 3)

    def test_coverage_card_and_map_overlay_present(self):
        dashboard = tncstation_web.load_page("dashboard")
        for marker in ('id="coverageCard"', 'id="setupInput"',
                       "refreshCoverage", "/coverage/setup"):
            self.assertIn(marker, dashboard)
        page = tncstation_web.load_page("map")
        for marker in ('id="coverageBanner"', "showCoverage",
                       'params.get("coverage")'):
            self.assertIn(marker, page)

    def test_backup_card_present(self):
        dashboard = tncstation_web.load_page("dashboard")
        for marker in ('id="backupCard"', "refreshBackup", "/backup/now"):
            self.assertIn(marker, dashboard)

    def test_every_page_links_log_and_telemetry(self):
        for name in tncstation_web.PAGE_NAMES:
            html = tncstation_web.load_page(name)
            with self.subTest(page=name):
                if name != "log":
                    self.assertIn('id="logLink"', html)
                if name != "telemetry":
                    self.assertIn('id="telemetryLink"', html)

    def test_every_page_has_a_phone_viewport(self):
        # Without it phones draw the page at desktop width and shrink it.
        for name in tncstation_web.PAGE_NAMES:
            with self.subTest(page=name):
                self.assertIn(
                    '<meta name="viewport" content="width=device-width, '
                    'initial-scale=1">',
                    tncstation_web.load_page(name),
                )

    def test_files_are_inlined_verbatim(self):
        folder = tncstation_web.PAGES_DIR / "dashboard"
        html = tncstation_web.load_page("dashboard")
        for filename in ("style.css", "app.js"):
            content = (folder / filename).read_text(encoding="utf-8")
            self.assertIn(content, html)

    def test_rest_constants_are_the_loaded_pages(self):
        for name, const in (
            ("dashboard", "DASHBOARD_HTML"), ("terminal", "TERMINAL_HTML"),
            ("pbbs", "PBBS_HTML"), ("winlink", "WINLINK_HTML"),
            ("map", "MAP_HTML"), ("log", "LOG_HTML"),
            ("telemetry", "TELEMETRY_HTML"),
        ):
            self.assertEqual(getattr(tncstation_rest, const), tncstation_web.load_page(name))

    def test_missing_placeholder_is_an_error(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        page = tmp / "broken"
        page.mkdir()
        (page / "index.html").write_text("<!doctype html><p>no tags</p>")
        (page / "style.css").write_text("")
        (page / "app.js").write_text("")

        original = tncstation_web.PAGES_DIR
        tncstation_web.PAGES_DIR = tmp
        try:
            with self.assertRaises(ValueError):
                tncstation_web.load_page("broken")
        finally:
            tncstation_web.PAGES_DIR = original

    def test_all_page_scripts_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        paths = [tncstation_web.PAGES_DIR / name / "app.js"
                 for name in tncstation_web.PAGE_NAMES]
        for shared in ("theme.js", "features.js"):
            paths.append(tncstation_web.PAGES_DIR / tncstation_web.SHARED_DIR / shared)
        for path in paths:
            result = subprocess.run(
                [node, "--check", str(path)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")

    def test_package_data_covers_page_files(self):
        pyproject = (Path(tncstation_web.__file__).parent.parent / "pyproject.toml")
        text = pyproject.read_text()
        for pattern in ("*/index.html", "*/style.css", "*/app.js",
                        "_shared/theme.css", "_shared/theme.js",
                        "_shared/features.js"):
            self.assertIn(pattern, text)
        self.assertIn('packages = ["tncstation_web"]', text)


if __name__ == "__main__":
    unittest.main()
