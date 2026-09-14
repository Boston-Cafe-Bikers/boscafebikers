#!/usr/bin/env python3
"""Run the whole ride-data sync in one process, in the one order that works.

    python scripts/sync.py --data-dir _data      # on the sync runner
    python scripts/sync.py --data-dir site       # locally, after scripts/pull_data.sh

Fetch the feed, absorb the rides that have already happened, backfill a few
archived ones, mirror a few ride photos, geocode any new café, draw the maps
that are missing, publish the upcoming list, re-export the public calendar —
then write only the files
whose content actually changed. A quiet sync writes zero bytes, which is the
property the workflow's "commit if changed" guard depends on.

The order used to live in the YAML of the sync workflow, where nothing could
test it. It lives in ``STEPS`` now, and tests/test_sync.py reorders that tuple
to prove each rule still bites:

* **archive before promote** — the archive is merged from the *previously*
  committed events.json, so a ride that has since started is kept even if
  Partiful already dropped it from the feed.
* **maps before promote** — render_route_maps adds ``map_image``, which a fresh
  fetch never carries; add it after publishing and the committed file can never
  equal the next fetch, so every run commits a new ``updated_at`` forever.
* **posters before promote** — same rule, same reason: the poster mirror adds
  ``poster``, and a fetch never carries that either.
* **backfill between fetch and archive** — the listed rides the feed never had
  (scripts/backfill_events.json) are fetched by id from their event pages; it
  needs the fetch's lists to know which ids are already covered, and the
  archive is where its rides go.
* **the ICS export after promote** — its DTSTAMP comes from the payload's
  ``updated_at``, which is stamped fresh every run. Exported from the freshly
  fetched payload the calendar would churn every 6 hours; exported from the
  published events.json it only moves when the rides do.

Offline: ``--ics-file`` reads a local feed and makes zero network calls (no
enrichment, no geocoding, no router, no tiles), exactly like
``fetch_rides.py --ics-file``. Tests get the same offline run *with* the
enrichment steps by injecting stubs through ``run()``'s keyword seams.

Every external step is fail-soft, which is right and also silent: a Partiful
markup change would leave every new ride with no photo, no route and no map and
nothing would say so. So the run counts what it got — see ``Report`` — prints
the counts, writes them beside the data as ``sync-report.json``, and raises
GitHub annotations when a count says something is degraded. **Warnings are
annotations, never failures**: the exit status is exactly what it always was.

The feed URL comes from PARTIFUL_ICS_URL and is never printed — not in logs,
not in error messages. Exits nonzero only when the feed can't be fetched or
parsed (or a local data file is malformed); an enrichment miss is soft, as
everywhere else in this pipeline.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from argparse import Namespace
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

import archive_events  # noqa: E402
import backfill_events  # noqa: E402
import enrich_archive  # noqa: E402
import export_ics  # noqa: E402
import fetch_rides  # noqa: E402
import geocode_cafes  # noqa: E402
import promote_events  # noqa: E402
import render_route_maps  # noqa: E402
import ride_fields  # noqa: E402
import route_map  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "site"
DEFAULT_URL_PREFIX = render_route_maps.DEFAULT_URL_PREFIX
DEFAULT_ENRICH_LIMIT = enrich_archive.DEFAULT_LIMIT
DEFAULT_POSTER_LIMIT = 8
DEFAULT_BACKFILL_LIMIT = backfill_events.DEFAULT_LIMIT
LOCAL_TZ = fetch_rides.LOCAL_TZ


class SyncError(Exception):
    """A local data file is malformed. Fatal, like a feed parse failure."""


# --------------------------------------------------------------------------
# Configuration


@dataclass
class Config:
    """Where the data lives and how this run behaves.

    Every published path hangs off ``data_dir``; there is deliberately no
    hard-coded ``site/`` below this class, so the data can move out of the code
    branch by pointing --data-dir somewhere else.
    """

    data_dir: Path = DEFAULT_DATA_DIR
    ics_file: Optional[Path] = None
    ride_images: Path = fetch_rides.RIDE_IMAGES_PATH
    excluded_events: Path = fetch_rides.EXCLUDED_EVENTS_PATH
    # The class body sees the module `backfill_events` here, not the field it
    # is about to define — the default is evaluated before the name is bound.
    backfill_events: Path = backfill_events.BACKFILL_EVENTS_PATH
    now: Optional[datetime] = None
    enrich_limit: int = DEFAULT_ENRICH_LIMIT
    backfill_limit: int = DEFAULT_BACKFILL_LIMIT
    poster_limit: int = DEFAULT_POSTER_LIMIT
    url_prefix: str = DEFAULT_URL_PREFIX
    dry_run: bool = False

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        self.ics_file = Path(self.ics_file) if self.ics_file else None
        self.ride_images = Path(self.ride_images)
        self.excluded_events = Path(self.excluded_events)
        self.backfill_events = Path(self.backfill_events)
        self.url_prefix = str(self.url_prefix).strip("/")
        now = self.now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=LOCAL_TZ)
        # One `now` for the whole run: computing it per step could drop (or
        # duplicate) a ride that starts between two of them.
        self.now = now.astimezone(LOCAL_TZ)

    @property
    def events_path(self) -> Path:
        return self.data_dir / "events.json"

    @property
    def archive_path(self) -> Path:
        return self.data_dir / "events-past.json"

    @property
    def cafe_points_path(self) -> Path:
        return self.data_dir / "cafe-points.json"

    @property
    def rides_ics_path(self) -> Path:
        return self.data_dir / "rides.ics"

    @property
    def maps_dir(self) -> Path:
        return self.data_dir / "maps"

    @property
    def posters_dir(self) -> Path:
        """Where the mirrored ride photos live — see the poster section below."""
        return self.data_dir / POSTER_PREFIX

    @property
    def report_path(self) -> Path:
        """The run report, beside the data it describes (see ``Report``)."""
        return self.data_dir / "sync-report.json"

    @property
    def offline(self) -> bool:
        """A --ics-file run: the fixture UIDs aren't real Partiful event ids."""
        return self.ics_file is not None


def _no_network(*_args, **_kwargs):
    """Stand-in for a link resolver on an offline run.

    ValueError is what ``rides_routes`` already treats as "not a route", so an
    offline run simply finds no routes instead of failing.
    """
    raise ValueError("offline run: no network")


def _no_length(_points) -> None:
    """Stand-in for the router: ``measure_route`` expects None, never an error."""
    return None


