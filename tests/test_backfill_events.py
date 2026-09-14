"""Tests for scripts/backfill_events.py — rides listed by id, built from their
public Partiful event pages. Offline: every page here is built in the test.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import backfill_events  # noqa: E402
import fetch_rides  # noqa: E402

EASTERN = ZoneInfo("America/New_York")
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sample.ics"
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=EASTERN)

# What the fixture's maps.app.goo.gl route link resolves to — the same
# directions URL tests/test_sync.py uses, so the route comes out with stops.
RESOLVED = (
    "https://maps.google.com/?saddr=Bluebikes,+Cleveland+Circle,+Boston,+MA"
    "&daddr=Tatte+Bakery,+Boston,+MA&dirflg=b"
    "&geocode=FTf9hQId6lPC-ylRGlXiU3jjiTGY1w16bG3cRw%3D%3D;"
    "FWd3hgIdjcbB-ykdbpobJHnjiTGRSDuTYi0pzA%3D%3D"
)
ROUTE_LINK = "https://maps.app.goo.gl/RouteShortLink1?g_st=ic"
PHOTO = "https://firebasestorage.googleapis.com/v0/b/p/o/anniversary.jpg?alt=media"


def event(**overrides) -> dict:
    """The `event` object of a Partiful page, shaped like the real one."""
    base = {
        "id": "Rm4Lv7N60seCePrNfg79",
        "title": "Boston Cafe Bikers        1 year anniversary",
        "status": "PUBLISHED",
        "startDate": "2026-09-07T14:00:00.000Z",
        "endDate": None,
        "timezone": "America/New_York",
        "location": "Tatte Bakery, 1003 Beacon St, Brookline, MA 02446",
        "description": "One year!\n\n\n\nCoffee after.",
        "image": {"url": PHOTO, "blurHash": "L6Pj0^~q00?b~q%MIVt7~q-;t7RP"},
        "customFields": [{"icon": "link", "value": "Estimated Route", "url": ROUTE_LINK}],
    }
    base.update(overrides)
    return base


def page(ev: dict) -> str:
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"event": ev}}})
        + "</script>"
    )


def resolve(url: str) -> str:
    if url == ROUTE_LINK:
        return RESOLVED
    raise ValueError(f"cannot resolve {url}")


def length(points: list) -> float:
    return 1000.0 * (len(points) - 1)


# --- the record is the feed's record ---------------------------------------


def test_the_record_carries_exactly_the_keys_the_feed_writes():
    """archive_events can't tell a backfilled ride from a fed one — by design.

    Pinned against a ride parsed from the ICS fixture, so a key added to one
    side has to be added to the other.
    """
    ride = backfill_events.ride_from_event(event())
    fed = fetch_rides.parse_events(FIXTURE.read_bytes(), now=NOW)[0]
    assert set(ride) == set(fed)


def test_the_start_is_the_pages_utc_instant_spelled_in_eastern():
    ride = backfill_events.ride_from_event(event())
    assert ride["start"] == "2026-09-07T10:00:00-04:00"
    assert ride["date_display"] == "Monday, September 7"
    assert ride["time_display"] == "10:00 am"
    # Standard time too — the offset follows the date, never a fixed -04:00.
    winter = backfill_events.ride_from_event(event(startDate="2026-01-11T16:00:00.000Z"))
    assert winter["start"] == "2026-01-11T11:00:00-05:00"
    assert winter["time_display"] == "11:00 am"


def test_the_end_is_optional_like_the_feeds_dtend():
    assert backfill_events.ride_from_event(event())["end"] is None
    ride = backfill_events.ride_from_event(event(endDate="2026-09-07T16:30:00.000Z"))
    assert ride["end"] == "2026-09-07T12:30:00-04:00"


def test_the_title_is_cleaned_the_way_the_feeds_is():
    assert backfill_events.ride_from_event(event())["title"] == "Boston Cafe Bikers 1 year anniversary"
    assert backfill_events.ride_from_event(event(title="Linger @ Linger | Partiful"))["title"] == "Linger @ Linger"
    assert backfill_events.ride_from_event(event(title=""))["title"] == "Café ride"
    assert backfill_events.ride_from_event(event(title=None))["title"] == "Café ride"


def test_the_description_collapses_blank_runs():
    assert backfill_events.ride_from_event(event())["description"] == "One year!\n\nCoffee after."
    assert backfill_events.ride_from_event(event(description=None))["description"] == ""


def test_the_rsvp_link_is_the_event_page():
    assert backfill_events.ride_from_event(event())["rsvp_url"] == (
        "https://partiful.com/e/Rm4Lv7N60seCePrNfg79"
    )


def test_photo_and_routes_wait_for_the_whole_page():
    ride = backfill_events.ride_from_event(event())
    assert ride["image"] is None
    assert ride["routes"] is None


# --- the three readings of Location ------------------------------------------


def test_an_address_is_the_location():
    ride = backfill_events.ride_from_event(event())
    assert ride["location"] == "Tatte Bakery, 1003 Beacon St, Brookline, MA 02446"
    assert ride["location_hidden"] is False
    assert ride["location_url"] is None


def test_a_bare_maps_link_becomes_location_url():
    link = "https://maps.app.goo.gl/SHnpKYA1WpN91ArZ9?g_st=ic"
    ride = backfill_events.ride_from_event(
        event(location=link, locationInfo={"type": "freeform", "value": link})
    )
    assert ride["location"] is None
    assert ride["location_hidden"] is False
    assert ride["location_url"] == link


def test_an_address_held_back_until_rsvp_reads_as_hidden():
    # The public page then has no `location` at all, only the neighbourhood.
    ride = backfill_events.ride_from_event(
        event(
            location=None,
            locationInfo={"type": "structured", "mapsInfo": {"approximateLocation": "Watertown, MA"}},
        )
    )
    assert ride["location"] is None
    assert ride["location_hidden"] is True
    assert ride["location_url"] is None


def test_no_location_at_all_is_just_no_location():
    ride = backfill_events.ride_from_event(event(location="", locationInfo=None))
    assert ride["location"] is None
    assert ride["location_hidden"] is False


# --- what builds nothing -----------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "CANCELLED"},
        {"status": "canceled"},
        {"status": "DELETED"},
        {"id": ""},
        {"id": None},
        {"startDate": None},
        {"startDate": "someday"},
    ],
    ids=["cancelled", "canceled-us", "deleted", "empty-id", "no-id", "no-start", "bad-start"],
)
def test_dead_or_incomplete_events_build_nothing(overrides):
    assert backfill_events.ride_from_event(event(**overrides)) is None


def test_an_unknown_status_is_taken_at_face_value():
    assert backfill_events.ride_from_event(event(status="DRAFT")) is not None
    assert backfill_events.ride_from_event(event(status=None)) is not None


def test_not_an_event_object_builds_nothing():
    assert backfill_events.ride_from_event(None) is None
    assert backfill_events.ride_from_event("event") is None
    assert backfill_events.ride_from_event([]) is None


# --- the whole page: photo and routes ----------------------------------------


def test_ride_from_page_adds_the_photo_and_the_routes():
    ride = backfill_events.ride_from_page(page(event()), resolve, length)
    assert ride["uid"] == "Rm4Lv7N60seCePrNfg79"
    assert ride["image"] == PHOTO
    assert [route["label"] for route in ride["routes"]] == ["Estimated Route"]
    route = ride["routes"][0]
    assert route["url"] == ROUTE_LINK
    assert route["start"] == "Bluebikes, Cleveland Circle, Boston, MA"
    assert route["distance_m"] == 1000


def test_a_page_with_no_routes_still_marks_them_checked():
    """[] not None: the archive's never-checked queue must not pick it up."""
    ride = backfill_events.ride_from_page(page(event(customFields=[])), resolve, length)
    assert ride["routes"] == []


