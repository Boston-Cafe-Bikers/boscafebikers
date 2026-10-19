#!/usr/bin/env python3
"""Fetch the Partiful ICS feed and write upcoming rides to site/events.json.

The feed URL comes from the PARTIFUL_ICS_URL env var and is never printed —
not in logs, not in error messages. For local runs and tests, point the script
at a file instead:

    python scripts/fetch_rides.py --ics-file tests/fixtures/sample.ics

--past-out additionally writes the feed's already-happened rides, which
scripts/archive_events.py folds into site/events-past.json so the calendar can
keep showing them.

The feed is the organizer's *whole* Partiful calendar, so events they were only
invited to show up in it too. On a live run each event's Partiful page is read
during enrichment and the event is kept only when one of the accounts in
scripts/group_hosts.json hosts it; events whose page can't be read are kept.

Exits nonzero on any fetch or parse failure.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import struct
import sys
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote_plus, urlsplit

import requests
from icalendar import Calendar

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ride_fields  # noqa: E402

LOCAL_TZ = ride_fields.LOCAL_TZ
REPO_ROOT = ride_fields.REPO_ROOT
DEFAULT_OUTPUT = REPO_ROOT / "site" / "events.json"
# Optional sidecar mapping ride UID -> image URL, merged into each event as
# `image`. The ICS feed itself carries no photos, so this is how the organizer
# attaches a picture to a ride without touching the site code.
RIDE_IMAGES_PATH = REPO_ROOT / "scripts" / "ride_images.json"
# UIDs of feed events that are not group rides (the organizer's calendar
# export carries their personal events too). archive_events.py reads the
# same file, so an excluded ride is dropped from the upcoming list, the past
# export, and any archive that already holds it.
EXCLUDED_EVENTS_PATH = ride_fields.EXCLUDED_EVENTS_PATH
# Partiful user ids whose events the site shows. The feed is the organizer's
# whole personal calendar, so it also carries events they were merely invited
# to; a feed event is kept only when its Partiful page lists one of these
# accounts as a host. The page is read during enrichment (see `enrich_rides`),
# so an event whose page can't be read is *kept* rather than dropped — the same
# fail-soft rule as the rest of enrichment. Without this file, all events pass.
GROUP_HOSTS_PATH = REPO_ROOT / "scripts" / "group_hosts.json"
FETCH_TIMEOUT_SECONDS = 30
# A ride does not stop being "the next ride" the instant it starts: people turn
# up late at the dock and still catch the group, so the upcoming export keeps a
# ride for this long after its start time and the past export doesn't take it
# until then. The number itself lives in scripts/ride_fields.py — one
# definition, imported here and by the stdlib-only archive_events.py, and
# written into each ride as `grace_until` for the card's "Rolling now" pill.
#
# Measured from `start`, never from `end`. Only 6 of the 40 rides in the feed
# carry a DTEND at all, and those run 3, 4, 6 and 10 hours — a 10-hour end time
# would pin a finished ride to the top of the page as "the next ride" for the
# whole day and hold it out of the archive just as long. Latecomers are a
# start-time question, and one rule means the Python filter and the card's
# "Rolling now" tag can't disagree about which window a ride is in.
GRACE_PERIOD = ride_fields.GRACE_PERIOD
# Enrichment: the sync backfills `image` from each ride's public Partiful
# event page. The page is unauthenticated, so no new secret — the event IDs
# still come from the secret feed, which is why this stays sync-time work.
ENRICH_TIMEOUT_SECONDS = 15
# Next.js embeds the event data (including the cover image) in a __NEXT_DATA__
# script; the real JSON never contains a literal </script> (Next escapes '<').
NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
# Partiful stores event images in Firebase Storage; the URL appears verbatim
# in the page (in __NEXT_DATA__ and/or an <img>). Stop before a quote/angle
# bracket/brace/whitespace so a URL inside JSON isn't over-matched.
FIREBASE_IMAGE_URL_RE = re.compile(
    r"https://firebasestorage\.googleapis\.com/[^\"'\s<>}]+", re.IGNORECASE
)

# RSVP links sit in DESCRIPTION in one of three phrasings — "RSVP: <url>",
# "RSVP at <url>", or "View this event on Partiful at <url>" — and are
# line-folded across the phrase / URL boundary. Capture the URL regardless.
RSVP_RE = re.compile(
    r"(?:RSVP\s*(?::|\bat\b)|View this event on Partiful at)\s*(https?://\S+)",
    re.IGNORECASE,
)
# Partiful's ICS export uses the bare event id as the UID; the event's page is
# https://partiful.com/e/<id>. Used as a fallback when no link is in the text.
PARTIFUL_EVENT_URL = "https://partiful.com/e/"
# Anything that looks like a URL, so it can be scrubbed from error text.
URL_RE = re.compile(r"\b(?:webcal|https?)://\S+", re.IGNORECASE)


class FeedError(Exception):
    """Fetching or parsing the feed failed."""


def scrub(text: object) -> str:
    """Strip URLs out of arbitrary text so the secret feed URL never leaks."""
    return URL_RE.sub("<url redacted>", str(text))


def fetch_ics(url: str) -> bytes:
    """Download the feed. Raises FeedError with a URL-free message."""
    try:
        response = requests.get(
            url,
            timeout=FETCH_TIMEOUT_SECONDS,
            headers={"User-Agent": "boscafebikers-sync/1.0"},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        # Never echo the exception text: requests embeds the request URL (and
        # therefore the secret feed URL) in most of its messages.
        detail = type(exc).__name__
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is not None:
            detail += f", HTTP {status}"
        raise FeedError(f"could not fetch the ICS feed ({detail})") from None
    if not response.content.strip():
        raise FeedError("the ICS feed was empty")
    return response.content


def _as_local_datetime(value: object) -> datetime:
    """Normalise a DTSTART value to an aware America/New_York datetime."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        # All-day event: treat it as starting at midnight local time.
        moment = datetime(value.year, value.month, value.day)
    else:
        raise FeedError(f"unsupported DTSTART value: {type(value).__name__}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LOCAL_TZ)
    return moment.astimezone(LOCAL_TZ)


def _text(component, key: str) -> str:
    value = component.get(key)
    return "" if value is None else str(value).strip()


def extract_rsvp_url(description: str) -> str | None:
    match = RSVP_RE.search(description)
    return match.group(1).rstrip(".,);") if match else None


def derive_partiful_url(uid: str) -> str | None:
    """Build the Partiful event URL from a bare UID.

    Partiful's ICS export uses the event id as a bare UID (no ``@…``). Leave
    descriptive or suffixed UIDs (e.g. ``evt-…@partiful.com``) alone — those
    aren't event ids, and a description usually carries the link anyway.
    """
    if not uid or "@" in uid or uid.startswith(("http://", "https://", "webcal://")):
        return None
    return PARTIFUL_EVENT_URL + uid


def _load_uid_sidecar(
    path: Path | None, default: Path, label: str, *, as_set: bool
) -> dict | set[str]:
    """Read a UID → note/URL sidecar. A missing file means "empty".

    The three sidecars (ride images, excluded events, group hosts) have the
    same shape and the same rules: optional, a JSON object keyed by UID, a
    parse error fatal and reported with the filename only (never a value).
    They differ only in the label and whether callers want the values (`dict`)
    or just the keys (`set`).
    """
    path = Path(path) if path is not None else default
    if not path.exists():
        return set() if as_set else {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise FeedError(f"could not parse {label} ({path.name}): {scrub(exc)}") from None
    if not isinstance(data, dict):
        raise FeedError(f"{label} ({path.name}) must be a JSON object of UID → value")
    return {str(uid) for uid in data} if as_set else data


def load_ride_images(path: Path | None = None) -> dict:
    """Read the optional UID → image-URL sidecar. A missing file means "{}"."""
    return _load_uid_sidecar(path, RIDE_IMAGES_PATH, "ride images", as_set=False)


def load_excluded_events(path: Path | None = None) -> set[str]:
    """Read the optional UID → note sidecar of events to leave out.

    The value is a human note (what the event was and why it is excluded);
    only the keys matter. A missing file means "exclude nothing".
    """
    return _load_uid_sidecar(
        path, EXCLUDED_EVENTS_PATH, "excluded events", as_set=True
    )


def load_group_hosts(path: Path | None = None) -> set[str]:
    """Read the Partiful user-id allowlist of accounts whose events we show.

    The value is a human note (who the account is), only the keys matter. A
    missing file means "no allowlist", in which case every event is kept.
    """
    return _load_uid_sidecar(path, GROUP_HOSTS_PATH, "group hosts", as_set=True)


def _event_from_data(data: dict | None) -> dict | None:
    """The `event` object out of an already-parsed __NEXT_DATA__ blob."""
    if data is None:
        return None
    try:
        event = data["props"]["pageProps"]["event"]
    except (KeyError, TypeError):
        return None
    return event if isinstance(event, dict) else None


def _event_from_page(html: str) -> dict | None:
    """The `event` object out of a Partiful event page's __NEXT_DATA__."""
    return _event_from_data(_extract_next_data(html))


def event_owner_ids(event: dict) -> list[str]:
    """The Partiful user ids that host an event, deduplicated and stable.

    The event page spells the hosts in three ways — ``ownerIds`` (bare
    strings), ``owners`` (``{"id", "path"}`` objects) and ``createdBy`` (the
    single creator) — and on any given page only some of them are present. All
    three are read, so a jointly-hosted event is recognised however Partiful
    chooses to name its hosts.
    """
    ids = [str(uid) for uid in event.get("ownerIds") or [] if uid]
    for owner in event.get("owners") or []:
        if isinstance(owner, dict) and owner.get("id"):
            ids.append(str(owner["id"]))
    creator = event.get("createdBy")
    if isinstance(creator, dict) and creator.get("id"):
        ids.append(str(creator["id"]))
    return list(dict.fromkeys(ids))


def event_hosted_by_group(event: dict, group_hosts: set[str]) -> bool | None:
    """Whether one of `group_hosts` is among an event's hosts.

    None when the page carried no host information at all. An unknown event is
    kept, never dropped (see ``filter_unhosted``), so a Partiful markup change
    can't quietly empty the calendar.
    """
    owner_ids = event_owner_ids(event)
    if not owner_ids:
        return None
    return bool(set(owner_ids) & group_hosts)


def _extract_next_data(html: str) -> dict | None:
    """The parsed __NEXT_DATA__ blob from a Partiful event page, or None."""
    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _image_url_value(value: object) -> str | None:
    """A cover-image URL out of whatever shape the event page embeds.

    ``event.image`` is either a plain URL string or an object like
    ``{"url": …, "blurHash": …}`` (blurHash is a tiny placeholder encoding,
    not a URL). A storage object *name* (e.g. ``images/a.jpg``) is not a URL
    and is left for the regex fallback to resolve.
    """
    if isinstance(value, str):
        return value if value.startswith(("http://", "https://")) else None
    if isinstance(value, dict):
        for key in ("url", "src", "image", "imageUrl"):
            found = _image_url_value(value.get(key))
            if found:
                return found
    return None


def _first_image_url(text: str) -> str | None:
    """Any Firebase Storage URL in the page — a fallback for unknown shapes."""
    match = FIREBASE_IMAGE_URL_RE.search(text)
    return match.group(0) if match else None


def _extract_event_image(html: str, event: dict | None = None) -> str | None:
    """The event's cover image URL from its public Partiful event page.

    Structured path first (__NEXT_DATA__.props.pageProps.event.image), then a
    raw regex for a Firebase Storage URL anywhere in the page. Pass `event`
    when the caller has already parsed the page's __NEXT_DATA__, so the page
    is not parsed a second time just to read its image.
    """
    if event is None:
        event = _event_from_page(html)
    if event is not None:
        image = _image_url_value(event.get("image"))
        if image:
            return image
    return _first_image_url(html)


# --- Google Maps route links -------------------------------------------------
# The organizer attaches the ride's route to the Partiful event as a "custom
# field" — a labelled link ("Estimated Route", "Team A & C Route") pointing at
# a maps.app.goo.gl short link. Those live in __NEXT_DATA__ alongside the image
# the enrichment already reads, so grabbing them costs no new secret and no new
# page fetch; only resolving each short link needs a request of its own.
MAPS_LINK_HOSTS = (
    "maps.app.goo.gl",
    "goo.gl",
    "maps.google.com",
    "www.google.com",
    "google.com",
)
# A resolved Google Maps *directions* URL carries both endpoints. Two shapes
# turn up: the classic ?saddr=…&daddr=… query and the newer /maps/dir/A/B path.
MAPS_DIR_PATH_RE = re.compile(r"/maps/dir/+([^/@?]+)/+([^/@?]+)")


def _extract_custom_links(event: dict) -> list[dict]:
    """The event's labelled custom-field links, in the order the host set them."""
    links = []
    for field in event.get("customFields") or []:
        if not isinstance(field, dict):
            continue
        url = (field.get("url") or "").strip()
        label = (field.get("value") or "").strip()
        if url:
            links.append({"label": label or "Route", "url": url})
    return links


def _is_maps_link(url: str) -> bool:
    """True for a link worth resolving — anything Google Maps could shorten."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    host = host.lower()
    if host not in MAPS_LINK_HOSTS:
        return False
    if host in ("goo.gl", "www.google.com", "google.com"):
        # Only the maps paths on these hosts; goo.gl also shortens other things.
        return "/maps" in urlsplit(url).path
    return True


def _resolve_link(url: str) -> str:
    """Follow a short link to its final URL.

    maps.app.goo.gl serves a JavaScript interstitial to browser user-agents and
    a plain 302 to everyone else — so the sync's own User-Agent is what makes
    this work without running a browser.
    """
    response = requests.get(
        url,
        timeout=ENRICH_TIMEOUT_SECONDS,
        headers={"User-Agent": "boscafebikers-sync/1.0"},
        allow_redirects=True,
    )
    return response.url


def maps_geocode_points(query: dict) -> list[tuple[float, float]]:
    """Endpoint coordinates out of a maps URL's ``geocode`` token list.

    Google pairs each stop with a base64 token in ``geocode=t1;t2;…``. Each one
    is a tiny protobuf whose first two fields are 32-bit fixed ints: field 2
    (tag ``0x15``) is latitude and field 3 (tag ``0x1d``) longitude, both scaled
    by 1e6. Reading them beats geocoding the address strings — Nominatim
    resolves fewer than half of Google's free-form place names ("Bluebikes,
    Washington St at Temple Pl" and friends come back empty) — and beats
    scraping coordinates out of the maps page's minified JavaScript.

    Returns [] unless every token parses, so a partial read never produces a
    route that silently skips a stop.
    """
    raw_tokens = (query.get("geocode") or [""])[0]
    if not raw_tokens:
        return []
    points = []
    for token in raw_tokens.split(";"):
        token = token.strip()
        if not token:
            continue
        try:
            blob = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        except (ValueError, binascii.Error):
            return []
        if len(blob) < 10 or blob[0] != 0x15 or blob[5] != 0x1D:
            return []
        lat = struct.unpack("<i", blob[1:5])[0] / 1e6
        lon = struct.unpack("<i", blob[6:10])[0] / 1e6
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return []
        points.append((round(lat, 6), round(lon, 6)))
    return points


# Rides always start at a Bluebikes dock (the group meets there), so a route
# whose *end* is a dock and whose start isn't was built backwards in Google
# Maps — café → meeting point. The organizer did exactly that on at least one
# ride, which left the card's "from <place>" and the map's Start/End labels
# reversed. The dock is recognised structurally, by its leading address
# segment, the same way `route_map._start_name` reads one.
BLUEBIKES_PLACE_RE = re.compile(r"^bluebikes\b", re.IGNORECASE)


def _is_bluebikes(place: str) -> bool:
    """True when an address's first comma-segment names a Bluebikes dock."""
    first = str(place or "").split(",")[0].strip()
    return bool(BLUEBIKES_PLACE_RE.match(first))


def orient_route(route: dict) -> dict:
    """Flip a route that was entered café → Bluebikes dock, in place.

    Start and end swap, `via` stops reverse (the last one becomes the first)
    and so do `points`. Distance is the same either way, so `distance_m` /
    `distance_display` are left alone. A route that already starts at a dock —
    or one with no dock at either end, which is a ride that met somewhere else
    — is returned untouched.
    """
    start = route.get("start", "")
    end = route.get("end", "")
    if not _is_bluebikes(end) or _is_bluebikes(start):
        return route
    route["start"], route["end"] = end, start
    if route.get("via"):
        route["via"] = list(reversed(route["via"]))
    if route.get("points"):
        route["points"] = list(reversed(route["points"]))
    return route


def route_from_maps_url(url: str) -> dict | None:
    """Start, end, stops and coordinates out of a Google Maps directions URL.

    Returns None for a maps link that only points at a place — that's how a
    "Start/Bluebikes" pin is told apart from an actual route. A route entered
    backwards (café → Bluebikes dock) comes back flipped; see `orient_route`.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    query = parse_qs(parts.query)
    start = (query.get("saddr") or [""])[0].strip()
    destination = (query.get("daddr") or [""])[0].strip()
    if not (start and destination):
        match = MAPS_DIR_PATH_RE.search(parts.path)
        if not match:
            return None
        start = unquote_plus(match.group(1)).strip()
        destination = unquote_plus(match.group(2)).strip()
        if not (start and destination):
            return None
    # A multi-stop route packs its waypoints into daddr as "A to:B to:C": the
    # last one is where the ride ends, the rest are stops along the way.
    stops = [stop.strip() for stop in destination.split(" to:") if stop.strip()]
    route = {"start": start, "end": stops[-1] if stops else destination}
    if len(stops) > 1:
        route["via"] = stops[:-1]
    points = maps_geocode_points(query)
    # One coordinate per stop, or we don't trust the pairing.
    if len(points) == len(stops) + 1:
        route["points"] = [list(point) for point in points]
    # dirflg=b is Google's bicycling mode; keep whatever it says, if anything.
    mode = (query.get("dirflg") or [""])[0].strip()
    if mode:
        route["mode"] = mode
    return orient_route(route)


# --- route distance ----------------------------------------------------------
# Google publishes a route's distance only through its billed Directions API —
# the maps page computes it in JavaScript and the HTML carries no number. So the
# distance is *measured*, from the same stops Google was given, by BRouter: a
# keyless public cycling router (the `trekking` profile). It won't match
# Google's figure to the tenth of a mile, which is why the site says "~4.0 mi".
BROUTER_URL = "https://brouter.de/brouter"
BROUTER_PROFILE = "trekking"
METRES_PER_MILE = 1609.344


def _fetch_route_length(points: list) -> float | None:
    """Metres for a cycling route through `points`, or None. Never raises."""
    lonlats = "|".join(f"{point[1]},{point[0]}" for point in points)
    try:
        response = requests.get(
            BROUTER_URL,
            params={
                "lonlats": lonlats,
                "profile": BROUTER_PROFILE,
                "alternativeidx": "0",
                "format": "geojson",
            },
            timeout=ENRICH_TIMEOUT_SECONDS,
            headers={"User-Agent": "boscafebikers-sync/1.0"},
        )
        response.raise_for_status()
        properties = response.json()["features"][0]["properties"]
        return float(properties["track-length"])
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None  # soft: a ride without a distance just doesn't show one


def format_distance(metres: float) -> str:
    """Metres → the display string, e.g. '~4.0 mi'."""
    return f"~{metres / METRES_PER_MILE:.1f} mi"


def measure_route(route: dict, fetch_length: Callable[[list], float | None]) -> None:
    """Add `distance_m` / `distance_display` to a route, when it can be had."""
    points = route.get("points") or []
    if len(points) < 2:
        return
    metres = fetch_length(points)
    if metres and metres > 0:
        route["distance_m"] = round(metres)
        route["distance_display"] = format_distance(metres)


def rides_routes(
    event: dict,
    resolve_link: Callable[[str], str],
    fetch_length: Callable[[list], float | None] | None = None,
) -> list[dict]:
    """Every custom-field link on the event that is really a route.

    The `url` kept is the organizer's original short link, not the resolved
    one: it's what belongs behind a "Route" button, and it survives Google
    rewriting its long-URL format. Routes whose stops carry coordinates are
    also measured (see `measure_route`).
    """
    if fetch_length is None:
        fetch_length = _fetch_route_length
    routes = []
    for link in _extract_custom_links(event):
        if not _is_maps_link(link["url"]):
            continue
        try:
            resolved = resolve_link(link["url"])
        except (requests.RequestException, ValueError, TypeError):
            continue  # soft: an unresolvable link is just not a route
        route = route_from_maps_url(resolved or "")
        if route:
            route["label"] = link["label"]
            route["url"] = link["url"]
            measure_route(route, fetch_length)
            routes.append(route)
    return routes


def _fetch_event_page(url: str) -> str:
    response = requests.get(
        url,
        timeout=ENRICH_TIMEOUT_SECONDS,
        headers={"User-Agent": "boscafebikers-sync/1.0"},
    )
    response.raise_for_status()
    return response.text


def enrich_rides(
    rides: list[dict],
    fetch_page: Callable[[str], str] | None = None,
    resolve_link: Callable[[str], str] | None = None,
    fetch_length: Callable[[list], float | None] | None = None,
    group_hosts: set[str] | None = None,
) -> int:
    """Backfill each ride from its public Partiful event page.

    Three things come off the page: ``image`` (the cover photo), ``routes``
    (the host's labelled Google Maps route links — see ``rides_routes``) and
    ``hosted_by_group`` (whether one of ``group_hosts`` is an event host — see
    ``event_hosted_by_group``). Rides whose ``image`` is already set (the
    explicit ``ride_images.json`` sidecar wins) keep it, as do rides with no
    event-page URL. A fetch or parse failure is *not* a sync failure — a ride
    that can't be enriched keeps what it has. Returns how many rides had an
    ``image`` backfilled; ``routes`` and ``hosted_by_group`` are set as side
    effects (``routes`` to a list, empty when the event has none, which is how
    it's told apart from the None of a never-enriched ride; ``hosted_by_group``
    likewise left None when it can't be decided, so ``filter_unhosted`` never
    drops a ride whose page went unread).
    """
    if fetch_page is None:
        fetch_page = _fetch_event_page
    if resolve_link is None:
        resolve_link = _resolve_link
    backfilled = 0
    for ride in rides:
        page_url = ride.get("rsvp_url") or derive_partiful_url(ride.get("uid", ""))
        if not page_url or not page_url.startswith(PARTIFUL_EVENT_URL):
            continue
        try:
            html = fetch_page(page_url)
        except (requests.RequestException, ValueError, TypeError):
            continue  # soft: enrichment must never break the sync
        # Parse the page once; the image (with its raw-URL regex fallback, for
        # pages whose __NEXT_DATA__ doesn't parse) and the routes/host check
        # both read the same event object.
        event = _event_from_data(_extract_next_data(html))
        if not ride.get("image"):
            image = _extract_event_image(html, event)
            if image:
                ride["image"] = image
                backfilled += 1
        if event is not None:
            ride["routes"] = rides_routes(event, resolve_link, fetch_length)
            if group_hosts:
                ride["hosted_by_group"] = event_hosted_by_group(event, group_hosts)
    return backfilled


def filter_unhosted(
    rides: list[dict], group_hosts: set[str] | None = None
) -> tuple[list[dict], set[str]]:
    """Drop rides we know are *not* hosted by the group.

    ``enrich_rides`` sets ``hosted_by_group`` from the event page: True when one
    of ``group_hosts`` owns the event, False when the page was read and none
    did, and None when the page could not be read (or no allowlist is
    configured). Only a definite False is dropped — an unreadable page keeps
    its ride, so a Partiful hiccup can never hide a real one. With no
    ``group_hosts`` at all this is a no-op. Returns ``(kept, dropped_uids)``.
    """
    if not group_hosts:
        return rides, set()
    kept = [ride for ride in rides if ride.get("hosted_by_group") is not False]
    dropped = {
        ride["uid"]
        for ride in rides
        if ride.get("hosted_by_group") is False and ride.get("uid")
    }
    return kept, dropped


def _strip_rsvp(description: str) -> str:
    """The blurb without the RSVP/invite line (any of the three phrasings)."""
    return RSVP_RE.sub("", description).strip()


# Partiful appends " | Partiful" to every exported event title; strip it so the
# site shows the organizer's actual name. Real exports also carry stray runs of
# whitespace ("Boston Cafe Bikers        Ice Cream Crawl"), so tidy those too.
TITLE_SUFFIX_RE = re.compile(r"\s*\|\s*Partiful\s*$", re.IGNORECASE)

# Partiful hides the event address until guests RSVP; its ICS export then
# substitutes this placeholder for the real LOCATION value. It is not an
# address, so don't surface it verbatim — the site shows a friendly note
# instead and the .ics download omits it.
HIDDEN_LOCATION_RE = re.compile(
    r"^location\s+available\s+(?:once|after)\s+rsvp", re.IGNORECASE
)

# Some rides carry a *link* as their Location: the organizer pastes the meeting
# point's Google Maps URL into Partiful's Location field instead of an address.
# A bare URL is not a place name — rendered verbatim it's an unbreakable string
# that overflows the ride card — so it becomes `location_url` and the site shows
# a "Meeting point on Google Maps" link instead. The whole value has to be the
# link ("Meet at <url>" keeps its prose and stays a location).
BARE_URL_LOCATION_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def _clean_title(title: str) -> str:
    """Strip Partiful's ' | Partiful' title suffix and collapse whitespace."""
    return re.sub(r"\s+", " ", TITLE_SUFFIX_RE.sub("", title)).strip()


def _clean_location(value: str) -> tuple[str, bool, str | None]:
    """Return (location, hidden, url).

    ``location`` is the cleaned address ("" when Partiful's hidden-address
    placeholder is present, and also when the field holds nothing but a link);
    ``hidden`` is True exactly in the placeholder case; ``url`` is the meeting
    point's link when the organizer pasted one in place of an address, else
    None. The three are mutually exclusive readings of the *one* Location field
    Partiful gives an event — there is no start/end split to pull out.
    """
    value = value.strip()
    if HIDDEN_LOCATION_RE.match(value):
        return "", True, None
    if BARE_URL_LOCATION_RE.match(value):
        return "", False, value
    return value, False, None


def _clean_description(description: str) -> str:
    """Drop the RSVP/invite line, trim lines, and collapse runs of blank lines.

    Partiful prose is plain paragraphs; 3+ newlines (e.g. after the stripped
    invite line) should read as a single paragraph break, not a ragged gap.
    """
    body = _strip_rsvp(description)
    lines: list[str] = []
    blank = False
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            if not blank:
                lines.append("")
            blank = True
        else:
            lines.append(line)
            blank = False
    return "\n".join(lines).strip()


def is_upcoming(start: datetime, now: datetime) -> bool:
    """True while a ride still belongs in the *upcoming* list.

    That is until GRACE_PERIOD after its start — a ride is upcoming at exactly
    its start time (as it always was) and stays so through the last instant of
    its grace hour; one tick later it is past. `parse_events` uses this for both
    passes, so upcoming and past are exact complements and no ride can land in
    both files or in neither.
    """
    return start + GRACE_PERIOD >= now


def parse_events(
    data: bytes,
    now: datetime | None = None,
    images: dict | None = None,
    past: bool = False,
    excluded: set[str] | None = None,
    skipped: set[str] | None = None,
) -> list[dict]:
    """Parse feed bytes into a sorted list of non-cancelled rides.

    By default only rides that haven't happened yet; `past=True` returns the
    ones that already have, so the site can keep showing them on the calendar
    (see scripts/archive_events.py). "Yet" allows GRACE_PERIOD of slack after a
    ride's start — see `is_upcoming`. Either way the list is sorted by start.

    `images` is the optional UID → image-URL sidecar; each ride carries its
    photo URL as `image` (None when absent). `excluded` is the set of UIDs to
    leave out of either pass (see `load_excluded_events`); pass a set as
    `skipped` and the ones this pass actually left out are added to it, which
    is all the sync's run report needs to say how many the sidecar caught.
    """
    images = images or {}
    excluded = excluded or set()
    now = (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)
    try:
        calendar = Calendar.from_ical(data)
    except Exception as exc:  # icalendar raises bare ValueError subclasses
        raise FeedError(f"could not parse the ICS feed: {scrub(exc)}") from None

    rides = []
    for component in calendar.walk("VEVENT"):
        if _text(component, "STATUS").upper() == "CANCELLED":
            continue
        dtstart = component.get("DTSTART")
        if dtstart is None:
            raise FeedError("an event in the feed has no DTSTART")
        start = _as_local_datetime(dtstart.dt)
        if is_upcoming(start, now) == past:
            continue
        uid = _text(component, "UID")
        if uid in excluded:
            if skipped is not None:
                skipped.add(uid)
            continue
        description = _text(component, "DESCRIPTION")
        dtend = component.get("DTEND")
        location, location_hidden, location_url = _clean_location(
            _text(component, "LOCATION")
        )
        rides.append(
            {
                "uid": uid,
                "title": _clean_title(_text(component, "SUMMARY")) or "Café ride",
                "start": start.isoformat(),
                # End time is optional; the site's "add to calendar" ICS needs it
                # to block out the right slot, but not every feed event has one.
                "end": _as_local_datetime(dtend.dt).isoformat() if dtend is not None else None,
                "date_display": f"{start:%A, %B} {start.day}",
                "time_display": f"{start:%-I:%M %p}".replace("AM", "am").replace("PM", "pm"),
                # True when Partiful exported its 'Location available once
                # RSVP'd' placeholder instead of a real address.
                "location": location or None,
                "location_hidden": location_hidden,
                # Set when the Location field held a link (a Google Maps pin
                # for the meeting point) rather than an address; the card
                # renders it as a link, never as raw text.
                "location_url": location_url,
                "description": _clean_description(description),
                "rsvp_url": extract_rsvp_url(description) or derive_partiful_url(uid),
                "image": images.get(uid),
                # None until enrichment looks at the event page; a list (even
                # an empty one) means "we checked". archive_events relies on
                # that difference so a re-export can't erase known routes.
                "routes": None,
                # None until enrichment names the event's hosts; True/False
                # once it has (see filter_unhosted). A None here must never
                # overwrite a decided value in archive_events.merge_ride, so a
                # re-export can't un-decide a ride's ownership.
                "hosted_by_group": None,
            }
        )

    rides.sort(key=lambda ride: ride["start"])
    return rides


def build_payload(rides: list[dict], now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)
    return {
        "updated_at": now.isoformat(),
        "count": len(rides),
        "events": rides,
    }


def write_events(payload: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def load_source(ics_file: str | Path | None = None) -> bytes:
    """The feed bytes: from `ics_file`, else the secret PARTIFUL_ICS_URL."""
    if ics_file:
        path = Path(ics_file)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise FeedError(f"could not read {path}: {exc.strerror}") from None
    url = os.environ.get("PARTIFUL_ICS_URL", "").strip()
    if not url:
        raise FeedError(
            "PARTIFUL_ICS_URL is not set (or pass --ics-file for a local feed)"
        )
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    return fetch_ics(url)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ics-file",
        help="read the feed from this file instead of PARTIFUL_ICS_URL",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUTPUT),
        help=f"where to write the JSON (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--ride-images",
        default=str(RIDE_IMAGES_PATH),
        help=f"UID → image-URL sidecar JSON (default: {RIDE_IMAGES_PATH})",
    )
    parser.add_argument(
        "--excluded-events",
        default=str(EXCLUDED_EVENTS_PATH),
        help="UID → note sidecar JSON of feed events that are not group rides "
             f"(default: {EXCLUDED_EVENTS_PATH})",
    )
    parser.add_argument(
        "--group-hosts",
        default=str(GROUP_HOSTS_PATH),
        help="Partiful user-id allowlist JSON of accounts whose events we show; "
             "feed events hosted by nobody on the list are dropped "
             f"(default: {GROUP_HOSTS_PATH})",
    )
    parser.add_argument(
        "--past-out",
        help="also write the feed's already-happened rides here, for the "
             "archive scripts/archive_events.py merges into events-past.json",
    )
    args = parser.parse_args(argv)

    # One `now` for both passes: computing it twice could drop (or duplicate) a
    # ride that starts between the two calls.
    now = datetime.now(timezone.utc).astimezone(LOCAL_TZ)
    try:
        data = load_source(args.ics_file)
        images = load_ride_images(args.ride_images)
        excluded = load_excluded_events(args.excluded_events)
        group_hosts = load_group_hosts(args.group_hosts)
        rides = parse_events(data, now=now, images=images, excluded=excluded)
        past_rides = (
            parse_events(data, now=now, images=images, past=True, excluded=excluded)
            if args.past_out
            else []
        )
    except FeedError as exc:
        print(f"fetch_rides: {exc}", file=sys.stderr)
        return 1

    if not args.ics_file:
        # Only a live-feed run reaches out to the public event pages; local
        # --ics-file runs stay offline (fixture UIDs aren't real event ids).
        backfilled = enrich_rides(rides, group_hosts=group_hosts)
        if backfilled:
            print(
                f"fetch_rides: pulled images for {backfilled} ride(s) "
                "from their Partiful event pages"
            )
        rides, unhosted = filter_unhosted(rides, group_hosts)
        if unhosted:
            print(
                f"fetch_rides: dropped {len(unhosted)} event(s) not hosted by "
                "the group"
            )

    # The display fields the page reads instead of deriving (grace_until,
    # place_name, address, year, each route's start_name/end_name). Last, so
    # enrichment's routes are named too; scripts/sync.py does the same thing at
    # the same point, which is what keeps a hand run and a sync run identical.
    rides = ride_fields.derive_all(rides)
    past_rides = ride_fields.derive_all(past_rides)

    try:
        write_events(build_payload(rides, now=now), Path(args.out))
        if args.past_out:
            write_events(build_payload(past_rides, now=now), Path(args.past_out))
    except OSError as exc:
        target = exc.filename or args.out
        print(f"fetch_rides: could not write {target}: {exc.strerror}", file=sys.stderr)
        return 1

    print(f"fetch_rides: wrote {len(rides)} upcoming ride(s) to {args.out}")
    if args.past_out:
        print(f"fetch_rides: wrote {len(past_rides)} past ride(s) to {args.past_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