@dataclass
class Seams:
    """The network calls, so a test can hand the run stubs instead.

    Each one falls back to its module's real implementation on a live run and
    is switched off on an offline (--ics-file) run — which is what keeps
    ``sync.py --ics-file`` byte-for-byte network-free while letting
    tests/test_sync.py exercise enrichment, geocoding and the maps offline.
    """

    fetch_page: Optional[Callable] = None
    resolve_link: Optional[Callable] = None
    fetch_length: Optional[Callable] = None
    fetch_geometry: Optional[Callable] = None
    fetch_tile: Optional[Callable] = None
    geocode_fetch: Optional[Callable] = None
    geocode_sleep: Optional[Callable] = None
    fetch_image: Optional[Callable] = None
    resize_image: Optional[Callable] = None
    offline: bool = False

    @property
    def enrich_enabled(self) -> bool:
        return self.fetch_page is not None or not self.offline

    @property
    def geocode_enabled(self) -> bool:
        return self.geocode_fetch is not None or not self.offline

    @property
    def maps_enabled(self) -> bool:
        return self.fetch_geometry is not None or not self.offline

    @property
    def posters_enabled(self) -> bool:
        return self.fetch_image is not None or not self.offline

    def poster_resizer(self) -> Optional[Callable]:
        """The bytes → web-sized-JPEG converter, or None if there isn't one.

        None means ImageMagick is not installed, which is a *skip*, never a
        failure: the cards keep pointing at the hotlinked original. A test
        hands in ``resize_image`` and never touches the machine's binaries.
        """
        if self.resize_image is not None:
            return self.resize_image
        binary = imagemagick()
        if binary is None:
            return None
        return lambda data: resize_poster(data, binary)

    def enrich_kwargs(self) -> dict:
        return {
            "fetch_page": self.fetch_page or fetch_rides._fetch_event_page,
            "resolve_link": self.resolve_link
            or (_no_network if self.offline else fetch_rides._resolve_link),
            "fetch_length": self.fetch_length
            or (_no_length if self.offline else fetch_rides._fetch_route_length),
        }

    def map_kwargs(self) -> dict:
        return {
            "fetch": self.fetch_geometry or render_route_maps.fetch_geometry,
            "fetch_tile": self.fetch_tile or render_route_maps.fetch_tile,
        }

    def geocode_kwargs(self) -> dict:
        return {
            "fetch": self.geocode_fetch or geocode_cafes.fetch_json,
            "sleep": self.geocode_sleep,
        }


# --------------------------------------------------------------------------
# Writes


class FileSet:
    """Every read and write of a published file goes through here.

    Two jobs. It compares against what was on disk when the run started, so
    "changed" means changed *for the commit*, however many steps touched a file
    on the way. And it holds an overlay of what has been written, so --dry-run
    can compute the whole run — including the steps that read back what an
    earlier step wrote — without touching the disk.
    """

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._original: dict = {}
        self._current: dict = {}

    def _seen(self, path) -> Path:
        path = Path(path)
        if path not in self._original:
            try:
                data = path.read_bytes()
            except OSError:
                data = None
            self._original[path] = data
            self._current[path] = data
        return path

    def read_bytes(self, path):
        return self._current[self._seen(path)]

    def read_text(self, path):
        data = self.read_bytes(path)
        return None if data is None else data.decode("utf-8")

    def read_json(self, path, default=None):
        text = self.read_text(path)
        if text is None:
            return default
        try:
            return json.loads(text)
        except ValueError:
            raise SyncError(f"{path} is not valid JSON") from None

    def write_bytes(self, path, data: bytes) -> bool:
        path = self._seen(path)
        if self._current[path] == data:
            return False
        self._current[path] = data
        if not self.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return True

    def write_text(self, path, text: str) -> bool:
        return self.write_bytes(path, text.encode("utf-8"))

    def changed(self) -> list:
        """The paths whose bytes differ from what the run started with."""
        return sorted(
            (p for p in self._current if self._current[p] != self._original[p]),
            key=str,
        )

    def known(self) -> list:
        """Every path this run has read or written — the overlay's keys."""
        return sorted(self._current, key=str)

    def original_json(self, path):
        """A file's JSON *as the run found it*, ignoring anything written since.

        This is how the report answers "which UIDs are new?" without a step
        having to remember: the FileSet already keeps the pre-run bytes.
        Tolerant — a report must never be the thing that fails a sync.
        """
        data = self._original[self._seen(path)]
        if data is None:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None


def _dump(payload: dict) -> str:
    """The JSON spelling every payload file in site/ is written with."""
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# The ride-photo mirror
#
# A ride with no drawn route map shows its Partiful poster, and that <img> used
# to point straight at the original upload: a Firebase Storage URL with no
# resize parameter, 0.1–3.5 MB, fetched from Google by every visitor's browser.
# So the sync keeps a small copy beside the rest of the data and the card
# prefers it — same instinct as the route maps' embedded tiles, one step
# further. `image` is never touched; it stays the fallback.

POSTER_PREFIX = "posters"  # the directory under --data-dir *and* the URL prefix
POSTER_MAX_PX = 800  # longest side; a phone card is ~380 CSS px, retina ~760
POSTER_QUALITY = 72
POSTER_TIMEOUT_SECONDS = 30  # the download
POSTER_RESIZE_TIMEOUT_SECONDS = 60  # the ImageMagick subprocess
POSTER_USER_AGENT = "boscafebikers-sync/1.0"
# ImageMagick 7 calls it `magick`, 6 called it `convert`; the Ubuntu runner has
# one of them preinstalled. Neither is a dependency — see poster_resizer().
IMAGEMAGICK_NAMES = ("magick", "convert")
MIRROR_HOSTS = ("firebasestorage.googleapis.com",)


class PosterFetchError(Exception):
    """A *transient* failure mirroring one photo — retried on the next run."""


def mirrorable(url) -> bool:
    """Whether this cover image is the organizer's own upload.

    Partiful puts what the host uploaded into Firebase Storage; everything else
    that can sit in the cover slot is somebody else's artwork the host picked
    off a shelf — Partiful's own stock posters (assets.getpartiful.com) or a
    Giphy GIF. Copying those into a public repo is not ours to decide, so only
    the Firebase ones are mirrored and the rest stay hotlinked exactly as they
    are today. Today's data: 20 of 34 ride photos are mirrorable, 7 are
    Partiful stock and 7 are Giphy.
    """
    if not isinstance(url, str):
        return False
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.netloc.rsplit("@", 1)[-1].split(":")[0].lower()
    if host in MIRROR_HOSTS or host.endswith(".appspot.com"):
        return True
    # The other spelling of the same bucket: storage.googleapis.com/<bucket>/…
    bucket = parts.path.lstrip("/").split("/", 1)[0].lower()
    return host == "storage.googleapis.com" and bucket.endswith(".appspot.com")


