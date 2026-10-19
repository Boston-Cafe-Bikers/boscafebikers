"""Tests for scripts/fetch_rides.py — always offline, always on the fixture."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import fetch_rides  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sample.ics"
EASTERN = ZoneInfo("America/New_York")
# Pinned "now": after the past event, well before the 2030 rides.
NOW = datetime(2025, 1, 1, 12, 0, tzinfo=EASTERN)


@pytest.fixture(scope="module")
def feed_bytes() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(scope="module")
def rides(feed_bytes: bytes) -> list[dict]:
    return fetch_rides.parse_events(feed_bytes, now=NOW)


def test_keeps_only_the_two_future_rides(rides):
    assert [ride["uid"] for ride in rides] == [
        "evt-future-charles-loop@partiful.com",
        "evt-future-minuteman@partiful.com",
    ]


def test_past_event_is_dropped(rides):
    assert all("evt-past" not in ride["uid"] for ride in rides)


def test_cancelled_event_is_dropped(rides):
    """The Blue Hills ride is in 2030 but STATUS:CANCELLED."""
    assert all("blue-hills" not in ride["uid"] for ride in rides)


def test_sorted_by_start(rides):
    starts = [ride["start"] for ride in rides]
    assert starts == sorted(starts)


def test_rsvp_urls_extracted_despite_line_folding(rides):
    assert [ride["rsvp_url"] for ride in rides] == [
        "https://partiful.com/e/3mTnV6xJaQ9wLpEr",
        "https://partiful.com/e/5cXyB8kFgH2dNqUw",
    ]


def test_rsvp_line_stripped_from_description(rides):
    for ride in rides:
        assert "RSVP:" not in ride["description"]
        assert "partiful.com" not in ride["description"]
        assert ride["description"]


@pytest.mark.parametrize(
    "description, expected",
    [
        ("Ride then coffee.\n\nRSVP: https://partiful.com/e/abc123", "https://partiful.com/e/abc123"),
        ("rsvp:   https://partiful.com/e/abc123", "https://partiful.com/e/abc123"),
        ("See you there (RSVP: https://partiful.com/e/abc123).", "https://partiful.com/e/abc123"),
        ("RSVP at https://partiful.com/e/abc123", "https://partiful.com/e/abc123"),
        ("View this event on Partiful at https://partiful.com/e/abc123\n\nCome!", "https://partiful.com/e/abc123"),
        ("No link here at all.", None),
        ("", None),
    ],
)
def test_extract_rsvp_url(description, expected):
    assert fetch_rides.extract_rsvp_url(description) == expected


@pytest.mark.parametrize(
    "uid, expected",
    [
        ("TskpmnYxmCi7eGn1mb0G", "https://partiful.com/e/TskpmnYxmCi7eGn1mb0G"),
        ("evt-future-charles-loop@partiful.com", None),
        ("noend@example.com", None),
        ("https://partiful.com/e/abc123", None),
        ("", None),
        (None, None),
    ],
)
def test_derive_partiful_url(uid, expected):
    assert fetch_rides.derive_partiful_url(uid) == expected


def test_timezone_is_eastern_with_correct_offsets(rides):
    charles, minuteman = rides
    # Both fixture rides are in EDT (UTC-4), not UTC and not the runner's tz.
    assert charles["start"] == "2030-06-22T09:30:00-04:00"
    assert minuteman["start"] == "2030-07-06T10:00:00-04:00"
    assert datetime.fromisoformat(charles["start"]).utcoffset().total_seconds() == -4 * 3600


def test_end_time_extracted(rides):
    """The fixture carries DTEND on every event; it should flow through as `end`."""
    charles, minuteman = rides
    assert charles["end"] == "2030-06-22T12:00:00-04:00"
    assert minuteman["end"] == "2030-07-06T14:00:00-04:00"
    assert datetime.fromisoformat(charles["end"]).utcoffset().total_seconds() == -4 * 3600


def _real_feed(data: bytes):
    """Parse an inline feed at a fixed 'now', like the sync bot would."""
    return fetch_rides.parse_events(data, now=datetime(2025, 1, 1, tzinfo=EASTERN))


def test_real_partiful_rsvp_at_phrasing():
    """Current Partiful exports say 'RSVP at <url>' and use a bare UID."""
    data = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:TskpmnYxmCi7eGn1mb0G\r\n"
        b"DTSTART;TZID=America/New_York:20300808T180000\r\n"
        b"SUMMARY:Dinner Party\r\n"
        b"DESCRIPTION:RSVP at https://partiful.com/e/TskpmnYxmCi7eGn1mb0G\\n\\n"
        b"You are cordially invited. Just showing up for the food is valid!\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    rides = _real_feed(data)
    assert len(rides) == 1
    assert rides[0]["rsvp_url"] == "https://partiful.com/e/TskpmnYxmCi7eGn1mb0G"
    assert "RSVP at" not in rides[0]["description"]
    assert "partiful.com" not in rides[0]["description"]
    assert rides[0]["description"].startswith("You are cordially invited.")


def test_real_partiful_view_event_phrasing():
    """Some Partiful exports say 'View this event on Partiful at <url>'."""
    data = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:LjbmPGgu1Z3Zc7bJTibI\r\n"
        b"DTSTART;TZID=America/New_York:20300622T093000\r\n"
        b"SUMMARY:Celebration\r\n"
        b"DESCRIPTION:View this event on Partiful at https://partiful.com/e/LjbmPGgu1Z3Zc7bJTibI\\n\\n"
        b"Come celebrate with us.\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    rides = _real_feed(data)
    assert len(rides) == 1
    assert rides[0]["rsvp_url"] == "https://partiful.com/e/LjbmPGgu1Z3Zc7bJTibI"
    assert "Partiful at" not in rides[0]["description"]
    assert rides[0]["description"].startswith("Come celebrate with us.")


def test_rsvp_url_derived_from_bare_uid_without_link():
    """No link in the text at all → a bare Partiful UID still yields the page."""
    data = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:Z50H1Zr8gbbyZtSyH75e\r\n"
        b"DTSTART;TZID=America/New_York:20300622T093000\r\n"
        b"SUMMARY:No link here\r\n"
        b"DESCRIPTION:Just ride details. See you at the start.\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    rides = _real_feed(data)
    assert len(rides) == 1
    assert rides[0]["rsvp_url"] == "https://partiful.com/e/Z50H1Zr8gbbyZtSyH75e"


def test_rsvp_url_not_derived_from_suffixed_uid():
    """A descriptive '<name>@partiful.com' UID must not be treated as an id."""
    data = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:evt-future-charles-loop@partiful.com\r\n"
        b"DTSTART;TZID=America/New_York:20300622T093000\r\n"
        b"SUMMARY:No RSVP line\r\n"
        b"DESCRIPTION:No link anywhere here.\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    rides = _real_feed(data)
    assert len(rides) == 1
    assert rides[0]["rsvp_url"] is None


def test_clean_title_strips_partiful_suffix():
    """Partiful exports append ' | Partiful' to every title; the site shouldn't show it."""
    assert (
        fetch_rides._clean_title("The journey is really the best part | Partiful")
        == "The journey is really the best part"
    )
    assert fetch_rides._clean_title("Post PMC Celebration!! | Partiful") == "Post PMC Celebration!!"
    assert fetch_rides._clean_title("Salt Bread and Coffee pop-up | Partiful") == "Salt Bread and Coffee pop-up"
    assert fetch_rides._clean_title("Café Ride | partiful") == "Café Ride"  # case-insensitive
    assert fetch_rides._clean_title("No suffix here") == "No suffix here"
    assert fetch_rides._clean_title("") == ""


