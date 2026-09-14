"""Tests for scripts/sync.py — the one-process pipeline and its step order.

Fully offline. The feed is tests/fixtures/sample.ics, the Partiful event page
is tests/fixtures/event-page.html, the basemap "tiles" are the 8x8 fixture PNG,
and the router / geocoder are stubs, all injected through ``sync.run``'s
keyword seams.

The property under test is the one the workflow's commit guard depends on: a
sync that finds nothing new writes zero bytes. Each ordering rule gets its own
test, which reorders ``sync.STEPS`` and asserts the specific damage.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import backfill_events  # noqa: E402
import fetch_rides  # noqa: E402
import geocode_cafes  # noqa: E402
import render_route_maps  # noqa: E402
import route_map  # noqa: E402
import sync  # noqa: E402


@pytest.fixture(autouse=True)
def _outside_actions(monkeypatch):
    """Every test starts outside GitHub Actions.

    `sync.main()` reads the real environment, and the runner sets both of
    these: GITHUB_ACTIONS flips the warning prefix to `::warning::` and
    GITHUB_STEP_SUMMARY would make a test append to the job's real summary.
    The first dev dispatch of the pytest gate failed on exactly that. Tests
    that want the Actions behaviour pass `env=` explicitly or setenv.
    """
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

EASTERN = ZoneInfo("America/New_York")
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "sample.ics"
EVENT_PAGE = (REPO_ROOT / "tests" / "fixtures" / "event-page.html").read_text(
    encoding="utf-8"
)
TILE = (REPO_ROOT / "tests" / "fixtures" / "tile.png").read_bytes()

# Pinned clock: after the fixture's one past ride (2024) and well before its
# two future ones (2030), so the partition never depends on the wall clock.
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=EASTERN)
# The second run happens a few minutes later — that is the whole point. Every
# run stamps a fresh `updated_at`; only the *files* have to stay still.
LATER = NOW + timedelta(minutes=7)

# A short Boston polyline, standing in for BRouter's geometry.
GEOMETRY = [
    (42.3355, -71.1506),
    (42.3400, -71.1600),
    (42.3500, -71.1700),
    (42.3668, -71.1868),
]

# What the fixture page's maps.app.goo.gl custom fields resolve to. Same pair
# tests/test_fetch_rides.py uses: one real directions URL (with the geocode
# tokens that give the route its coordinates) and one plain place pin.
RESOLVED_LINKS = {
    "https://maps.app.goo.gl/RouteShortLink1?g_st=ic": (
        "https://maps.google.com/?saddr=Bluebikes,+Cleveland+Circle,+Boston,+MA"
        "&daddr=Tatte+Bakery,+Boston,+MA&dirflg=b"
        "&geocode=FTf9hQId6lPC-ylRGlXiU3jjiTGY1w16bG3cRw%3D%3D;"
        "FWd3hgIdjcbB-ykdbpobJHnjiTGRSDuTYi0pzA%3D%3D"
    ),
    "https://maps.app.goo.gl/PlaceShortLink1?g_st=ic": (
        "https://maps.google.com?q=Bluebikes,+Cleveland+Circle&entry=gps"
    ),
}


# --- the stubbed network ---------------------------------------------------


def fake_page(url: str) -> str:
    """Every ride's Partiful page is the fixture page."""
    return EVENT_PAGE


def fake_resolve(url: str) -> str:
    if url in RESOLVED_LINKS:
        return RESOLVED_LINKS[url]
    raise ValueError(f"cannot resolve {url}")


def fake_length(points: list) -> float:
    """Stand-in for BRouter's distance: 1 km per leg."""
    return 1000.0 * (len(points) - 1)


def fake_geometry(points: list) -> list:
    return GEOMETRY


def fake_tile(zoom: int, x: int, y: int) -> bytes:
    return TILE


def fake_geocode(url: str) -> list:
    return [{"lat": "42.348765", "lon": "-71.123456"}]


def no_sleep(_seconds) -> None:
    pass


SEAMS = {
    "fetch_page": fake_page,
    "resolve_link": fake_resolve,
    "fetch_length": fake_length,
    "fetch_geometry": fake_geometry,
    "fetch_tile": fake_tile,
    "geocode_fetch": fake_geocode,
    "geocode_sleep": no_sleep,
}


# --- helpers ---------------------------------------------------------------


def config(data_dir: Path, now: datetime = NOW, **extra) -> sync.Config:
    return sync.Config(data_dir=data_dir, ics_file=FIXTURE, now=now, **extra)


def do_run(data_dir: Path, now: datetime = NOW, steps=None, **extra) -> sync.Run:
    return sync.run(config(data_dir, now, **extra), steps=steps, **SEAMS)


def ordered(*names) -> tuple:
    """``sync.STEPS`` in a different order — the same steps, nothing dropped."""
    by_name = dict(sync.STEPS)
    assert set(names) == set(by_name), "an ordering test must keep every step"
    return tuple((name, by_name[name]) for name in names)


def snapshot(data_dir: Path) -> dict:
    """Every file under the data dir, by relative path → bytes."""
    return {
        str(path.relative_to(data_dir)): path.read_bytes()
        for path in sorted(data_dir.rglob("*"))
        if path.is_file()
    }


def payload_of(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def uids(path: Path) -> set:
    """The UIDs in a payload. A file that was never written holds nothing —
    an archive with no rides in it is not written at all."""
    if not path.exists():
        return set()
    return {ride["uid"] for ride in payload_of(path)["events"]}


def feed_rides(now: datetime = NOW, past: bool = False) -> list:
    return fetch_rides.parse_events(FIXTURE.read_bytes(), now=now, past=past)


def write_payload(path: Path, rides: list, now: datetime = NOW) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            fetch_rides.build_payload(rides, now=now), indent=2, ensure_ascii=False
        )
        + "\n",
        encoding="utf-8",
    )


def dropped_ride() -> dict:
    """A ride that has already happened and is no longer in the feed.

    Partiful gives no guarantee it keeps exporting old events, so the only copy
    of this one is the previously committed events.json — which is exactly what
    the archive step reads, and exactly what it loses if it runs after the
    promote overwrites that file.
    """
    start = NOW - timedelta(hours=2)
    return {
        "uid": "evt-dropped-somerville@partiful.com",
        "title": "Somerville coffee crawl",
        "start": start.isoformat(),
        "end": None,
        "date_display": f"{start:%A, %B} {start.day}",
        "time_display": "10:00 am",
        "location": "Diesel Café, 257 Elm St, Somerville, MA 02144",
        "location_hidden": False,
        "location_url": None,
        "description": "Three stops, one pastry each.",
        "rsvp_url": None,
        "image": None,
        "routes": None,
    }


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    target = tmp_path / "site"
    target.mkdir()
    return target


@pytest.fixture
def seeded(data_dir: Path) -> Path:
    """A data dir that already holds a plausible previous sync's output."""
    write_payload(data_dir / "events.json", feed_rides(), now=NOW - timedelta(hours=6))
    write_payload(
        data_dir / "events-past.json", feed_rides(past=True), now=NOW - timedelta(hours=6)
    )
    return data_dir


# --- the run-twice invariant ----------------------------------------------


def test_a_second_run_over_an_empty_dir_changes_nothing(data_dir):
    first = do_run(data_dir)
    assert first.files.changed(), "the first run has to write something"
    before = snapshot(data_dir)

    second = do_run(data_dir, now=LATER)
    assert second.files.changed() == []
    assert snapshot(data_dir) == before


