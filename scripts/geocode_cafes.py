#!/usr/bin/env python3
"""Geocode the café addresses in the ride payloads into site/cafe-points.json.

    python scripts/geocode_cafes.py --cache site/cafe-points.json \
        site/events-past.json site/events.json

cafes.html draws a pin per café on a Leaflet map, and the ride payloads carry
no coordinates — only the free-form `location` string the organizer typed into
Partiful. So the sync geocodes those strings once, through OpenStreetMap's
Nominatim, and commits the answers as a cache.

Two properties make that cheap and churn-free:

* **A known location is never queried again.** Both hits and resolved misses
  are remembered, so a run costs one request per café that has never been seen
  — which is zero on almost every sync, and one the week a new café appears.
* **The file is rewritten only when the mapping changed** (the same no-churn
  guard archive_events.py keeps). No timestamp is stamped into it, so a no-op
  run leaves the bytes byte-identical.

A *resolved* miss (Nominatim answered, with nothing) is recorded in "missing"
and never retried — `--retry-missing` is the manual escape hatch. A *transport*
failure (timeout, 5xx, no network) is not recorded at all, so a Nominatim
outage never poisons the cache; the location is simply looked at again next
sync. Either way the ride keeps its card and just gets no pin: fail-soft, like
every other enrichment here.

Nominatim's usage policy caps this at one request a second and asks for an
identifying User-Agent — DELAY and USER_AGENT below, and --limit bounds a run.

Stdlib only, and it never touches the ICS feed or its secret.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "boscafebikers-sync/1.0"
# Nominatim's usage policy: at most one request per second, absolute maximum.
DELAY = 1.1
TIMEOUT = 20
DEFAULT_LIMIT = 12
# Round to ~11cm. Nominatim's extra digits are noise on a city map and would
# only make the committed file churn on a re-query.
PRECISION = 6


class GeocodeError(Exception):
    """A malformed cache — the one thing worth failing the sync over."""


# --------------------------------------------------------------------------
# Locations out of the payloads


def cafe_locations(payloads: list) -> list:
    """Every distinct `location` string across the payloads, sorted.

    `location_url` rides are deliberately skipped: those short links resolve to
    the Bluebikes dock the ride met at, not to the café, so a pin drawn from one
    would put the wrong dot on a map of places we drank coffee. A ride with no
    location at all has nothing to geocode either.
    """
    seen = set()
    for payload in payloads:
        events = (payload or {}).get("events")
        if not isinstance(events, list):
            continue
        for ride in events:
            if not isinstance(ride, dict):
                continue
            location = ride.get("location")
            if isinstance(location, str) and location.strip():
                seen.add(location.strip())
    return sorted(seen)


def query_variants(location: str) -> list:
    """The search strings to try for one location, most precise first.

    Nominatim is good at street addresses and bad at the café names in front of
    them: measured on the real archive, "Localito Cafe, 30 Riverside Ave,
    Medford, MA" comes back empty while "30 Riverside Ave, Medford, MA" lands on
    the building. But when the name *does* resolve it is the better answer — the
    café's own POI rather than the middle of an address range — so the whole
    string goes first and the pieces come off only as fallbacks:

    1. the location as typed;
    2. without the leading name — but only when that segment is a *name*. A
       leading segment starting with a house number IS the address ("597
       Prospect St Apt B, New Haven, CT"), and dropping it would ask Nominatim
       for a bare city and get a pin in the middle of it, which is worse than
       no pin at all;
    3. name plus locality, i.e. the first segment and the last two, which is
       what rescues an address carrying a suite or a landmark in the middle
       ("City Hall Plaza, 1 City Hall Ave, Ste 500, Boston, MA").

    Duplicates collapse, so a short location simply gets fewer queries.
    """
    collapsed = " ".join(str(location or "").split())
    parts = [part.strip() for part in collapsed.split(",")]
    parts = [part for part in parts if part]
    if not parts:
        return []

    variants = [", ".join(parts)]
    # Fewer than four segments is a name and a locality with no street address
    # behind it, and trimming that only leaves a city to put a pin in.
    if len(parts) > 3:
        if not parts[0][:1].isdigit():
            variants.append(", ".join(parts[1:]))
        variants.append(", ".join([parts[0]] + parts[-2:]))

    out = []
    for variant in variants:
        if variant not in out:
            out.append(variant)
    return out


# --------------------------------------------------------------------------
# Nominatim


def fetch_json(url: str, timeout: int = TIMEOUT):
    """GET a JSON document with the identifying User-Agent the policy asks for."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def search_url(query: str) -> str:
    return NOMINATIM_URL + "?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "limit": "1", "countrycodes": "us"}
    )