def test_clean_title_collapses_whitespace():
    """Real exports carry stray runs of spaces; collapse them to one."""
    assert (
        fetch_rides._clean_title("Boston Cafe Bikers        Ice Cream Crawl | Partiful")
        == "Boston Cafe Bikers Ice Cream Crawl"
    )
    assert fetch_rides._clean_title("  Leading and  trailing  ") == "Leading and trailing"


def test_clean_description_collapses_blank_lines():
    """3+ newlines (e.g. left after the stripped invite line) read as one break."""
    dirty = "RSVP at https://partiful.com/e/abc\n\nYou are invited!\n\n\n\nBring a lock."
    assert fetch_rides._clean_description(dirty) == "You are invited!\n\nBring a lock."


def test_clean_description_keeps_single_paragraph_break():
    """One blank line between paragraphs is legitimate prose, not cruft."""
    assert (
        fetch_rides._clean_description("Meet at the café.\n\nThen we ride.")
        == "Meet at the café.\n\nThen we ride."
    )
    assert fetch_rides._clean_description("Just prose.") == "Just prose."


def test_real_partiful_title_suffix_stripped():
    """A real Partiful-style title comes out clean end-to-end."""
    data = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:gRzH7L3EoMRl3q8pl7OS\r\n"
        b"DTSTART;TZID=America/New_York:20300622T093000\r\n"
        b"SUMMARY:The journey is really the best part | Partiful\r\n"
        b"DESCRIPTION:View this event on Partiful at https://partiful.com/e/gRzH7L3EoMRl3q8pl7OS\\n\\n"
        b"Yippee! Another Saturday, another ride.\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    rides = _real_feed(data)
    assert len(rides) == 1
    assert rides[0]["title"] == "The journey is really the best part"
    assert "Partiful" not in rides[0]["title"]
    assert rides[0]["description"] == "Yippee! Another Saturday, another ride."


def test_fixture_title_suffix_stripped(rides):
    """The fixture's Charles title carries ' | Partiful'; it must come out clean."""
    charles = rides[0]
    assert charles["title"] == "Charles River Loop → Tatte"
    assert charles["title"].endswith("Partiful") is False


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Location available once RSVP'd", ("", True, None)),
        ("location available once rsvp'd", ("", True, None)),  # case-insensitive
        ("Location available after RSVP", ("", True, None)),   # variant wording
        (
            "Tatte Bakery & Café, 1003 Beacon St, Brookline, MA 02446",
            ("Tatte Bakery & Café, 1003 Beacon St, Brookline, MA 02446", False, None),
        ),
        ("   ", ("", False, None)),
        ("", ("", False, None)),
        # The organizer sometimes pastes the meeting point's map link into
        # Partiful's Location field; that is a link, not an address.
        (
            "https://maps.app.goo.gl/7zBmEn5ZTHEhJtSZ7",
            ("", False, "https://maps.app.goo.gl/7zBmEn5ZTHEhJtSZ7"),
        ),
        (
            "  https://maps.app.goo.gl/KafN4kidaBpozyew9?g_st=ic  ",
            ("", False, "https://maps.app.goo.gl/KafN4kidaBpozyew9?g_st=ic"),
        ),
        # Only a *bare* URL: prose around a link is still a location.
        (
            "Meet at https://maps.app.goo.gl/abc",
            ("Meet at https://maps.app.goo.gl/abc", False, None),
        ),
    ],
)
def test_clean_location(raw, expected):
    assert fetch_rides._clean_location(raw) == expected


def test_a_pasted_map_link_becomes_location_url():
    """A Location that is just a link renders as a link, not as raw text."""
    data = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:M4psL0c8Zc7bJTibI\r\n"
        b"DTSTART;TZID=America/New_York:20300808T180000\r\n"
        b"SUMMARY:Better Buffers & Bagels\r\n"
        b"LOCATION:https://maps.app.goo.gl/7zBmEn5ZTHEhJtSZ7\r\n"
        b"DESCRIPTION:Meet at the dock.\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    rides = fetch_rides.parse_events(data, now=datetime(2029, 1, 1, tzinfo=EASTERN))
    assert len(rides) == 1
    assert rides[0]["location"] is None
    assert rides[0]["location_url"] == "https://maps.app.goo.gl/7zBmEn5ZTHEhJtSZ7"
    # It is not the hidden-address placeholder — the address just isn't one.
    assert rides[0]["location_hidden"] is False


def test_ordinary_rides_carry_a_null_location_url(rides):
    """The key is always present, like location_hidden — never missing."""
    charles, minuteman = rides
    assert charles["location_url"] is None
    assert minuteman["location_url"] is None
    assert all("location_url" in ride for ride in rides)


def test_hidden_placeholder_cleaned_from_fixture(rides):
    """Partiful hides some addresses until RSVP; the placeholder must not leak."""
    charles, minuteman = rides
    assert charles["location"] == "Tatte Bakery & Café, 1003 Beacon St, Brookline, MA 02446"
    assert charles["location_hidden"] is False
    assert minuteman["location"] is None
    assert minuteman["location_hidden"] is True
    # The placeholder text never reaches events.json / the site.
    assert all("RSVP'd" not in (ride["location"] or "") for ride in rides)


def test_real_partiful_hidden_location():
    """A hidden address exports as the placeholder; parse it to null + flag."""
    data = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:HddenL0c8Zc7bJTibI\r\n"
        b"DTSTART;TZID=America/New_York:20300808T180000\r\n"
        b"SUMMARY:Salt Bread and Coffee pop-up\r\n"
        b"LOCATION:Location available once RSVP'd\r\n"
        b"DESCRIPTION:RSVP at https://partiful.com/e/HddenL0c8Zc7bJTibI\\n\\n"
        b"Ride with us from Boston Common.\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    rides = fetch_rides.parse_events(data, now=datetime(2029, 1, 1, tzinfo=EASTERN))
    assert len(rides) == 1
    assert rides[0]["location"] is None
    assert rides[0]["location_hidden"] is True
    assert rides[0]["location_url"] is None


def test_missing_dtend_yields_null_end():
    """A feed event with no DTEND must still parse, with `end` null."""
    data = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:noend@example.com\r\n"
        b"DTSTART;TZID=America/New_York:20300101T100000\r\n"
        b"SUMMARY:No End\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    rides = fetch_rides.parse_events(data, now=datetime(2029, 1, 1, tzinfo=EASTERN))
    assert len(rides) == 1
    assert rides[0]["uid"] == "noend@example.com"
    assert rides[0]["end"] is None


def test_display_strings_are_precomputed(rides):
    charles, minuteman = rides
    assert charles["date_display"] == "Saturday, June 22"
    assert charles["time_display"] == "9:30 am"
    assert minuteman["date_display"] == "Saturday, July 6"
    assert minuteman["time_display"] == "10:00 am"