def resize_argv(src, dst, binary: str = "magick") -> list:
    """The ImageMagick command line that makes one web-sized poster.

    Pure, so a test can pin it: this argv *is* the whole image pipeline. There
    is deliberately no PIL — the stdlib-only rule holds and ImageMagick is a
    subprocess, exactly like nothing else here, but it is preinstalled on the
    runner and adding a compiled dependency to requirements.txt for eight
    thumbnails a month is not a trade worth making.

    * ``[0]`` takes the first frame, so an animated source writes one file
      instead of poster-0.jpg, poster-1.jpg, …
    * ``-auto-orient`` *before* ``-strip``: the EXIF rotation has to be applied
      while it still exists, or a phone photo comes out sideways.
    * ``-strip`` drops the metadata — which on a ride photo can include the
      camera's GPS fix, and none of it belongs on a public page.
    * ``800x800>`` only ever shrinks; a poster already smaller is left alone.
    * ``jpg:`` forces the output format from the command rather than the
      filename, so a PNG upload still lands as the JPEG the card expects.
    """
    return [
        binary,
        f"{src}[0]",
        "-auto-orient",
        "-strip",
        "-resize",
        f"{POSTER_MAX_PX}x{POSTER_MAX_PX}>",
        "-quality",
        str(POSTER_QUALITY),
        f"jpg:{dst}",
    ]


def imagemagick(which=None) -> Optional[str]:
    """ImageMagick's binary on this machine, or None when it isn't installed."""
    which = shutil.which if which is None else which
    for name in IMAGEMAGICK_NAMES:
        found = which(name)
        if found:
            return found
    return None


def resize_poster(data: bytes, binary: str) -> Optional[bytes]:
    """One photo's bytes, shrunk to a web-sized JPEG. None if that failed.

    Through temporary files, because ImageMagick is a separate process — but
    the result comes back as *bytes*, so ``FileSet`` stays the only thing that
    writes into the data dir and --dry-run still writes nothing.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "poster.in"
        dst = Path(tmp) / "poster.jpg"
        try:
            src.write_bytes(data)
            done = subprocess.run(
                resize_argv(src, dst, binary),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=POSTER_RESIZE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0 or not dst.exists():
            return None
        return dst.read_bytes() or None


def fetch_image(url: str) -> Optional[bytes]:
    """The bytes behind a ride photo. ``None`` means "and don't ask again".

    ``None`` is reserved for a definitive 4xx — the upload is gone, or its
    access token was rotated — because re-asking every six hours forever is
    both pointless and rude. Everything else (a timeout, a 5xx, a reset
    connection) raises, which leaves the ride's ``poster`` key absent so the
    next run simply tries again. Nothing here logs the URL: it carries a
    Firebase access token.
    """
    try:
        response = requests.get(
            url,
            timeout=POSTER_TIMEOUT_SECONDS,
            headers={"User-Agent": POSTER_USER_AGENT},
        )
    except requests.RequestException as exc:
        raise PosterFetchError(type(exc).__name__) from None
    if 400 <= response.status_code < 500:
        return None
    if response.status_code >= 300 or not response.content:
        raise PosterFetchError(f"HTTP {response.status_code}")
    return response.content


def pending_posters(rides: list) -> list:
    """Rides whose photo can be mirrored and hasn't been tried, newest first.

    A ``poster`` key that is present — a path *or* an explicit null — means
    this ride has had its turn. Same "absent is the never-checked marker" rule
    ``enrich_archive.pending`` uses for ``routes``, and the same newest-first
    order: this year's rides are the ones anybody scrolls to.
    """
    out = [
        ride
        for ride in rides
        if isinstance(ride, dict)
        and "poster" not in ride
        and mirrorable(ride.get("image"))
    ]
    out.sort(key=lambda ride: str(ride.get("start") or ""), reverse=True)
    return out


def poster_path(config: Config, ride: dict) -> Optional[Path]:
    """Where this ride's mirrored photo belongs. None for a ride with no UID."""
    uid = ride.get("uid")
    if not uid:
        return None
    return config.posters_dir / f"{render_route_maps.safe_name(uid)}.jpg"


def poster_url(target: Path) -> str:
    """Relative, always: the site is served from the /boscafebikers/ subpath."""
    return f"{POSTER_PREFIX}/{target.name}"


# --------------------------------------------------------------------------
# The run


@dataclass
class Run:
    """One sync, start to finish: the data in flight plus what was written."""

    config: Config
    seams: Seams
    files: FileSet
    payload: Optional[dict] = None
    feed_past: list = field(default_factory=list)
    excluded: set = field(default_factory=set)
    archive: Optional[list] = None
    archive_before: list = field(default_factory=list)
    # The backfill list (scripts/backfill_events.json): every id it names, the
    # records this run built from their pages — a source for the archive step —
    # and the ids whose page it couldn't read, which are retried next run.
    backfill_listed: list = field(default_factory=list)
    backfilled: list = field(default_factory=list)
    backfill_missed: list = field(default_factory=list)
    # True once events.json holds *this* run's upcoming payload. It is what
    # tells the later steps which file is "the published list" — and it is why
    # reordering the steps changes the result instead of quietly working.
    promoted: bool = False
    drawn: int = 0
    notes: list = field(default_factory=list)
    # Counters the report reads. Each is "what this run did", which is why they
    # are collected here and not recomputed afterwards from the files.
    skipped_uids: set = field(default_factory=set)
    posters_mirrored: int = 0
    posters_skipped: bool = False
    archive_enriched: int = 0
    archive_unchecked_before: int = 0
    geocode_stats: dict = field(default_factory=dict)
    report: Optional["Report"] = None

    def note(self, message: str) -> None:
        self.notes.append(message)

    # -- the two payload files ------------------------------------------
    def save_events(self) -> bool:
        return self.files.write_text(self.config.events_path, _dump(self.payload))

    def save_archive(self) -> bool:
        """Write the archive, but only when the rides in it actually moved.

        The same no-churn rule as the upcoming list: a fresh ``updated_at`` on
        an unchanged archive would make the workflow commit every 6 hours.
        """
        if self.archive is None or self.archive == self.archive_before:
            return False
        payload = archive_events.build_payload(self.archive, self.config.now)
        return self.files.write_text(self.config.archive_path, _dump(payload))

    def write_map(self, target, svg: str) -> None:
        """The write seam render_route_maps draws each SVG through."""
        self.files.write_text(Path(target), svg)


# --- steps -----------------------------------------------------------------


def _load_feed(config: Config) -> bytes:
    """The feed bytes, from --ics-file or from the secret URL. Never logged."""
    return fetch_rides.load_source(
        Namespace(ics_file=str(config.ics_file) if config.ics_file else None)
    )


