"""Structural checks on the static pages in site/.

Offline and dependency-free (``html.parser`` from the stdlib), because the
pages are hand-written with no build step: nothing else would notice an
unclosed ``<div>`` or a root-relative ``href``.

Two rules, both from CLAUDE.md:

* **Well-formed markup** — every element that opens closes, in order. The
  browser would silently repair most of it; the calendar and modal JS would
  not.
* **No root-relative URLs** — the site is served from the ``/boscafebikers/``
  subpath (and mirrored again under ``/preview/`` for the ``dev`` branch), so
  a leading ``/`` 404s in production even though it works on a local
  ``http.server``. Protocol-relative ``//host/…`` counts as the same failure:
  nothing here needs it.

* **Every third-party subresource is pinned** — the two CDN libraries
  (FullCalendar, MapLibre) are the only bytes on the site the repo does not
  own, so each ``<script src="https://…">`` / ``<link rel=stylesheet
  href="https://…">`` has to carry an ``integrity`` hash. A mismatch blocks
  the file, which for both of them is a designed fallback, not a broken page.

Plus the shared chrome: every page carries the same ``.nav`` and ``<footer>``.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

import pytest

SITE = Path(__file__).resolve().parent.parent / "site"

# HTML void elements: they never take an end tag.
VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

# Attributes that hold a URL the browser will resolve against the page.
URL_ATTRS = frozenset({"href", "src", "action", "poster", "srcset", "data-bg", "formaction"})

# A subresource fetched from another origin — the only bytes on the site this
# repo does not own, and so the only ones that need an integrity hash.
OFFSITE_RE = re.compile(r"^(?:https?:)?//", re.IGNORECASE)

# Google Fonts serves a per-browser stylesheet, so its bytes are deliberately
# not hashable. Nothing on the site uses it today; the exemption is here so a
# future font link doesn't have to argue with this test.
SRI_EXEMPT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")

# The seven pages CLAUDE.md documents. Checked as a subset of the glob so that
# adding an eighth page doesn't fail the suite — but deleting one does.
EXPECTED_PAGES = frozenset({
    "index.html", "cafes.html", "gallery.html", "shopify.html",
    "meta-business.html", "contact.html", "donate.html",
})


def site_pages() -> list[Path]:
    return sorted(SITE.glob("*.html"))


PAGES = site_pages()
PAGE_IDS = [p.name for p in PAGES]


class PageParser(HTMLParser):
    """Collects tag-nesting errors, URL attributes and class names."""

    def __init__(self, name: str) -> None:
        super().__init__(convert_charrefs=True)
        self.name = name
        self._stack: list[tuple[str, int]] = []
        self.errors: list[str] = []
        self.urls: list[tuple[str, str, int]] = []   # (attr, value, line)
        self.tags: list[str] = []
        self.classes: set[str] = set()
        # (url, has_integrity, line) for every subresource fetched off-site.
        self.external: list[tuple[str, bool, int]] = []

    # -- helpers -------------------------------------------------------
    def _record(self, tag: str, attrs) -> None:
        self.tags.append(tag)
        line = self.getpos()[0]
        seen = {}
        for name, value in attrs:
            if value is None:
                continue
            seen[name] = value
            if name == "class":
                self.classes.update(value.split())
            if name in URL_ATTRS:
                self.urls.append((name, value, line))
        url = None
        if tag == "script":
            url = seen.get("src")
        elif tag == "link" and "stylesheet" in seen.get("rel", "").split():
            url = seen.get("href")
        if url and OFFSITE_RE.match(url):
            self.external.append((url, bool(seen.get("integrity")), line))

    # -- HTMLParser hooks ----------------------------------------------
    def handle_starttag(self, tag, attrs):
        self._record(tag, attrs)
        if tag not in VOID_ELEMENTS:
            self._stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        # <br /> and friends: opened and closed in one go.
        self._record(tag, attrs)

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            self.errors.append(f"line {self.getpos()[0]}: </{tag}> on a void element")
            return
        if not self._stack:
            self.errors.append(f"line {self.getpos()[0]}: </{tag}> with nothing open")
            return
        open_tag, open_line = self._stack[-1]
        if open_tag == tag:
            self._stack.pop()
            return
        # Mismatch: report it against whatever is actually open, and recover by
        # unwinding to the matching tag if there is one, so one stray tag
        # doesn't cascade into a hundred errors.
        self.errors.append(
            f"line {self.getpos()[0]}: </{tag}> closes <{open_tag}> opened on line {open_line}"
        )
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                return

    def close(self):  # type: ignore[override]
        super().close()
        for tag, line in self._stack:
            self.errors.append(f"line {line}: <{tag}> is never closed")
        return self


def parse(path: Path) -> PageParser:
    parser = PageParser(path.name)
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser


def test_the_documented_pages_all_exist():
    assert EXPECTED_PAGES <= {p.name for p in PAGES}


@pytest.mark.parametrize("path", PAGES, ids=PAGE_IDS)
def test_page_is_well_formed(path: Path):
    parser = parse(path)
    assert parser.errors == [], f"{path.name}:\n  " + "\n  ".join(parser.errors)


@pytest.mark.parametrize("path", PAGES, ids=PAGE_IDS)
def test_page_has_the_shared_nav_and_footer(path: Path):
    parser = parse(path)
    assert "nav" in parser.classes, f"{path.name} is missing the shared .nav"
    assert "footer" in parser.tags, f"{path.name} is missing the shared <footer>"


@pytest.mark.parametrize("path", PAGES, ids=PAGE_IDS)
def test_page_has_no_root_relative_urls(path: Path):
    """Served from /boscafebikers/ (and /boscafebikers/preview/), so a leading
    slash points at the wrong host root. Protocol-relative // is out too."""
    offenders = [
        f"line {line}: {attr}={value!r}"
        for attr, value, line in parse(path).urls
        if value.startswith("/")
    ]
    assert offenders == [], f"{path.name} has root-relative URLs:\n  " + "\n  ".join(offenders)


