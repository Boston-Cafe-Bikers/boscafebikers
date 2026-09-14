#!/usr/bin/env python3
"""Backfill rides the feed never carried, from a list of Partiful event ids.

    scripts/backfill_events.json    {"<event id>": "<note>", ...}

The calendar-sync feed behind PARTIFUL_ICS_URL is one *account's* calendar: it
carries what that account hosts or has RSVP'd to. A ride nobody on that
account answered — every ride from before the sync existed, say — never
reaches events-past.json, so the archive and the café map only go back as far
as the feed owner's first RSVP. This is the way in for the rest (issue #5).

List the event ids — the id in each ride's ``partiful.com/e/<id>`` link — and
the sync fetches each listed event's public page (the same page enrichment
already reads for photos and routes, so no new secret), builds the ride record
the feed would have produced, and hands it to the archive step. An id the sync
already has a record for — archived, in the feed, or excluded — is skipped
without a fetch, so the file is a queue that drains itself: once a ride is in,
its line costs nothing, and the file can stay as it is forever.

Two limits keep it honest. Only rides that have already happened are archived:
an upcoming id is fetched and set aside each run until its grace hour passes,
then it lands. And at most ``--backfill-limit`` pages are fetched per run; the
rest follow next time. Every fetch is fail-soft, as everywhere else in the
pipeline — a page that is missing or unreadable is counted, reported, and
retried next run, never a failed sync.

The record is built here rather than by parsing the page into ICS: it must
carry exactly the keys ``fetch_rides.parse_events`` writes (a test pins that),
but the page has no DTSTART to fold — its ``startDate`` is UTC.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_rides  # noqa: E402
import ride_fields  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKFILL_EVENTS_PATH = REPO_ROOT / "scripts" / "backfill_events.json"
DEFAULT_LIMIT = 10
LOCAL_TZ = fetch_rides.LOCAL_TZ

# Partiful's own words for an event that no longer happens. Anything else —
# "PUBLISHED", or a value never seen before — is taken at face value, the way
# the feed's STATUS is: only CANCELLED is ever dropped there.
DEAD_STATUSES = frozenset({"CANCELLED", "CANCELED", "DELETED"})

# The page's timestamps look like "2026-08-23T15:00:00.000Z". fromisoformat
# only learned the trailing Z in Python 3.11, so it is rewritten as an offset;
# the fraction is dropped so a sub-second start can never leak into the ISO
# string the site stores (the feed's DTSTART has whole seconds).
FRACTION_RE = re.compile(r"\.\d+")


def load_backfill_events(path=None) -> list:
    """The listed event ids, in file order. A missing file lists nothing.

    The value is a human note (which ride, and why it had to be listed); only
    the keys matter. A file that can't be read is a real error, like the other
    sidecars: a typo there should stop the sync, not silently list nothing.
    """
    path = Path(path) if path is not None else BACKFILL_EVENTS_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise fetch_rides.FeedError(
            f"could not parse backfill events ({path.name}): {fetch_rides.scrub(exc)}"
        ) from None
    if not isinstance(data, dict):
        raise fetch_rides.FeedError(
            f"backfill events ({path.name}) must be a JSON object of event id → note"
        )
    return [str(uid).strip() for uid in data if str(uid).strip()]


def pending(listed: list, known: set) -> list:
    """The listed ids that still need a page, in list order, each once."""
    seen = set()
    out = []
    for uid in listed:
        if uid in known or uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
    return out


def parse_instant(value) -> Optional[datetime]:
    """A page timestamp as an aware Eastern datetime, or None.

    "2026-08-23T15:00:00.000Z" is the shape Partiful writes; an offset form is
    accepted too, and a naive one is read as UTC, which is what the page means.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = FRACTION_RE.sub("", text, count=1)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL_TZ)


