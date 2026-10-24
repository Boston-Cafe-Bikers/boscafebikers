"""Tests for scripts/archive_events.py — the accumulating past-rides archive."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from helpers import EASTERN, FIXTURE

import archive_events
import fetch_rides
import ride_fields

# Pinned "now" so "past" never depends on the clock.
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=EASTERN)


def ride(uid: str, start: str, **extra) -> dict:
    base = {
        "uid": uid,
        "title": "Café ride",
        "start": start,
        "end": None,
        "date_display": "Saturday, May 2",
        "time_display": "11:00 am",
        "location": None,
        "location_hidden": False,
        "location_url": None,
        "description": "",
        "rsvp_url": None,
        "image": None,
    }
    base.update(extra)
    return base


def write_payload(path: Path, rides: list[dict]) -> Path:
    path.write_text(
        json.dumps({"updated_at": "x", "count": len(rides), "events": rides}),
        encoding="utf-8",
    )
    return path


# --- merge_archive ---


def test_only_past_rides_are_absorbed():
    sources = [[ride("a", "2026-05-02T11:00:00-04:00"), ride("b", "2026-07-04T11:00:00-04:00")]]
    merged = archive_events.merge_archive([], sources, NOW)
    assert [r["uid"] for r in merged] == ["a"]


def test_archive_entries_survive_a_source_that_forgot_them():
    """Partiful pruning its history must not erase rides we already archived."""
    archive = [ride("old", "2025-09-01T11:00:00-04:00")]
    merged = archive_events.merge_archive(archive, [[ride("a", "2026-05-02T11:00:00-04:00")]], NOW)
    assert [r["uid"] for r in merged] == ["old", "a"]


def test_archive_entries_are_kept_without_re_testing_now():
    """A future-dated ride already in the archive is not re-filtered out."""
    archive = [ride("weird", "2030-01-01T11:00:00-05:00")]
    merged = archive_events.merge_archive(archive, [], NOW)
    assert [r["uid"] for r in merged] == ["weird"]


def test_a_ride_flagged_not_group_hosted_is_purged():
    """An event enrichment found to be somebody else's does not stay history."""
    archive = [
        ride("ours", "2026-05-02T11:00:00-04:00"),
        ride("theirs", "2026-05-03T11:00:00-04:00", hosted_by_group=False),
    ]
    merged = archive_events.merge_archive(archive, [], NOW)
    assert [r["uid"] for r in merged] == ["ours"]


def test_an_undecided_host_is_never_purged():
    """None means "never checked" — only a decided False is dropped."""
    archive = [
        ride("maybe", "2026-05-02T11:00:00-04:00", hosted_by_group=None),
        ride("unchecked", "2026-05-03T11:00:00-04:00"),
    ]
    merged = archive_events.merge_archive(archive, [], NOW)
    assert [r["uid"] for r in merged] == ["maybe", "unchecked"]


def test_result_is_sorted_by_start():
    sources = [[
        ride("b", "2026-05-20T11:00:00-04:00"),
        ride("a", "2026-05-02T11:00:00-04:00"),
    ]]
    merged = archive_events.merge_archive([], sources, NOW)
    assert [r["start"] for r in merged] == sorted(r["start"] for r in merged)


def test_a_later_source_wins_field_by_field():
    old = [ride("a", "2026-05-02T11:00:00-04:00", title="Old name")]
    new = [[ride("a", "2026-05-02T11:00:00-04:00", title="Real name")]]
    merged = archive_events.merge_archive(old, new, NOW)
    assert len(merged) == 1
    assert merged[0]["title"] == "Real name"


def test_none_never_overwrites_an_existing_value():
    """The feed's past export has no image; the enriched archive entry keeps it."""
    photo = "https://example.invalid/ride.jpg"
    archived = [ride("a", "2026-05-02T11:00:00-04:00", image=photo, rsvp_url="https://p/e/a")]
    from_feed = [[ride("a", "2026-05-02T11:00:00-04:00")]]
    merged = archive_events.merge_archive(archived, from_feed, NOW)
    assert merged[0]["image"] == photo
    assert merged[0]["rsvp_url"] == "https://p/e/a"