def test_a_second_run_over_a_seeded_dir_changes_nothing(seeded):
    do_run(seeded)
    before = snapshot(seeded)

    second = do_run(seeded, now=LATER)
    assert second.files.changed() == []
    assert snapshot(seeded) == before


def test_the_first_run_produces_the_whole_site_payload(data_dir):
    state = do_run(data_dir)
    written = {path.name for path in state.files.changed()}
    assert {"events.json", "events-past.json", "cafe-points.json", "rides.ics"} <= written
    assert any(path.suffix == ".svg" for path in state.files.changed())
    # Everything hangs off --data-dir, nothing off a hard-coded site/.
    for path in state.files.changed():
        assert data_dir in path.parents


def test_both_payloads_carry_the_precomputed_display_fields(data_dir):
    """ride_fields.derive runs on every stored ride, every run.

    They are a pure function of what is already in the file, so the archive
    picks them up on the next sync with no network and no backfill script.
    """
    do_run(data_dir)
    fields = ("grace_until", "place_name", "address", "year")
    for name in ("events.json", "events-past.json"):
        events = payload_of(data_dir / name)["events"]
        assert events, f"{name} has rides to check"
        for ride in events:
            assert all(field in ride for field in fields), name
    upcoming = payload_of(data_dir / "events.json")["events"][0]
    assert upcoming["grace_until"] == "2030-06-22T10:30:00-04:00"
    assert upcoming["place_name"] == "Tatte Bakery & Café"
    assert upcoming["year"] == "2030"
    # The enriched routes are named too — the map label and the card read these.
    route = upcoming["routes"][0]
    assert route["start_name"] == "Cleveland Circle"
    assert route["end_name"] == "Tatte Bakery"


def test_the_maps_are_drawn_with_their_basemap(data_dir):
    do_run(data_dir)
    svgs = sorted((data_dir / "maps").glob("*.svg"))
    assert svgs, "the enriched rides carry routes with coordinates"
    for svg in svgs:
        assert route_map.has_basemap(svg.read_text(encoding="utf-8"))
    # …and the ride points at the map, under the --url-prefix.
    events = payload_of(data_dir / "events.json")["events"]
    assert all(ride["map_image"].startswith("maps/") for ride in events)


def test_the_second_run_redraws_no_maps(data_dir):
    do_run(data_dir)
    drawn = do_run(data_dir, now=LATER).drawn
    assert drawn == 0


def test_the_calendar_is_byte_stable_though_updated_at_moves(data_dir):
    first = do_run(data_dir)
    calendar = (data_dir / "rides.ics").read_bytes()
    stamp = payload_of(data_dir / "events.json")["updated_at"]

    second = do_run(data_dir, now=LATER)
    # Each run really did stamp a fresh time on the payload it built…
    assert first.payload["updated_at"] != second.payload["updated_at"]
    # …and neither the published list nor the calendar moved because of it.
    assert payload_of(data_dir / "events.json")["updated_at"] == stamp
    assert (data_dir / "rides.ics").read_bytes() == calendar
    assert b"DTSTAMP:" in calendar


# --- one test per ordering rule -------------------------------------------


MAPS_AFTER_PROMOTE = ordered(
    "fetch", "backfill", "archive", "enrich_archive", "posters", "geocode", "promote", "maps",
    "export_ics",
)
ICS_BEFORE_PROMOTE = ordered(
    "fetch", "backfill", "archive", "enrich_archive", "posters", "geocode", "maps", "export_ics",
    "promote",
)
ARCHIVE_AFTER_PROMOTE = ordered(
    "fetch", "backfill", "promote", "archive", "enrich_archive", "posters", "geocode", "maps",
    "export_ics",
)
POSTERS_AFTER_PROMOTE = ordered(
    "fetch", "backfill", "archive", "enrich_archive", "geocode", "maps", "promote", "posters",
    "export_ics",
)


def test_maps_after_the_promote_restamps_events_json_every_run(data_dir):
    """`map_image` is added by the maps step and never by a fetch.

    Publish before drawing and the committed file can never equal the next
    fetch, so every run commits a fresh `updated_at` — forever.
    """
    do_run(data_dir, steps=MAPS_AFTER_PROMOTE)
    stamp = payload_of(data_dir / "events.json")["updated_at"]

    second = do_run(data_dir, now=LATER, steps=MAPS_AFTER_PROMOTE)
    assert config(data_dir).events_path in second.files.changed()
    assert payload_of(data_dir / "events.json")["updated_at"] != stamp
    # The rides themselves are identical; only the timestamp moved.
    assert [ride["uid"] for ride in second.payload["events"]] == [
        ride["uid"] for ride in payload_of(data_dir / "events.json")["events"]
    ]


def test_the_correct_order_leaves_events_json_alone(data_dir):
    """The mirror of the test above: drawn before publishing, nothing churns."""
    do_run(data_dir)
    stamp = payload_of(data_dir / "events.json")["updated_at"]
    second = do_run(data_dir, now=LATER)
    assert config(data_dir).events_path not in second.files.changed()
    assert payload_of(data_dir / "events.json")["updated_at"] == stamp


def test_exporting_the_calendar_before_the_promote_restamps_it_every_run(data_dir):
    """DTSTAMP comes from `updated_at`, which is fresh on every fetch.

    Built from the freshly fetched payload the .ics changes every 6 hours;
    built from the published events.json it moves only when the rides do.
    """
    do_run(data_dir, steps=ICS_BEFORE_PROMOTE)
    calendar = (data_dir / "rides.ics").read_bytes()

    second = do_run(data_dir, now=LATER, steps=ICS_BEFORE_PROMOTE)
    assert config(data_dir).rides_ics_path in second.files.changed()
    assert (data_dir / "rides.ics").read_bytes() != calendar
    # Nothing about the rides changed — only the stamp the export was given.
    assert config(data_dir).events_path not in second.files.changed()


def test_archiving_after_the_promote_loses_a_ride_the_feed_dropped(seeded):
    """The archive is merged from the *previously* committed events.json.

    Promote first and that file has already been overwritten with the fresh
    upcoming list, where the ride that has since started simply isn't.
    """
    rides = feed_rides() + [dropped_ride()]
    rides.sort(key=lambda ride: ride["start"])
    write_payload(seeded / "events.json", rides, now=NOW - timedelta(hours=6))

    do_run(seeded, steps=ARCHIVE_AFTER_PROMOTE)
    assert "evt-dropped-somerville@partiful.com" not in uids(seeded / "events-past.json")


def test_archiving_before_the_promote_keeps_a_ride_the_feed_dropped(seeded):
    """The correct order: the ride is absorbed before its only copy is replaced."""
    rides = feed_rides() + [dropped_ride()]
    rides.sort(key=lambda ride: ride["start"])
    write_payload(seeded / "events.json", rides, now=NOW - timedelta(hours=6))

    do_run(seeded)
    assert "evt-dropped-somerville@partiful.com" in uids(seeded / "events-past.json")
    # …and it is out of the upcoming list, so the calendar can't draw it twice.
    assert "evt-dropped-somerville@partiful.com" not in uids(seeded / "events.json")


def test_the_dropped_ride_stays_archived_on_later_runs(seeded):
    rides = feed_rides() + [dropped_ride()]
    rides.sort(key=lambda ride: ride["start"])
    write_payload(seeded / "events.json", rides, now=NOW - timedelta(hours=6))
    do_run(seeded)
    before = snapshot(seeded)

    second = do_run(seeded, now=LATER)
    assert second.files.changed() == []
    assert snapshot(seeded) == before


# --- --dry-run -------------------------------------------------------------


