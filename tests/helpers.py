"""Shared fixture data and offline network stand-ins for the pytest suite.

Every test file used to declare its own copy of the fixture paths, the Eastern
timezone and (in two files) the same polyline/geocode table and stub functions.
They live here once; tests/conftest.py puts ``scripts/`` on sys.path so the
modules under test import without each file repeating that prologue too.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures"
FIXTURE = FIXTURES / "sample.ics"
EVENT_PAGE = FIXTURES / "event-page.html"
EVENT_PAGE_HTML = EVENT_PAGE.read_text(encoding="utf-8")
TILE = (FIXTURES / "tile.png").read_bytes()
EASTERN = ZoneInfo("America/New_York")

# A short Boston polyline, standing in for BRouter's geometry.
GEOMETRY = [
    (42.3355, -71.1506),
    (42.3400, -71.1600),
    (42.3500, -71.1700),
    (42.3668, -71.1868),
]

# What the fixture page's maps.app.goo.gl custom fields resolve to: one real
# directions URL (whose geocode tokens give the route its coordinates) and one
# plain place pin.
RESOLVED_LINKS = {
    "https://maps.app.goo.gl/RouteShortLink1?g_st=ic": (
        "https://maps.google.com/?saddr=Bluebikes,+Cleveland+Circle,+Boston,+MA"
        "&daddr=Tatte+Bakery,+Boston,+MA&dirflg=b"
        "&geocode=FTf9hQId6lPC-ylRGlXiU3jjiTGY1w16bG3cRw%3D%3D;"
        "FWd3hgIdjcbB-ykdbpobJHnjiTGRSDuTYi0pzA%3D%3D"
    ),
    "https://maps.app.goo.gl/PlaceShortLink1?g_st=ic": (
        "https://maps.google.com?q=Bluebikes,+Cleveland+Circle&entry=gps"
    ),
}

# --- the stubbed network ---------------------------------------------------


def fake_page(url: str) -> str:
    """Every ride's Partiful page is the fixture page."""
    return EVENT_PAGE_HTML


def fake_resolve(url: str) -> str:
    if url in RESOLVED_LINKS:
        return RESOLVED_LINKS[url]
    raise ValueError(f"cannot resolve {url}")


def fake_length(points: list) -> float:
    """Stand-in for BRouter's distance: 1 km per leg."""
    return 1000.0 * (len(points) - 1)


def fake_geometry(points: list) -> list:
    return GEOMETRY


def fake_tile(zoom: int, x: int, y: int) -> bytes:
    return TILE


def fake_geocode(url: str) -> list:
    return [{"lat": "42.348765", "lon": "-71.123456"}]


def no_sleep(_seconds) -> None:
    pass


SEAMS = {
    "fetch_page": fake_page,
    "resolve_link": fake_resolve,
    "fetch_length": fake_length,
    "fetch_geometry": fake_geometry,
    "fetch_tile": fake_tile,
    "geocode_fetch": fake_geocode,
    "geocode_sleep": no_sleep,
}
