#!/usr/bin/env python3
"""Merge rides that have already happened into site/events-past.json.

The site's calendar shows past rides as well as upcoming ones, but
site/events.json only ever holds the upcoming list — a ride vanishes from it
an hour after it starts (see GRACE_PERIOD). So the archive has to *accumulate*: it is the union of
everything it already held and every already-happened ride in the payload
files handed to it. That way history survives even if Partiful stops exporting
old events, which the feed gives no guarantee about.

    python scripts/archive_events.py --archive site/events-past.json \
        site/events.json "$RUNNER_TEMP/events-past.json"

Sources are ordered oldest → newest; a later source wins field by field, except
that a `null` never overwrites a value that is already there. That is what
keeps a ride's `image` after it moves into the past: it was enriched from its
Partiful event page while it was still upcoming, and the feed's own past-ride
export carries no photo.

Like promote_events.py this rewrites the archive only when the `events` list
actually changed, so the sync workflow doesn't commit a fresh timestamp every
6 hours. Exits 0 whether or not it wrote; nonzero only on real errors.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ride_fields  # noqa: E402

LOCAL_TZ = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARCHIVE = REPO_ROOT / "site" / "events-past.json"
# The same UID → note sidecar fetch_rides.py reads (its EXCLUDED_EVENTS_PATH;
# duplicated because this module is stdlib-only). Excluded rides are skipped
# from every source *and purged from the archive itself*, so adding a UID is
# enough to remove an already-archived event on the next sync — no hand
# editing of events-past.json, which the feed would only re-fill.
DEFAULT_EXCLUDED = REPO_ROOT / "scripts" / "excluded_events.json"
# The same grace hour fetch_rides.py keeps a just-started ride in the upcoming
# export for (a latecomer can still catch the group). It has to be the same
# number here: the sync feeds this script the *previous* site/events.json, so a
# shorter rule would archive a ride that the fresh fetch still lists as
# upcoming, and app.js — which just concatenates the archive with events.json —
# would draw it on the calendar twice, once dimmed and once live. One
# definition, in the stdlib-only scripts/ride_fields.py so this module can
# import it too; tests/test_archive_events.py pins that neither side re-declares.
GRACE_PERIOD = ride_fields.GRACE_PERIOD


class ArchiveError(Exception):
    """A source file could not be read as an events payload."""


def load_payload(path: Path, required: bool = True) -> dict:
    """Read an events payload. A missing optional file reads as empty."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise ArchiveError(f"{path} does not exist") from None
        return {"events": []}
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"could not read {path}: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ArchiveError(f"{path} is not an events payload (no `events` list)")
    return data