def test_dry_run_writes_nothing_but_says_what_it_would(data_dir):
    state = sync.run(config(data_dir, dry_run=True), **SEAMS)
    assert state.files.changed(), "it still computes the whole run"
    assert snapshot(data_dir) == {}


def test_dry_run_over_a_finished_dir_reports_nothing(data_dir):
    do_run(data_dir)
    before = snapshot(data_dir)
    state = sync.run(config(data_dir, now=LATER, dry_run=True), **SEAMS)
    assert state.files.changed() == []
    assert snapshot(data_dir) == before


def test_dry_run_reports_every_path_it_would_write(data_dir):
    """Under --dry-run the later steps still see what an earlier one wrote."""
    state = sync.run(config(data_dir, dry_run=True), **SEAMS)
    names = {path.name for path in state.files.changed()}
    # rides.ics is built from the events.json the promote step "wrote": if the
    # overlay didn't work, the export would have had nothing to read.
    assert {"events.json", "events-past.json", "rides.ics"} <= names


# --- excluded events -------------------------------------------------------


def test_excluded_uids_reach_neither_output(data_dir, tmp_path):
    excluded = tmp_path / "excluded.json"
    excluded.write_text(
        json.dumps(
            {
                "evt-future-minuteman@partiful.com": "not a ride",
                "evt-past-jamaica-pond@partiful.com": "a birthday",
            }
        ),
        encoding="utf-8",
    )
    do_run(data_dir, excluded_events=excluded)

    upcoming = uids(data_dir / "events.json")
    assert "evt-future-minuteman@partiful.com" not in upcoming
    assert "evt-past-jamaica-pond@partiful.com" not in uids(data_dir / "events-past.json")
    assert "evt-future-charles-loop@partiful.com" in upcoming


def test_an_excluded_uid_is_purged_from_an_archive_that_already_holds_it(data_dir):
    do_run(data_dir)
    assert "evt-past-jamaica-pond@partiful.com" in uids(data_dir / "events-past.json")

    excluded = data_dir.parent / "excluded.json"
    excluded.write_text(
        json.dumps({"evt-past-jamaica-pond@partiful.com": "a birthday"}),
        encoding="utf-8",
    )
    do_run(data_dir, now=LATER, excluded_events=excluded)
    assert payload_of(data_dir / "events-past.json")["events"] == []


# --- offline is really offline --------------------------------------------


def test_an_ics_file_run_switches_every_network_seam_off():
    seams = sync.Seams(offline=True)
    assert not seams.enrich_enabled
    assert not seams.geocode_enabled
    assert not seams.maps_enabled
    live = sync.Seams(offline=False)
    assert live.enrich_enabled and live.geocode_enabled and live.maps_enabled


def test_the_cli_makes_no_network_calls_on_an_ics_file_run(data_dir, monkeypatch, capsys):
    """--ics-file is the offline contract: no page fetch, no router, no tiles,
    no Nominatim. Every transport this pipeline can reach is booby-trapped."""

    def forbidden(*args, **kwargs):
        raise AssertionError("an --ics-file run must not touch the network")

    monkeypatch.setattr(fetch_rides.requests, "get", forbidden)
    monkeypatch.setattr(render_route_maps.requests, "get", forbidden)
    monkeypatch.setattr(geocode_cafes.urllib.request, "urlopen", forbidden)
    monkeypatch.delenv("PARTIFUL_ICS_URL", raising=False)

    code = sync.main(
        [
            "--data-dir", str(data_dir),
            "--ics-file", str(FIXTURE),
            "--now", NOW.isoformat(),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "wrote" in out and "file(s) changed" in out
    assert (data_dir / "events.json").exists()
    # Nothing that needs the network was produced.
    assert not (data_dir / "cafe-points.json").exists()
    assert not (data_dir / "maps").exists()


def test_the_cli_dry_run_writes_nothing(data_dir, capsys):
    code = sync.main(
        [
            "--data-dir", str(data_dir),
            "--ics-file", str(FIXTURE),
            "--now", NOW.isoformat(),
            "--dry-run",
        ]
    )
    assert code == 0
    assert "would write" in capsys.readouterr().out
    assert snapshot(data_dir) == {}


def test_a_quiet_cli_run_says_no_changes(data_dir, capsys):
    sync.main(["--data-dir", str(data_dir), "--ics-file", str(FIXTURE),
               "--now", NOW.isoformat()])
    capsys.readouterr()
    sync.main(["--data-dir", str(data_dir), "--ics-file", str(FIXTURE),
               "--now", LATER.isoformat()])
    assert "no changes" in capsys.readouterr().out


def test_a_missing_feed_is_the_one_fatal_error(data_dir, capsys, monkeypatch):
    monkeypatch.delenv("PARTIFUL_ICS_URL", raising=False)
    assert sync.main(["--data-dir", str(data_dir)]) == 1
    assert "PARTIFUL_ICS_URL" in capsys.readouterr().err


def test_an_enrichment_miss_is_not_a_failure(data_dir):
    """Fail-soft everywhere: an unreachable event page leaves the sync green."""

    def unreachable(url):
        raise fetch_rides.requests.RequestException("nope")

    state = sync.run(
        config(data_dir),
        **{**SEAMS, "fetch_page": unreachable},
    )
    assert state.payload["count"] == 2
    assert all(ride["image"] is None for ride in state.payload["events"])
    assert (data_dir / "events.json").exists()


# --- the paths all hang off --data-dir ------------------------------------


def test_every_published_path_derives_from_the_data_dir(tmp_path):
    cfg = sync.Config(data_dir=tmp_path / "elsewhere")
    for path in (
        cfg.events_path,
        cfg.archive_path,
        cfg.cafe_points_path,
        cfg.rides_ics_path,
        cfg.maps_dir,
    ):
        assert path.parent == cfg.data_dir or path.parent.parent == cfg.data_dir
    # The sidecars are code, not data: they stay in scripts/.
    assert cfg.ride_images == fetch_rides.RIDE_IMAGES_PATH
    assert cfg.excluded_events == fetch_rides.EXCLUDED_EVENTS_PATH
    assert cfg.backfill_events == backfill_events.BACKFILL_EVENTS_PATH


def test_the_step_list_is_the_documented_pipeline():
    assert [name for name, _ in sync.STEPS] == [
        "fetch",
        "backfill",
        "archive",
        "enrich_archive",
        "posters",
        "geocode",
        "maps",
        "promote",
        "export_ics",
    ]


# --- the backfill list -----------------------------------------------------
#
# Rides the feed never carried, listed by id in scripts/backfill_events.json
# and fetched from their public event pages through the same fetch_page seam
# as enrichment. The stub below serves pages by URL — the fixture page to every
# fed ride, something else to a listed one — so one run can do both.

BACKFILL_UID = "BackfillRide0001"
# Well before NOW and in daylight time, so the offset in the record is -04:00.
PAST_START = datetime(2025, 10, 5, 10, 0, tzinfo=EASTERN)


def event_url(uid: str) -> str:
    return f"https://partiful.com/e/{uid}"


def backfill_page(uid: str, start: datetime, title: str = "Powder House to Intelligentsia") -> str:
    """A Partiful event page for a ride that was never in the feed."""
    event = {
        "id": uid,
        "title": title,
        "status": "PUBLISHED",
        "startDate": start.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "endDate": None,
        "timezone": "America/New_York",
        "location": "Intelligentsia Coffee, 810 Boylston St, Boston, MA",
        "description": "Powder House Square to the Back Bay.\n\n\nCoffee at the end.",
        "image": {
            "url": "https://firebasestorage.googleapis.com/v0/b/getpartiful.appspot.com/o/rides%2Fpowder-house.jpg?alt=media"
        },
        "customFields": [
            {"icon": "link", "value": "Estimated Route", "url": "https://maps.app.goo.gl/RouteShortLink1?g_st=ic"},
        ],
    }
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"event": event}}})
        + "</script>"
    )