def _location(event: dict) -> tuple:
    """(location, hidden, url), read the way ``_clean_location`` reads the feed.

    The page's ``location`` is the same string the feed exports — an address,
    or a bare maps link when the host pasted one in place of an address — so
    the feed's own cleaner decides which. An event whose address is held back
    until RSVP has no ``location`` on its public page at all; its
    ``locationInfo`` still exists (it names the neighbourhood), and that is
    the feed's "hidden" case.
    """
    raw = event.get("location")
    if isinstance(raw, str) and raw.strip():
        location, hidden, url = fetch_rides._clean_location(raw)
        return location or None, hidden, url
    info = event.get("locationInfo")
    hidden = isinstance(info, dict) and bool(info)
    return None, hidden, None


def ride_from_event(event) -> Optional[dict]:
    """The ride record for one event page's ``event`` object, or None.

    None for anything that can't go on the calendar: no id, no start, or a
    status Partiful uses for an event that no longer happens. The keys — and
    what each means — are exactly ``fetch_rides.parse_events``'s, so nothing
    downstream can tell a backfilled ride from a fed one. ``image`` and
    ``routes`` are left for ``ride_from_page``, which has the whole page.
    """
    if not isinstance(event, dict):
        return None
    uid = str(event.get("id") or "").strip()
    start = parse_instant(event.get("startDate"))
    if not uid or start is None:
        return None
    if str(event.get("status") or "").strip().upper() in DEAD_STATUSES:
        return None
    end = parse_instant(event.get("endDate"))
    location, hidden, url = _location(event)
    return {
        "uid": uid,
        "title": fetch_rides._clean_title(str(event.get("title") or "")) or "Café ride",
        "start": start.isoformat(),
        "end": end.isoformat() if end is not None else None,
        "date_display": f"{start:%A, %B} {start.day}",
        "time_display": f"{start:%-I:%M %p}".replace("AM", "am").replace("PM", "pm"),
        "location": location,
        "location_hidden": hidden,
        "location_url": url,
        "description": fetch_rides._clean_description(str(event.get("description") or "")),
        "rsvp_url": fetch_rides.derive_partiful_url(uid),
        "image": None,
        "routes": None,
    }


def ride_from_page(
    html: str,
    resolve_link: Optional[Callable] = None,
    fetch_length: Optional[Callable] = None,
) -> Optional[dict]:
    """The ride record for one fetched event page, photo and routes included.

    Both come off the page the way ``enrich_rides`` reads them for a fed ride,
    so a backfilled ride arrives already enriched and never joins the
    archive's never-checked queue. ``routes`` is a list (possibly empty), which
    is how that queue tells a checked ride from an unchecked one.
    """
    event = fetch_rides._event_from_page(html)
    ride = ride_from_event(event)
    if ride is None:
        return None
    if resolve_link is None:
        resolve_link = fetch_rides._resolve_link
    ride["image"] = fetch_rides._extract_event_image(html)
    ride["routes"] = fetch_rides.rides_routes(event, resolve_link, fetch_length)
    return ride


def backfill(
    uids: list,
    fetch_page: Optional[Callable] = None,
    resolve_link: Optional[Callable] = None,
    fetch_length: Optional[Callable] = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple:
    """Fetch a page for each id, at most ``limit`` of them, oldest-listed first.

    Returns ``(rides, missed)``: the records built (display fields derived, so
    they are ready for the archive), and the ids whose page could not be
    fetched or didn't describe an event — those are retried next run. Nothing
    here raises for a bad page; the sync stays green.
    """
    if fetch_page is None:
        fetch_page = fetch_rides._fetch_event_page
    rides = []
    missed = []
    batch = uids[:limit] if limit > 0 else []
    for uid in batch:
        url = fetch_rides.derive_partiful_url(uid)
        if not url:
            missed.append(uid)
            continue
        try:
            html = fetch_page(url)
        except (fetch_rides.requests.RequestException, ValueError, TypeError):
            missed.append(uid)
            continue
        ride = ride_from_page(html, resolve_link, fetch_length)
        if ride is None:
            missed.append(uid)
            continue
        rides.append(ride)
    return ride_fields.derive_all(rides), missed