def load_excluded(path: Path) -> set[str]:
    """The UIDs to leave out. A missing file means none; a broken one is an error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as exc:
        raise ArchiveError(f"could not read {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ArchiveError(f"{path} must be a JSON object of UID → note")
    return {str(uid) for uid in data}


def ride_key(ride: dict) -> str:
    """Identity of a ride across syncs: its feed UID, else start + title."""
    uid = ride.get("uid")
    if uid:
        return str(uid)
    return f"{ride.get('start')}|{ride.get('title')}"


def merge_ride(old: dict, new: dict) -> dict:
    """`new` wins field by field, but never replaces a value with None.

    Rides come back from the feed stripped of anything the sync added later
    (most visibly `image`, backfilled from the ride's Partiful page while it
    was still upcoming), so a plain dict update would erase them.
    """
    merged = dict(old)
    for key, value in new.items():
        if value is None and merged.get(key) is not None:
            continue
        merged[key] = value
    return merged


def is_past(ride: dict, now: datetime) -> bool:
    """True when the ride's start is far enough behind us to be history.

    Not simply `start < now`: a ride inside its GRACE_PERIOD is still the one
    happening right now and still sits in site/events.json, so archiving it
    there and then would put it on the calendar twice. `start` is an ISO string
    that already carries its offset, so comparing the parsed values is
    timezone-correct wherever this runs.
    """
    start = ride.get("start")
    if not isinstance(start, str):
        return False
    try:
        moment = datetime.fromisoformat(start)
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LOCAL_TZ)
    return moment + GRACE_PERIOD < now


def merge_archive(
    archive: list[dict],
    sources: list[list[dict]],
    now: datetime,
    excluded: set[str] | None = None,
) -> list[dict]:
    """Fold every already-happened ride from `sources` into `archive`.

    Rides already in the archive stay even if a source no longer mentions them
    — the feed pruning its history must not erase ours. Two exceptions: UIDs in
    `excluded` (from excluded_events.json) are dropped wherever they come from,
    the archive included; and a ride carrying ``hosted_by_group: false`` — an
    event enrichment has already found not to be the group's — is dropped too,
    so one archived before the host allowlist existed is purged. A ``None`` or
    missing flag is kept (never checked). Returns a new list sorted by start.
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    excluded = excluded or set()

    def absorb(rides: list[dict], past_only: bool) -> None:
        for ride in rides:
            if not isinstance(ride, dict):
                continue
            if past_only and not is_past(ride, now):
                continue
            key = ride_key(ride)
            if key in excluded:
                continue
            # An event enrichment has already found not to be hosted by the
            # group is history we don't keep either — this is what purges one
            # that slipped into the archive before the host allowlist existed.
            # None (never checked) is kept; only a decided False is dropped.
            if ride.get("hosted_by_group") is False:
                continue
            if key in merged:
                merged[key] = merge_ride(merged[key], ride)
            else:
                merged[key] = dict(ride)
                order.append(key)

    # Whatever the archive holds is history by definition — never re-filter it
    # on `now`, or a clock skew could silently drop rides we already keep.
    absorb(archive, past_only=False)
    for rides in sources:
        absorb(rides, past_only=True)

    return sorted(
        (merged[key] for key in order),
        key=lambda ride: (str(ride.get("start") or ""), ride_key(ride)),
    )


def build_payload(rides: list[dict], now: datetime) -> dict:
    return {"updated_at": now.isoformat(), "count": len(rides), "events": rides}


def write_archive(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def run(
    archive_path: Path,
    source_paths: list[Path],
    now: datetime | None = None,
    excluded_path: Path | None = None,
) -> bool:
    """Update the archive from the sources. True when the file was rewritten."""
    now = (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)
    existing = load_payload(archive_path, required=False)
    sources = [load_payload(path)["events"] for path in source_paths]
    excluded = load_excluded(excluded_path or DEFAULT_EXCLUDED)
    # Derived last, on the merged result, so `merge_ride`'s "None never
    # overwrites" rule can't leave a stale place_name behind: whatever the
    # merge settled on is what the display fields are computed from.
    rides = ride_fields.derive_all(
        merge_archive(existing["events"], sources, now, excluded=excluded)
    )
    if rides == existing["events"]:
        return False
    write_archive(build_payload(rides, now), archive_path)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        default=str(DEFAULT_ARCHIVE),
        help=f"the accumulating past-rides file (default: {DEFAULT_ARCHIVE})",
    )
    parser.add_argument(
        "--excluded",
        default=str(DEFAULT_EXCLUDED),
        help=f"UID → note JSON of events to leave out (default: {DEFAULT_EXCLUDED})",
    )
    parser.add_argument(
        "sources",
        nargs="+",
        metavar="SOURCE",
        help="events payload files to fold in, oldest first",
    )
    args = parser.parse_args(argv)

    archive_path = Path(args.archive)
    try:
        changed = run(
            archive_path,
            [Path(p) for p in args.sources],
            excluded_path=Path(args.excluded),
        )
    except ArchiveError as exc:
        print(f"archive_events: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"archive_events: could not write {archive_path}: {exc.strerror}", file=sys.stderr)
        return 1

    if changed:
        count = json.loads(archive_path.read_text(encoding="utf-8"))["count"]
        print(f"archive_events: {archive_path} now holds {count} past ride(s)")
    else:
        print("archive_events: no new past rides; keeping the committed file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
