#!/usr/bin/env python3
"""Draw a route map for every ride that has one, and point the ride at it.

    python scripts/render_route_maps.py --out-dir site/maps --url-prefix maps \\
        site/events.json site/events-past.json

For each ride whose first measured route carries coordinates, this fetches the
route geometry from BRouter and the basemap tiles under it from OpenStreetMap,
renders
them with scripts/route_map.py, writes ``<out-dir>/<uid>.svg``, and sets the
ride's ``map_image`` to the site-relative path. The ride card prefers
``map_image`` over the Partiful poster.

Idempotent by design: a ride whose finished SVG already exists is skipped, so
the 6-hour sync only ever draws maps for rides it hasn't drawn yet, and
re-running costs nothing. "Finished" means drawn with its basemap: a map drawn
while the tile server was unreachable is written anyway (the route alone beats
no map) but left unmarked, and gets redrawn on a later run. ``--redraw`` forces
every map to be drawn again, for when the style changes. Fail-soft everywhere —
a ride with no route, no coordinates, or an unreachable router simply keeps
whatever image it had.

Exits nonzero only if a payload file is missing or unparseable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ride_fields  # noqa: E402
import route_map  # noqa: E402

REPO_ROOT = ride_fields.REPO_ROOT
DEFAULT_OUT_DIR = REPO_ROOT / "site" / "maps"
DEFAULT_URL_PREFIX = "maps"
BROUTER_URL = "https://brouter.de/brouter"
BROUTER_PROFILE = "trekking"
# Basemap: OpenStreetMap's own standard-style raster tiles. Chosen because
# the tiles end up *stored* — embedded in SVGs that are committed forever —
# and OSM's is the one keyless server whose terms allow that: the data is
# ODbL and the standard style is public domain, so a served tile can be kept
# and redistributed with the "© OpenStreetMap contributors" credit the map
# draws. (CARTO's basemaps were tried first and rejected: their terms require
# an API key and forbid storing or redistributing tile content, including as
# static images, so embedding them would have been a breach however light the
# use.) OSM's tile usage policy asks for a clear, unique User-Agent and no
# bulk downloading; the sync sends its own UA and fetches 2-16 tiles per new
# ride, once, and never from a visitor's browser. The tiles carry the OSM
# credit already, so there is no separate provider line.
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_SOURCE = "osm-standard"
TILE_CREDIT = ""
USER_AGENT = "boscafebikers-sync/1.0"
TIMEOUT_SECONDS = 30
# UIDs become filenames, so keep them to something a URL and a filesystem can
# both hold without escaping.
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(uid: str) -> str:
    name = SAFE_NAME_RE.sub("-", str(uid or ""))
    # No separators survive the substitution above, but collapse dot runs too
    # so a ".." can never appear in a path this filename is joined into, and
    # tidy the dash runs the substitutions leave behind.
    name = re.sub(r"\.{2,}", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-.")
    return name[:80] or "ride"


def fetch_geometry(points: list) -> list:
    """The cycling route through `points` as [(lat, lon), …]. [] on failure."""
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
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        coordinates = response.json()["features"][0]["geometry"]["coordinates"]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return []
    # GeoJSON is [lon, lat, elevation?]; the renderer wants (lat, lon).
    return [(point[1], point[0]) for point in coordinates if len(point) >= 2]


def fetch_tile(zoom: int, x: int, y: int) -> bytes | None:
    """One basemap tile's bytes, as served. None on any failure."""
    try:
        response = requests.get(
            TILE_URL.format(z=zoom, x=x, y=y),
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
    except requests.RequestException:
        return None
    data = response.content
    return data if route_map.tile_mime(data) else None


def fetch_tiles(geometry: list, fetch_tile=fetch_tile) -> dict:
    """The tiles `route_map.tile_plan(geometry)` needs, keyed (zoom, x, y).

    All or nothing: the first miss empties the set, since the renderer would
    reject a partial basemap anyway and there is no point fetching the rest.
    """
    zoom, plan = route_map.tile_plan(geometry)
    tiles = {}
    for x, y in plan:
        data = fetch_tile(zoom, x, y)
        if not data:
            return {}
        tiles[(zoom, x, y)] = data
    return tiles


def write_svg(target: Path, svg: str) -> None:
    """Put one finished map on disk. The seam scripts/sync.py swaps out so a
    --dry-run can draw every map and write none of them."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(svg, encoding="utf-8")


def mappable_route(ride: dict) -> dict | None:
    """The first route on the ride that has enough coordinates to draw."""
    for route in ride.get("routes") or []:
        if isinstance(route, dict) and len(route.get("points") or []) >= 2:
            return route
    return None


def render_for_ride(
    ride: dict,
    out_dir: Path,
    url_prefix: str,
    fetch=fetch_geometry,
    fetch_tile=fetch_tile,
    redraw: bool = False,
    write=None,
) -> str | None:
    """Draw this ride's map if it needs one. Returns the new path, or None."""
    route = mappable_route(ride)
    if route is None:
        return None
    target = out_dir / f"{safe_name(ride.get('uid'))}.svg"
    url = f"{url_prefix}/{target.name}" if url_prefix else target.name
    existing = target.read_text(encoding="utf-8") if target.exists() else None
    if existing is not None:
        # Whatever happens below, the ride points at the map it has.
        ride["map_image"] = url
        if not redraw and route_map.has_basemap(existing):
            return None  # finished — nothing to fetch
    geometry = fetch(route["points"])
    if len(geometry) < 2:
        return None
    svg = route_map.render_route_svg(
        geometry,
        start=route.get("start", ""),
        end=route.get("end", ""),
        distance=route.get("distance_display", ""),
        title=f"{ride.get('title', 'Ride')} — route map",
        tiles=fetch_tiles(geometry, fetch_tile),
        tile_source=TILE_SOURCE,
        tile_credit=TILE_CREDIT,
    )
    if svg == existing:
        return None  # e.g. tiles still unavailable: the old drawing stands
    (write or write_svg)(target, svg)
    ride["map_image"] = url
    return url


def process(
    payload_path: Path,
    out_dir: Path,
    url_prefix: str,
    fetch=fetch_geometry,
    fetch_tile=fetch_tile,
    redraw: bool = False,
    write=None,
) -> int:
    """Render maps for one payload file. Returns how many were drawn."""
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"render_route_maps: could not read {payload_path}: {exc}")
    events = payload.get("events")
    if not isinstance(events, list):
        raise SystemExit(f"render_route_maps: {payload_path} has no `events` list")

    before = json.dumps(payload, sort_keys=True)
    drawn = 0
    for ride in events:
        if not isinstance(ride, dict):
            continue
        if render_for_ride(ride, out_dir, url_prefix, fetch, fetch_tile, redraw, write):
            drawn += 1
    after = json.dumps(payload, sort_keys=True)
    if before != after:
        payload_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    return drawn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payloads", nargs="+", metavar="PAYLOAD",
                        help="events payload files to render maps for")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                        help=f"where the SVGs go (default: {DEFAULT_OUT_DIR})")
    parser.add_argument("--url-prefix", default=DEFAULT_URL_PREFIX,
                        help="path prefix written into map_image "
                             f"(default: {DEFAULT_URL_PREFIX}); must stay relative")
    parser.add_argument("--redraw", action="store_true",
                        help="draw every map again, even the finished ones")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    total = 0
    for payload in args.payloads:
        total += process(Path(payload), out_dir, args.url_prefix.strip("/"), redraw=args.redraw)
    unfinished = [
        path.name for path in sorted(out_dir.glob("*.svg"))
        if not route_map.has_basemap(path.read_text(encoding="utf-8"))
    ] if out_dir.is_dir() else []
    print(f"render_route_maps: drew {total} route map(s) into {out_dir}")
    if unfinished:
        print(f"render_route_maps: {len(unfinished)} map(s) still without a basemap "
              f"(will retry next run): {', '.join(unfinished)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