def step_fetch(run: Run) -> None:
    """Parse the feed into the upcoming payload and the feed's past export.

    Both passes share one `now`, so upcoming and past stay exact complements.
    """
    config = run.config
    data = _load_feed(config)
    images = fetch_rides.load_ride_images(config.ride_images)
    run.excluded = fetch_rides.load_excluded_events(config.excluded_events)
    # `skipped` is filled by both passes: it is the sidecar's UIDs that were
    # really in the feed, which is the only excluded number worth reporting.
    rides = fetch_rides.parse_events(
        data,
        now=config.now,
        images=images,
        excluded=run.excluded,
        skipped=run.skipped_uids,
    )
    run.feed_past = fetch_rides.parse_events(
        data,
        now=config.now,
        images=images,
        past=True,
        excluded=run.excluded,
        skipped=run.skipped_uids,
    )
    if run.seams.enrich_enabled:
        backfilled = fetch_rides.enrich_rides(rides, **run.seams.enrich_kwargs())
        if backfilled:
            run.note(f"pulled images for {backfilled} upcoming ride(s)")
    # The precomputed display fields (grace_until, place_name, address, year,
    # each route's start_name/end_name) — after enrichment, so the routes it
    # just found are named too. Pure functions of what is already stored, so
    # this needs no network and every stored ride can be re-derived for free.
    rides = ride_fields.derive_all(rides)
    run.feed_past = ride_fields.derive_all(run.feed_past)
    run.payload = fetch_rides.build_payload(rides, now=config.now)
    run.note(
        f"{len(rides)} upcoming ride(s), {len(run.feed_past)} already-happened "
        "ride(s) in the feed"
    )


def step_backfill(run: Run) -> None:
    """Fetch the listed rides the feed never carried, for the archive step.

    After the fetch, because "already covered" means the feed's two lists and
    the excluded set as well as the archive — a listed id the feed also has is
    the feed's to deliver. Before the archive, because that is where these go.
    Nothing here writes a file, and nothing here can fail the sync: a page
    that can't be read is counted and retried next run.
    """
    config = run.config
    run.backfill_listed = backfill_events.load_backfill_events(config.backfill_events)
    if not run.backfill_listed:
        return
    existing = run.files.read_json(config.archive_path, default={}) or {}
    known = {
        archive_events.ride_key(ride)
        for ride in (
            list(existing.get("events") or [])
            + list((run.payload or {}).get("events") or [])
            + list(run.feed_past)
        )
        if isinstance(ride, dict)
    }
    known |= run.excluded
    queue = backfill_events.pending(run.backfill_listed, known)
    if not queue:
        return
    if not run.seams.enrich_enabled:
        run.note(
            f"backfill: {len(queue)} listed ride(s) not yet archived; "
            "offline run, none fetched"
        )
        return
    run.backfilled, run.backfill_missed = backfill_events.backfill(
        queue, limit=config.backfill_limit, **run.seams.enrich_kwargs()
    )
    message = (
        f"backfill: fetched {len(run.backfilled)} of {len(queue)} listed "
        "ride(s) not yet archived"
    )
    if run.backfill_missed:
        message += f"; {len(run.backfill_missed)} page(s) could not be read"
    run.note(message)


def step_archive(run: Run) -> None:
    """Fold the rides that have happened into the accumulating archive.

    Reads events.json *as it stands*, which is the whole point of running
    before the promote: it still holds the previous upcoming list, so a ride
    that has since started is archived even if the feed has dropped it.
    """
    config = run.config
    committed = run.files.read_json(config.events_path, default={}) or {}
    existing = run.files.read_json(config.archive_path, default={}) or {}
    run.archive_before = existing.get("events") or []
    # The backfill list's rides last: they are only ever ids nothing else
    # covers, so the order is moot, and merge_archive keeps just the ones that
    # have happened — an upcoming one waits for its grace hour.
    sources = [committed.get("events") or [], run.feed_past, run.backfilled]
    # Derived on the *merged* result: merge_ride's "None never overwrites" rule
    # decides which location survives, and the display fields have to follow it
    # rather than an older entry's. Running over the whole archive every sync is
    # also what backfills these fields — and propagates a rule change — without
    # a script and without touching the network.
    run.archive = ride_fields.derive_all(
        archive_events.merge_archive(
            run.archive_before, sources, config.now, excluded=run.excluded
        )
    )
    run.save_archive()
    # Before the backfill step runs, so the report can say whether this run
    # actually drained any of the queue or the backfill is stuck.
    run.archive_unchecked_before = len(enrich_archive.pending(run.archive))
    run.note(f"archive holds {len(run.archive)} past ride(s)")


def step_enrich_archive(run: Run) -> None:
    """Backfill a few never-checked archived rides from their event pages."""
    if run.archive is None or not run.seams.enrich_enabled:
        return
    limit = run.config.enrich_limit
    batch = enrich_archive.pending(run.archive)[:limit] if limit > 0 else []
    if not batch:
        return
    fetch_rides.enrich_rides(batch, **run.seams.enrich_kwargs())
    run.archive_enriched = len(batch)
    # The backfill just gave these rides routes; name them now rather than
    # leaving the labels a sync behind.
    run.archive = ride_fields.derive_all(run.archive)
    run.save_archive()
    remaining = len(enrich_archive.pending(run.archive))
    run.note(f"looked at {len(batch)} archived ride(s); {remaining} still unchecked")


def step_posters(run: Run) -> None:
    """Mirror a few ride photos onto the data branch, small enough to serve.

    Runs over the upcoming payload *and* the archive, like the maps step does,
    and for the same reason has to come before the promote: it adds ``poster``,
    which a fresh fetch never carries.

    Three fail-soft outcomes, none of them visible to a visitor, because
    ``image`` is left exactly as it was and the card falls back to it:

    * **No ImageMagick on the machine** — the whole step is skipped and the
      report prints a ``notice``. Never a failure; a sync that can't make
      thumbnails is a sync with no thumbnails, not a broken one.
    * **A definitive 4xx** — ``poster`` is set to ``null``. The ride has had
      its turn and is never asked about again.
    * **Anything else** (timeout, 5xx, a photo ImageMagick won't read) — the
      key is left absent, so the next run picks the ride up again.
    """
    if not run.seams.posters_enabled:
        return
    config = run.config
    rides = [
        ride
        for group in ((run.payload or {}).get("events") or [], run.archive or [])
        for ride in group
        if isinstance(ride, dict)
    ]
    # First re-point every ride at a copy that already exists. The upcoming
    # payload comes back from the feed with no `poster` at all, every run, so
    # without this pass the promote would see a changed rides list forever —
    # the same trap render_for_ride avoids for `map_image`. A ride re-pointed
    # here costs nothing and does not count against the limit.
    todo = []
    for ride in pending_posters(rides):
        target = poster_path(config, ride)
        if target is None:
            continue
        if run.files.read_bytes(target) is not None:
            ride["poster"] = poster_url(target)
        else:
            todo.append((ride, target))
    resize = run.seams.poster_resizer() if todo else None
    if todo and resize is None:
        # Nothing is broken: every card keeps the hotlinked original.
        run.posters_skipped = True
    elif todo:
        fetch = run.seams.fetch_image or fetch_image
        limit = config.poster_limit
        for ride, target in todo[:limit] if limit > 0 else []:
            try:
                data = fetch(ride.get("image"))
            except (PosterFetchError, requests.RequestException, ValueError, TypeError):
                continue  # transient: no key written, retried next run
            if data is None:
                ride["poster"] = None  # asked, answered — don't ask again
                continue
            jpeg = resize(data)
            if not jpeg:
                continue
            run.files.write_bytes(target, jpeg)
            ride["poster"] = poster_url(target)
            run.posters_mirrored += 1
        if run.posters_mirrored:
            run.note(f"mirrored {run.posters_mirrored} ride photo(s)")
    run.save_archive()
    if run.promoted:
        # Only reachable with the steps reordered: events.json already holds
        # this payload, so the `poster` just added has to be written again —
        # which is exactly the churn the correct order avoids.
        run.save_events()