def geocode(location: str, fetch=None, sleep=None) -> tuple:
    """Geocode one location. Returns `(point_or_None, resolved)`.

    `resolved` says whether Nominatim actually answered. `(None, True)` is a
    real miss worth remembering; `(None, False)` means the network failed and
    the location must stay unknown-but-unrecorded so a later run retries it.

    `fetch`/`sleep` are looked up here rather than bound as defaults, so a test
    that swaps the module attribute really does stay offline.
    """
    fetch = fetch or fetch_json
    sleep = sleep or time.sleep
    resolved = False
    for index, query in enumerate(query_variants(location)):
        if index:
            sleep(DELAY)
        try:
            results = fetch(search_url(query))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            # Transport or garbage-response failure — say nothing, try later.
            continue
        resolved = True
        point = first_point(results)
        if point:
            return point, True
    return None, resolved


def first_point(results) -> list:
    """`[lat, lon]` off Nominatim's first result, or `[]` if there isn't one."""
    if not isinstance(results, list) or not results:
        return []
    head = results[0]
    if not isinstance(head, dict):
        return []
    try:
        lat = round(float(head["lat"]), PRECISION)
        lon = round(float(head["lon"]), PRECISION)
    except (KeyError, TypeError, ValueError):
        return []
    return [lat, lon]


# --------------------------------------------------------------------------
# The cache


def load_cache(path: Path) -> dict:
    """Read the committed cache. A missing file is an empty one; junk is fatal."""
    if not path.exists():
        return {"points": {}, "missing": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GeocodeError(f"could not read {path}: {exc}")
    if not isinstance(data, dict):
        raise GeocodeError(f"{path} must hold a JSON object")
    points = data.get("points", {})
    missing = data.get("missing", [])
    if not isinstance(points, dict) or not isinstance(missing, list):
        raise GeocodeError(f"{path} needs a `points` object and a `missing` list")
    clean = {}
    for key, value in points.items():
        if (isinstance(value, list) and len(value) == 2
                and all(isinstance(n, (int, float)) for n in value)):
            clean[str(key)] = [float(value[0]), float(value[1])]
        else:
            raise GeocodeError(f"{path}: {key!r} is not a [lat, lon] pair")
    return {"points": clean, "missing": [str(name) for name in missing]}


def serialize(cache: dict) -> str:
    """Stable text: keys sorted, misses sorted, one trailing newline."""
    payload = {
        "points": {key: cache["points"][key] for key in sorted(cache["points"])},
        "missing": sorted(set(cache["missing"])),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def write_cache(path: Path, cache: dict) -> bool:
    """Write only when the bytes changed. Returns True if it wrote."""
    text = serialize(cache)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def update_cache(locations: list, cache: dict, limit: int = DEFAULT_LIMIT,
                 retry_missing: bool = False, fetch=None, sleep=None) -> dict:
    """Geocode the locations this cache has never resolved. Mutates `cache`."""
    fetch = fetch or fetch_json
    sleep = sleep or time.sleep
    known = set(cache["points"])
    if not retry_missing:
        known |= set(cache["missing"])
    todo = [name for name in locations if name not in known]
    if limit > 0:
        todo = todo[:limit]

    stats = {"queried": 0, "found": 0, "missed": 0, "deferred": 0, "pending": 0}
    for index, name in enumerate(todo):
        if index:
            sleep(DELAY)
        point, resolved = geocode(name, fetch=fetch, sleep=sleep)
        stats["queried"] += 1
        if point:
            cache["points"][name] = point
            # A location that once missed and now resolves stops being a miss.
            cache["missing"] = [m for m in cache["missing"] if m != name]
            stats["found"] += 1
        elif resolved:
            if name not in cache["missing"]:
                cache["missing"].append(name)
            stats["missed"] += 1
        else:
            # Nominatim never answered — leave no trace, retry next run.
            stats["deferred"] += 1
    stats["pending"] = max(
        0, len([n for n in locations if n not in known]) - len(todo))
    return stats


# --------------------------------------------------------------------------
# CLI


def read_payload(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GeocodeError(f"could not read {path}: {exc}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("payloads", nargs="+",
                        help="events payloads to collect café locations from")
    parser.add_argument("--cache", default="site/cafe-points.json",
                        help="the committed lat/lon cache (default: %(default)s)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="most locations to look up in one run "
                             "(default: %(default)s, 0 for no limit)")
    parser.add_argument("--retry-missing", action="store_true",
                        help="look up the recorded misses again as well")
    args = parser.parse_args(argv)

    try:
        payloads = [read_payload(Path(name)) for name in args.payloads]
        cache = load_cache(Path(args.cache))
    except GeocodeError as exc:
        print(f"geocode_cafes: {exc}", file=sys.stderr)
        return 1

    locations = cafe_locations(payloads)
    stats = update_cache(locations, cache, limit=args.limit,
                         retry_missing=args.retry_missing)
    wrote = write_cache(Path(args.cache), cache)

    print(f"geocode_cafes: {len(locations)} café locations, "
          f"{len(cache['points'])} with coordinates, "
          f"{len(cache['missing'])} unresolvable")
    if stats["queried"]:
        print(f"  looked up {stats['queried']} "
              f"({stats['found']} found, {stats['missed']} missed, "
              f"{stats['deferred']} deferred)")
    if stats["pending"]:
        print(f"  {stats['pending']} left for the next run (--limit)")
    print("  wrote " + args.cache if wrote else "  cache unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
