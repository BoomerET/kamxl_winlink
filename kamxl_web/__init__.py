"""
Web pages served by kamxl_rest.py.

Each page lives in its own folder as three ordinary files you can edit
with normal HTML/CSS/JavaScript tooling:

    kamxl_web/<page>/index.html   markup
    kamxl_web/<page>/style.css    page styles
    kamxl_web/<page>/app.js       page script

index.html references the other two with exactly

    <link rel="stylesheet" href="style.css">
    <script src="app.js"></script>

and load_page() inlines them, so each page is still served as one
self-contained response from the same authenticated route as before
(the bearer token arrives as ?token= on the page URL; a separate
/static/... request for the CSS or JS wouldn't carry it). Opening an
index.html straight from disk also works for layout work, since the
browser follows those relative links itself.

Pages are read once, when kamxl_rest is imported -- restart the web
service after editing them.
"""

from pathlib import Path

PAGES_DIR = Path(__file__).resolve().parent

STYLE_TAG = '<link rel="stylesheet" href="style.css">'
SCRIPT_TAG = '<script src="app.js"></script>'

PAGE_NAMES = ("dashboard", "terminal", "pbbs", "winlink", "map")


def _read(path: Path) -> str:
    # newline="" keeps the files' exact line endings (no translation).
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def load_page(name: str) -> str:
    """Return the complete HTML for page ``name`` with CSS/JS inlined."""
    folder = PAGES_DIR / name
    html = _read(folder / "index.html")

    for tag, filename, wrap in (
        (STYLE_TAG, "style.css", ("<style>", "</style>")),
        (SCRIPT_TAG, "app.js", ("<script>", "</script>")),
    ):
        if html.count(tag) != 1:
            raise ValueError(
                f"{folder / 'index.html'} must contain {tag!r} exactly once"
            )
        content = _read(folder / filename)
        html = html.replace(tag, wrap[0] + content + wrap[1])

    return html