def backfill_list(tmp_path: Path, *uids: str) -> Path:
    path = tmp_path / "backfill_events.json"
    path.write_text(
        json.dumps({uid: "a ride from before the sync existed" for uid in uids}),
        encoding="utf-8",
    )
    return path


def paged(pages: dict, calls: list = None):
    """A fetch_page stub: the given pages by URL, the fixture page otherwise.

    A value that is an exception is raised, which is what an unreachable or
    deleted event page looks like to the sync.
    """

    def fetch(url):
        if calls is not None:
            calls.append(url)
        if url in pages:
            page = pages[url]
            if isinstance(page, Exception):
                raise page
            return page
        return EVENT_PAGE

    return fetch


def backfill_notices(state: sync.Run) -> list:
    return [message for _level, message in state.report.warnings() if "backfill" in message]


def test_a_listed_ride_the_feed_never_had_is_archived_from_its_page(data_dir, tmp_path):
    calls = []
    pages = {event_url(BACKFILL_UID): backfill_page(BACKFILL_UID, PAST_START)}
    state = sync.run(
        config(data_dir, backfill_events=backfill_list(tmp_path, BACKFILL_UID)),
        **{**SEAMS, "fetch_page": paged(pages, calls)},
    )

    archived = {ride["uid"]: ride for ride in payload_of(data_dir / "events-past.json")["events"]}
    ride = archived[BACKFILL_UID]
    assert ride["title"] == "Powder House to Intelligentsia"
    assert ride["start"] == "2025-10-05T10:00:00-04:00"
    assert ride["date_display"] == "Sunday, October 5"
    assert ride["time_display"] == "10:00 am"
    assert ride["location"] == "Intelligentsia Coffee, 810 Boylston St, Boston, MA"
    assert ride["place_name"] == "Intelligentsia Coffee"
    assert ride["description"] == "Powder House Square to the Back Bay.\n\nCoffee at the end."
    assert ride["rsvp_url"] == event_url(BACKFILL_UID)
    assert ride["grace_until"] == "2025-10-05T11:00:00-04:00"
    # Enriched on arrival, through the same seams as a fed ride.
    assert ride["image"].startswith("https://firebasestorage.googleapis.com/")
    assert [route["label"] for route in ride["routes"]] == ["Estimated Route"]
    assert ride["routes"][0]["distance_m"] == 1000
    assert ride["routes"][0]["start_name"] == "Cleveland Circle"
    # …so it never joins the never-checked queue.
    assert state.report.archive_unchecked == 0

    assert calls.count(event_url(BACKFILL_UID)) == 1, "one fetch, no more"
    # The upcoming list is the feed's alone.
    assert uids(data_dir / "events.json") == {ride["uid"] for ride in feed_rides()}
    assert state.report.backfill_listed == 1
    assert state.report.backfill_fetched == 1
    assert state.report.backfill_queued == 0
    assert backfill_notices(state) == []


def test_a_backfilled_ride_is_placed_on_the_cafe_map_and_gets_a_route_map(data_dir, tmp_path):
    pages = {event_url(BACKFILL_UID): backfill_page(BACKFILL_UID, PAST_START)}
    sync.run(
        config(data_dir, backfill_events=backfill_list(tmp_path, BACKFILL_UID)),
        **{**SEAMS, "fetch_page": paged(pages)},
    )
    cafes = payload_of(data_dir / "cafe-points.json")
    assert any("Intelligentsia" in str(point) for point in cafes.get("points", cafes)), cafes
    assert (data_dir / "maps" / f"{BACKFILL_UID}.svg").exists()


def test_the_second_run_fetches_nothing_and_writes_nothing(data_dir, tmp_path):
    pages = {event_url(BACKFILL_UID): backfill_page(BACKFILL_UID, PAST_START)}
    listed = backfill_list(tmp_path, BACKFILL_UID)
    sync.run(config(data_dir, backfill_events=listed), **{**SEAMS, "fetch_page": paged(pages)})
    before = snapshot(data_dir)

    calls = []
    state = sync.run(
        config(data_dir, now=LATER, backfill_events=listed),
        **{**SEAMS, "fetch_page": paged(pages, calls)},
    )
    assert event_url(BACKFILL_UID) not in calls, "an archived id is never fetched again"
    assert snapshot(data_dir) == before
    assert state.files.changed() == []
    assert state.report.backfill_fetched == 0
    assert state.report.backfill_queued == 0
    assert backfill_notices(state) == []


def test_an_upcoming_listed_ride_waits_for_its_grace_hour(data_dir, tmp_path):
    start = NOW + timedelta(days=2)
    pages = {event_url(BACKFILL_UID): backfill_page(BACKFILL_UID, start)}
    listed = backfill_list(tmp_path, BACKFILL_UID)

    state = sync.run(config(data_dir, backfill_events=listed), **{**SEAMS, "fetch_page": paged(pages)})
    assert BACKFILL_UID not in uids(data_dir / "events-past.json")
    assert BACKFILL_UID not in uids(data_dir / "events.json"), "the upcoming list is the feed's"
    assert state.report.backfill_fetched == 1
    assert state.report.backfill_queued == 1
    assert backfill_notices(state) == [], "waiting is not a problem"

    # Two hours after it started — past the grace hour — it lands.
    state = sync.run(
        config(data_dir, now=start + timedelta(hours=2), backfill_events=listed),
        **{**SEAMS, "fetch_page": paged(pages)},
    )
    assert BACKFILL_UID in uids(data_dir / "events-past.json")
    assert state.report.backfill_queued == 0


def test_an_unreadable_page_is_a_notice_and_a_retry_never_a_failure(data_dir, tmp_path):
    listed = backfill_list(tmp_path, BACKFILL_UID)
    gone = {event_url(BACKFILL_UID): fetch_rides.requests.RequestException("nope")}
    state = sync.run(config(data_dir, backfill_events=listed), **{**SEAMS, "fetch_page": paged(gone)})
    assert (data_dir / "events.json").exists(), "the rest of the sync went ahead"
    assert BACKFILL_UID not in uids(data_dir / "events-past.json")
    assert state.report.backfill_missed == 1
    assert state.report.backfill_queued == 1
    notices = backfill_notices(state)
    assert len(notices) == 1 and "fetched none" in notices[0]
    assert ("notice", notices[0]) in state.report.warnings()

    # Next run tries again, and this time the page is there.
    calls = []
    pages = {event_url(BACKFILL_UID): backfill_page(BACKFILL_UID, PAST_START)}
    state = sync.run(
        config(data_dir, now=LATER, backfill_events=listed),
        **{**SEAMS, "fetch_page": paged(pages, calls)},
    )
    assert calls.count(event_url(BACKFILL_UID)) == 1
    assert BACKFILL_UID in uids(data_dir / "events-past.json")
    assert backfill_notices(state) == []


def test_a_page_that_is_not_an_event_counts_as_unreadable(data_dir, tmp_path):
    pages = {event_url(BACKFILL_UID): "<html><body>This event is no longer available.</body></html>"}
    state = sync.run(
        config(data_dir, backfill_events=backfill_list(tmp_path, BACKFILL_UID)),
        **{**SEAMS, "fetch_page": paged(pages)},
    )
    assert BACKFILL_UID not in uids(data_dir / "events-past.json")
    assert state.report.backfill_missed == 1