@pytest.mark.parametrize("path", PAGES, ids=PAGE_IDS)
def test_page_has_no_root_relative_urls_in_raw_text(path: Path):
    """Belt and braces for anything the parser doesn't treat as an attribute
    (inline CSS url(), a stray single-quoted href)."""
    text = path.read_text(encoding="utf-8")
    bad = re.findall(r"""(?:href|src|action|poster)\s*=\s*['"]//?[^'"]*""", text)
    assert bad == [], f"{path.name}: {bad}"


@pytest.mark.parametrize("path", PAGES, ids=PAGE_IDS)
def test_third_party_subresources_carry_an_integrity_hash(path: Path):
    """FullCalendar and MapLibre are the site's only off-site bytes.

    Both are version-pinned on jsDelivr, so their content can never legitimately
    change under a pinned URL — an SRI hash costs nothing and turns "the CDN
    served something else" into "the script doesn't run". Both libraries degrade
    on their own when that happens (the hand-rolled month grid; a hidden map
    box), so a mismatch is a designed fallback rather than a broken page.
    """
    offenders = [
        f"line {line}: {url}"
        for url, has_integrity, line in parse(path).external
        if not has_integrity and not any(host in url for host in SRI_EXEMPT_HOSTS)
    ]
    assert offenders == [], (
        f"{path.name} loads third-party code with no integrity= hash:\n  "
        + "\n  ".join(offenders)
    )


def test_the_cdn_subresources_are_version_pinned():
    """An SRI hash on a floating version would just break on the next release."""
    unpinned = []
    for path in PAGES:
        for url, _has_integrity, line in parse(path).external:
            if "@" not in url.rsplit("/", 1)[0]:
                unpinned.append(f"{path.name} line {line}: {url}")
    assert unpinned == [], "CDN URLs without a pinned version:\n  " + "\n  ".join(unpinned)


# --- the subscribe row on the rides page ------------------------------------
# The public feed has one URL; every way of subscribing to it on the page has to
# point at that same feed. The Google Calendar button is the one that is easy
# to get subtly wrong: it is a deep link into Google's "add calendar from URL"
# screen, and the feed URL travels inside its `cid` query parameter.

RIDES_ICS_URL = "https://cafebikers.org/rides.ics"
WEBCAL_URL = "webcal://cafebikers.org/rides.ics"
GOOGLE_SUBSCRIBE_PREFIX = "https://calendar.google.com/calendar/r?cid="


class AnchorParser(HTMLParser):
    """Every <a> on a page, as (attrs, text), plus the class of its parent."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._open: list[dict] | None = None
        self._parents: list[str] = []
        self.anchors: list[dict] = []

    def handle_starttag(self, tag, attrs):
        seen = dict(attrs)
        if tag == "a":
            self._open = {"attrs": seen, "text": "", "parent": " ".join(self._parents)}
        if tag not in VOID_ELEMENTS:
            self._parents.append(seen.get("class") or "")

    def handle_endtag(self, tag):
        if tag not in VOID_ELEMENTS and self._parents:
            self._parents.pop()
        if tag == "a" and self._open is not None:
            self._open["text"] = " ".join(self._open["text"].split())
            self.anchors.append(self._open)
            self._open = None

    def handle_data(self, data):
        if self._open is not None:
            self._open["text"] += data


def subscribe_links() -> list[dict]:
    parser = AnchorParser()
    parser.feed((SITE / "index.html").read_text(encoding="utf-8"))
    parser.close()
    return [a for a in parser.anchors if "subscribe" in a["parent"].split()]


def test_the_subscribe_row_offers_google_calendar():
    """A one-click subscribe for Google, not just a URL to paste (issue #6)."""
    google = [a for a in subscribe_links() if a["text"] == "Google Calendar"]
    assert len(google) == 1, "exactly one Google Calendar button in the subscribe row"
    href = google[0]["attrs"]["href"]
    assert href.startswith(GOOGLE_SUBSCRIBE_PREFIX)
    cid = unquote(href[len(GOOGLE_SUBSCRIBE_PREFIX):])
    assert cid == WEBCAL_URL, "the cid must be the feed URL Apple Calendar subscribes to"
    # A link off-site opens a new tab, like the per-ride Google export does.
    assert google[0]["attrs"].get("target") == "_blank"
    assert "noopener" in (google[0]["attrs"].get("rel") or "")


def test_every_subscribe_link_points_at_the_one_feed():
    links = subscribe_links()
    assert len(links) == 3, [a["text"] for a in links]
    hrefs = {a["attrs"]["href"] for a in links}
    assert "rides.ics" in hrefs, "the relative .ics download"
    assert WEBCAL_URL in hrefs, "the Apple Calendar webcal:// link"
    # The pasteable URL in the note beneath the row is the https form.
    text = (SITE / "index.html").read_text(encoding="utf-8")
    assert f"<code>{RIDES_ICS_URL}</code>" in text


def test_stylesheet_has_no_root_relative_urls():
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"url\(\s*['\"]?/", css) is None