def test_non_ascii_survives_the_round_trip(rides):
    assert "→" in rides[0]["title"]
    assert "Café" in rides[0]["location"]


# --- the grace hour ---------------------------------------------------------
# People turn up late and still catch the group, so a ride stays in the
# *upcoming* export until GRACE_PERIOD after its start. The Charles ride starts
# 2030-06-22 09:30 ET and (deliberately, for the DTEND test below) runs to 12:00.
CHARLES = "evt-future-charles-loop@partiful.com"
CHARLES_START = datetime(2030, 6, 22, 9, 30, tzinfo=EASTERN)


def uids_at(feed_bytes: bytes, now: datetime, past: bool = False) -> list[str]:
    return [r["uid"] for r in fetch_rides.parse_events(feed_bytes, now=now, past=past)]


def test_grace_period_is_one_hour():
    assert fetch_rides.GRACE_PERIOD == timedelta(hours=1)


def test_now_boundary_keeps_events_starting_exactly_now(feed_bytes):
    assert len(fetch_rides.parse_events(feed_bytes, now=CHARLES_START)) == 2


def test_a_ride_half_an_hour_old_is_still_upcoming(feed_bytes):
    """The whole point: at 10:00 the 09:30 ride is the one to go and join."""
    now = CHARLES_START + timedelta(minutes=30)
    assert CHARLES in uids_at(feed_bytes, now)
    assert CHARLES not in uids_at(feed_bytes, now, past=True)


def test_a_ride_inside_its_grace_hour_is_still_the_next_ride(feed_bytes):
    """Sorted by start, so the graced ride stays events[0] — the featured card."""
    rides = fetch_rides.parse_events(feed_bytes, now=CHARLES_START + timedelta(minutes=30))
    assert rides[0]["uid"] == CHARLES


def test_exactly_one_hour_after_the_start_is_still_upcoming(feed_bytes):
    """The boundary lands on the upcoming side, as `start >= now` always did."""
    now = CHARLES_START + timedelta(hours=1)
    assert CHARLES in uids_at(feed_bytes, now)
    assert CHARLES not in uids_at(feed_bytes, now, past=True)


def test_one_second_past_the_grace_hour_the_ride_is_past(feed_bytes):
    now = CHARLES_START + timedelta(hours=1, seconds=1)
    assert CHARLES not in uids_at(feed_bytes, now)
    assert CHARLES in uids_at(feed_bytes, now, past=True)


def test_the_grace_hour_ignores_dtend(feed_bytes):
    """Charles has DTEND 12:00 — 2.5h out — and the window is still one hour.

    Only 6 of the 40 real rides carry a DTEND at all and they run 3-10 hours; a
    10-hour one would hold a finished ride at the top of the page all day.
    """
    charles_end = datetime(2030, 6, 22, 12, 0, tzinfo=EASTERN)
    for ride in fetch_rides.parse_events(feed_bytes, now=NOW):
        if ride["uid"] == CHARLES:
            assert ride["end"] == charles_end.isoformat()
    now = CHARLES_START + timedelta(hours=1, minutes=30)  # ride still "running"
    assert CHARLES not in uids_at(feed_bytes, now)
    assert CHARLES in uids_at(feed_bytes, now, past=True)


def test_upcoming_and_past_stay_exact_complements_across_the_grace_hour(feed_bytes):
    """No ride may be in both files, or in neither, at any point in the window."""
    for minutes in (-1, 0, 1, 30, 59, 60, 61, 120):
        now = CHARLES_START + timedelta(minutes=minutes)
        upcoming = set(uids_at(feed_bytes, now))
        past = set(uids_at(feed_bytes, now, past=True))
        assert upcoming & past == set(), minutes
        assert len(upcoming) + len(past) == 3, minutes  # the cancelled one aside


def test_all_events_in_the_past_yields_empty_list(feed_bytes):
    assert fetch_rides.parse_events(feed_bytes, now=datetime(2031, 1, 1, tzinfo=EASTERN)) == []


def test_malformed_feed_raises_feed_error():
    with pytest.raises(fetch_rides.FeedError):
        fetch_rides.parse_events(b"this is not an ics file at all")


def test_build_payload_shape(rides):
    payload = fetch_rides.build_payload(rides, now=NOW)
    assert payload["count"] == 2
    assert payload["updated_at"] == NOW.isoformat()
    assert payload["events"] == rides


def test_image_is_null_without_sidecar(rides):
    """No images passed in → every ride carries image: None."""
    assert all(ride["image"] is None for ride in rides)


def test_image_merged_from_sidecar(feed_bytes):
    images = {"evt-future-charles-loop@partiful.com": "https://example.com/img/charles.jpg"}
    rides = fetch_rides.parse_events(feed_bytes, now=NOW, images=images)
    charles, minuteman = rides
    assert charles["image"] == "https://example.com/img/charles.jpg"
    assert minuteman["image"] is None


# --- Event-page image enrichment -------------------------------------------

EVENT_PAGE = REPO_ROOT / "tests" / "fixtures" / "event-page.html"
CHARLES_IMAGE = (
    "https://firebasestorage.googleapis.com/v0/b/getpartiful.appspot.com/o/"
    "rides%2Fcharles-loop.jpg?alt=media"
)


def test_extract_event_image_from_fixture_page():
    html = EVENT_PAGE.read_text(encoding="utf-8")
    assert fetch_rides._extract_event_image(html) == CHARLES_IMAGE


def test_extract_event_image_string_image():
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"event":{"image":"' + CHARLES_IMAGE + '"}}}}'
        "</script>"
    )
    assert fetch_rides._extract_event_image(html) == CHARLES_IMAGE


def test_extract_event_image_falls_back_to_first_firebase_url():
    """No __NEXT_DATA__ → a Firebase Storage URL anywhere in the page wins."""
    html = (
        '<img src="https://firebasestorage.googleapis.com/v0/b/getpartiful'
        '.appspot.com/o/a%2Fb.jpg?alt=media">'
    )
    assert fetch_rides._extract_event_image(html) == (
        "https://firebasestorage.googleapis.com/v0/b/getpartiful.appspot.com/o/a%2Fb.jpg?alt=media"
    )


def test_extract_event_image_blank_page_is_none():
    assert fetch_rides._extract_event_image("<html><body></body></html>") is None


def _fake_event_page(url: str) -> str:
    if url == "https://partiful.com/e/3mTnV6xJaQ9wLpEr":
        return EVENT_PAGE.read_text(encoding="utf-8")
    raise fetch_rides.requests.RequestException(f"no page at {url}")


# The fixture page's custom fields carry maps.app.goo.gl short links. Resolving
# one is a network call, so every enrich_rides() test injects this instead —
# the suite must stay fully offline.
RESOLVED_LINKS = {
    "https://maps.app.goo.gl/RouteShortLink1?g_st=ic": (
        "https://maps.google.com/?saddr=Bluebikes,+Cleveland+Circle,+Boston,+MA"
        "&daddr=Tatte+Bakery,+Boston,+MA&dirflg=b"
        # Real geocode tokens for those two points (42.335543,-71.150614 and
        # 42.366823,-71.186803) — see maps_geocode_points.
        "&geocode=FTf9hQId6lPC-ylRGlXiU3jjiTGY1w16bG3cRw%3D%3D;"
        "FWd3hgIdjcbB-ykdbpobJHnjiTGRSDuTYi0pzA%3D%3D"
    ),
    "https://maps.app.goo.gl/PlaceShortLink1?g_st=ic": (
        "https://maps.google.com?q=Bluebikes,+Cleveland+Circle&entry=gps"
    ),
}