def test_a_known_location_url_survives_a_source_without_one():
    """The pasted meeting-point link is kept when a re-export carries none.

    The feed's past export re-derives `location_url` every time, but an older
    archive entry may predate the key entirely — neither a null nor a missing
    key may drop a link we already have.
    """
    link = "https://maps.app.goo.gl/7zBmEn5ZTHEhJtSZ7"
    archived = [ride("a", "2026-05-02T11:00:00-04:00", location_url=link)]
    with_null = [[ride("a", "2026-05-02T11:00:00-04:00", location_url=None)]]
    assert archive_events.merge_archive(archived, with_null, NOW)[0]["location_url"] == link
    # A source that never learned about the field at all.
    without_key = [dict(ride("a", "2026-05-02T11:00:00-04:00"), title="Renamed")]
    without_key[0].pop("location_url", None)
    merged = archive_events.merge_archive(archived, [without_key], NOW)
    assert merged[0]["location_url"] == link
    assert merged[0]["title"] == "Renamed"


def test_a_missing_location_url_is_filled_in_from_a_source():
    """An archive entry from before the key gains it without a crash."""
    old_entry = ride("a", "2026-05-02T11:00:00-04:00")
    old_entry.pop("location_url", None)
    link = "https://maps.app.goo.gl/KafN4kidaBpozyew9?g_st=ic"
    fresh = [[ride("a", "2026-05-02T11:00:00-04:00", location_url=link)]]
    assert archive_events.merge_archive([old_entry], fresh, NOW)[0]["location_url"] == link


def test_rides_without_uids_fall_back_to_start_and_title():
    a = ride("", "2026-05-02T11:00:00-04:00", title="One")
    b = ride("", "2026-05-02T11:00:00-04:00", title="Two")
    merged = archive_events.merge_archive([], [[a, b]], NOW)
    assert len(merged) == 2


def test_naive_and_offset_starts_are_both_comparable():
    """A start without an offset is read as Eastern, not crashed on."""
    merged = archive_events.merge_archive([], [[ride("a", "2026-05-02T11:00:00")]], NOW)
    assert [r["uid"] for r in merged] == ["a"]


# --- the grace hour ---
# fetch_rides keeps a ride in site/events.json for an hour after it starts, so
# a latecomer can still find it. The archive has to use the *same* window: the
# sync hands this script the previous site/events.json, and app.js just
# concatenates the archive onto events.json, so archiving a ride that is still
# in the upcoming list would draw it on the calendar twice — once dimmed from
# the archive and once live. The "one definition, no re-declare" assertion
# lives with the rule itself in test_ride_fields.py.


def test_a_ride_inside_its_grace_hour_is_not_archived():
    started = (NOW - timedelta(minutes=30)).isoformat()
    assert archive_events.merge_archive([], [[ride("a", started)]], NOW) == []


def test_exactly_one_hour_old_is_still_not_archived():
    """The boundary sits on the upcoming side, the same way fetch_rides puts it."""
    started = (NOW - timedelta(hours=1)).isoformat()
    assert archive_events.merge_archive([], [[ride("a", started)]], NOW) == []


def test_a_ride_past_its_grace_hour_is_archived():
    started = (NOW - timedelta(hours=1, seconds=1)).isoformat()
    merged = archive_events.merge_archive([], [[ride("a", started)]], NOW)
    assert [r["uid"] for r in merged] == ["a"]


def test_a_graced_ride_is_never_in_the_archive_and_events_json_at_once():
    """The sync's own shape: previous events.json + the fresh past export.

    Parsed from the real fixture 30 minutes after the Charles ride starts, so
    the ride is genuinely mid-grace rather than hand-built.
    """
    feed = FIXTURE.read_bytes()
    charles = "evt-future-charles-loop@partiful.com"
    now = datetime(2030, 6, 22, 10, 0, tzinfo=EASTERN)  # start 09:30 + 30 min
    upcoming = fetch_rides.parse_events(feed, now=now)
    from_feed_past = fetch_rides.parse_events(feed, now=now, past=True)
    # Source order mirrors sync.yml: site/events.json, then the past export.
    merged = archive_events.merge_archive([], [upcoming, from_feed_past], now)
    assert charles in {r["uid"] for r in upcoming}
    assert charles not in {r["uid"] for r in merged}