def step_geocode(run: Run) -> None:
    """Place any café we haven't got coordinates for yet.

    Reads the archive this run just updated *and* the fresh upcoming payload,
    so a café is already on the map by the time its ride moves into the past.
    Cannot churn: a location already in the cache (hit or recorded miss) is
    never queried again, and the file is rewritten only when the mapping moved.
    """
    if not run.seams.geocode_enabled:
        return
    config = run.config
    payloads = [run.payload or {}, {"events": run.archive or []}]
    locations = geocode_cafes.cafe_locations(payloads)
    try:
        cache = geocode_cafes.load_cache(config.cafe_points_path)
    except geocode_cafes.GeocodeError as exc:
        raise SyncError(str(exc)) from None
    stats = geocode_cafes.update_cache(locations, cache, **run.seams.geocode_kwargs())
    run.geocode_stats = stats
    run.files.write_text(config.cafe_points_path, geocode_cafes.serialize(cache))
    if stats["queried"]:
        run.note(
            f"geocoded {stats['queried']} café location(s) "
            f"({stats['found']} found, {stats['missed']} missed, "
            f"{stats['deferred']} deferred)"
        )


def step_maps(run: Run) -> None:
    """Draw the route map of every ride that hasn't got a finished one.

    Idempotent: a ride whose SVG already carries its basemap is skipped and
    only re-pointed at it. The rides it touches gain ``map_image``, which is
    why this has to happen before the promote — see the module docstring.
    """
    if not run.seams.maps_enabled:
        return
    config = run.config
    kwargs = run.seams.map_kwargs()
    drawn = 0
    for rides in ((run.payload or {}).get("events") or [], run.archive or []):
        for ride in rides:
            if not isinstance(ride, dict):
                continue
            if render_route_maps.render_for_ride(
                ride,
                config.maps_dir,
                config.url_prefix,
                write=run.write_map,
                **kwargs,
            ):
                drawn += 1
    run.drawn = drawn
    run.save_archive()
    if run.promoted:
        # events.json already holds this payload, so the map_image just added
        # has to go in with it — which is exactly the churn the correct order
        # avoids, since the promote below would have carried it for free.
        run.save_events()
    if drawn:
        run.note(f"drew {drawn} route map(s)")


def step_promote(run: Run) -> None:
    """Publish the upcoming list — but only when the rides themselves moved."""
    config = run.config
    committed = run.files.read_json(config.events_path, default=None)
    current = committed.get("events") if isinstance(committed, dict) else None
    if promote_events.rides_changed(run.payload["events"], current):
        run.save_events()
    run.promoted = True


def step_export_ics(run: Run) -> None:
    """Re-export the public subscribable calendar from the published list.

    From the *published* file, never the fresh payload: DTSTAMP comes from
    ``updated_at``, so exporting the fetch would restamp rides.ics every run.
    """
    config = run.config
    payload = None
    if run.promoted:
        payload = run.files.read_json(config.events_path, default=None)
    if not isinstance(payload, dict):
        payload = run.payload or {}
    run.files.write_bytes(
        config.rides_ics_path, export_ics.build_calendar(payload).encode("utf-8")
    )


# The pipeline, as data. Reordering this tuple is how tests/test_sync.py shows
# each ordering rule earning its place; nothing else depends on the sequence.
STEPS = (
    ("fetch", step_fetch),
    ("backfill", step_backfill),
    ("archive", step_archive),
    ("enrich_archive", step_enrich_archive),
    ("posters", step_posters),
    ("geocode", step_geocode),
    ("maps", step_maps),
    ("promote", step_promote),
    ("export_ics", step_export_ics),
)


# --------------------------------------------------------------------------
# The report


