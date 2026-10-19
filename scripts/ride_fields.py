#!/usr/bin/env python3
"""The display fields the browser used to work out for itself.

The site's rule is that every string a page prints is computed here, in the
sync, and the browser only reads it: that is why a visitor in Tokyo sees the
same Eastern date as one in Somerville. Four values were still being derived in
JavaScript, each a hand-kept twin of a Python rule — the grace hour, the
Bluebikes-dock start name, the café name/address split, and the year a ride
happened. They live here now, once, and `derive()` writes them onto every
event the sync stores:

    grace_until   when a ride stops being "rolling now" (start + GRACE_PERIOD)
    place_name    "O'Some Café, 100 Main St, …" → "O'Some Café"
    address       …and the rest of it, "100 Main St, Watertown, MA 02472"
    year          "2026", off the ISO start
    routes[].start_name / .end_name   the card's and the map's route labels

Pure functions, no I/O and no network, stdlib only (archive_events.py imports
this and must stay that way). `derive()` is idempotent and doesn't mutate its
argument, which is what lets the sync run it over the whole archive every time:
the fields are a function of stored data, so a change to a rule here reaches
every ride on the next sync with no backfill script and no network calls.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# The one definition of the group's timezone and where the repo sits. Every
# module that needs either imports it here: fetch_rides.py, archive_events.py
# (stdlib-only, and already importing this module) and sync.py used to each
# declare their own copy, which is exactly how a filter and its archive drift.
LOCAL_TZ = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parent.parent
# The UID → note sidecar of feed events that are not group rides. fetch_rides.py
# reads it to drop them from the upcoming list; archive_events.py reads it too,
# to purge an already-archived one. One path so they can't disagree.
EXCLUDED_EVENTS_PATH = REPO_ROOT / "scripts" / "excluded_events.json"

# A ride does not stop being "the next ride" the instant it starts: latecomers
# still catch the group at the dock. This is the one definition — fetch_rides.py
# (the upcoming/past filter) and archive_events.py (what counts as history) both
# import it, and `grace_until` puts the resulting instant in the data so the
# card's "Rolling now" pill can read it instead of re-deriving the hour.
GRACE_PERIOD = timedelta(hours=1)

# Rides start at Bluebikes docks, whose address is
# "Bluebikes, <station>, <city>, MA <zip>" — the leading segment is the brand,
# the station is what tells you where to meet.
BLUEBIKES_RE = re.compile(r"^bluebikes\b[\s:\-–—]*(.*)$", re.IGNORECASE)
STATE_RE = re.compile(r"^[A-Z]{2}(\s+\d{5}(-\d{4})?)?$")


def _segments(location: str | None) -> list[str]:
    """A location split into its trimmed, non-empty comma-segments."""
    raw = " ".join(str(location or "").split())
    return [part.strip() for part in raw.split(",") if part.strip()]


def grace_until(start_iso: str | None) -> str | None:
    """``start`` + GRACE_PERIOD, spelled exactly like ``start``.

    The offset is carried through untouched, so the string parses to the right
    instant in any timezone — which is all the browser does with it (compare
    ``Date.now()`` against it). An unparseable or missing start has no grace
    window, and the card then treats the ride as not rolling.
    """
    if not isinstance(start_iso, str) or not start_iso.strip():
        return None
    try:
        moment = datetime.fromisoformat(start_iso.strip())
    except ValueError:
        return None
    return (moment + GRACE_PERIOD).isoformat()


def place_name(location: str | None) -> str:
    """"O'Some Café, 100 Main St, Watertown, MA 02472" → "O'Some Café"."""
    parts = _segments(location)
    return parts[0] if parts else ""


def address(location: str | None) -> str:
    """Everything after the place name: "100 Main St, Watertown, MA 02472"."""
    return ", ".join(_segments(location)[1:])


def start_name(place: str | None) -> str:
    """"Bluebikes, Cleveland Circle, Boston, MA 02135" → "Cleveland Circle".

    A two-segment station ("Bunker Hill Mall, Main St at Austin St") survives
    whole, by stripping the trailing "<city>, <ST> [zip]" pair; a bare
    "Bluebikes" falls back to the whole string, since dropping the only word
    there is would leave nothing. Anything that isn't a dock keeps its leading
    segment, exactly like `place_name` — a café "near Bluebikes" is not a dock.
    """
    raw = " ".join(str(place or "").split())
    parts = _segments(raw)
    if not parts:
        return ""
    match = BLUEBIKES_RE.match(parts[0])
    if not match:
        return parts[0]
    detail = ([match.group(1)] if match.group(1) else []) + parts[1:]
    if not detail:
        return raw
    if len(detail) >= 3 and STATE_RE.match(detail[-1]):
        return ", ".join(detail[:-2])
    return detail[0]


def year(start_iso: str | None) -> str | None:
    """"2026-08-16T10:00:00-04:00" → "2026".

    Sliced off the ISO prefix, never parsed: `date_display` carries no year and
    the archive spans two of them, so the café list needs one — and re-parsing
    a start in the visitor's timezone is how a ride lands in the wrong year.
    """
    if not isinstance(start_iso, str):
        return None
    prefix = start_iso.strip()[:4]
    return prefix if prefix.isdigit() else None


def derive_route(route: dict) -> dict:
    """A copy of one route entry carrying its two label names."""
    if not isinstance(route, dict):
        return route
    out = dict(route)
    out["start_name"] = start_name(route.get("start")) or None
    out["end_name"] = place_name(route.get("end")) or None
    return out


def derive(event: dict) -> dict:
    """A copy of `event` with every precomputed display field written on it.

    Idempotent (the fields are a function of `start`, `location` and `routes`,
    none of which this touches) and non-mutating, so it is safe to run over the
    whole archive on every sync. `None` wherever the source is missing: a ride
    whose address is hidden until RSVP, or that carried only a map link, has no
    café name and no street address to show.
    """
    if not isinstance(event, dict):
        return event
    out = dict(event)
    start = out.get("start")
    location = out.get("location")
    out["grace_until"] = grace_until(start)
    out["place_name"] = place_name(location) or None
    out["address"] = address(location) or None
    out["year"] = year(start)
    routes = out.get("routes")
    if isinstance(routes, list):
        out["routes"] = [derive_route(route) for route in routes]
    return out


def derive_all(events: list) -> list:
    """`derive` over a list of events, as a new list."""
    return [derive(event) for event in events]
