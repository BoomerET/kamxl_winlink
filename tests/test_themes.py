"""
Themes: tncstation_web/_shared/theme.css (palettes) and theme.js (picker).

Every color a page uses is a --t-* token, defined once per theme in
theme.css. These tests keep it that way: no page may carry its own
colors, every theme must define every token, the Light and Night
palettes must stay readable, and the picker must behave.
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import fakes  # noqa: F401  (puts the repo root on sys.path)

import tncstation_web


SHARED = tncstation_web.PAGES_DIR / tncstation_web.SHARED_DIR
THEME_CSS = (SHARED / "theme.css").read_text(encoding="utf-8")
THEME_JS = SHARED / "theme.js"

COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)")
TOKEN_USE_RE = re.compile(r"var\(--t-([a-z0-9-]+)")

THEME_SELECTORS = {
    "terminal": ":root {",
    "light": ':root[data-theme="light"] {',
    "night": ':root[data-theme="night"] {',
}


def strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def palettes():
    out = {}
    for name, selector in THEME_SELECTORS.items():
        start = THEME_CSS.index(selector)
        end = THEME_CSS.index("}", start)
        out[name] = dict(re.findall(r"--t-([a-z0-9-]+):\s*([^;]+);", THEME_CSS[start:end]))
    return out


# -- contrast (WCAG 2.x relative luminance) ---------------------------------

def parse_color(text, under=(0, 0, 0)):
    text = text.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{3,8})", text)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h)
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        a = int(h[6:8], 16) / 255 if len(h) == 8 else 1.0
    else:
        nums = [float(x) for x in re.findall(r"[\d.]+", text)]
        r, g, b = nums[:3]
        a = nums[3] if len(nums) > 3 else 1.0
    return tuple(c * a + u * (1 - a) for c, u in zip((r, g, b), under))


def luminance(rgb):
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(fg, bg):
    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# (text token, background token, Light minimum, Night minimum).
# Night trades some contrast for darkness on secondary text -- that's the
# point of a night-vision theme -- but body text stays at 4.5:1.
CONTRAST_PAIRS = [
    ("ink", "bg", 4.5, 4.5), ("ink", "surface", 4.5, 4.5),
    ("ink-strong", "surface", 4.5, 4.5), ("heading", "surface", 4.5, 4.5),
    ("muted", "surface", 4.5, 3.0), ("muted", "bg", 4.5, 3.0),
    ("label", "surface", 4.5, 3.0), ("header-ink", "surface", 4.5, 3.0),
    ("notice", "bg", 4.5, 3.0), ("control-ink", "surface", 4.5, 3.0),
    ("link", "bg", 4.5, 4.5), ("link", "surface-raised", 4.5, 4.5),
    ("info", "bg", 4.5, 3.0), ("bad", "surface", 4.5, 4.5),
    ("alert", "surface", 4.5, 4.5), ("ok", "surface", 3.0, 3.0),
    ("warn", "surface", 3.0, 3.0), ("amber", "surface", 3.0, 3.0),
    ("on-amber", "amber", 4.5, 4.5), ("on-badge", "badge-bg", 4.5, 4.5),
    ("ink", "bubble-in", 4.5, 4.5), ("ink", "bubble-out", 4.5, 4.5),
    ("ink", "item-selected", 4.5, 4.5), ("field-alt-ink", "field-alt-bg", 4.5, 4.5),
    ("ink-on-mark", "mark-bg", 4.5, 3.0), ("dim", "surface", 3.0, 2.0),
    ("faint", "bg", 3.0, 2.0), ("accent", "surface", 3.0, 2.5),
    ("series", "surface", 3.0, 3.0), ("gold", "overlay-bg", 4.5, 3.0),
]


class PaletteTests(unittest.TestCase):

    def test_every_theme_defines_the_same_tokens(self):
        p = palettes()
        for name in ("light", "night"):
            with self.subTest(theme=name):
                self.assertEqual(sorted(p[name]), sorted(p["terminal"]))

    def test_every_token_used_is_defined(self):
        used = set(TOKEN_USE_RE.findall(THEME_CSS))
        for page in tncstation_web.PAGE_NAMES:
            css = (tncstation_web.PAGES_DIR / page / "style.css").read_text(encoding="utf-8")
            used |= set(TOKEN_USE_RE.findall(css))
        for name, palette in palettes().items():
            with self.subTest(theme=name):
                self.assertEqual(used - set(palette), set())

    def test_terminal_is_the_original_look(self):
        # Spot-check the defaults against the colors the pages had before
        # themes, so the default can't drift by accident.
        t = palettes()["terminal"]
        for token, original in (("bg", "#0b0f10"), ("surface", "#101617"),
                                ("ink", "#d4f7d4"), ("ink-strong", "#cfc"),
                                ("border", "#234"), ("control-ink", "#9c9"),
                                ("link", "#3388ff"), ("amber", "#f1c40f")):
            self.assertEqual(t[token], original, token)

    def test_light_and_night_are_readable(self):
        p = palettes()
        for name, column in (("light", 2), ("night", 3)):
            palette = p[name]
            page_bg = parse_color(palette["bg"])
            for pair in CONTRAST_PAIRS:
                fg, bg, minimum = pair[0], pair[1], pair[column]
                with self.subTest(theme=name, pair=f"{fg} on {bg}"):
                    bg_rgb = parse_color(palette[bg], under=page_bg)
                    ratio = contrast(parse_color(palette[fg], under=bg_rgb), bg_rgb)
                    self.assertGreaterEqual(round(ratio, 2), minimum)

    def test_night_has_no_blue_or_green_light(self):
        # Red keeps night vision; any real green or blue undoes it.
        for token, value in palettes()["night"].items():
            if value.startswith("rgba(0, 0, 0"):
                continue
            r, g, b = parse_color(value)
            with self.subTest(token=token):
                self.assertGreaterEqual(r, g, value)
                self.assertGreaterEqual(r, b, value)
                self.assertLessEqual(max(g, b), 0.45 * max(r, 1) + 1, value)


class PagesUseTokensTests(unittest.TestCase):

    def test_no_page_stylesheet_has_its_own_colors(self):
        for page in tncstation_web.PAGE_NAMES:
            css = (tncstation_web.PAGES_DIR / page / "style.css").read_text(encoding="utf-8")
            with self.subTest(page=page):
                self.assertEqual(COLOR_RE.findall(strip_comments(css)), [])

    def test_no_inline_style_colors_in_markup(self):
        for page in tncstation_web.PAGE_NAMES:
            html = (tncstation_web.PAGES_DIR / page / "index.html").read_text(encoding="utf-8")
            inline = " ".join(re.findall(r'style="([^"]*)"', html))
            with self.subTest(page=page):
                self.assertEqual(COLOR_RE.findall(inline), [])

    def test_theme_loads_before_the_page_draws(self):
        js = THEME_JS.read_text(encoding="utf-8")
        for page in tncstation_web.PAGE_NAMES:
            html = tncstation_web.load_page(page)
            page_css = (tncstation_web.PAGES_DIR / page / "style.css").read_text(encoding="utf-8")
            with self.subTest(page=page):
                head = html[:html.index("</head>")]
                self.assertIn(js, head)
                self.assertIn(THEME_CSS, head)
                self.assertLess(html.index(js), html.index(page_css))
                self.assertLess(html.index(THEME_CSS), html.index(page_css))
                self.assertNotIn(tncstation_web.THEME_STYLE_TAG, html)
                self.assertNotIn(tncstation_web.THEME_SCRIPT_TAG, html)


NODE_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");
const scenario = JSON.parse(process.argv[3]);

function el(tag) {
  return {
    tagName: tag.toUpperCase(), children: [], attrs: {}, listeners: {},
    value: "", className: "", textContent: "", title: "",
    get firstElementChild() { return this.children[0] || null; },
    get firstChild() { return this.children[0] || null; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { this.children.push(c); return c; },
    insertBefore(c) { this.children.unshift(c); return c; },
    addEventListener(t, f) { this.listeners[t] = f; },
    querySelector(sel) { return query(this, sel)[0] || null; },
    querySelectorAll(sel) { return query(this, sel); },
  };
}
function walk(node, out) { for (const c of node.children) { out.push(c); walk(c, out); } return out; }
function query(node, sel) {
  return walk(node, []).filter(n =>
    sel === ".paneHeader" ? n.className === "paneHeader" :
    sel === "select.themePicker" ? n.tagName === "SELECT" && n.className === "themePicker" :
    false);
}

const html = el("html");
const body = el("body");
html.appendChild(body);
const header = el("div"); header.className = "paneHeader";
const title = el("span"); title.textContent = "DASHBOARD";
header.appendChild(title); header.appendChild(el("span"));
body.appendChild(header);

const store = Object.assign({}, scenario.stored === undefined ? {} : {"tncstation.theme": scenario.stored});
const windowListeners = {};
const window = {
  localStorage: {
    getItem(k) { if (scenario.storageThrows) throw new Error("blocked"); return k in store ? store[k] : null; },
    setItem(k, v) { if (scenario.storageThrows) throw new Error("blocked"); store[k] = String(v); },
  },
  matchMedia(q) { return { matches: !!scenario.deviceLight, addEventListener() {} }; },
  addEventListener(t, f) { windowListeners[t] = f; },
};
const document = Object.assign(html, {
  documentElement: html, readyState: "complete",
  createElement: el, addEventListener() {},
  querySelector(sel) { return query(html, sel)[0] || null; },
  querySelectorAll(sel) { return query(html, sel); },
});

vm.runInNewContext(src, { window, document });

const picker = query(html, "select.themePicker")[0];
const result = {
  theme: html.attrs["data-theme"],
  choice: html.attrs["data-theme-choice"],
  pickerInTitle: !!picker && title.children.includes(picker),
  options: picker ? picker.children.map(o => o.value) : [],
  pickerValue: picker ? picker.value : null,
};
if (scenario.pick) {
  picker.value = scenario.pick;
  picker.listeners.change();
  result.afterPick = { theme: html.attrs["data-theme"], stored: store["tncstation.theme"] || null };
}
if (scenario.otherTab) {
  store["tncstation.theme"] = scenario.otherTab;
  windowListeners.storage({ key: "tncstation.theme" });
  result.afterOtherTab = html.attrs["data-theme"];
}
console.log(JSON.stringify(result));
"""


class ThemeScriptTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        if cls.node:
            cls.tmp = Path(tempfile.mkdtemp())
            cls.harness = cls.tmp / "harness.js"
            cls.harness.write_text(NODE_HARNESS)

    @classmethod
    def tearDownClass(cls):
        if cls.node:
            shutil.rmtree(cls.tmp)

    def run_scenario(self, **scenario):
        if not self.node:
            self.skipTest("node not available")
        result = subprocess.run(
            [self.node, str(self.harness), str(THEME_JS), json.dumps(scenario)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_default_is_terminal(self):
        r = self.run_scenario()
        self.assertEqual((r["theme"], r["choice"]), ("terminal", "terminal"))

    def test_saved_theme_is_applied(self):
        for theme in ("light", "night", "terminal"):
            with self.subTest(theme=theme):
                self.assertEqual(self.run_scenario(stored=theme)["theme"], theme)

    def test_unknown_saved_value_falls_back(self):
        self.assertEqual(self.run_scenario(stored="neon")["theme"], "terminal")

    def test_blocked_storage_falls_back(self):
        r = self.run_scenario(storageThrows=True, pick="night")
        self.assertEqual(r["theme"], "terminal")
        # Picking still works for this visit, it just isn't remembered.
        self.assertEqual(r["afterPick"]["theme"], "night")

    def test_auto_follows_the_device(self):
        light = self.run_scenario(stored="auto", deviceLight=True)
        dark = self.run_scenario(stored="auto", deviceLight=False)
        self.assertEqual((light["theme"], light["choice"]), ("light", "auto"))
        self.assertEqual((dark["theme"], dark["choice"]), ("terminal", "auto"))

    def test_picker_added_beside_the_title(self):
        r = self.run_scenario(stored="night")
        self.assertTrue(r["pickerInTitle"])
        self.assertEqual(r["options"], ["terminal", "light", "night", "auto"])
        self.assertEqual(r["pickerValue"], "night")

    def test_picking_applies_and_remembers(self):
        r = self.run_scenario(pick="light")
        self.assertEqual(r["afterPick"], {"theme": "light", "stored": "light"})

    def test_other_tab_change_is_followed(self):
        r = self.run_scenario(otherTab="night")
        self.assertEqual(r["afterOtherTab"], "night")



class PageFeatureTests(unittest.TestCase):
    """features.js hides links to pages a station's TNC can't use."""

    def test_every_page_inlines_features_js(self):
        js = (SHARED / "features.js").read_text(encoding="utf-8")
        for page in tncstation_web.PAGE_NAMES:
            html = tncstation_web.load_page(page)
            self.assertIn(js, html, page)
            self.assertNotIn("_shared/features.js", html, page)

    def test_links_and_cards_are_marked(self):
        for page in tncstation_web.PAGE_NAMES:
            html = tncstation_web.load_page(page)
            for feature in ("terminal", "pbbs", "winlink"):
                if f'id="{feature}Link"' in html:
                    self.assertIn(f'id="{feature}Link" data-feature="{feature}"',
                                  html, (page, feature))
        dashboard = tncstation_web.load_page("dashboard")
        self.assertIn('id="pbbsRow" data-feature="pbbs"', dashboard)
        self.assertIn('id="winlinkRow" data-feature="winlink"', dashboard)
        self.assertIn('id="mailCard" data-hide-when-empty', dashboard)

    def test_feature_pages_say_what_they_need(self):
        for page in ("pbbs", "winlink", "terminal"):
            self.assertIn(f'<body data-page-feature="{page}">',
                          tncstation_web.load_page(page))


class TabTitleTests(unittest.TestCase):
    """features.js adds the station's TNC to the tab title."""

    # Just enough of a browser for features.js: a title, an empty page,
    # and a fetch() that answers /status with the given result.
    HARNESS = """
const fs = require("fs");
const [, , script, statusJson] = process.argv;
const doc = {
  title: "tncstation dashboard", readyState: "complete",
  querySelectorAll: () => [], querySelector: () => null,
  body: { getAttribute: () => null },
};
const g = globalThis;
g.document = doc;
g.location = { search: "" };
g.fetch = () => Promise.resolve({
  json: () => Promise.resolve({ ok: true, result: JSON.parse(statusJson) }),
});
eval(fs.readFileSync(script, "utf8"));
setTimeout(() => console.log(doc.title), 50);
"""

    def title(self, status):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.js"
            harness.write_text(self.HARNESS, encoding="utf-8")
            result = subprocess.run(
                [node, str(harness), str(SHARED / "features.js"), json.dumps(status)],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_label_is_added(self):
        self.assertEqual(
            self.title({"tnc_model": "KISS TNC", "tnc_label": "Direwolf"}),
            "tncstation dashboard · Direwolf",
        )

    def test_model_without_a_label(self):
        # A daemon from before tnc_label.
        self.assertEqual(self.title({"tnc_model": "OpenTNC"}),
                         "tncstation dashboard · OpenTNC")

    def test_unchanged_without_a_tnc(self):
        self.assertEqual(self.title({}), "tncstation dashboard")

    def test_dashboard_unread_count_keeps_the_tnc(self):
        # The dashboard's "(N) " unread prefix used to restore a title
        # saved at load, wiping out the TNC features.js adds afterwards
        # (seen live: every page but the dashboard showed it). It must
        # rebuild from the current title instead.
        js = (tncstation_web.PAGES_DIR / "dashboard" / "app.js").read_text(
            encoding="utf-8")
        self.assertNotIn("baseTitle", js)
        self.assertIn('document.title.replace(/^\\(\\d+\\) /, "")', js)


if __name__ == "__main__":
    unittest.main()