@dataclass
class Report:
    """What this run found — the counts that make a silent degradation visible.

    Every external step here is fail-soft by design: an event page that stops
    carrying a photo leaves ``image: null``, an unreachable router leaves no
    distance, a tile outage leaves a map without its basemap, a café Nominatim
    can't place is recorded as a miss. That is the right behaviour and it is
    also completely silent — a Partiful markup change would strip every new
    ride of its poster, route and map and nobody would find out. So the run
    counts what it got.

    **A count is never a failure.** ``warnings()`` returns GitHub annotations;
    the exit status of ``main()`` is unchanged by any of them (nonzero only on
    a FeedError or a malformed local file), and it must stay that way.
    """

    # the feed
    upcoming: int = 0
    feed_past: int = 0
    excluded_skipped: int = 0
    # enrichment of the upcoming list
    enrichment_ran: bool = False
    with_image: int = 0
    with_route: int = 0
    routes_total: int = 0
    routes_measured: int = 0
    # the archive and its bounded backfill
    archive_size: int = 0
    archive_enriched: int = 0
    enrich_limit: int = 0
    archive_unchecked: int = 0
    archive_unchecked_before: int = 0
    # the backfill list — rides the feed never carried, fetched by id
    backfill_listed: int = 0
    backfill_fetched: int = 0
    backfill_missed: int = 0
    backfill_queued: int = 0
    # geocoding
    geocode_queried: int = 0
    geocode_found: int = 0
    geocode_missed: int = 0
    cafes_placed: int = 0
    cafes_unplaced: int = 0
    # maps
    maps_drawn: int = 0
    routes_without_map: int = 0
    maps_without_basemap: list = field(default_factory=list)
    # the ride-photo mirror
    posters_mirrored: int = 0
    posters_local: int = 0
    posters_mirrorable: int = 0
    imagemagick_missing: bool = False
    # stdout / summary only — deliberately not in state(), see below
    dry_run: bool = False
    written: list = field(default_factory=list)
    first_seen: list = field(default_factory=list)

    def state(self) -> dict:
        """The subset written to ``sync-report.json`` — and it cannot churn.

        The report lives on the data branch beside the data it describes, so it
        goes through the same "write only when the bytes changed" rule as
        everything else, and it has to be **byte-identical across two quiet
        runs** or the sync commits (and therefore deploys) every 6 hours
        forever. So this carries only facts that are the same on both runs:
        sizes *after* the run, and this run's hit rates on the freshly fetched
        feed (recomputed from scratch each time, so they don't depend on what
        the last run cached).

        Deliberately absent: any timestamp or ``now``; "drew N maps this run",
        "backfilled N archived rides", "looked up N cafés" (all of them are the
        work this run did *because* the last one hadn't, so they read N then 0);
        the written-files list; and the first-seen UIDs. Those go to stdout and
        the step summary, where a per-run number belongs.
        """
        return {
            "feed": {
                "upcoming": self.upcoming,
                "past": self.feed_past,
                "excluded": self.excluded_skipped,
            },
            "upcoming": {
                "with_image": self.with_image,
                "with_route": self.with_route,
                "routes": self.routes_total,
                "routes_measured": self.routes_measured,
            },
            "archive": {
                "rides": self.archive_size,
                "never_checked": self.archive_unchecked,
            },
            # The list's size and how much of it is still to land: both the
            # same on two quiet runs. "fetched this run" is stdout-only.
            "backfill": {
                "listed": self.backfill_listed,
                "queued": self.backfill_queued,
            },
            "cafes": {"placed": self.cafes_placed, "unplaced": self.cafes_unplaced},
            # Sizes after the run, so they are the same on a second quiet one.
            # "mirrored this run" is deliberately absent, like "drew N maps".
            "posters": {
                "mirrored": self.posters_local,
                "mirrorable": self.posters_mirrorable,
            },
            "maps": {
                "rides_with_a_route_and_no_map": self.routes_without_map,
                "without_a_basemap": len(self.maps_without_basemap),
            },
        }

    def lines(self) -> list:
        """The compact stdout block, one line per stage of the pipeline."""
        return [
            f"feed: {self.upcoming} upcoming, {self.feed_past} already happened, "
            f"{self.excluded_skipped} excluded",
            f"upcoming: {self.with_image}/{self.upcoming} with a photo, "
            f"{self.with_route}/{self.upcoming} with a route, "
            f"{self.routes_measured}/{self.routes_total} route(s) measured",
            f"archive: {self.archive_size} ride(s), {self.archive_enriched} backfilled "
            f"this run (limit {self.enrich_limit}), "
            f"{self.archive_unchecked} never checked",
            f"backfill: {self.backfill_fetched} listed ride(s) fetched this run, "
            f"{self.backfill_missed} page(s) unreadable, "
            f"{self.backfill_queued}/{self.backfill_listed} listed id(s) still "
            "not archived",
            f"cafes: {self.geocode_queried} looked up this run "
            f"({self.geocode_found} found, {self.geocode_missed} missed), "
            f"{self.cafes_placed} placed, {self.cafes_unplaced} unplaced",
            f"posters: {self.posters_mirrored} mirrored this run, "
            f"{self.posters_local}/{self.posters_mirrorable} mirrorable ride "
            "photo(s) held locally",
            f"maps: {self.maps_drawn} drawn this run, {self.routes_without_map} "
            f"ride(s) with a route and no map, "
            f"{len(self.maps_without_basemap)} without a basemap",
        ]

    def warnings(self) -> list:
        """``(level, message)`` for every count that says something degraded.

        Annotations only — nothing here changes an exit status. The two
        ``warning``s are the "someone else's site changed under us" signals;
        the ``notice``s are the softer "this is retrying, keep an eye on it".
        """
        notes = []
        if self.enrichment_ran and self.upcoming and not self.with_image:
            notes.append((
                "warning",
                f"enrichment came back empty: {self.upcoming} upcoming ride(s) "
                "and not one photo — Partiful's event pages may have changed",
            ))
        if not self.upcoming and not self.feed_past and self.archive_size:
            notes.append((
                "warning",
                "the feed carried no events at all while the archive holds "
                f"{self.archive_size} ride(s) — check the PARTIFUL_ICS_URL secret",
            ))
        if self.imagemagick_missing:
            notes.append((
                "notice",
                "no ImageMagick on this machine, so ride photos were not "
                "mirrored — the cards fall back to the hotlinked originals",
            ))
        if self.maps_without_basemap:
            notes.append((
                "notice",
                f"{len(self.maps_without_basemap)} route map(s) still without a "
                "basemap, redrawn next run: "
                f"{', '.join(self.maps_without_basemap)}",
            ))
        # A draining backfill is normal — say something only when it stopped
        # draining, which is what an unreachable Partiful looks like from here.
        if (
            self.enrichment_ran
            and self.enrich_limit > 0
            and self.archive_unchecked
            and self.archive_unchecked >= self.archive_unchecked_before
        ):
            notes.append((
                "notice",
                f"{self.archive_unchecked} archived ride(s) have never been "
                "checked and this run cleared none of them",
            ))
        # A listed id that never lands is a wrong id or an event Partiful no
        # longer serves. An upcoming listed ride is fetched every run until its
        # grace hour passes, so it counts as fetched and never trips this.
        if self.enrichment_ran and self.backfill_queued and not self.backfill_fetched:
            notes.append((
                "notice",
                f"{self.backfill_queued} id(s) in backfill_events.json are still "
                "not archived and this run fetched none of them — a wrong id, "
                "or an event Partiful no longer serves",
            ))
        return notes


def under_actions(env=None) -> bool:
    """Whether to spell the notes as GitHub workflow commands."""
    env = os.environ if env is None else env
    return str(env.get("GITHUB_ACTIONS", "")).lower() == "true"


def format_note(level: str, message: str, actions: bool) -> str:
    """A run annotation on the runner; a plain prefixed line in a terminal.

    ``::warning::…`` is a magic string only Actions understands, so locally it
    would just look like line noise — same text, ordinary prefix.
    """
    return f"::{level}::{message}" if actions else f"{level}: {message}"