def _fake_resolve_link(url: str) -> str:
    if url in RESOLVED_LINKS:
        return RESOLVED_LINKS[url]
    raise fetch_rides.requests.RequestException(f"cannot resolve {url}")


def _fake_route_length(points: list) -> float:
    """Stand-in for BRouter: 1 km per leg, so the maths stays checkable."""
    return 1000.0 * (len(points) - 1)


def test_enrich_rides_sets_image_on_fixture_ride(feed_bytes):
    """The sync pipeline over the fixture + a stubbed transport backfills image."""
    rides = fetch_rides.parse_events(feed_bytes, now=NOW)
    assert all(ride["image"] is None for ride in rides)
    assert fetch_rides.enrich_rides(rides, fetch_page=_fake_event_page, resolve_link=_fake_resolve_link, fetch_length=_fake_route_length) == 1
    charles, minuteman = rides
    assert charles["image"] == CHARLES_IMAGE
    assert minuteman["image"] is None  # page fetch failed → soft, stays null


def test_enrich_rides_sidecar_wins(feed_bytes):
    """An explicit ride_images.json entry is never overwritten by enrichment."""
    rides = fetch_rides.parse_events(
        feed_bytes,
        now=NOW,
        images={
            "evt-future-charles-loop@partiful.com": "https://example.com/img/curated.jpg"
        },
    )
    charles, minuteman = rides
    assert fetch_rides.enrich_rides(rides, fetch_page=_fake_event_page, resolve_link=_fake_resolve_link, fetch_length=_fake_route_length) == 0
    assert charles["image"] == "https://example.com/img/curated.jpg"
    assert minuteman["image"] is None


def test_enrich_rides_fetch_failure_is_soft(feed_bytes):
    def boom(url: str) -> str:
        raise fetch_rides.requests.RequestException("nope")

    rides = fetch_rides.parse_events(feed_bytes, now=NOW)
    assert fetch_rides.enrich_rides(rides, fetch_page=boom, resolve_link=_fake_resolve_link, fetch_length=_fake_route_length) == 0
    assert all(ride["image"] is None for ride in rides)