def test_ids_the_sync_already_covers_are_never_fetched(data_dir, tmp_path):
    archived = dict(
        dropped_ride(), uid="ArchivedRide0001", rsvp_url=event_url("ArchivedRide0001"), routes=[]
    )
    write_payload(data_dir / "events-past.json", [archived])
    excluded = tmp_path / "excluded.json"
    excluded.write_text(json.dumps({"ExcludedRide0001": "a party, not a ride"}), encoding="utf-8")
    fed = feed_rides()[0]["uid"]
    listed = backfill_list(tmp_path, "ArchivedRide0001", "ExcludedRide0001", fed)

    calls = []
    state = sync.run(
        config(data_dir, backfill_events=listed, excluded_events=excluded),
        **{**SEAMS, "fetch_page": paged({}, calls)},
    )
    assert event_url("ArchivedRide0001") not in calls
    assert event_url("ExcludedRide0001") not in calls
    assert state.backfilled == [] and state.backfill_missed == []
    assert "ExcludedRide0001" not in uids(data_dir / "events-past.json")
    assert state.report.backfill_listed == 3
    assert state.report.backfill_queued == 0, "covered is covered, whichever way"
    assert backfill_notices(state) == []


def test_the_backfill_limit_bounds_one_run_and_the_rest_follow_next_time(data_dir, tmp_path):
    ids = ["BackfillRideA", "BackfillRideB", "BackfillRideC"]
    pages = {
        event_url(uid): backfill_page(uid, PAST_START - timedelta(days=i), title=uid)
        for i, uid in enumerate(ids)
    }
    listed = backfill_list(tmp_path, *ids)

    state = sync.run(
        config(data_dir, backfill_events=listed, backfill_limit=2),
        **{**SEAMS, "fetch_page": paged(pages)},
    )
    assert uids(data_dir / "events-past.json") >= {"BackfillRideA", "BackfillRideB"}
    assert "BackfillRideC" not in uids(data_dir / "events-past.json")
    assert state.report.backfill_fetched == 2
    assert state.report.backfill_queued == 1

    state = sync.run(
        config(data_dir, now=LATER, backfill_events=listed, backfill_limit=2),
        **{**SEAMS, "fetch_page": paged(pages)},
    )
    assert "BackfillRideC" in uids(data_dir / "events-past.json")
    assert state.report.backfill_fetched == 1
    assert state.report.backfill_queued == 0


def test_backfill_limit_zero_fetches_nothing(data_dir, tmp_path):
    calls = []
    state = sync.run(
        config(data_dir, backfill_events=backfill_list(tmp_path, BACKFILL_UID), backfill_limit=0),
        **{**SEAMS, "fetch_page": paged({}, calls)},
    )
    assert event_url(BACKFILL_UID) not in calls
    assert state.report.backfill_queued == 1


def test_an_offline_run_without_a_page_seam_fetches_nothing(data_dir, tmp_path):
    """--ics-file with no seams is byte-for-byte network-free, list or no list."""
    state = sync.run(config(data_dir, backfill_events=backfill_list(tmp_path, BACKFILL_UID)))
    assert (data_dir / "events.json").exists()
    assert BACKFILL_UID not in uids(data_dir / "events-past.json")
    assert state.report.backfill_queued == 1
    assert any("offline run, none fetched" in note for note in state.notes)


def test_archiving_before_the_backfill_loses_the_listed_ride(data_dir, tmp_path):
    """The ordering rule: the backfill's rides are the archive step's input."""
    steps = ordered(
        "fetch", "archive", "backfill", "enrich_archive", "posters", "geocode", "maps",
        "promote", "export_ics",
    )
    pages = {event_url(BACKFILL_UID): backfill_page(BACKFILL_UID, PAST_START)}
    sync.run(
        config(data_dir, backfill_events=backfill_list(tmp_path, BACKFILL_UID)),
        steps=steps,
        **{**SEAMS, "fetch_page": paged(pages)},
    )
    assert BACKFILL_UID not in uids(data_dir / "events-past.json")


def test_the_report_and_summary_carry_the_backfill(data_dir, tmp_path):
    pages = {event_url(BACKFILL_UID): backfill_page(BACKFILL_UID, PAST_START)}
    state = sync.run(
        config(data_dir, backfill_events=backfill_list(tmp_path, BACKFILL_UID)),
        **{**SEAMS, "fetch_page": paged(pages)},
    )
    assert state.report.state()["backfill"] == {"listed": 1, "queued": 0}
    assert any(line.startswith("backfill: 1 listed ride(s) fetched") for line in state.report.lines())
    assert "| Listed rides still not archived | 0 / 1 |" in sync.summary_markdown(state.report)


# --- the ride-photo mirror -------------------------------------------------
#
# Without a fetch_image seam an --ics-file run never touches the step, which is
# why every test above still sees no posters/ directory. These hand one in.

# What the stubbed ImageMagick "writes": short, and it starts with the JPEG
# magic so a reader can tell at a glance what the bytes are pretending to be.
STUB_JPEG = b"\xff\xd8\xff\xe0stub-jpeg\xff\xd9"


def fake_image(url: str) -> bytes:
    """Every mirrorable photo downloads. The URL is echoed so a test can see it."""
    return b"original:" + url.encode("utf-8")


def fake_resize(data: bytes) -> bytes:
    """Stands in for the ImageMagick subprocess — no binary, no temp files."""
    assert data.startswith(b"original:")
    return STUB_JPEG


POSTER_SEAMS = {**SEAMS, "fetch_image": fake_image, "resize_image": fake_resize}


def mirror_run(data_dir: Path, now: datetime = NOW, steps=None, **extra) -> sync.Run:
    return sync.run(config(data_dir, now, **extra), steps=steps, **POSTER_SEAMS)


def posters_in(data_dir: Path) -> dict:
    """The mirrored files, by name → bytes."""
    directory = data_dir / "posters"
    if not directory.is_dir():
        return {}
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def stored_rides(data_dir: Path) -> dict:
    """Every ride the run published, upcoming and archived, keyed by UID."""
    out = {}
    for name in ("events.json", "events-past.json"):
        path = data_dir / name
        if path.exists():
            for ride in payload_of(path)["events"]:
                out[ride["uid"]] = ride
    return out


# The three hosts that really appear in site/events*.json today: the
# organizer's own Firebase uploads (20 of 34 rides), Partiful's stock posters
# (7) and the Giphy GIFs they picked from the gallery (7).
FIREBASE_IMAGE = (
    "https://firebasestorage.googleapis.com/v0/b/getpartiful.appspot.com/"
    "o/external%2Fuser%2FHs47uq5mucZyXLBJZCda%2Fa_n4RCOs?alt=media&token=431384ca"
)
STOCK_IMAGE = "https://assets.getpartiful.com/posters/File.png"
GIPHY_IMAGE = "https://media2.giphy.com/media/ZcYmkPx47LLh3jIFhi/giphy.gif?cid=c00e53c6"


@pytest.mark.parametrize(
    "url, expected",
    [
        (FIREBASE_IMAGE, True),
        ("https://getpartiful.appspot.com/o/x.jpg", True),
        ("https://storage.googleapis.com/getpartiful.appspot.com/x.jpg", True),
        (STOCK_IMAGE, False),
        (GIPHY_IMAGE, False),
        # Not ours to copy, and not a fetchable image either.
        ("https://firebasestorage.googleapis.com.evil.invalid/x.jpg", False),
        ("http://FIREBASESTORAGE.GOOGLEAPIS.COM/x.jpg", True),
        ("ftp://firebasestorage.googleapis.com/x.jpg", False),
        ("data:image/png;base64,AAAA", False),
        ("", False),
        (None, False),
        (42, False),
    ],
)
def test_mirrorable_accepts_only_the_organizers_own_uploads(url, expected):
    """Firebase Storage is where Partiful puts what the *host* uploaded.

    Everything else in the cover slot is somebody else's artwork the host
    picked off a shelf, and copying that into a public repo is not our call.
    """
    assert sync.mirrorable(url) is expected