def test_the_ride_reaches_the_archive_once_its_grace_hour_is_over():
    feed = FIXTURE.read_bytes()
    charles = "evt-future-charles-loop@partiful.com"
    now = datetime(2030, 6, 22, 11, 0, tzinfo=EASTERN)  # start + 90 min
    upcoming = fetch_rides.parse_events(feed, now=now)
    from_feed_past = fetch_rides.parse_events(feed, now=now, past=True)
    merged = archive_events.merge_archive([], [upcoming, from_feed_past], now)
    assert charles not in {r["uid"] for r in upcoming}
    assert charles in {r["uid"] for r in merged}


def test_unparseable_start_is_not_treated_as_past():
    merged = archive_events.merge_archive([], [[ride("a", "not a date")]], NOW)
    assert merged == []


# --- run() / the file layer ---


def test_run_creates_a_missing_archive(tmp_path):
    src = write_payload(tmp_path / "events.json", [ride("a", "2026-05-02T11:00:00-04:00")])
    archive = tmp_path / "events-past.json"
    assert archive_events.run(archive, [src], now=NOW) is True
    payload = json.loads(archive.read_text(encoding="utf-8"))
    assert payload["count"] == 1
    assert payload["events"][0]["uid"] == "a"


def test_run_is_idempotent(tmp_path):
    """A second run with nothing new must not rewrite the file (no churn commit)."""
    src = write_payload(tmp_path / "events.json", [ride("a", "2026-05-02T11:00:00-04:00")])
    archive = tmp_path / "events-past.json"
    archive_events.run(archive, [src], now=NOW)
    before = archive.read_text(encoding="utf-8")
    assert archive_events.run(archive, [src], now=NOW) is False
    assert archive.read_text(encoding="utf-8") == before


def test_run_writes_the_precomputed_display_fields(tmp_path):
    """A hand run matches a sync run: both derive before writing."""
    src = write_payload(tmp_path / "events.json", [ride(
        "a", "2026-05-02T11:00:00-04:00",
        location="Localito Cafe, 30 Riverside Ave, Medford, MA",
        routes=[{"label": "Estimated Route",
                 "start": "Bluebikes, Cleveland Circle, Boston, MA 02135",
                 "end": "Localito Cafe, 30 Riverside Ave, Medford, MA"}],
    )])
    archive = tmp_path / "events-past.json"
    archive_events.run(archive, [src], now=NOW)
    archived = json.loads(archive.read_text(encoding="utf-8"))["events"][0]
    assert archived["grace_until"] == "2026-05-02T12:00:00-04:00"
    assert archived["place_name"] == "Localito Cafe"
    assert archived["address"] == "30 Riverside Ave, Medford, MA"
    assert archived["year"] == "2026"
    assert archived["routes"][0]["start_name"] == "Cleveland Circle"
    assert archived["routes"][0]["end_name"] == "Localito Cafe"
    # …and re-running changes nothing, so the fields can't churn a commit.
    assert archive_events.run(archive, [src], now=NOW) is False


def test_a_derived_field_follows_the_merged_location_not_an_older_one(tmp_path):
    """merge_ride settles the location; the display fields follow *that*."""
    archived = [ride("a", "2026-05-02T11:00:00-04:00", location="Old Cafe, 1 Old St, Boston, MA")]
    fresh = [[ride("a", "2026-05-02T11:00:00-04:00", location="New Cafe, 2 New St, Boston, MA")]]
    merged = ride_fields.derive_all(archive_events.merge_archive(archived, fresh, NOW))
    assert merged[0]["place_name"] == "New Cafe"
    assert merged[0]["address"] == "2 New St, Boston, MA"


def test_run_reports_a_missing_source(tmp_path):
    with pytest.raises(archive_events.ArchiveError):
        archive_events.run(tmp_path / "past.json", [tmp_path / "nope.json"], now=NOW)


def test_run_rejects_a_non_payload_source(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"nope": true}', encoding="utf-8")
    with pytest.raises(archive_events.ArchiveError):
        archive_events.run(tmp_path / "past.json", [bad], now=NOW)


def test_main_exits_nonzero_on_a_missing_source(tmp_path, capsys):
    code = archive_events.main(["--archive", str(tmp_path / "past.json"), str(tmp_path / "nope.json")])
    assert code == 1
    assert "archive_events" in capsys.readouterr().err


