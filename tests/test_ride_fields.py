"""Tests for scripts/ride_fields.py — the display fields the sync precomputes.

These rules used to live twice: once in Python and once in JavaScript, kept in
step by hand. They live here now, and the browser only reads what `derive`
wrote — so the cases the JS suite used to assert (the Bluebikes-dock start
name above all) are asserted here instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import archive_events
import fetch_rides
import ride_fields
import route_map

DOCK = "Bluebikes, Cleveland Circle, Boston, MA 02135"
CAFE = "O'Some Café, 100 Main St, Watertown, MA 02472"


def ride(**extra) -> dict:
    base = {
        "uid": "abc123",
        "title": "O'Some Sunday",
        "start": "2026-08-16T10:00:00-04:00",
        "end": None,
        "date_display": "Sunday, August 16",
        "time_display": "10:00 am",
        "location": CAFE,
        "location_hidden": False,
        "location_url": None,
        "description": "",
        "rsvp_url": None,
        "image": None,
        "routes": None,
    }
    base.update(extra)
    return base


# --- the grace hour -------------------------------------------------------
# One number, in one module. fetch_rides filters the upcoming list on it,
# archive_events decides what is history on it, and `grace_until` puts the end
# of the window in the data so ride-card.js can read it instead of carrying a
# GRACE_MS of its own.


def test_the_grace_hour_has_exactly_one_definition():
    assert ride_fields.GRACE_PERIOD == timedelta(hours=1)
    assert fetch_rides.GRACE_PERIOD is ride_fields.GRACE_PERIOD
    assert archive_events.GRACE_PERIOD is ride_fields.GRACE_PERIOD


def test_grace_until_is_the_start_plus_the_grace_period():
    assert ride_fields.grace_until("2026-08-16T10:00:00-04:00") == "2026-08-16T11:00:00-04:00"


@pytest.mark.parametrize(
    "start",
    [
        # Either side of the November fallback, when Eastern rides switch
        # offset: the offset in the string is carried through untouched, so the
        # result is the same instant plus an hour whatever the visitor's clock.
        "2026-11-01T01:30:00-04:00",
        "2026-11-01T01:30:00-05:00",
        # …and the March forward switch.
        "2026-03-08T01:30:00-05:00",
        "2026-03-08T10:00:00-04:00",
    ],
)
def test_grace_until_keeps_the_offset_and_moves_one_real_hour(start):
    until = ride_fields.grace_until(start)
    assert until.endswith(start[-6:]), "the offset spelling is the start's own"
    moved = datetime.fromisoformat(until) - datetime.fromisoformat(start)
    assert moved == ride_fields.GRACE_PERIOD


def test_grace_until_handles_a_naive_start_and_a_missing_one():
    # archive_events tolerates a start with no offset; so does this.
    assert ride_fields.grace_until("2026-05-02T11:00:00") == "2026-05-02T12:00:00"
    assert ride_fields.grace_until(None) is None
    assert ride_fields.grace_until("") is None
    assert ride_fields.grace_until("not a date") is None


# --- the café name and its address ---------------------------------------


@pytest.mark.parametrize(
    "location, name, address",
    [
        (CAFE, "O'Some Café", "100 Main St, Watertown, MA 02472"),
        ("Tatte Bakery, Brookline", "Tatte Bakery", "Brookline"),
        ("Localito", "Localito", ""),
        ("  Merai ,  Brookline , MA  ", "Merai", "Brookline, MA"),
        ("", "", ""),
        (None, "", ""),
    ],
)
def test_place_name_and_address_split_on_the_first_comma(location, name, address):
    assert ride_fields.place_name(location) == name
    assert ride_fields.address(location) == address


# --- where a ride starts --------------------------------------------------
# Moved here from tests/js/site.test.mjs, which used to assert the same table
# against a JavaScript twin of this rule.


@pytest.mark.parametrize(
    "place, expected",
    [
        (DOCK, "Cleveland Circle"),
        ("Bluebikes, Washington St at Temple Pl, Boston, MA 02111", "Washington St at Temple Pl"),
        # A two-segment station survives whole once the city tail is stripped.
        ("Bluebikes, Bunker Hill Mall, Main St at Austin St, Charlestown, MA 02129",
         "Bunker Hill Mall, Main St at Austin St"),
        # No comma: the rest of the first segment is the station.
        ("Bluebikes Cleveland Circle", "Cleveland Circle"),
        ("bluebikes  Cleveland Circle", "Cleveland Circle"),
        # Dropping the only word there is would leave nothing.
        ("Bluebikes", "Bluebikes"),
        # Not a dock: the leading place name, like place_name().
        (CAFE, "O'Some Café"),
        ("Tatte near Bluebikes, Brookline, MA", "Tatte near Bluebikes"),
        ("J.P. Licks, 659 Centre St, Boston, MA 02130", "J.P. Licks"),
        ("", ""),
        (None, ""),
    ],
)
def test_start_name_reads_the_bluebikes_dock(place, expected):
    assert ride_fields.start_name(place) == expected


def test_the_map_labels_use_the_same_functions():
    """route_map's label helpers are these, not copies of them."""
    assert route_map._start_name is ride_fields.start_name
    assert route_map._place_name is ride_fields.place_name


