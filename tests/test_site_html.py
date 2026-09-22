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

* **Every third-party subresource is pinned** — FullCalendar is the only
  executable byte fetched from a CDN, so its ``<script src="https://…">`` has
  to carry an ``integrity`` hash. MapLibre v6 is a split ES-module graph and is
  vendored instead; fixed SHA-256 hashes below pin every file from its verified
  npm tarball.
* **Every page has the same browser-enforced security policy** — GitHub Pages
  cannot set custom response headers, so the CSP and referrer policy live in
  early ``<meta>`` elements. Header-only controls are documented in README.md.

Plus the shared chrome: every page carries the same ``.nav`` and ``<footer>``,
and the nav's pinned/hamburger contract is pinned here too — that behaviour has
been reverted once by accident already.
"""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

SITE = Path(__file__).resolve().parent.parent / "site"

# HTML void elements: they never take an end tag.
VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})

# Attributes that hold a URL the browser will resolve against the page.
URL_ATTRS = frozenset({"href", "src", "action", "poster", "srcset", "data-bg", "formaction"})

# A subresource fetched from another origin — the only executable/style bytes
# the repo does not own, and so the only ones that need an integrity hash.
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

EXPECTED_CSP = (
    "default-src 'self'; base-uri 'none'; object-src 'none'; "
    "script-src 'self' https://cdn.jsdelivr.net; script-src-attr 'none'; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: blob: https://tiles.openfreemap.org "
    "https://assets.getpartiful.com https://*.giphy.com "
    "https://firebasestorage.googleapis.com https://storage.googleapis.com "
    "https://*.appspot.com; connect-src 'self' https://tiles.openfreemap.org; "
    "font-src 'self' data:; worker-src 'self' blob:; frame-src 'none'; "
    "form-action 'self' mailto:; upgrade-insecure-requests"
)
EXPECTED_REFERRER_POLICY = "strict-origin-when-cross-origin"

# The brand logo in the shared nav (added on master 2026-09-19). A 500x500
# PNG whose artwork floats in the middle and whose background is transparent,
# so styles.css crops to the ink and draws no chip behind it.
NAV_LOGO = "images/evenly-sized-logo.png"

# The nav bar's background. The bare transparent logo depends on it: its
# #75472e ink is 4.97:1 here and was 2.07:1 on the old espresso bar.
NAV_BAR_COLOUR = "#abd2ff"

MAPLIBRE_VENDOR = SITE / "vendor" / "maplibre-gl-6.10.0"
MAPLIBRE_SHA256 = {
    "LICENSE.txt": "ee5fc05a0677eaf69601d2c7db0d9ecd6cc27c3abc1d0733bc9ed34707cf8ef2",
    "maplibre-gl.css": "8e2dbbab312dc57656fbb76e9fa5308c75c9d7c7ba5808a7d55bcdb64cc813fa",
    "maplibre-gl.mjs": "454e0bbb16c721cfbf952a92ff3e4362130346ed59a5bff56ae5d6fbebb651f7",
    "maplibre-gl-shared.mjs": "0996a0ff2ecb2807afc3f053992539ff7fe45c1678a02304b2b33120136f81f3",
    "maplibre-gl-worker.mjs": "7d5ebf88ec25a72cc48cc320d374f41d921e5105d2f1fc774d463a4b68164227",
}


def site_pages() -> list[Path]:
    return sorted(SITE.glob("*.html"))


PAGES = site_pages()
PAGE_IDS = [p.name for p in PAGES]

# The structural checks above run over whatever is in site/, so a scratch copy
# left in the directory still has to be well-formed. The shared-chrome checks
# below are about what ships, so they run over the documented seven only.
SHIPPED = [p for p in PAGES if p.name in EXPECTED_PAGES]
SHIPPED_IDS = [p.name for p in SHIPPED]


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
        self.metas: list[tuple[dict[str, str], int]] = []
        # (tag, attrs, line) for every element. Boolean attributes (``defer``)
        # come through as "" so ``"defer" in attrs`` answers the question.
        self.elements: list[tuple[str, dict[str, str], int]] = []
        # (url, has_integrity, line) for every subresource fetched off-site.
        self.external: list[tuple[str, bool, int]] = []

    # -- helpers -------------------------------------------------------
    def _record(self, tag: str, attrs) -> None:
        self.tags.append(tag)
        line = self.getpos()[0]
        seen = {}
        boolean_attrs = {}
        for name, value in attrs:
            boolean_attrs[name] = "" if value is None else value
            if value is None:
                continue
            seen[name] = value
            if name == "class":
                self.classes.update(value.split())
            if name in URL_ATTRS:
                self.urls.append((name, value, line))
        self.elements.append((tag, boolean_attrs, line))
        if tag == "meta":
            self.metas.append((seen, line))
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


@pytest.mark.parametrize("path", SHIPPED, ids=SHIPPED_IDS)
def test_page_collapses_the_nav_into_a_hamburger_on_phones(path: Path):
    """The phone nav is one labelled button that owns the tab list by id.

    ``aria-controls`` has to name the list, ``aria-expanded`` has to start
    ``"false"`` (js/nav.js flips it), and the button needs an accessible name
    because its only content is three decorative bars.
    """
    elements = parse(path).elements

    toggles = [
        (attrs, line) for tag, attrs, line in elements
        if tag == "button" and "nav-toggle" in attrs.get("class", "").split()
    ]
    assert len(toggles) == 1, f"{path.name} must have exactly one .nav-toggle"
    attrs, line = toggles[0]
    assert attrs.get("type") == "button", f"{path.name} line {line}: default type submits"
    assert attrs.get("aria-expanded") == "false", f"{path.name} line {line}"
    assert attrs.get("aria-controls") == "site-tabs", f"{path.name} line {line}"
    assert attrs.get("aria-label"), f"{path.name} line {line}: the icon needs a name"

    bars = [
        a for tag, a, _ in elements
        if tag == "span" and "bars" in a.get("class", "").split()
    ]
    assert len(bars) == 1 and bars[0].get("aria-hidden") == "true", (
        f"{path.name}: the three-line glyph is decorative"
    )

    tabs = [
        a for tag, a, _ in elements
        if tag == "ul" and "tabs" in a.get("class", "").split()
    ]
    assert len(tabs) == 1 and tabs[0].get("id") == "site-tabs", (
        f"{path.name}: aria-controls has nothing to point at"
    )


@pytest.mark.parametrize("path", SHIPPED, ids=SHIPPED_IDS)
def test_page_carries_the_logo_at_the_left_of_the_nav(path: Path):
    """The logo is shared chrome now — index.html's masthead band is gone.

    It has to be the nav's first child (the left end of the bar), a link home,
    and it has to carry the accessible name that the old ``<h1>`` held.
    """
    elements = parse(path).elements

    brands = [
        (attrs, line) for tag, attrs, line in elements
        if tag == "a" and "nav-brand" in attrs.get("class", "").split()
    ]
    assert len(brands) == 1, f"{path.name} must have exactly one .nav-brand"
    assert brands[0][0].get("href") == "index.html", f"{path.name}: the logo links home"

    # First element inside the nav's .wrap, i.e. the left end of the bar.
    wrap_at = next(
        i for i, (tag, attrs, _) in enumerate(elements)
        if tag == "div" and "wrap" in attrs.get("class", "").split()
    )
    first_tag, first_attrs, _ = elements[wrap_at + 1]
    assert first_tag == "a" and "nav-brand" in first_attrs.get("class", "").split(), (
        f"{path.name}: the logo must come first in the bar, not after the tabs"
    )

    # The brand's own image: the first <img> after the .nav-brand anchor. The
    # six sub-pages still show the OLD logo lower down as .hero .mark, so this
    # has to be positional rather than a search by src.
    brand_at = next(
        i for i, (tag, attrs, _) in enumerate(elements)
        if tag == "a" and "nav-brand" in attrs.get("class", "").split()
    )
    logo = next(
        attrs for tag, attrs, _ in elements[brand_at:] if tag == "img"
    )
    assert logo.get("src") == NAV_LOGO, f"{path.name}: nav logo is {logo.get('src')!r}"
    assert logo.get("alt") == "Boston Café Bikers", f"{path.name}: the logo needs its name"
    assert (logo.get("width"), logo.get("height")) == ("500", "500"), (
        f"{path.name}: width/height must match the file, or the crop maths shift"
    )


@pytest.mark.parametrize("path", SHIPPED, ids=SHIPPED_IDS)
def test_the_logo_appears_once_per_page(path: Path):
    """One logo per page, and it is the nav's.

    The sub-pages used to repeat it as ``.hero .mark`` above their own ``<h1>``,
    which after the logo moved into the shared bar meant two different logos
    (the new transparent PNG, then the old sky-blue-tiled JPEG) stacked a few
    pixels apart.
    """
    imgs = [
        attrs for tag, attrs, _ in parse(path).elements
        if tag == "img" and "logo" in attrs.get("src", "")
    ]
    assert len(imgs) == 1, (
        f"{path.name} has {len(imgs)} logo images: {[i.get('src') for i in imgs]}"
    )
    assert imgs[0].get("src") == NAV_LOGO
    assert "mark" not in imgs[0].get("class", "").split(), (
        f"{path.name}: the hero mark is gone; the nav brand is the only logo"
    )


@pytest.mark.parametrize("path", SHIPPED, ids=SHIPPED_IDS)
def test_page_has_exactly_one_h1(path: Path):
    """index.html lost its visible ``<h1>`` with the masthead band.

    It keeps a ``.visually-hidden`` one instead, so every page still has
    exactly one — the nav's logo is that heading's visible form.
    """
    h1s = [attrs for tag, attrs, _ in parse(path).elements if tag == "h1"]
    assert len(h1s) == 1, f"{path.name} has {len(h1s)} <h1> elements"


def test_the_landing_page_has_no_masthead_band_left():
    """The logo moved into the nav; the hero band it used to fill is gone."""
    html = (SITE / "index.html").read_text(encoding="utf-8")
    assert 'class="masthead"' not in html
    assert 'class="hero"' not in html
    assert 'class="visually-hidden"' in html


@pytest.mark.parametrize("path", SHIPPED, ids=SHIPPED_IDS)
def test_page_loads_nav_js_render_blocking(path: Path):
    """js/nav.js sets ``html.has-js``, which is what collapses the tabs.

    Deferring it would paint the expanded tab row first and then snap it shut,
    so this one script is deliberately not ``defer``/``async`` — and it has to
    be on every page, including the four that load no other JS.
    """
    scripts = [
        (attrs, line) for tag, attrs, line in parse(path).elements
        if tag == "script" and attrs.get("src") == "js/nav.js"
    ]
    assert len(scripts) == 1, f"{path.name} must load js/nav.js exactly once"
    attrs, line = scripts[0]
    assert "defer" not in attrs and "async" not in attrs, (
        f"{path.name} line {line}: nav.js must run before the first paint"
    )


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
    """FullCalendar is the site's only off-site executable/style resource.

    It is version-pinned on jsDelivr, so its bytes can never legitimately
    change under that URL — an SRI hash turns "the CDN served something else"
    into the designed hand-rolled calendar fallback. MapLibre's split v6 module
    graph is vendored and hash-pinned separately below.
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