def test_main_merges_sources_in_order(tmp_path):
    older = write_payload(
        tmp_path / "older.json", [ride("a", "2026-05-02T11:00:00-04:00", title="Old")]
    )
    newer = write_payload(
        tmp_path / "newer.json", [ride("a", "2026-05-02T11:00:00-04:00", title="New")]
    )
    archive = tmp_path / "events-past.json"
    assert archive_events.main(["--archive", str(archive), str(older), str(newer)]) == 0
    assert json.loads(archive.read_text(encoding="utf-8"))["events"][0]["title"] == "New"


# --- excluded events (scripts/excluded_events.json) ---


def test_excluded_rides_are_skipped_from_sources():
    sources = [[ride("a", "2026-05-02T11:00:00-04:00"), ride("b", "2026-05-03T11:00:00-04:00")]]
    merged = archive_events.merge_archive([], sources, NOW, excluded={"b"})
    assert [r["uid"] for r in merged] == ["a"]


def test_excluded_rides_are_purged_from_the_archive_itself():
    """The one exception to "the archive never forgets": adding a UID to the
    list is enough to drop a ride that was archived before it was listed."""
    archive = [ride("a", "2026-05-02T11:00:00-04:00"), ride("b", "2026-05-03T11:00:00-04:00")]
    merged = archive_events.merge_archive(archive, [], NOW, excluded={"b"})
    assert [r["uid"] for r in merged] == ["a"]


def test_exclusion_keys_on_the_uid_only():
    """A ride without a UID is keyed start|title, so a bare title can't match."""
    archive = [ride(None, "2026-05-02T11:00:00-04:00", title="Baby shower")]
    merged = archive_events.merge_archive(archive, [], NOW, excluded={"Baby shower"})
    assert len(merged) == 1


def test_no_exclusions_is_the_old_behaviour():
    archive = [ride("a", "2026-05-02T11:00:00-04:00")]
    assert archive_events.merge_archive(archive, [], NOW) == (
        archive_events.merge_archive(archive, [], NOW, excluded=set())
    )


def test_load_excluded_missing_file_is_empty(tmp_path):
    assert archive_events.load_excluded(tmp_path / "nope.json") == set()


def test_load_excluded_reads_the_keys(tmp_path):
    path = tmp_path / "excluded_events.json"
    path.write_text('{"abc": "a birthday"}', encoding="utf-8")
    assert archive_events.load_excluded(path) == {"abc"}


@pytest.mark.parametrize("text", ["{oops", '["abc"]'])
def test_load_excluded_rejects_a_broken_file(tmp_path, text):
    path = tmp_path / "excluded_events.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(archive_events.ArchiveError):
        archive_events.load_excluded(path)


def test_run_purges_an_excluded_ride_and_rewrites(tmp_path):
    archive = tmp_path / "events-past.json"
    write_payload(archive, [ride("a", "2026-05-02T11:00:00-04:00"), ride("b", "2026-05-03T11:00:00-04:00")])
    source = write_payload(tmp_path / "source.json", [ride("b", "2026-05-03T11:00:00-04:00")])
    excluded = tmp_path / "excluded_events.json"
    excluded.write_text('{"b": "not a ride"}', encoding="utf-8")
    assert archive_events.run(archive, [source], now=NOW, excluded_path=excluded) is True
    payload = json.loads(archive.read_text(encoding="utf-8"))
    assert [r["uid"] for r in payload["events"]] == ["a"]
    assert payload["count"] == 1
    # Now that it is gone, the same sources make no further change.
    assert archive_events.run(archive, [source], now=NOW, excluded_path=excluded) is False


def test_main_takes_an_excluded_flag(tmp_path):
    archive = tmp_path / "events-past.json"
    source = write_payload(tmp_path / "source.json", [ride("b", "2026-05-03T11:00:00-04:00")])
    excluded = tmp_path / "excluded_events.json"
    excluded.write_text('{"b": "not a ride"}', encoding="utf-8")
    assert archive_events.main(["--archive", str(archive), "--excluded", str(excluded), str(source)]) == 0
    assert not archive.exists()  # nothing to write: the only ride was excluded


def test_main_exits_nonzero_on_a_broken_exclusion_list(tmp_path, capsys):
    archive = tmp_path / "events-past.json"
    source = write_payload(tmp_path / "source.json", [ride("b", "2026-05-03T11:00:00-04:00")])
    excluded = tmp_path / "excluded_events.json"
    excluded.write_text("{oops", encoding="utf-8")
    assert archive_events.main(["--archive", str(archive), "--excluded", str(excluded), str(source)]) == 1
    assert "archive_events" in capsys.readouterr().err