def test_resize_argv_is_the_whole_image_pipeline():
    """Pinned, because this argv *is* the resize — there is no PIL here."""
    assert sync.resize_argv("/tmp/in.bin", "/tmp/out.jpg", "magick") == [
        "magick",
        "/tmp/in.bin[0]",
        "-auto-orient",
        "-strip",
        "-resize",
        "800x800>",
        "-quality",
        "72",
        "jpg:/tmp/out.jpg",
    ]
    # ImageMagick 6 spells the same command `convert`; the default is 7's name.
    assert sync.resize_argv("a", "b")[0] == "magick"
    assert sync.resize_argv("a", "b", "convert")[0] == "convert"
    # -auto-orient has to come before -strip or an EXIF-rotated phone photo
    # loses the rotation it was about to be turned by.
    argv = sync.resize_argv("a", "b")
    assert argv.index("-auto-orient") < argv.index("-strip")


def test_imagemagick_prefers_magick_then_convert():
    assert sync.imagemagick(which={"magick": "/usr/bin/magick"}.get) == "/usr/bin/magick"
    assert sync.imagemagick(which={"convert": "/usr/bin/convert"}.get) == "/usr/bin/convert"
    assert sync.imagemagick(which=lambda name: None) is None


def test_the_step_mirrors_every_ride_photo_and_points_the_cards_at_it(data_dir):
    state = mirror_run(data_dir)

    assert state.report.posters_mirrored == 3
    files = posters_in(data_dir)
    assert set(files) == {
        "evt-future-charles-loop-partiful.com.jpg",
        "evt-future-minuteman-partiful.com.jpg",
        "evt-past-jamaica-pond-partiful.com.jpg",
    }
    assert set(files.values()) == {STUB_JPEG}
    for uid, ride in stored_rides(data_dir).items():
        assert ride["poster"] == "posters/" + sync.render_route_maps.safe_name(uid) + ".jpg"
        # The original stays exactly where it was: it is the card's fallback.
        assert ride["image"].startswith("https://firebasestorage.googleapis.com/")


def test_the_poster_path_is_relative_and_lands_under_the_data_dir(data_dir):
    """Served from /boscafebikers/, so a leading slash 404s in production."""
    state = mirror_run(data_dir)
    for path in state.files.changed():
        assert data_dir in path.parents
    for ride in stored_rides(data_dir).values():
        assert not ride["poster"].startswith(("/", "http"))
        assert ride["poster"].startswith("posters/")


def test_without_a_fetch_image_seam_an_offline_run_mirrors_nothing(data_dir):
    """The --ics-file contract: a seam that isn't handed in doesn't run."""
    state = do_run(data_dir)
    assert not (data_dir / "posters").exists()
    assert state.report.posters_mirrored == 0
    assert all("poster" not in ride for ride in stored_rides(data_dir).values())
    # …but the report still says how many *could* be mirrored.
    assert state.report.posters_mirrorable == 3


def test_a_stock_poster_is_left_hotlinked(data_dir):
    """Partiful's own artwork and a Giphy GIF are never copied here."""
    asked = []

    def watching_fetch(url):
        asked.append(url)
        return fake_image(url)

    def stock_page(url):
        return EVENT_PAGE.replace(
            "https://firebasestorage.googleapis.com/v0/b/getpartiful.appspot.com"
            "/o/rides%2Fcharles-loop.jpg?alt=media",
            STOCK_IMAGE,
        )

    state = sync.run(
        config(data_dir),
        **{**POSTER_SEAMS, "fetch_page": stock_page, "fetch_image": watching_fetch},
    )
    assert asked == [], "nobody's stock art gets fetched, let alone stored"
    assert not (data_dir / "posters").exists()
    assert state.report.posters_mirrorable == 0
    for ride in stored_rides(data_dir).values():
        assert ride["image"] == STOCK_IMAGE
        assert "poster" not in ride


def test_the_limit_bounds_one_run_and_the_rest_follow_next_time(data_dir):
    """Same shape as the archive backfill: a few per sync, newest first."""
    first = mirror_run(data_dir, poster_limit=1)
    assert first.report.posters_mirrored == 1
    # Newest first — the September 21 ride, not the September 7 one.
    assert set(posters_in(data_dir)) == {"evt-future-minuteman-partiful.com.jpg"}

    second = mirror_run(data_dir, now=LATER, poster_limit=1)
    assert second.report.posters_mirrored == 1
    assert set(posters_in(data_dir)) == {
        "evt-future-charles-loop-partiful.com.jpg",
        "evt-future-minuteman-partiful.com.jpg",
    }

    third = mirror_run(data_dir, now=LATER, poster_limit=1)
    assert third.report.posters_mirrored == 1
    assert len(posters_in(data_dir)) == 3
    # And once there is nothing left, the step is a no-op forever.
    fourth = mirror_run(data_dir, now=LATER, poster_limit=1)
    assert fourth.report.posters_mirrored == 0
    assert fourth.files.changed() == []


def test_poster_limit_zero_mirrors_nothing(data_dir):
    state = mirror_run(data_dir, poster_limit=0)
    assert state.report.posters_mirrored == 0
    assert not (data_dir / "posters").exists()


def test_pending_posters_is_newest_first_and_skips_anything_already_answered():
    rides = [
        {"uid": "a", "start": "2026-01-01T10:00:00-05:00", "image": FIREBASE_IMAGE},
        {"uid": "b", "start": "2026-03-01T10:00:00-05:00", "image": FIREBASE_IMAGE},
        {"uid": "c", "start": "2026-02-01T10:00:00-05:00", "image": STOCK_IMAGE},
        # Already mirrored, and already given up on: both have had their turn.
        {"uid": "d", "start": "2026-04-01T10:00:00-04:00", "image": FIREBASE_IMAGE,
         "poster": "posters/d.jpg"},
        {"uid": "e", "start": "2026-05-01T10:00:00-04:00", "image": FIREBASE_IMAGE,
         "poster": None},
        {"uid": "f", "start": "2026-06-01T10:00:00-04:00", "image": None},
        "not a ride",
    ]
    assert [ride["uid"] for ride in sync.pending_posters(rides)] == ["b", "a"]


def test_a_definitive_4xx_records_a_null_poster_and_is_never_retried(data_dir):
    """Gone is gone: asking again every six hours would be rude and pointless."""

    def gone(url):
        return None  # what fetch_image returns for a 4xx

    first = sync.run(config(data_dir), **{**POSTER_SEAMS, "fetch_image": gone})
    assert first.report.posters_mirrored == 0
    assert not (data_dir / "posters").exists()
    rides = stored_rides(data_dir)
    assert all(ride["poster"] is None for ride in rides.values())

    # The archive remembers, so the archived ride is never asked about again.
    # The two *upcoming* rides are, because the feed hands them back bare every
    # run and a null in a published file is not a memory the fetch can read —
    # they stop being asked the moment they move into the archive.
    asked = []
    second = sync.run(
        config(data_dir, now=LATER),
        **{**POSTER_SEAMS, "fetch_image": lambda url: asked.append(url) or None},
    )
    assert len(asked) == 2, "the archived ride's null stuck; the upcoming two retry"
    assert stored_rides(data_dir)["evt-past-jamaica-pond@partiful.com"]["poster"] is None
    assert second.files.changed() == [], "a remembered null is not a rewrite"