@pytest.mark.parametrize("path", PAGES, ids=PAGE_IDS)
def test_page_has_the_security_meta_policies(path: Path):
    """The controls GitHub Pages can enforce from HTML must not drift by page."""
    metas = parse(path).metas
    csp = [
        (attrs, line) for attrs, line in metas
        if attrs.get("http-equiv", "").lower() == "content-security-policy"
    ]
    assert len(csp) == 1, f"{path.name} must carry exactly one CSP meta element"
    assert csp[0][0].get("content") == EXPECTED_CSP, (
        f"{path.name} line {csp[0][1]} has a stale or unexpected CSP"
    )

    referrer = [
        (attrs, line) for attrs, line in metas
        if attrs.get("name", "").lower() == "referrer"
    ]
    assert len(referrer) == 1, (
        f"{path.name} must carry exactly one referrer-policy meta element"
    )
    assert referrer[0][0].get("content") == EXPECTED_REFERRER_POLICY


def test_vendored_maplibre_matches_the_verified_6_10_0_tarball():
    """Pin every runtime/license file because module imports cannot carry SRI."""
    for name, expected in MAPLIBRE_SHA256.items():
        path = MAPLIBRE_VENDOR / name
        assert path.is_file(), f"missing vendored MapLibre file: {path}"
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == expected, f"vendored MapLibre file changed: {path}"

    cafes_html = (SITE / "cafes.html").read_text(encoding="utf-8")
    cafes_js = (SITE / "js" / "cafes.js").read_text(encoding="utf-8")
    assert "vendor/maplibre-gl-6.10.0/maplibre-gl.css" in cafes_html
    assert "../vendor/maplibre-gl-6.10.0/maplibre-gl.mjs" in cafes_js
    assert "maplibre-gl@5.24.0" not in cafes_html