def test_excluded_path_matches_fetch_rides():
    """Both scripts must read the same sidecar (duplicated, not imported)."""
    assert archive_events.DEFAULT_EXCLUDED == fetch_rides.EXCLUDED_EVENTS_PATH


# --- destination overrides (scripts/ride_destinations.json) ---


def test_load_destinations_missing_file_is_empty(tmp_path):
    assert archive_events.load_location_overrides(tmp_path / "nope.json") == {}


def test_load_destinations_reads_the_values(tmp_path):
    path = tmp_path / "ride_destinations.json"
    path.write_text('{"abc": "Café, 2 Main St, Cambridge, MA"}', encoding="utf-8")
    assert archive_events.load_location_overrides(path) == {
        "abc": "Café, 2 Main St, Cambridge, MA"
    }


@pytest.mark.parametrize("text", ["{oops", '["abc"]', '{"abc": ""}', '{"abc": 7}'])
def test_load_destinations_rejects_a_broken_file(tmp_path, text):
    path = tmp_path / "ride_destinations.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(archive_events.ArchiveError):
        archive_events.load_location_overrides(path)


def test_an_override_beats_the_feeds_non_null_location(tmp_path):
    """The whole point: a start location from the feed cannot undo a fix."""
    archived = [ride("a", "2026-05-02T11:00:00-04:00",
                     location="City Hall Plaza, 1 City Hall Ave, Boston, MA")]
    # The feed re-export carries the same start location, which merge_ride
    # would normally let win.
    fresh = [[ride("a", "2026-05-02T11:00:00-04:00",
                   location="City Hall Plaza, 1 City Hall Ave, Boston, MA")]]
    overrides = {"a": "Lovestruck Books, 44 Brattle St, Cambridge, MA 02138"}
    merged = ride_fields.derive_all(
        ride_fields.apply_location_overrides(
            archive_events.merge_archive(archived, fresh, NOW), overrides
        )
    )
    assert merged[0]["location"] == "Lovestruck Books, 44 Brattle St, Cambridge, MA 02138"
    assert merged[0]["place_name"] == "Lovestruck Books"
    assert merged[0]["address"] == "44 Brattle St, Cambridge, MA 02138"


def test_run_applies_the_destination_sidecar(tmp_path):
    archive = write_payload(
        tmp_path / "past.json",
        [ride("a", "2026-05-02T11:00:00-04:00",
              location="Park Street Station, Boston, MA")],
    )
    source = write_payload(
        tmp_path / "source.json",
        [ride("a", "2026-05-02T11:00:00-04:00",
              location="Park Street Station, Boston, MA")],
    )
    destinations = tmp_path / "ride_destinations.json"
    destinations.write_text(
        '{"a": "Yafa Bakery & Café, 594 Somerville Ave, Somerville, MA 02143"}',
        encoding="utf-8",
    )
    assert archive_events.run(
        archive, [source], now=NOW, destinations_path=destinations
    ) is True
    written = json.loads(archive.read_text(encoding="utf-8"))["events"][0]
    assert written["place_name"] == "Yafa Bakery & Café"
    assert written["address"] == "594 Somerville Ave, Somerville, MA 02143"
    # A re-run is a no-op, even though the source still carries the start.
    assert archive_events.run(
        archive, [source], now=NOW, destinations_path=destinations
    ) is False


def test_main_takes_a_ride_destinations_flag(tmp_path):
    archive = tmp_path / "events-past.json"
    source = write_payload(
        tmp_path / "source.json",
        [ride("a", "2026-05-02T11:00:00-04:00", location="Start Plaza, Boston, MA")],
    )
    destinations = tmp_path / "ride_destinations.json"
    destinations.write_text('{"a": "Café, 2 Main St, Cambridge, MA"}', encoding="utf-8")
    code = archive_events.main([
        "--archive", str(archive),
        "--ride-destinations", str(destinations),
        str(source),
    ])
    assert code == 0
    assert json.loads(archive.read_text(encoding="utf-8"))["events"][0]["place_name"] == "Café"


def test_destinations_path_lives_with_the_other_sidecars():
    assert archive_events.DEFAULT_DESTINATIONS == ride_fields.RIDE_DESTINATIONS_PATH
    assert archive_events.DEFAULT_DESTINATIONS.name == "ride_destinations.json"