def test_any_other_failure_leaves_the_key_absent_so_the_next_run_retries(data_dir):
    """A timeout is not an answer — it has to stay on the queue."""

    def flaky(url):
        raise sync.PosterFetchError("ConnectionError")

    first = sync.run(config(data_dir), **{**POSTER_SEAMS, "fetch_image": flaky})
    assert first.report.posters_mirrored == 0
    for ride in stored_rides(data_dir).values():
        assert "poster" not in ride, "no key at all — the question is still open"

    second = mirror_run(data_dir, now=LATER)
    assert second.report.posters_mirrored == 3
    assert len(posters_in(data_dir)) == 3


def test_a_resize_ImageMagick_refuses_also_leaves_the_key_absent(data_dir):
    state = sync.run(
        config(data_dir), **{**POSTER_SEAMS, "resize_image": lambda data: None}
    )
    assert state.report.posters_mirrored == 0
    assert not (data_dir / "posters").exists()
    for ride in stored_rides(data_dir).values():
        assert "poster" not in ride


def test_no_imagemagick_skips_the_step_with_a_notice_and_no_failure(
    data_dir, monkeypatch, capsys
):
    """A machine with no ImageMagick makes no thumbnails. That is all it means."""
    monkeypatch.setattr(sync, "imagemagick", lambda which=None: None)
    seams = {**POSTER_SEAMS}
    seams.pop("resize_image")

    state = sync.run(config(data_dir), **seams)
    assert state.posters_skipped and state.report.imagemagick_missing
    assert not (data_dir / "posters").exists()
    assert all("poster" not in ride for ride in stored_rides(data_dir).values())
    # …and every card still has its photo, hotlinked exactly as before.
    assert all(ride["image"] for ride in stored_rides(data_dir).values())

    assert [level for level, _ in state.report.warnings()] == ["notice"]
    sync.print_report(state.report, env={})
    out = capsys.readouterr().out
    assert "notice: no ImageMagick on this machine" in out
    assert "warning:" not in out


def test_the_cli_still_exits_zero_with_no_imagemagick(data_dir, monkeypatch, capsys):
    """The owner's rule: a report line never changes an exit status."""
    monkeypatch.setattr(sync, "imagemagick", lambda which=None: None)
    code = sync.main(
        ["--data-dir", str(data_dir), "--ics-file", str(FIXTURE),
         "--now", NOW.isoformat()]
    )
    capsys.readouterr()
    assert code == 0


def test_a_second_run_with_the_mirror_on_changes_nothing(data_dir):
    """The run-twice invariant, extended to the posters."""
    mirror_run(data_dir)
    before = snapshot(data_dir)

    second = mirror_run(data_dir, now=LATER)
    assert second.report.posters_mirrored == 0
    assert second.files.changed() == []
    assert snapshot(data_dir) == before


def test_a_fresh_fetch_is_re_pointed_at_the_poster_it_already_has(data_dir):
    """`poster` is added here and never by a fetch — same trap as `map_image`.

    The upcoming payload comes back from the feed bare every run, so without
    the re-point pass the promote would see a changed rides list forever.
    """
    mirror_run(data_dir)
    asked = []
    second = sync.run(
        config(data_dir, now=LATER),
        **{**POSTER_SEAMS, "fetch_image": lambda url: asked.append(url) or STUB_JPEG},
    )
    assert asked == [], "the copies are already on disk; nothing is downloaded"
    for ride in second.payload["events"]:
        assert ride["poster"].startswith("posters/")
    assert second.files.changed() == []


def test_the_dry_run_mirrors_nothing_to_disk(data_dir):
    state = sync.run(config(data_dir, dry_run=True), **POSTER_SEAMS)
    assert state.report.posters_mirrored == 3
    assert [path.name for path in state.files.changed() if path.suffix == ".jpg"]
    assert not (data_dir / "posters").exists()
    assert list(data_dir.iterdir()) == []


def test_posters_after_the_promote_restamps_events_json_every_run(data_dir):
    """`poster` is added by this step and never by a fetch — like `map_image`."""
    sync.run(config(data_dir), steps=POSTERS_AFTER_PROMOTE, **POSTER_SEAMS)
    stamp = payload_of(data_dir / "events.json")["updated_at"]

    second = sync.run(
        config(data_dir, now=LATER), steps=POSTERS_AFTER_PROMOTE, **POSTER_SEAMS
    )
    assert config(data_dir).events_path in second.files.changed()
    assert payload_of(data_dir / "events.json")["updated_at"] != stamp


def test_the_report_counts_the_mirror(data_dir):
    report = mirror_run(data_dir).report
    assert (report.posters_mirrored, report.posters_local, report.posters_mirrorable) == (
        3, 3, 3,
    )
    assert report.state()["posters"] == {"mirrored": 3, "mirrorable": 3}
    assert "posters: 3 mirrored this run" in "\n".join(report.lines())
    assert "Ride photos mirrored this run" in sync.summary_markdown(report)


# --- the run report --------------------------------------------------------
#
# Every miss in this pipeline is fail-soft, which is right and also silent: a
# Partiful markup change would strip every new ride of its photo, route and map
# and nobody would find out. The report gives that degradation a number. What
# it must never do is fail the job — the counts are annotations, and the exit
# status stays "nonzero only on a FeedError".


def test_the_report_counts_the_fixture_run(data_dir):
    """Exact numbers for tests/fixtures/sample.ics through the stubbed seams."""
    report = do_run(data_dir).report

    # The feed: two future rides, one past, one cancelled (never counted).
    assert (report.upcoming, report.feed_past, report.excluded_skipped) == (2, 1, 0)
    # Enrichment of the upcoming list: the fixture event page carries a cover
    # image and two custom-field links, one of which is really directions.
    assert report.enrichment_ran
    assert (report.with_image, report.with_route) == (2, 2)
    assert (report.routes_total, report.routes_measured) == (2, 2)
    # The archive: the one past ride, backfilled this run, nothing left over.
    assert (report.archive_size, report.archive_enriched) == (1, 1)
    assert (report.archive_unchecked_before, report.archive_unchecked) == (1, 0)
    assert report.enrich_limit == sync.DEFAULT_ENRICH_LIMIT
    # Geocoding: the two rides with a public address (Minuteman's is hidden).
    assert (report.geocode_queried, report.geocode_found, report.geocode_missed) == (
        2, 2, 0,
    )
    assert (report.cafes_placed, report.cafes_unplaced) == (2, 0)
    # The photo mirror is off without a fetch_image seam, but the report still
    # says how many of the fixture's photos are the organizer's own uploads.
    assert (report.posters_mirrored, report.posters_local) == (0, 0)
    assert report.posters_mirrorable == 3
    # Maps: both upcoming rides plus the archived one, all with a basemap.
    assert report.maps_drawn == 3
    assert report.routes_without_map == 0
    assert report.maps_without_basemap == []
    # And the two stdout-only lists.
    assert sorted(report.first_seen) == [
        "evt-future-charles-loop@partiful.com",
        "evt-future-minuteman@partiful.com",
        "evt-past-jamaica-pond@partiful.com",
    ]
    assert str(config(data_dir).report_path) in report.written


