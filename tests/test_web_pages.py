"""
Tests for kamxl_web: the dashboard and other pages now live in
kamxl_web/<page>/{index.html,style.css,app.js} and are assembled into
one self-contained response by load_page().
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import kamxl_rest
import kamxl_web


class LoadPageTests(unittest.TestCase):

    def test_every_page_assembles_self_contained(self):
        for name in kamxl_web.PAGE_NAMES:
            html = kamxl_web.load_page(name)
            with self.subTest(page=name):
                self.assertTrue(html.lower().startswith("<!doctype html>"))
                self.assertNotIn(kamxl_web.STYLE_TAG, html)
                self.assertNotIn(kamxl_web.SCRIPT_TAG, html)
                self.assertEqual(len(re.findall(r"<style>", html)), 1)
                self.assertEqual(len(re.findall(r"<script>", html)), 1)

    def test_coverage_card_and_map_overlay_present(self):
        dashboard = kamxl_web.load_page("dashboard")
        for marker in ('id="coverageCard"', 'id="setupInput"',
                       "refreshCoverage", "/coverage/setup"):
            self.assertIn(marker, dashboard)
        page = kamxl_web.load_page("map")
        for marker in ('id="coverageBanner"', "showCoverage",
                       'params.get("coverage")'):
            self.assertIn(marker, page)

    def test_backup_card_present(self):
        dashboard = kamxl_web.load_page("dashboard")
        for marker in ('id="backupCard"', "refreshBackup", "/backup/now"):
            self.assertIn(marker, dashboard)

    def test_every_page_has_a_phone_viewport(self):
        # Without it phones draw the page at desktop width and shrink it.
        for name in kamxl_web.PAGE_NAMES:
            with self.subTest(page=name):
                self.assertIn(
                    '<meta name="viewport" content="width=device-width, '
                    'initial-scale=1">',
                    kamxl_web.load_page(name),
                )

    def test_files_are_inlined_verbatim(self):
        folder = kamxl_web.PAGES_DIR / "dashboard"
        html = kamxl_web.load_page("dashboard")
        for filename in ("style.css", "app.js"):
            content = (folder / filename).read_text(encoding="utf-8")
            self.assertIn(content, html)

    def test_rest_constants_are_the_loaded_pages(self):
        for name, const in (
            ("dashboard", "DASHBOARD_HTML"), ("terminal", "TERMINAL_HTML"),
            ("pbbs", "PBBS_HTML"), ("winlink", "WINLINK_HTML"),
            ("map", "MAP_HTML"), ("log", "LOG_HTML"),
        ):
            self.assertEqual(getattr(kamxl_rest, const), kamxl_web.load_page(name))

    def test_missing_placeholder_is_an_error(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        page = tmp / "broken"
        page.mkdir()
        (page / "index.html").write_text("<!doctype html><p>no tags</p>")
        (page / "style.css").write_text("")
        (page / "app.js").write_text("")

        original = kamxl_web.PAGES_DIR
        kamxl_web.PAGES_DIR = tmp
        try:
            with self.assertRaises(ValueError):
                kamxl_web.load_page("broken")
        finally:
            kamxl_web.PAGES_DIR = original

    def test_all_page_scripts_parse(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        for name in kamxl_web.PAGE_NAMES:
            path = kamxl_web.PAGES_DIR / name / "app.js"
            result = subprocess.run(
                [node, "--check", str(path)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, f"{path}: {result.stderr}")

    def test_package_data_covers_page_files(self):
        pyproject = (Path(kamxl_web.__file__).parent.parent / "pyproject.toml")
        text = pyproject.read_text()
        for pattern in ("*/index.html", "*/style.css", "*/app.js"):
            self.assertIn(pattern, text)
        self.assertIn('packages = ["kamxl_web"]', text)


if __name__ == "__main__":
    unittest.main()