# --- the year -------------------------------------------------------------


def test_year_is_sliced_off_the_iso_prefix():
    assert ride_fields.year("2026-08-16T10:00:00-04:00") == "2026"
    assert ride_fields.year("2025-11-16T10:00:00-05:00") == "2025"
    assert ride_fields.year(None) is None
    assert ride_fields.year("") is None
    assert ride_fields.year("undated") is None


# --- derive ---------------------------------------------------------------


def test_derive_writes_every_field():
    out = ride_fields.derive(ride())
    assert out["grace_until"] == "2026-08-16T11:00:00-04:00"
    assert out["place_name"] == "O'Some Café"
    assert out["address"] == "100 Main St, Watertown, MA 02472"
    assert out["year"] == "2026"


def test_a_hidden_or_link_only_location_has_no_name_and_no_address():
    hidden = ride_fields.derive(ride(location=None, location_hidden=True))
    assert hidden["place_name"] is None and hidden["address"] is None

    link = "https://maps.app.goo.gl/7zBmEn5ZTHEhJtSZ7"
    linked = ride_fields.derive(ride(location=None, location_url=link))
    assert linked["place_name"] is None and linked["address"] is None
    assert linked["location_url"] == link

    # A one-segment address is a name with nothing left over.
    single = ride_fields.derive(ride(location="Localito"))
    assert single["place_name"] == "Localito" and single["address"] is None


def test_derive_does_not_mutate_its_argument():
    original = ride(routes=[{"label": "Estimated Route", "start": DOCK, "end": CAFE}])
    before = repr(original)
    ride_fields.derive(original)
    assert repr(original) == before
    assert "place_name" not in original
    assert "start_name" not in original["routes"][0]


def test_derive_is_idempotent():
    once = ride_fields.derive(ride(routes=[{"label": "R", "start": DOCK, "end": CAFE}]))
    assert ride_fields.derive(once) == once
    # …and again, since the sync re-derives the whole archive every run.
    assert ride_fields.derive(ride_fields.derive(once)) == once


def test_routes_gain_their_two_label_names():
    out = ride_fields.derive(ride(routes=[
        {"label": "Estimated Route", "start": DOCK, "end": CAFE},
        {"label": "Team B Route", "start": "H Mart Brookline, Beacon Street, Brookline, MA",
         "end": "88 Seaport"},
    ]))
    assert out["routes"][0]["start_name"] == "Cleveland Circle"
    assert out["routes"][0]["end_name"] == "O'Some Café"
    assert out["routes"][1]["start_name"] == "H Mart Brookline"
    assert out["routes"][1]["end_name"] == "88 Seaport"


def test_a_route_without_stops_gets_nulls_not_empty_strings():
    out = ride_fields.derive(ride(routes=[{"label": "Start/Bluebikes"}]))
    assert out["routes"][0]["start_name"] is None
    assert out["routes"][0]["end_name"] is None


def test_unchecked_routes_stay_none():
    """`None` means "enrichment never looked" — archive_events depends on it."""
    assert ride_fields.derive(ride(routes=None))["routes"] is None
    assert ride_fields.derive(ride(routes=[]))["routes"] == []


def test_derive_all_maps_the_list():
    out = ride_fields.derive_all([ride(), ride(uid="b")])
    assert [r["place_name"] for r in out] == ["O'Some Café", "O'Some Café"]
    assert ride_fields.derive_all([]) == []


# --- manual destination overrides -----------------------------------------


def test_location_override_replaces_only_its_ride():
    events = [
        ride(uid="a", location="Start Plaza, 1 Start St, Boston, MA"),
        ride(uid="b", location=CAFE),
    ]
    out = ride_fields.apply_location_overrides(
        events, {"a": "Lovestruck Books, 44 Brattle St, Cambridge, MA 02138"}
    )
    assert out[0]["location"] == "Lovestruck Books, 44 Brattle St, Cambridge, MA 02138"
    assert out[1]["location"] == CAFE


def test_location_override_does_not_mutate_its_argument():
    original = ride(uid="a", location="Start Plaza")
    before = repr(original)
    ride_fields.apply_location_overrides([original], {"a": "Café, MA"})
    assert repr(original) == before


def test_location_override_is_a_no_op_without_a_map():
    alone = ride(uid="a")
    assert ride_fields.apply_location_overrides([alone], None)[0] is alone
    assert ride_fields.apply_location_overrides([alone], {})[0] is alone


def test_derive_after_override_names_the_destination():
    fixed = ride_fields.apply_location_overrides(
        [ride(uid="a", location="Start Plaza, 1 Start St, Boston, MA")],
        {"a": "Café, 2 Main St, Cambridge, MA 02139"},
    )
    out = ride_fields.derive(fixed[0])
    assert out["place_name"] == "Café"
    assert out["address"] == "2 Main St, Cambridge, MA 02139"
