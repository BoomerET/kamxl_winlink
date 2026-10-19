"""
Web pages served by tncstation_rest.py.

Each page lives in its own folder as three ordinary files you can edit
with normal HTML/CSS/JavaScript tooling:

    tncstation_web/<page>/index.html   markup
    tncstation_web/<page>/style.css    page styles
    tncstation_web/<page>/app.js       page script

index.html references the other two with exactly

    <link rel="stylesheet" href="style.css">
    <script src="app.js"></script>

Every page also loads the shared theme files, with exactly

    <script src="../_shared/theme.js"></script>
    <link rel="stylesheet" href="../_shared/theme.css">

in its <head> (see _shared/theme.css for the themes), and load_page()
inlines all four, so each page is still served as one
self-contained response from the same authenticated route as before
(the bearer token arrives as ?token= on the page URL; a separate
/static/... request for the CSS or JS wouldn't carry it). Opening an
index.html straight from disk also works for layout work, since the
browser follows those relative links itself.

Pages are read once, when tncstation_rest is imported -- restart the web
service after editing them.
"""

from pathlib import Path

PAGES_DIR = Path(__file__).resolve().parent

STYLE_TAG = '<link rel="stylesheet" href="style.css">'
SCRIPT_TAG = '<script src="app.js"></script>'

SHARED_DIR = "_shared"
THEME_STYLE_TAG = '<link rel="stylesheet" href="../_shared/theme.css">'
THEME_SCRIPT_TAG = '<script src="../_shared/theme.js"></script>'

PAGE_NAMES = ("dashboard", "terminal", "pbbs", "winlink", "map", "log", "telemetry")


def _read(path: Path) -> str:
    # newline="" keeps the files' exact line endings (no translation).
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def load_page(name: str) -> str:
    """Return the complete HTML for page ``name`` with CSS/JS inlined."""
    folder = PAGES_DIR / name
    html = _read(folder / "index.html")

    shared = PAGES_DIR / SHARED_DIR

    for tag, path, wrap in (
        (THEME_SCRIPT_TAG, shared / "theme.js", ("<script>", "</script>")),
        (THEME_STYLE_TAG, shared / "theme.css", ("<style>", "</style>")),
        (STYLE_TAG, folder / "style.css", ("<style>", "</style>")),
        (SCRIPT_TAG, folder / "app.js", ("<script>", "</script>")),
    ):
        if html.count(tag) != 1:
            raise ValueError(
                f"{folder / 'index.html'} must contain {tag!r} exactly once"
            )
        content = _read(path)
        html = html.replace(tag, wrap[0] + content + wrap[1])

    return html