def test_enrich_rides_skips_rides_without_event_page():
    """No rsvp link and a non-id UID → there is no page to fetch."""
    data = (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\nUID:evt-no-link@partiful.com\r\n"
        b"DTSTART;TZID=America/New_York:20300101T100000\r\n"
        b"SUMMARY:No Link\r\n"
        b"DESCRIPTION:No RSVP line at all.\r\n"
        b"END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    rides = fetch_rides.parse_events(data, now=datetime(2029, 1, 1, tzinfo=EASTERN))
    assert rides[0]["rsvp_url"] is None

    def unreachable(url: str) -> str:  # pragma: no cover - must not be called
        raise AssertionError("fetch_page must not be called for a ride with no page")

    assert fetch_rides.enrich_rides(rides, fetch_page=unreachable, resolve_link=_fake_resolve_link, fetch_length=_fake_route_length) == 0
    assert rides[0]["image"] is None


def test_main_enriches_only_on_live_feed(tmp_path, monkeypatch):
    """--ics-file stays offline; only a network feed run reaches event pages."""
    out = tmp_path / "events.json"
    calls = []
    monkeypatch.setattr(
        fetch_rides, "enrich_rides", lambda rides: calls.append(rides) or len(rides)
    )
    assert fetch_rides.main(["--ics-file", str(FIXTURE), "--out", str(out)]) == 0
    assert calls == []


def test_main_enriches_when_feed_comes_from_live_url(tmp_path, monkeypatch):
    out = tmp_path / "events.json"
    enriched = []
    monkeypatch.setattr(
        fetch_rides, "enrich_rides", lambda rides, **kwargs: enriched.append(rides) or 2
    )
    monkeypatch.setattr(fetch_rides, "fetch_ics", lambda url: FIXTURE.read_bytes())
    monkeypatch.setenv("PARTIFUL_ICS_URL", "https://partiful.example.invalid/feed.ics")
    assert fetch_rides.main(["--out", str(out)]) == 0
    assert len(enriched) == 1
    assert [ride["uid"] for ride in enriched[0]] == [
        "evt-future-charles-loop@partiful.com",
        "evt-future-minuteman@partiful.com",
    ]


def test_load_ride_images_missing_file_is_empty(tmp_path):
    assert fetch_rides.load_ride_images(tmp_path / "nope.json") == {}


def test_load_ride_images_valid_object(tmp_path):
    sidecar = tmp_path / "ride_images.json"
    sidecar.write_text('{"abc@partiful.com": "https://example.com/img/a.jpg"}', encoding="utf-8")
    assert fetch_rides.load_ride_images(sidecar) == {
        "abc@partiful.com": "https://example.com/img/a.jpg"
    }


def test_load_ride_images_malformed_raises(tmp_path):
    sidecar = tmp_path / "ride_images.json"
    sidecar.write_text("{not json", encoding="utf-8")
    with pytest.raises(fetch_rides.FeedError):
        fetch_rides.load_ride_images(sidecar)


def test_load_ride_images_non_object_raises(tmp_path):
    sidecar = tmp_path / "ride_images.json"
    sidecar.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(fetch_rides.FeedError):
        fetch_rides.load_ride_images(sidecar)


def test_main_writes_image_from_sidecar(tmp_path):
    sidecar = tmp_path / "ride_images.json"
    sidecar.write_text(
        '{"evt-future-charles-loop@partiful.com": "https://example.com/img/c.jpg"}',
        encoding="utf-8",
    )
    out = tmp_path / "events.json"
    assert fetch_rides.main(
        ["--ics-file", str(FIXTURE), "--ride-images", str(sidecar), "--out", str(out)]
    ) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["events"][0]["image"] == "https://example.com/img/c.jpg"
    assert payload["events"][1]["image"] is None


def test_main_writes_expected_json(tmp_path):
    out = tmp_path / "nested" / "events.json"
    assert fetch_rides.main(["--ics-file", str(FIXTURE), "--out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["count"] == 2
    assert payload["events"][0]["title"] == "Charles River Loop → Tatte"
    assert payload["events"][0]["rsvp_url"].startswith("https://partiful.com/e/")


def test_main_writes_the_precomputed_display_fields(tmp_path):
    """The CLI derives the same fields scripts/sync.py does, in both outputs."""
    out = tmp_path / "events.json"
    past_out = tmp_path / "events-past.json"
    assert fetch_rides.main([
        "--ics-file", str(FIXTURE), "--out", str(out), "--past-out", str(past_out)
    ]) == 0
    charles = json.loads(out.read_text(encoding="utf-8"))["events"][0]
    assert charles["grace_until"] == "2030-06-22T10:30:00-04:00"
    assert charles["place_name"] == "Tatte Bakery & Café"
    assert charles["address"] == "1003 Beacon St, Brookline, MA 02446"
    assert charles["year"] == "2030"
    # Minuteman's address is hidden until you RSVP: no name, no address.
    minuteman = json.loads(out.read_text(encoding="utf-8"))["events"][1]
    assert minuteman["location_hidden"] is True
    assert minuteman["place_name"] is None and minuteman["address"] is None
    # The past export carries them too — that is what the archive absorbs.
    assert json.loads(past_out.read_text(encoding="utf-8"))["events"][0]["year"] == "2024"


def test_main_exits_nonzero_on_malformed_feed(tmp_path, capsys):
    bad = tmp_path / "bad.ics"
    bad.write_text("BEGIN:VCALENDAR\nthis is broken\n", encoding="utf-8")
    out = tmp_path / "events.json"
    assert fetch_rides.main(["--ics-file", str(bad), "--out", str(out)]) == 1
    assert not out.exists()
    assert "fetch_rides:" in capsys.readouterr().err


def test_main_exits_nonzero_on_missing_file(tmp_path):
    assert fetch_rides.main(["--ics-file", str(tmp_path / "nope.ics")]) == 1


def test_main_exits_nonzero_without_env_var(monkeypatch, capsys):
    monkeypatch.delenv("PARTIFUL_ICS_URL", raising=False)
    assert fetch_rides.main([]) == 1
    assert "PARTIFUL_ICS_URL" in capsys.readouterr().err


def test_feed_url_never_appears_in_fetch_errors(monkeypatch):
    """A network failure must not leak the secret URL into the message."""
    secret = "https://partiful.com/secret-feed-token/calendar.ics"

    class Boom(fetch_rides.requests.RequestException):
        def __str__(self) -> str:  # pragma: no cover - defensive
            return f"connection failed for url: {secret}"

    monkeypatch.setattr(
        fetch_rides.requests, "get", lambda *a, **kw: (_ for _ in ()).throw(Boom())
    )
    with pytest.raises(fetch_rides.FeedError) as excinfo:
        fetch_rides.fetch_ics(secret)
    assert secret not in str(excinfo.value)
    assert "secret-feed-token" not in str(excinfo.value)


def test_scrub_removes_urls():
    scrubbed = fetch_rides.scrub("failed: webcal://p.com/a.ics and https://p.com/b")
    assert "p.com" not in scrubbed
    assert "<url redacted>" in scrubbed


# --- past rides: the feed's already-happened events and the archive ---


@pytest.fixture(scope="module")
def past_rides(feed_bytes: bytes) -> list[dict]:
    return fetch_rides.parse_events(feed_bytes, now=NOW, past=True)


def test_past_mode_returns_only_the_past_ride(past_rides):
    assert [ride["uid"] for ride in past_rides] == [
        "evt-past-jamaica-pond@partiful.com"
    ]


def test_past_mode_still_drops_cancelled_events(feed_bytes):
    """The cancelled Blue Hills ride is in 2030 — past-mode from 2031 skips it."""
    later = datetime(2031, 1, 1, 12, 0, tzinfo=EASTERN)
    uids = [r["uid"] for r in fetch_rides.parse_events(feed_bytes, now=later, past=True)]
    assert all("blue-hills" not in uid for uid in uids)
    assert len(uids) == 3  # the past ride plus the two 2030 rides


def test_past_and_upcoming_partition_the_feed(feed_bytes, rides, past_rides):
    """Every non-cancelled event lands in exactly one of the two lists."""
    both = {r["uid"] for r in rides} & {r["uid"] for r in past_rides}
    assert both == set()
    assert len(rides) + len(past_rides) == 3


def test_past_ride_carries_the_same_shape(past_rides):
    ride = past_rides[0]
    assert set(ride) == {
        "uid", "title", "start", "end", "date_display", "time_display",
        "location", "location_hidden", "location_url", "description",
        "rsvp_url", "image", "routes", "hosted_by_group",
    }
    # None, not [] — "never enriched" has to stay distinguishable from
    # "checked, and this ride has no route links" (see archive_events).
    assert ride["routes"] is None
    # None, not False — a never-checked ride must never be filtered out.
    assert ride["hosted_by_group"] is None


def test_main_writes_the_past_file_only_when_asked(tmp_path):
    out = tmp_path / "events.json"
    past_out = tmp_path / "events-past.json"
    assert fetch_rides.main(["--ics-file", str(FIXTURE), "--out", str(out)]) == 0
    assert not past_out.exists()
    assert fetch_rides.main(
        ["--ics-file", str(FIXTURE), "--out", str(out), "--past-out", str(past_out)]
    ) == 0
    payload = json.loads(past_out.read_text(encoding="utf-8"))
    assert payload["count"] == len(payload["events"]) == 1
    assert payload["events"][0]["uid"] == "evt-past-jamaica-pond@partiful.com"


# --- Google Maps route links (event customFields) ---------------------------


def test_route_from_maps_url_reads_saddr_and_daddr():
    route = fetch_rides.route_from_maps_url(
        "https://maps.google.com/?saddr=Cleveland+Circle&daddr=O'Some+Cafe&dirflg=b"
    )
    assert route == {"start": "Cleveland Circle", "end": "O'Some Cafe", "mode": "b"}


def test_route_from_maps_url_reads_the_dir_path_form():
    route = fetch_rides.route_from_maps_url(
        "https://www.google.com/maps/dir/Cleveland+Circle/Tatte+Bakery/@42.3,-71.1,13z"
    )
    assert route == {"start": "Cleveland Circle", "end": "Tatte Bakery"}


def test_a_place_link_is_not_a_route():
    """The 'Start/Bluebikes' pin points at one place — not a route."""
    assert fetch_rides.route_from_maps_url(
        "https://maps.google.com?q=Bluebikes,+Cleveland+Circle&entry=gps"
    ) is None


def test_route_from_maps_url_needs_both_ends():
    assert fetch_rides.route_from_maps_url("https://maps.google.com/?saddr=A") is None
    assert fetch_rides.route_from_maps_url("https://maps.google.com/?daddr=B") is None


def test_is_maps_link_filters_out_other_hosts():
    assert fetch_rides._is_maps_link("https://maps.app.goo.gl/abc")
    assert fetch_rides._is_maps_link("https://www.google.com/maps/dir/a/b")
    assert not fetch_rides._is_maps_link("https://open.spotify.com/playlist/x")
    assert not fetch_rides._is_maps_link("https://www.google.com/search?q=maps")
    assert not fetch_rides._is_maps_link("")


def test_rides_routes_keeps_the_short_link_not_the_resolved_one():
    """The organizer's own link is what belongs behind a Route button."""
    event = fetch_rides._event_from_page(EVENT_PAGE.read_text(encoding="utf-8"))
    routes = fetch_rides.rides_routes(event, _fake_resolve_link, _fake_route_length)
    assert len(routes) == 1
    assert routes[0]["url"] == "https://maps.app.goo.gl/RouteShortLink1?g_st=ic"
    assert routes[0]["label"] == "Estimated Route"
    assert routes[0]["start"] == "Bluebikes, Cleveland Circle, Boston, MA"
    assert routes[0]["end"] == "Tatte Bakery, Boston, MA"
    assert routes[0]["mode"] == "b"


def test_rides_routes_skips_non_maps_links_without_resolving_them():
    """A Spotify link in customFields must not cost a request."""
    event = fetch_rides._event_from_page(EVENT_PAGE.read_text(encoding="utf-8"))

    seen = []

    def watching(url: str) -> str:
        seen.append(url)
        return _fake_resolve_link(url)

    fetch_rides.rides_routes(event, watching, _fake_route_length)
    assert all("spotify" not in url for url in seen)
    assert len(seen) == 2  # the two maps links only


def test_rides_routes_is_soft_on_an_unresolvable_link():
    event = {"customFields": [{"value": "Route", "url": "https://maps.app.goo.gl/gone"}]}

    def boom(url: str) -> str:
        raise fetch_rides.requests.RequestException("nope")

    assert fetch_rides.rides_routes(event, boom, _fake_route_length) == []


def test_rides_routes_on_an_event_with_no_custom_fields():
    assert fetch_rides.rides_routes({}, _fake_resolve_link, _fake_route_length) == []


def test_enrich_rides_sets_routes(feed_bytes):
    rides = fetch_rides.parse_events(feed_bytes, now=NOW)
    assert all(ride["routes"] is None for ride in rides)
    fetch_rides.enrich_rides(rides, fetch_page=_fake_event_page, resolve_link=_fake_resolve_link, fetch_length=_fake_route_length)
    charles, minuteman = rides
    assert [r["label"] for r in charles["routes"]] == ["Estimated Route"]
    # The page fetch failed for this one, so it was never checked — None, not [].
    assert minuteman["routes"] is None


def test_enrich_rides_sets_routes_even_when_the_image_is_already_known(feed_bytes):
    """The sidecar short-circuits the image, not the whole event page."""
    rides = fetch_rides.parse_events(
        feed_bytes,
        now=NOW,
        images={"evt-future-charles-loop@partiful.com": "https://example.invalid/x.jpg"},
    )
    fetch_rides.enrich_rides(rides, fetch_page=_fake_event_page, resolve_link=_fake_resolve_link, fetch_length=_fake_route_length)
    assert rides[0]["image"] == "https://example.invalid/x.jpg"
    assert len(rides[0]["routes"]) == 1


def test_enrich_rides_records_an_empty_route_list_when_there_are_none(feed_bytes):
    """"Checked, and there are no routes" must not read as "never checked"."""
    page = '<script id="__NEXT_DATA__" type="application/json">' \
           '{"props":{"pageProps":{"event":{"id":"x"}}}}</script>'
    rides = fetch_rides.parse_events(feed_bytes, now=NOW)
    fetch_rides.enrich_rides(
        rides, fetch_page=lambda url: page, resolve_link=_fake_resolve_link,
        fetch_length=_fake_route_length,
    )
    assert rides[0]["routes"] == []


# --- route distance ---------------------------------------------------------

# The real geocode tokens for 42.335543,-71.150614 and 42.366823,-71.186803.
GEOCODE_PAIR = (
    "FTf9hQId6lPC-ylRGlXiU3jjiTGY1w16bG3cRw==;"
    "FWd3hgIdjcbB-ykdbpobJHnjiTGRSDuTYi0pzA=="
)


def test_maps_geocode_points_decodes_google_tokens():
    points = fetch_rides.maps_geocode_points({"geocode": [GEOCODE_PAIR]})
    assert points == [(42.335543, -71.150614), (42.366823, -71.186803)]


def test_maps_geocode_points_without_a_token():
    assert fetch_rides.maps_geocode_points({}) == []
    assert fetch_rides.maps_geocode_points({"geocode": [""]}) == []


def test_maps_geocode_points_rejects_a_partial_read():
    """One bad token drops them all — a route must never skip a stop silently."""
    good = GEOCODE_PAIR.split(";")[0]
    assert fetch_rides.maps_geocode_points({"geocode": [good + ";not-a-token"]}) == []


def test_maps_geocode_points_rejects_an_unexpected_layout():
    """A blob whose field tags aren't 0x15 / 0x1d isn't a coordinate pair."""
    import base64
    blob = base64.urlsafe_b64encode(b"\x09" + b"\x00" * 12).decode().rstrip("=")
    assert fetch_rides.maps_geocode_points({"geocode": [blob]}) == []


def test_route_carries_points_when_the_counts_line_up():
    route = fetch_rides.route_from_maps_url(
        "https://maps.google.com/?saddr=A&daddr=B&geocode=" + GEOCODE_PAIR
    )
    assert route["points"] == [[42.335543, -71.150614], [42.366823, -71.186803]]


def test_route_drops_points_when_they_do_not_match_the_stops():
    """Two tokens, three stops → don't guess which stop is which."""
    route = fetch_rides.route_from_maps_url(
        "https://maps.google.com/?saddr=A&daddr=B to:C&geocode=" + GEOCODE_PAIR
    )
    assert "points" not in route


def test_route_splits_waypoints_out_of_daddr():
    route = fetch_rides.route_from_maps_url(
        "https://maps.google.com/?saddr=JP Licks&daddr=Gracie's to:Honeycomb to:Speedway"
    )
    assert route["start"] == "JP Licks"
    assert route["end"] == "Speedway"
    assert route["via"] == ["Gracie's", "Honeycomb"]


def test_a_two_point_route_has_no_via():
    route = fetch_rides.route_from_maps_url("https://maps.google.com/?saddr=A&daddr=B")
    assert "via" not in route


# --- routes entered backwards (café -> Bluebikes dock) ----------------------
# Rides always start at a Bluebikes dock, so a route whose end is the dock was
# built the wrong way round in Google Maps.

DOCK = "Bluebikes, Bunker Hill Mall, Main St at Austin St, Boston, MA 02129"
CAFE = "Localito, Riverside Avenue, Medford, MA"


def test_a_backwards_route_is_flipped_including_points():
    route = fetch_rides.route_from_maps_url(
        "https://maps.google.com/?saddr=" + CAFE + "&daddr=" + DOCK
        + "&geocode=" + GEOCODE_PAIR
    )
    assert route["start"] == DOCK
    assert route["end"] == CAFE
    # The coordinate list has to follow the stops it belongs to.
    assert route["points"] == [[42.366823, -71.186803], [42.335543, -71.150614]]


def test_a_backwards_multi_stop_route_reverses_the_via_stops():
    route = fetch_rides.route_from_maps_url(
        "https://maps.google.com/?saddr=O'Some Cafe&daddr=Gracie's to:Honeycomb to:"
        + DOCK
    )
    assert route["start"] == DOCK
    assert route["end"] == "O'Some Cafe"
    assert route["via"] == ["Honeycomb", "Gracie's"]


def test_orient_route_reverses_via_and_points_together():
    route = fetch_rides.orient_route(
        {
            "start": "Cafe",
            "end": DOCK,
            "via": ["B", "C"],
            "points": [[1.0, 1.0], [2.0, 2.0], [3.0, 3.0], [4.0, 4.0]],
            "distance_m": 5000,
            "distance_display": "~3.1 mi",
        }
    )
    assert route["start"] == DOCK
    assert route["end"] == "Cafe"
    assert route["via"] == ["C", "B"]
    assert route["points"] == [[4.0, 4.0], [3.0, 3.0], [2.0, 2.0], [1.0, 1.0]]
    # The ride is the same length in either direction.
    assert route["distance_m"] == 5000
    assert route["distance_display"] == "~3.1 mi"


def test_a_route_that_starts_at_the_dock_is_left_alone():
    route = fetch_rides.route_from_maps_url(
        "https://maps.google.com/?saddr=" + DOCK + "&daddr=" + CAFE
        + "&geocode=" + GEOCODE_PAIR
    )
    assert route["start"] == DOCK
    assert route["end"] == CAFE
    assert route["points"] == [[42.335543, -71.150614], [42.366823, -71.186803]]


def test_a_route_with_no_dock_at_either_end_is_left_alone():
    """The Scooper Bowl ride met at H Mart, not a dock — nothing to flip."""
    route = fetch_rides.route_from_maps_url(
        "https://maps.google.com/?saddr=H Mart Brookline, Beacon Street, Brookline, MA"
        "&daddr=Gracie's to:88 Seaport Boulevard, Boston, MA"
    )
    assert route["start"] == "H Mart Brookline, Beacon Street, Brookline, MA"
    assert route["end"] == "88 Seaport Boulevard, Boston, MA"
    assert route["via"] == ["Gracie's"]


def test_is_bluebikes_reads_the_leading_segment_only():
    assert fetch_rides._is_bluebikes(DOCK)
    assert fetch_rides._is_bluebikes("bluebikes Cleveland Circle")
    # A café that merely sits next to a dock is not a dock.
    assert not fetch_rides._is_bluebikes("Tatte, 1 Main St, near Bluebikes")
    assert not fetch_rides._is_bluebikes("")


def test_measure_route_adds_distance():
    route = {"points": [[42.0, -71.0], [42.1, -71.1]]}
    fetch_rides.measure_route(route, lambda points: 6448.0)
    assert route["distance_m"] == 6448
    assert route["distance_display"] == "~4.0 mi"


def test_measure_route_without_coordinates_does_not_call_the_router():
    def unreachable(points):
        raise AssertionError("no coordinates → no routing request")

    route = {"start": "A", "end": "B"}
    fetch_rides.measure_route(route, unreachable)
    assert "distance_m" not in route


def test_measure_route_is_soft_when_the_router_is_down():
    route = {"points": [[42.0, -71.0], [42.1, -71.1]]}
    fetch_rides.measure_route(route, lambda points: None)
    assert "distance_m" not in route
    assert "distance_display" not in route


def test_measure_route_ignores_a_zero_length():
    route = {"points": [[42.0, -71.0], [42.0, -71.0]]}
    fetch_rides.measure_route(route, lambda points: 0.0)
    assert "distance_m" not in route


def test_format_distance_is_approximate_and_in_miles():
    assert fetch_rides.format_distance(1609.344) == "~1.0 mi"
    assert fetch_rides.format_distance(15852) == "~9.8 mi"


def test_rides_routes_measures_the_fixture_route():
    event = fetch_rides._event_from_page(EVENT_PAGE.read_text(encoding="utf-8"))
    routes = fetch_rides.rides_routes(event, _fake_resolve_link, _fake_route_length)
    assert routes[0]["distance_m"] == 1000  # one leg, 1 km from the stub
    assert routes[0]["distance_display"] == "~0.6 mi"


# --- excluded events (scripts/excluded_events.json) ---

EXCLUDED_FIXTURE = REPO_ROOT / "scripts" / "excluded_events.json"


def test_excluded_uid_is_dropped_from_the_upcoming_pass(feed_bytes):
    rides = fetch_rides.parse_events(
        feed_bytes, now=NOW, excluded={"evt-future-charles-loop@partiful.com"}
    )
    assert [r["uid"] for r in rides] == ["evt-future-minuteman@partiful.com"]


def test_excluded_uid_is_dropped_from_the_past_pass(feed_bytes):
    assert [
        r["uid"] for r in fetch_rides.parse_events(feed_bytes, now=NOW, past=True)
    ] == ["evt-past-jamaica-pond@partiful.com"]
    assert fetch_rides.parse_events(
        feed_bytes, now=NOW, past=True, excluded={"evt-past-jamaica-pond@partiful.com"}
    ) == []


def test_no_exclusions_changes_nothing(feed_bytes):
    assert fetch_rides.parse_events(feed_bytes, now=NOW, excluded=set()) == (
        fetch_rides.parse_events(feed_bytes, now=NOW)
    )


def test_load_excluded_events_missing_file_is_empty(tmp_path):
    assert fetch_rides.load_excluded_events(tmp_path / "nope.json") == set()


def test_load_excluded_events_keys_are_the_uids(tmp_path):
    sidecar = tmp_path / "excluded_events.json"
    sidecar.write_text('{"abc": "a birthday", "def": ""}', encoding="utf-8")
    assert fetch_rides.load_excluded_events(sidecar) == {"abc", "def"}


def test_load_excluded_events_malformed_raises(tmp_path):
    sidecar = tmp_path / "excluded_events.json"
    sidecar.write_text("{not json", encoding="utf-8")
    with pytest.raises(fetch_rides.FeedError):
        fetch_rides.load_excluded_events(sidecar)


def test_load_excluded_events_non_object_raises(tmp_path):
    sidecar = tmp_path / "excluded_events.json"
    sidecar.write_text('["abc"]', encoding="utf-8")
    with pytest.raises(fetch_rides.FeedError):
        fetch_rides.load_excluded_events(sidecar)


def test_main_honours_the_exclusion_list_in_both_outputs(tmp_path):
    sidecar = tmp_path / "excluded_events.json"
    sidecar.write_text(
        '{"evt-future-charles-loop@partiful.com": "x", '
        '"evt-past-jamaica-pond@partiful.com": "y"}',
        encoding="utf-8",
    )
    out = tmp_path / "events.json"
    past_out = tmp_path / "events-past.json"
    assert fetch_rides.main([
        "--ics-file", str(FIXTURE),
        "--excluded-events", str(sidecar),
        "--out", str(out),
        "--past-out", str(past_out),
    ]) == 0
    upcoming = json.loads(out.read_text(encoding="utf-8"))
    past = json.loads(past_out.read_text(encoding="utf-8"))
    assert [r["uid"] for r in upcoming["events"]] == ["evt-future-minuteman@partiful.com"]
    assert upcoming["count"] == 1
    assert past["events"] == [] and past["count"] == 0


def test_main_exits_nonzero_on_a_malformed_exclusion_list(tmp_path, capsys):
    sidecar = tmp_path / "excluded_events.json"
    sidecar.write_text("{oops", encoding="utf-8")
    out = tmp_path / "events.json"
    assert fetch_rides.main([
        "--ics-file", str(FIXTURE), "--excluded-events", str(sidecar), "--out", str(out)
    ]) == 1
    assert not out.exists()
    assert "excluded events" in capsys.readouterr().err


def test_committed_exclusion_list_is_well_formed():
    """The real sidecar: bare Partiful ids with a note each."""
    data = json.loads(EXCLUDED_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data
    for uid, note in data.items():
        assert uid.isalnum(), uid  # a bare Partiful event id, not a name@partiful.com UID
        assert isinstance(note, str) and note.strip(), uid
    assert fetch_rides.load_excluded_events(EXCLUDED_FIXTURE) == set(data)


@pytest.mark.parametrize("name", ["events.json", "events-past.json"])
def test_excluded_rides_are_absent_from_the_ride_data(name):
    """No excluded UID is left in the published data — the sync would otherwise
    re-archive whatever was deleted by hand.

    The ride data lives on the `data` branch, not here: a plain code checkout
    has none of it until `scripts/pull_data.sh` pulls it into site/, so this
    check runs where the file exists and skips where it doesn't.
    """
    path = REPO_ROOT / "site" / name
    if not path.exists():
        pytest.skip(f"site/{name} lives on the data branch (scripts/pull_data.sh)")
    excluded = fetch_rides.load_excluded_events(EXCLUDED_FIXTURE)
    payload = json.loads(path.read_text(encoding="utf-8"))
    present = {r["uid"] for r in payload["events"]} & excluded
    assert not present, f"site/{name} still holds excluded rides: {sorted(present)}"


# --- the host allowlist -----------------------------------------------------

GROUP_HOST = "Hs47uq5mucZyXLBJZCda"
GROUP_FIXTURE = REPO_ROOT / "scripts" / "group_hosts.json"


def test_load_group_hosts_missing_file_is_empty(tmp_path):
    assert fetch_rides.load_group_hosts(tmp_path / "nope.json") == set()


def test_load_group_hosts_keys_are_the_user_ids(tmp_path):
    sidecar = tmp_path / "group_hosts.json"
    sidecar.write_text('{"Hs47": "Boston Cafe Bikers", "x": ""}', encoding="utf-8")
    assert fetch_rides.load_group_hosts(sidecar) == {"Hs47", "x"}


def test_load_group_hosts_malformed_raises(tmp_path):
    sidecar = tmp_path / "group_hosts.json"
    sidecar.write_text("{not json", encoding="utf-8")
    with pytest.raises(fetch_rides.FeedError):
        fetch_rides.load_group_hosts(sidecar)


def test_load_group_hosts_non_object_raises(tmp_path):
    sidecar = tmp_path / "group_hosts.json"
    sidecar.write_text('["Hs47"]', encoding="utf-8")
    with pytest.raises(fetch_rides.FeedError):
        fetch_rides.load_group_hosts(sidecar)


def test_committed_group_hosts_is_well_formed():
    data = json.loads(GROUP_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data
    for uid, note in data.items():
        assert uid.isalnum(), uid  # a bare Partiful user id, not a name@partiful.com UID
        assert isinstance(note, str) and note.strip(), uid
    assert fetch_rides.load_group_hosts(GROUP_FIXTURE) == set(data)


def test_event_owner_ids_reads_all_three_shapes():
    event = {
        "ownerIds": ["owner-id"],
        "owners": [{"id": "owner-in-object"}],
        "createdBy": {"id": "creator-id"},
    }
    assert fetch_rides.event_owner_ids(event) == [
        "owner-id", "owner-in-object", "creator-id",
    ]


def test_event_owner_ids_deduplicates():
    assert fetch_rides.event_owner_ids({"ownerIds": ["a", "a"]}) == ["a"]


def test_event_owner_ids_empty_when_the_page_names_nobody():
    assert fetch_rides.event_owner_ids({}) == []


def test_event_hosted_by_group_is_true_when_a_host_matches():
    event = {"ownerIds": ["a-friend", GROUP_HOST]}
    assert fetch_rides.event_hosted_by_group(event, {GROUP_HOST}) is True


def test_event_hosted_by_group_is_false_when_no_host_matches():
    assert fetch_rides.event_hosted_by_group({"ownerIds": ["friend"]}, {GROUP_HOST}) is False


def test_event_hosted_by_group_is_none_without_any_host_info():
    """An event the page names nobody for is kept, not dropped."""
    assert fetch_rides.event_hosted_by_group({}, {GROUP_HOST}) is None


def test_enrich_rides_records_ownership_when_an_allowlist_is_given(feed_bytes):
    rides = fetch_rides.parse_events(feed_bytes, now=NOW)
    fetch_rides.enrich_rides(
        rides,
        fetch_page=_fake_event_page,
        resolve_link=_fake_resolve_link,
        fetch_length=_fake_route_length,
        group_hosts={GROUP_HOST},
    )
    charles, minuteman = rides
    assert charles["hosted_by_group"] is True
    # minuteman's page could not be fetched → unknown, so it is never dropped.
    assert minuteman["hosted_by_group"] is None


def test_enrich_rides_leaves_ownership_unknown_without_an_allowlist(feed_bytes):
    rides = fetch_rides.parse_events(feed_bytes, now=NOW)
    fetch_rides.enrich_rides(
        rides,
        fetch_page=_fake_event_page,
        resolve_link=_fake_resolve_link,
        fetch_length=_fake_route_length,
    )
    assert all(ride["hosted_by_group"] is None for ride in rides)


def test_enrich_rides_flags_a_page_hosted_by_nobody_we_know(feed_bytes):
    def foreign_page(url: str) -> str:
        return EVENT_PAGE.read_text(encoding="utf-8").replace(GROUP_HOST, "someoneElse")

    rides = fetch_rides.parse_events(feed_bytes, now=NOW)
    fetch_rides.enrich_rides(
        rides,
        fetch_page=foreign_page,
        resolve_link=_fake_resolve_link,
        fetch_length=_fake_route_length,
        group_hosts={GROUP_HOST},
    )
    assert rides[0]["hosted_by_group"] is False


def test_filter_unhosted_drops_only_decided_non_group_rides():
    rides = [
        {"uid": "ours", "hosted_by_group": True},
        {"uid": "theirs", "hosted_by_group": False},
        {"uid": "unknown", "hosted_by_group": None},
        {"uid": "unchecked"},
    ]
    kept, dropped = fetch_rides.filter_unhosted(rides, {GROUP_HOST})
    assert [ride["uid"] for ride in kept] == ["ours", "unknown", "unchecked"]
    assert dropped == {"theirs"}


def test_filter_unhosted_is_a_noop_without_an_allowlist():
    rides = [{"uid": "theirs", "hosted_by_group": False}]
    kept, dropped = fetch_rides.filter_unhosted(rides, set())
    assert kept == rides and dropped == set()
    kept, dropped = fetch_rides.filter_unhosted(rides)
    assert kept == rides and dropped == set()


def test_main_drops_events_not_hosted_by_the_group(tmp_path, monkeypatch):
    out = tmp_path / "events.json"

    def enrich_with_one_drop(rides, **kwargs):
        for ride in rides:
            # Charles is ours; minuteman is a friend's event we were invited to.
            ride["hosted_by_group"] = ride["uid"].startswith("evt-future-charles")
        return 0

    monkeypatch.setattr(fetch_rides, "enrich_rides", enrich_with_one_drop)
    monkeypatch.setattr(fetch_rides, "fetch_ics", lambda url: FIXTURE.read_bytes())
    monkeypatch.setenv("PARTIFUL_ICS_URL", "https://partiful.example.invalid/feed.ics")
    assert fetch_rides.main(["--out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert [ride["uid"] for ride in payload["events"]] == [
        "evt-future-charles-loop@partiful.com"
    ]