def test_a_page_without_an_event_builds_nothing():
    assert backfill_events.ride_from_page("<html><body>nothing</body></html>", resolve) is None
    assert backfill_events.ride_from_page(page({"title": "no id, no start"}), resolve) is None


# --- timestamps --------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-08-23T15:00:00.000Z", "2026-08-23T11:00:00-04:00"),
        ("2026-08-23T15:00:00Z", "2026-08-23T11:00:00-04:00"),
        ("2026-08-23T15:00:00.123456Z", "2026-08-23T11:00:00-04:00"),
        ("2026-08-23T11:00:00-04:00", "2026-08-23T11:00:00-04:00"),
        ("2026-08-23T15:00:00", "2026-08-23T11:00:00-04:00"),  # naive → UTC
        ("  2026-12-06T16:00:00.000Z ", "2026-12-06T11:00:00-05:00"),
    ],
)
def test_parse_instant_reads_every_shape_the_page_uses(value, expected):
    assert backfill_events.parse_instant(value).isoformat() == expected


@pytest.mark.parametrize("value", ["", "   ", "yesterday", None, 42, {"seconds": 1}])
def test_parse_instant_rejects_anything_else(value):
    assert backfill_events.parse_instant(value) is None


# --- the sidecar -------------------------------------------------------------