def test_the_report_lands_beside_the_data(data_dir):
    state = do_run(data_dir)
    written = json.loads(
        (data_dir / "sync-report.json").read_text(encoding="utf-8")
    )
    assert written == state.report.state()
    assert written["feed"] == {"upcoming": 2, "past": 1, "excluded": 0}
    assert written["archive"] == {"rides": 1, "never_checked": 0}


def test_the_report_json_carries_nothing_that_moves_between_runs(data_dir):
    """The no-churn rule, spelled out: no timestamp, no per-run work counts.

    The file lives on the data branch, so anything that reads N on the run that
    does the work and 0 on the next one would commit — and deploy — every 6
    hours forever.
    """
    state = do_run(data_dir).report.state()
    assert set(state) == {"feed", "upcoming", "archive", "backfill", "cafes", "posters", "maps"}
    keys = {key for section in state.values() for key in section}
    for moving in ("updated_at", "now", "drawn", "written", "first_seen",
                   "queried", "backfilled", "enriched", "mirrored_this_run"):
        assert moving not in keys


def test_a_second_run_leaves_the_report_byte_identical(data_dir):
    """The run-twice invariant, extended to the report itself."""
    do_run(data_dir)
    report_path = data_dir / "sync-report.json"
    before = report_path.read_bytes()
    # Run one drew 3 maps, backfilled 1 archived ride and geocoded 2 cafés;
    # run two does none of that — and the file still may not move.
    second = do_run(data_dir, now=LATER)
    assert (second.report.maps_drawn, second.report.archive_enriched) == (0, 0)
    assert second.report.geocode_queried == 0
    assert second.files.changed() == []
    assert report_path.read_bytes() == before


def test_the_dry_run_writes_no_report_either(data_dir):
    state = sync.run(config(data_dir, dry_run=True), **SEAMS)
    assert config(data_dir).report_path in state.files.changed()
    assert not (data_dir / "sync-report.json").exists()
    assert snapshot(data_dir) == {}


# --- warnings: annotations, never failures ---------------------------------


def blank_page(url: str) -> str:
    """A Partiful page the extractor gets nothing out of — the markup changed."""
    return "<html><body>nothing we recognise</body></html>"


def test_an_enrichment_that_finds_no_photo_warns(data_dir, capsys):
    state = sync.run(config(data_dir), **{**SEAMS, "fetch_page": blank_page})
    assert state.report.upcoming == 2 and state.report.with_image == 0

    sync.print_report(state.report, env={})
    out = capsys.readouterr().out
    assert "warning: enrichment came back empty" in out
    # …and the run itself is untouched: the rides still published.
    assert (data_dir / "events.json").exists()


def test_a_healthy_run_warns_about_nothing(data_dir, capsys):
    state = do_run(data_dir)
    assert state.report.warnings() == []
    sync.print_report(state.report, env={})
    out = capsys.readouterr().out
    assert "warning:" not in out and "notice:" not in out
    assert "sync: feed: 2 upcoming" in out


def test_an_empty_feed_over_a_stocked_archive_warns(data_dir, tmp_path, capsys):
    """The "secret rotated / feed went dark" signal — and still exit 0."""
    empty_feed = tmp_path / "empty.ics"
    empty_feed.write_text(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Partiful//EN\r\nEND:VCALENDAR\r\n",
        encoding="utf-8",
    )
    write_payload(data_dir / "events-past.json", feed_rides(past=True))

    code = sync.main(
        [
            "--data-dir", str(data_dir),
            "--ics-file", str(empty_feed),
            "--now", NOW.isoformat(),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, "a warning must never change the exit status"
    assert "warning: the feed carried no events at all" in out
    assert "archive holds 1 ride(s)" in out


def test_under_actions_the_warning_is_a_workflow_command(data_dir, capsys):
    state = sync.run(config(data_dir), **{**SEAMS, "fetch_page": blank_page})
    sync.print_report(state.report, env={"GITHUB_ACTIONS": "true"})
    out = capsys.readouterr().out
    assert "::warning::enrichment came back empty" in out
    assert "warning: enrichment" not in out


def test_an_unfinished_map_is_a_notice_not_a_warning(data_dir):
    """A tile outage is soft twice over: a plain map, and only a `notice`."""

    def no_tiles(zoom, x, y):
        return None

    state = sync.run(config(data_dir), **{**SEAMS, "fetch_tile": no_tiles})
    assert state.report.maps_without_basemap, "the maps were drawn without tiles"
    levels = {level for level, _ in state.report.warnings()}
    assert levels == {"notice"}
    assert sync.format_note("notice", "x", actions=True) == "::notice::x"
    assert sync.format_note("notice", "x", actions=False) == "notice: x"


def test_a_stalled_archive_backfill_is_noticed_once_it_stops_draining(data_dir):
    """A draining queue is normal; a queue that stopped draining is not."""
    stalled = sync.Report(
        enrichment_ran=True, enrich_limit=8,
        archive_unchecked=5, archive_unchecked_before=5,
    )
    assert [level for level, _ in stalled.warnings()] == ["notice"]
    draining = sync.Report(
        enrichment_ran=True, enrich_limit=8,
        archive_unchecked=5, archive_unchecked_before=13,
    )
    assert draining.warnings() == []


def test_under_actions_is_read_off_the_environment():
    assert sync.under_actions({"GITHUB_ACTIONS": "true"})
    assert sync.under_actions({"GITHUB_ACTIONS": "True"})
    assert not sync.under_actions({"GITHUB_ACTIONS": "false"})
    assert not sync.under_actions({})


# --- $GITHUB_STEP_SUMMARY --------------------------------------------------


def test_the_summary_table_lands_in_the_step_summary(data_dir, tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.delenv("PARTIFUL_ICS_URL", raising=False)

    code = sync.main(
        [
            "--data-dir", str(data_dir),
            "--ics-file", str(FIXTURE),
            "--now", NOW.isoformat(),
        ]
    )
    assert code == 0
    text = summary.read_text(encoding="utf-8")
    assert "### Sync report" in text
    assert "| What | Count |" in text
    assert "| Upcoming rides in the feed | 2 |" in text
    assert "**Files written:**" in text
    assert "**First seen this run:**" in text
    # The table is appended, so the workflow's fenced stdout log still follows.
    assert text.endswith("\n")


def test_no_step_summary_variable_writes_no_summary(data_dir, tmp_path):
    state = do_run(data_dir)
    summary = tmp_path / "summary.md"
    assert sync.write_step_summary(state.report, env={}) is False
    assert not summary.exists()
    # …and with the variable set, it writes.
    assert sync.write_step_summary(
        state.report, env={"GITHUB_STEP_SUMMARY": str(summary)}
    ) is True
    assert "### Sync report" in summary.read_text(encoding="utf-8")


def test_a_dry_run_summary_says_would_change(data_dir):
    state = sync.run(config(data_dir, dry_run=True), **SEAMS)
    assert "**Files that would change:**" in sync.summary_markdown(state.report)


# --- the exclusion list is counted ----------------------------------------


def test_the_report_counts_the_uids_the_sidecar_caught(data_dir, tmp_path):
    excluded = tmp_path / "excluded.json"
    excluded.write_text(
        json.dumps(
            {
                "evt-future-minuteman@partiful.com": "not a ride",
                "evt-past-jamaica-pond@partiful.com": "a birthday",
                "evt-never-in-this-feed@partiful.com": "already gone",
            }
        ),
        encoding="utf-8",
    )
    report = do_run(data_dir, excluded_events=excluded).report
    # Two of the three are really in the feed; a UID the feed no longer carries
    # is not a skip, so the number stays honest as the sidecar grows.
    assert report.excluded_skipped == 2
    assert (report.upcoming, report.feed_past) == (1, 0)