def unfinished_maps(state: Run) -> list:
    """Maps drawn while the tile server was unreachable — retried next run.

    Read through the FileSet, so this sees the maps the run just drew (and,
    under --dry-run, the ones it would have drawn) rather than stale bytes.
    """
    maps_dir = state.config.maps_dir
    paths = set(maps_dir.glob("*.svg")) if maps_dir.is_dir() else set()
    paths |= {
        path
        for path in state.files.known()
        if path.suffix == ".svg" and path.parent == maps_dir
    }
    names = []
    for path in sorted(paths, key=lambda item: item.name):
        svg = state.files.read_text(path)
        if svg is not None and not route_map.has_basemap(svg):
            names.append(path.name)
    return names


def _cafe_cache(state: Run) -> dict:
    """The café cache as it stands after the run — tolerantly.

    Read here rather than taken from the geocode step, so an --ics-file run
    (which skips that step entirely) still reports how many cafés are placed.
    """
    empty = {"points": {}, "missing": []}
    try:
        data = state.files.read_json(state.config.cafe_points_path, default=None)
    except SyncError:
        return empty
    if not isinstance(data, dict):
        return empty
    points = data.get("points")
    missing = data.get("missing")
    return {
        "points": points if isinstance(points, dict) else {},
        "missing": missing if isinstance(missing, list) else [],
    }


def _first_seen(state: Run) -> list:
    """UIDs in neither the published list nor the archive when the run started.

    The FileSet already keeps every file's pre-run bytes, so this needs no
    bookkeeping from the steps.
    """
    known = set()
    for path in (state.config.events_path, state.config.archive_path):
        payload = state.files.original_json(path)
        for ride in (payload or {}).get("events") or []:
            if isinstance(ride, dict) and ride.get("uid"):
                known.add(ride["uid"])
    seen = []
    for ride in list((state.payload or {}).get("events") or []) + list(
        state.archive or []
    ):
        uid = ride.get("uid") if isinstance(ride, dict) else None
        if uid and uid not in known and uid not in seen:
            seen.append(uid)
    return seen


def build_report(state: Run) -> Report:
    """Count what this run found. Never raises — it is only a report."""
    rides = [
        ride for ride in (state.payload or {}).get("events") or []
        if isinstance(ride, dict)
    ]
    archive = [ride for ride in state.archive or [] if isinstance(ride, dict)]
    routes = [
        route
        for ride in rides
        for route in ride.get("routes") or []
        if isinstance(route, dict)
    ]
    cache = _cafe_cache(state)
    stats = state.geocode_stats or {}
    stored = rides + archive
    # What the backfill list still owes: every listed id the sync has no
    # record of — not archived, not in the feed, not excluded. Recomputed from
    # the finished archive so it reads 0 once the list has drained.
    covered = {
        archive_events.ride_key(ride) for ride in stored + list(state.feed_past)
    } | set(state.excluded)
    return Report(
        upcoming=len(rides),
        feed_past=len(state.feed_past),
        excluded_skipped=len(state.skipped_uids),
        enrichment_ran=state.seams.enrich_enabled,
        with_image=sum(1 for ride in rides if ride.get("image")),
        with_route=sum(1 for ride in rides if ride.get("routes")),
        routes_total=len(routes),
        routes_measured=sum(1 for route in routes if route.get("distance_m")),
        archive_size=len(archive),
        archive_enriched=state.archive_enriched,
        enrich_limit=state.config.enrich_limit,
        archive_unchecked=len(enrich_archive.pending(archive)),
        archive_unchecked_before=state.archive_unchecked_before,
        backfill_listed=len(state.backfill_listed),
        backfill_fetched=len(state.backfilled),
        backfill_missed=len(state.backfill_missed),
        backfill_queued=len(backfill_events.pending(state.backfill_listed, covered)),
        geocode_queried=stats.get("queried", 0),
        geocode_found=stats.get("found", 0),
        geocode_missed=stats.get("missed", 0),
        cafes_placed=len(cache["points"]),
        cafes_unplaced=len(cache["missing"]),
        maps_drawn=state.drawn,
        posters_mirrored=state.posters_mirrored,
        posters_local=sum(1 for ride in stored if ride.get("poster")),
        posters_mirrorable=sum(1 for ride in stored if mirrorable(ride.get("image"))),
        imagemagick_missing=state.posters_skipped,
        # A route link the maps step could not turn into a drawing: no
        # coordinates in the URL, or the router never answered.
        routes_without_map=sum(
            1
            for ride in rides + archive
            if ride.get("routes") and not ride.get("map_image")
        ),
        maps_without_basemap=unfinished_maps(state),
        dry_run=state.config.dry_run,
    )


def summary_markdown(report: Report) -> str:
    """The report as a markdown block for $GITHUB_STEP_SUMMARY."""
    rows = [
        ("Upcoming rides in the feed", str(report.upcoming)),
        ("Already-happened rides in the feed", str(report.feed_past)),
        ("Feed events skipped by the exclusion list", str(report.excluded_skipped)),
        ("Upcoming rides with a photo", f"{report.with_image} / {report.upcoming}"),
        ("Upcoming rides with a route", f"{report.with_route} / {report.upcoming}"),
        (
            "Routes with a measured distance",
            f"{report.routes_measured} / {report.routes_total}",
        ),
        ("Rides in the archive", str(report.archive_size)),
        (
            "Archived rides backfilled this run",
            f"{report.archive_enriched} / {report.enrich_limit}",
        ),
        ("Archived rides never checked", str(report.archive_unchecked)),
        ("Listed rides fetched this run (backfill list)", str(report.backfill_fetched)),
        (
            "Listed rides still not archived",
            f"{report.backfill_queued} / {report.backfill_listed}",
        ),
        (
            "Café locations looked up this run",
            f"{report.geocode_queried} "
            f"({report.geocode_found} found, {report.geocode_missed} missed)",
        ),
        ("Café locations placed", str(report.cafes_placed)),
        ("Café locations still unplaced", str(report.cafes_unplaced)),
        ("Ride photos mirrored this run", str(report.posters_mirrored)),
        (
            "Ride photos held locally",
            f"{report.posters_local} / {report.posters_mirrorable} mirrorable",
        ),
        ("Route maps drawn this run", str(report.maps_drawn)),
        ("Rides with a route and no map", str(report.routes_without_map)),
        ("Maps still without a basemap", str(len(report.maps_without_basemap))),
    ]
    out = ["### Sync report", "", "| What | Count |", "| --- | --- |"]
    out += [f"| {label} | {value} |" for label, value in rows]
    out.append("")
    verb = "Files that would change" if report.dry_run else "Files written"
    if report.written:
        out.append(f"**{verb}:** " + ", ".join(f"`{path}`" for path in report.written))
    else:
        out.append("**Nothing changed** — the rides are exactly as they were.")
    out.append("")
    if report.first_seen:
        out.append(
            "**First seen this run:** "
            + ", ".join(f"`{uid}`" for uid in report.first_seen)
        )
        out.append("")
    return "\n".join(out) + "\n"