def test_a_missing_list_lists_nothing(tmp_path):
    assert backfill_events.load_backfill_events(tmp_path / "none.json") == []


def test_the_list_is_the_keys_in_file_order(tmp_path):
    path = tmp_path / "backfill_events.json"
    path.write_text(
        json.dumps({" zNzLp290WKRSyUhpwGfI ": "Lovestruck on bike", "": "blank", "b2": "next"}),
        encoding="utf-8",
    )
    assert backfill_events.load_backfill_events(path) == ["zNzLp290WKRSyUhpwGfI", "b2"]


def test_the_committed_list_is_well_formed():
    """The real sidecar parses — a broken one would stop every sync."""
    assert isinstance(backfill_events.load_backfill_events(), list)


@pytest.mark.parametrize("text", ['["not", "an", "object"]', "{not json"])
def test_a_broken_list_is_a_feed_error_like_the_other_sidecars(tmp_path, text):
    path = tmp_path / "backfill_events.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(fetch_rides.FeedError):
        backfill_events.load_backfill_events(path)


def test_pending_keeps_order_and_drops_known_and_repeated_ids():
    assert backfill_events.pending(["a", "b", "a", "c", "b"], {"b"}) == ["a", "c"]
    assert backfill_events.pending([], set()) == []


# --- backfill(): the bounded, fail-soft fetch --------------------------------


def test_backfill_fetches_at_most_limit_and_counts_what_it_could_not_read():
    calls = []
    pages = {
        "https://partiful.com/e/a": page(event(id="a", startDate="2025-10-05T14:00:00.000Z")),
        "https://partiful.com/e/c": page(event(id="c")),
    }

    def fetch(url):
        calls.append(url)
        if url in pages:
            return pages[url]
        raise fetch_rides.requests.RequestException("nope")

    rides, missed = backfill_events.backfill(["a", "b", "c"], fetch, resolve, length, limit=2)
    assert [ride["uid"] for ride in rides] == ["a"]
    assert missed == ["b"]
    assert calls == ["https://partiful.com/e/a", "https://partiful.com/e/b"], "c waits for next run"
    # Ready for the archive: the display fields are already derived.
    assert rides[0]["grace_until"] == "2025-10-05T11:00:00-04:00"
    assert rides[0]["place_name"] == "Tatte Bakery"
    assert rides[0]["routes"][0]["start_name"] == "Cleveland Circle"


def test_backfill_limit_zero_fetches_nothing():
    def fetch(url):
        raise AssertionError("no fetch expected")

    assert backfill_events.backfill(["a", "b"], fetch, resolve, length, limit=0) == ([], [])


def test_a_page_that_is_not_an_event_is_missed_not_fatal():
    rides, missed = backfill_events.backfill(
        ["a"], lambda url: "<html>gone</html>", resolve, length
    )
    assert rides == []
    assert missed == ["a"]


def test_an_id_that_is_not_an_event_id_is_missed():
    """A descriptive UID has no event page to fetch — never a request."""
    def fetch(url):
        raise AssertionError("no fetch expected")

    assert backfill_events.backfill(["evt-x@partiful.com"], fetch, resolve, length) == (
        [],
        ["evt-x@partiful.com"],
    )