def test_stylesheet_has_no_root_relative_urls():
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"url\(\s*['\"]?/", css) is None


def test_the_nav_is_pinned():
    """Regression guard, not style policing.

    ``position: sticky; top: 0`` on ``.nav`` was added, removed on 2026-08-13
    when the request was re-opened with the opposite intent, and restored with
    the hamburger. Losing it again silently is the failure mode this catches.
    """
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    block = re.search(r"^\.nav\s*\{([^}]*)\}", css, re.MULTILINE)
    assert block, "no .nav rule in styles.css"
    body = block.group(1)
    assert "position: sticky" in body and "top: 0" in body, body
    # The pinned bar covers the top of the viewport, so fragment targets need
    # clearance; index.html's old section-only rule went out with the sticky.
    assert "scroll-margin-top" in css


def test_the_nav_bar_is_twice_the_page_width_and_insets_the_logo():
    """The bar is the site header: wider content box than the page, logo left.

    The espresso band was always full-bleed, so "double the width" is about
    the content inside it — 1360px against the shared .wrap's 680px — and the
    logo keeps two tab-widths of clear space before it.
    """
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    shared = re.search(r"^\.wrap\s*\{([^}]*)\}", css, re.MULTILINE)
    nav = re.search(r"^\.nav \.wrap\s*\{([^}]*)\}", css, re.MULTILINE)
    assert shared and nav
    assert "max-width: 680px" in shared.group(1)
    assert "max-width: 1360px" in nav.group(1), "the nav's content box is double the page's"
    assert "--brand-inset" in nav.group(1) and "padding-left" in nav.group(1)
    assert re.search(r"^\.nav-brand\s*\{", css, re.MULTILINE), "no .nav-brand rule"