def print_report(report: Report, env=None) -> None:
    """The compact counts block, then the annotations, on stdout.

    Annotations, not failures: nothing printed here changes an exit status.
    That is the owner's rule — a degraded sync still publishes what it could
    get, and a red run every time Partiful hiccups teaches everyone to ignore
    the red.
    """
    for line in report.lines():
        print(f"sync: {line}")
    if report.first_seen:
        print(f"sync: first seen this run: {', '.join(report.first_seen)}")
    actions = under_actions(env)
    for level, message in report.warnings():
        print(format_note(level, message, actions))


def write_step_summary(report: Report, env=None) -> bool:
    """Append the report to $GITHUB_STEP_SUMMARY, when the runner set one.

    The workflow appends the fenced stdout log after this step, so the table
    lands above it. Fail-soft, like everything else here: a summary that can't
    be written is not a reason to fail a sync.
    """
    env = os.environ if env is None else env
    target = env.get("GITHUB_STEP_SUMMARY")
    if not target:
        return False
    try:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(summary_markdown(report))
    except OSError:
        return False
    return True


def run(
    config: Config,
    *,
    fetch_page: Optional[Callable] = None,
    resolve_link: Optional[Callable] = None,
    fetch_length: Optional[Callable] = None,
    fetch_geometry: Optional[Callable] = None,
    fetch_tile: Optional[Callable] = None,
    geocode_fetch: Optional[Callable] = None,
    geocode_sleep: Optional[Callable] = None,
    fetch_image: Optional[Callable] = None,
    resize_image: Optional[Callable] = None,
    steps=None,
) -> Run:
    """Run the pipeline. Returns the finished Run (``.files.changed()`` etc.).

    The keyword arguments are the network seams — pass one and that call is
    stubbed, which is how the tests run the whole sync offline. ``steps``
    overrides ``STEPS`` for the ordering tests.
    """
    seams = Seams(
        fetch_page=fetch_page,
        resolve_link=resolve_link,
        fetch_length=fetch_length,
        fetch_geometry=fetch_geometry,
        fetch_tile=fetch_tile,
        geocode_fetch=geocode_fetch,
        geocode_sleep=geocode_sleep,
        fetch_image=fetch_image,
        resize_image=resize_image,
        offline=config.offline,
    )
    state = Run(config=config, seams=seams, files=FileSet(dry_run=config.dry_run))
    for _name, step in (steps or STEPS):
        step(state)
    state.report = build_report(state)
    # The report rides along with the data it describes, through the same
    # writer — so it obeys the same "only when the bytes changed" rule and a
    # --dry-run computes it without touching the disk. See Report.state() for
    # why it holds no per-run numbers.
    state.files.write_text(config.report_path, _dump(state.report.state()))
    # Filled after that write, so the list can include the report itself.
    state.report.written = [str(path) for path in state.files.changed()]
    state.report.first_seen = _first_seen(state)
    return state


# --------------------------------------------------------------------------
# CLI


def parse_now(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--now needs an ISO timestamp, not {value!r}"
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LOCAL_TZ)
    return moment.astimezone(LOCAL_TZ)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="where events.json, events-past.json, cafe-points.json, rides.ics, "
        f"maps/ and posters/ live (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--ics-file",
        help="read the feed from this file instead of PARTIFUL_ICS_URL; makes "
        "the whole run offline (no enrichment, geocoding, router or tiles)",
    )
    parser.add_argument(
        "--ride-images",
        default=str(fetch_rides.RIDE_IMAGES_PATH),
        help=f"UID → image-URL sidecar (default: {fetch_rides.RIDE_IMAGES_PATH})",
    )
    parser.add_argument(
        "--excluded-events",
        default=str(fetch_rides.EXCLUDED_EVENTS_PATH),
        help="UID → note sidecar of feed events that are not group rides "
        f"(default: {fetch_rides.EXCLUDED_EVENTS_PATH})",
    )
    parser.add_argument(
        "--backfill-events",
        default=str(backfill_events.BACKFILL_EVENTS_PATH),
        help="event id → note sidecar of rides the feed never carried, fetched "
        f"from their Partiful pages (default: {backfill_events.BACKFILL_EVENTS_PATH})",
    )
    parser.add_argument(
        "--now",
        type=parse_now,
        help="pin the clock to this ISO timestamp (tests; Eastern if naive)",
    )
    parser.add_argument(
        "--enrich-limit",
        type=int,
        default=DEFAULT_ENRICH_LIMIT,
        help="most archived rides to backfill per run "
        f"(default: {DEFAULT_ENRICH_LIMIT}, 0 for none)",
    )
    parser.add_argument(
        "--backfill-limit",
        type=int,
        default=DEFAULT_BACKFILL_LIMIT,
        help="most listed rides to fetch per run "
        f"(default: {DEFAULT_BACKFILL_LIMIT}, 0 for none)",
    )
    parser.add_argument(
        "--poster-limit",
        type=int,
        default=DEFAULT_POSTER_LIMIT,
        help="most ride photos to mirror per run "
        f"(default: {DEFAULT_POSTER_LIMIT}, 0 for none)",
    )
    parser.add_argument(
        "--url-prefix",
        default=DEFAULT_URL_PREFIX,
        help=f"path prefix written into map_image (default: {DEFAULT_URL_PREFIX}); "
        "must stay relative",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute everything and write nothing; report what would change",
    )
    return parser


def main(argv: list = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config(
        data_dir=args.data_dir,
        ics_file=args.ics_file,
        ride_images=args.ride_images,
        excluded_events=args.excluded_events,
        backfill_events=args.backfill_events,
        now=args.now,
        enrich_limit=args.enrich_limit,
        backfill_limit=args.backfill_limit,
        poster_limit=args.poster_limit,
        url_prefix=args.url_prefix,
        dry_run=args.dry_run,
    )
    try:
        state = run(config)
    except (
        fetch_rides.FeedError,
        archive_events.ArchiveError,
        export_ics.ExportError,
        SyncError,
    ) as exc:
        print(f"sync: {fetch_rides.scrub(exc)}", file=sys.stderr)
        return 1
    except OSError as exc:
        target = getattr(exc, "filename", None) or config.data_dir
        print(f"sync: could not write {target}: {exc.strerror}", file=sys.stderr)
        return 1

    for note in state.notes:
        print(f"sync: {note}")
    verb = "would write" if config.dry_run else "wrote"
    changed = state.files.changed()
    for path in changed:
        print(f"sync: {verb} {path}")
    print(f"sync: {len(changed)} file(s) changed" if changed else "sync: no changes")

    write_step_summary(state.report)
    print_report(state.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