def test_the_bare_logo_and_the_sky_bar_stay_a_matched_pair():
    """The logo is drawn with no chip, which only works on the light bar.

    Its #75472e ink is 4.97:1 against #ABD2FF and 2.07:1 against the espresso
    the bar used to be, so putting the dark bar back without restoring a chip
    would hide the logo rather than break it visibly. This fails if either half
    of that pair moves on its own.
    """
    css = (SITE / "styles.css").read_text(encoding="utf-8")

    root = re.search(r"^:root\s*\{(.*?)\}", css, re.MULTILINE | re.DOTALL)
    assert root and f"--sky:      {NAV_BAR_COLOUR}" in root.group(1), (
        f"--sky must be {NAV_BAR_COLOUR}"
    )
    nav = re.search(r"^\.nav\s*\{([^}]*)\}", css, re.MULTILINE)
    assert nav and "background: var(--sky)" in nav.group(1), "the bar must use --sky"

    brand = re.search(r"^\.nav-brand\s*\{([^}]*)\}", css, re.MULTILINE)
    assert brand and "background" not in brand.group(1), (
        "the logo is shown with no background of its own"
    )

    # …and the crop numbers must still match the file the page declares.
    assert (SITE / NAV_LOGO).is_file(), f"missing {NAV_LOGO}"
    window = re.search(r"^\.nav-brand \.logo-window\s*\{([^}]*)\}", css, re.MULTILINE)
    assert window and "263" in window.group(1) and "135" in window.group(1), (
        "the crop window must stay the ink's 263x135 box"
    )


def test_the_nav_spacing_follows_the_reference_header():
    """Spacing copied from transalt.org's header (measured 2026-09-19).

    The three numbers that came off that page are the 3vw side gutter, the
    1.56vw gap between nav items and the 3.1vw gap before the right-hand group.
    They are kept as literal vw values so the rhythm scales the way the
    reference does; the comment in styles.css records the measurements.
    """
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    nav_wrap = re.search(r"^\.nav \.wrap\s*\{([^}]*)\}", css, re.MULTILINE).group(1)
    assert "3vw" in nav_wrap, "side gutter"
    assert "3.1vw" in nav_wrap, "gap before the tab group"
    tabs = re.search(r"^\.nav \.tabs\s*\{([^}]*)\}", css, re.MULTILINE).group(1)
    assert "1.56vw" in tabs, "gap between tabs"
    # The bar grew to ~90px, so fragment targets need more clearance than the
    # 4rem that cleared the old 58px bar.
    assert "scroll-margin-top: 6rem" in css


def test_the_hamburger_only_hides_the_tabs_when_the_script_ran():
    """No-JS pages must not end up with an invisible menu.

    Every rule that collapses or reveals the tab panel is gated on
    ``html.has-js`` (set by js/nav.js), so without the script the nav stays the
    plain wrapping row it was before the hamburger existed.
    """
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    breakpoint_ = "(max-width: 559.98px)"

    lines = css.splitlines()
    opens = [i for i, ln in enumerate(lines) if ln.startswith(f"@media {breakpoint_}")]
    assert len(opens) == 1, f"expected one @media {breakpoint_} block"
    start = opens[0]
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")

    selectors = [
        ln.strip()[:-1].strip() for ln in lines[start + 1:end] if ln.strip().endswith("{")
    ]
    assert selectors, "the phone block declares nothing"
    # Anything that touches the tabs or the button has to be gated. Plain
    # layout rules for the bar itself (the brand's gutter) may be ungated:
    # they apply with or without JS, which is the point.
    for selector in selectors:
        if ".tabs" in selector or ".nav-toggle" in selector:
            assert selector.startswith("html.has-js"), f"ungated phone-nav rule: {selector}"

    # The toggle's own base rule is display:none, so a wide viewport (and a
    # scriptless one) never shows it.
    toggle = re.search(r"^\.nav-toggle\s*\{([^}]*)\}", css, re.MULTILINE)
    assert toggle and "display: none" in toggle.group(1)

    # …and the phone breakpoint matches the one nav.js watches.
    assert breakpoint_ in (SITE / "js" / "nav.js").read_text(encoding="utf-8")
