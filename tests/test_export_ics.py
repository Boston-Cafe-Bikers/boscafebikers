"""Tests for scripts/export_ics.py — the public, subscribable rides.ics.

Offline like the rest of the suite: the rides come from parsing
tests/fixtures/sample.ics with the same pinned clock test_fetch_rides.py uses,
so the payload under test is shaped exactly like a real one.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from icalendar import Calendar

from helpers import EASTERN, FIXTURE, REPO_ROOT

import export_ics
import fetch_rides

# Same pinned "now" as test_fetch_rides.py: after the past event, well before
# the 2030 rides.
NOW = datetime(2025, 1, 1, 12, 0, tzinfo=EASTERN)
UPDATED_AT = "2026-08-22T21:38:52.661815-04:00"


@pytest.fixture(scope="module")
def rides() -> list[dict]:
    return fetch_rides.parse_events(FIXTURE.read_bytes(), now=NOW)


@pytest.fixture(scope="module")
def payload(rides: list[dict]) -> dict:
    return {"updated_at": UPDATED_AT, "count": len(rides), "events": rides}


@pytest.fixture(scope="module")
def calendar(payload: dict) -> str:
    return export_ics.build_calendar(payload)


def content_lines(calendar: str) -> list[str]:
    """Unfold the calendar back into logical lines (RFC 5545 §3.1)."""
    out: list[str] = []
    for physical in calendar.split("\r\n"):
        if physical.startswith(" ") and out:
            out[-1] += physical[1:]
        else:
            out.append(physical)
    return [line for line in out if line]


def vevents(calendar: str) -> list[list[str]]:
    blocks, current = [], None
    for line in content_lines(calendar):
        if line == "BEGIN:VEVENT":
            current = []
        elif line == "END:VEVENT":
            blocks.append(current)
            current = None
        elif current is not None:
            current.append(line)
    return blocks


def value(block: list[str], name: str) -> str | None:
    for line in block:
        if line.split(":", 1)[0].split(";", 1)[0] == name:
            return line.split(":", 1)[1]
    return None


# --- header ---------------------------------------------------------------

def test_calendar_header_lines(calendar):
    lines = content_lines(calendar)
    assert lines[0] == "BEGIN:VCALENDAR"
    assert lines[-1] == "END:VCALENDAR"
    for expected in [
        "VERSION:2.0",
        "PRODID:-//Boston Café Bikers//boscafebikers//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Boston Café Bikers rides",
        "X-WR-TIMEZONE:America/New_York",
    ]:
        assert expected in lines


def test_vtimezone_block_is_present(calendar):
    lines = content_lines(calendar)
    assert "BEGIN:VTIMEZONE" in lines
    assert "TZID:America/New_York" in lines
    assert "END:VTIMEZONE" in lines
    # Both halves of the US rule, so a client that doesn't know the zone can
    # still place a summer ride and a winter one.
    assert "TZNAME:EDT" in lines and "TZNAME:EST" in lines


def test_refresh_interval_tells_subscribers_the_sync_cadence(calendar):
    lines = content_lines(calendar)
    assert "REFRESH-INTERVAL;VALUE=DURATION:PT6H" in lines
    assert "X-PUBLISHED-TTL:PT6H" in lines


def test_line_endings_are_crlf(calendar):
    assert calendar.endswith("\r\n")
    assert "\n" not in calendar.replace("\r\n", "")


# --- events ---------------------------------------------------------------

def test_one_vevent_per_ride(calendar, rides):
    assert len(vevents(calendar)) == len(rides) == 2


def test_dtstart_uses_the_tzid_form_and_the_wall_clock_prefix(calendar, rides):
    for block, ride in zip(vevents(calendar), rides):
        line = next(l for l in block if l.startswith("DTSTART"))
        assert line.startswith("DTSTART;TZID=America/New_York:")
        # "2030-06-22T09:30:00-04:00" → "20300622T093000"
        expected = ride["start"][:19].replace("-", "").replace(":", "")
        assert line.split(":", 1)[1] == expected


def test_dtend_present_only_when_the_feed_gave_one(calendar, rides):
    block = vevents(calendar)[0]
    assert value(block, "DTEND") == "20300622T120000"
    no_end = export_ics.build_calendar(
        {"updated_at": UPDATED_AT, "events": [dict(rides[0], end=None)]}
    )
    assert value(vevents(no_end)[0], "DTEND") is None


def test_summary_and_url_come_from_the_ride(calendar, rides):
    block = vevents(calendar)[0]
    assert value(block, "SUMMARY") == "Charles River Loop → Tatte"
    assert value(block, "URL") == rides[0]["rsvp_url"]


def test_description_ends_with_the_rsvp_link(calendar, rides):
    description = value(vevents(calendar)[0], "DESCRIPTION")
    assert description.endswith("\\n\\nRSVP: " + rides[0]["rsvp_url"])


# --- location: the three readings of Partiful's one field -----------------

def test_public_address_becomes_a_location_line(calendar):
    assert value(vevents(calendar)[0], "LOCATION") == (
        "Tatte Bakery & Café\\, 1003 Beacon St\\, Brookline\\, MA 02446"
    )


def test_hidden_location_emits_no_location_line(calendar):
    # The Minuteman ride is the fixture's "Location available once RSVP'd" one.
    block = vevents(calendar)[1]
    assert value(block, "LOCATION") is None
    assert not any(line.startswith("LOCATION") for line in block)


def test_link_only_location_goes_in_the_description_not_location(rides):
    url = "https://maps.app.goo.gl/abc123"
    ride = dict(rides[0], location=None, location_hidden=False, location_url=url)
    block = vevents(export_ics.build_calendar({"updated_at": UPDATED_AT, "events": [ride]}))[0]
    assert value(block, "LOCATION") is None
    assert "Meeting point: " + url in value(block, "DESCRIPTION")


def test_a_real_address_wins_over_a_stray_location_url(rides):
    ride = dict(rides[0], location_url="https://maps.app.goo.gl/abc123")
    block = vevents(export_ics.build_calendar({"updated_at": UPDATED_AT, "events": [ride]}))[0]
    assert value(block, "LOCATION").startswith("Tatte Bakery & Café")
    assert "Meeting point:" not in value(block, "DESCRIPTION")


# --- escaping and folding -------------------------------------------------

def test_escapes_commas_semicolons_backslashes_and_newlines(rides):
    ride = dict(
        rides[0],
        title="Coffee, cake; and a back\\slash",
        description="First line\nsecond line",
        location=None,
        location_hidden=False,
        location_url=None,
    )
    block = vevents(export_ics.build_calendar({"updated_at": UPDATED_AT, "events": [ride]}))[0]
    assert value(block, "SUMMARY") == "Coffee\\, cake\\; and a back\\\\slash"
    assert value(block, "DESCRIPTION").startswith("First line\\nsecond line")


def test_folds_on_octets_without_splitting_a_utf8_sequence(rides):
    # Long enough to need several folds, and made of multibyte characters so a
    # naive byte slice would land mid-sequence.
    ride = dict(rides[0], description="café ☕ ride → " * 30)
    calendar = export_ics.build_calendar({"updated_at": UPDATED_AT, "events": [ride]})
    physical = calendar.split("\r\n")
    assert any(line.startswith(" ") for line in physical), "nothing folded"
    for line in physical:
        encoded = line.encode("utf-8")
        assert len(encoded) <= 75, line
        # The real proof: every physical line is independently decodable, so no
        # fold cut a character in half.
        encoded.decode("utf-8")


def test_a_short_ascii_line_is_not_folded(calendar):
    assert "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" in calendar


def test_unfolding_restores_the_original_text(rides):
    description = "café ☕ ride → " * 30
    ride = dict(rides[0], description=description)
    calendar = export_ics.build_calendar({"updated_at": UPDATED_AT, "events": [ride]})
    restored = value(vevents(calendar)[0], "DESCRIPTION")
    assert restored.startswith(description.replace("\n", "\\n"))


# --- UIDs must match the browser's per-ride export ------------------------

def test_uid_is_the_rides_own_uid_like_the_js_export(calendar, rides):
    assert [value(block, "UID") for block in vevents(calendar)] == [
        ride["uid"] for ride in rides
    ]


def test_uid_falls_back_to_the_same_slug_the_js_builds(rides):
    ride = dict(rides[0], uid=None, title="Charles River Loop → Tatte")
    block = vevents(export_ics.build_calendar({"updated_at": UPDATED_AT, "events": [ride]}))[0]
    # ride-card.js: "boscafebikers-" + YYYYMMDDTHHMMSS + "-" + slugified title.
    assert value(block, "UID") == "boscafebikers-20300622T093000-charles-river-loop-tatte"


def test_the_js_uid_rule_is_still_the_one_we_mirror():
    """Pin the JS twin: if buildIcs's UID line changes, this test says so."""
    source = (REPO_ROOT / "site" / "js" / "ride-card.js").read_text(encoding="utf-8")
    assert '"UID:" + icsEscape(ev.uid || fallbackUid(ev))' in source
    assert '"boscafebikers-" + (icsDateTime(ev.start) || "undated") + "-" +' in source


# --- DTSTAMP / byte stability --------------------------------------------

def test_dtstamp_comes_from_updated_at_converted_to_utc(calendar):
    # 2026-08-22T21:38:52-04:00 is 2026-08-23T01:38:52Z.
    for block in vevents(calendar):
        assert value(block, "DTSTAMP") == "20260823T013852Z"


def test_dtstamp_is_stable_when_updated_at_is_unusable(rides):
    calendar = export_ics.build_calendar({"updated_at": None, "events": rides})
    assert value(vevents(calendar)[0], "DTSTAMP") == export_ics.FALLBACK_DTSTAMP


def test_the_same_payload_always_builds_the_same_bytes(payload):
    assert export_ics.build_calendar(payload) == export_ics.build_calendar(payload)


# --- empty list -----------------------------------------------------------

def test_no_rides_still_yields_a_valid_empty_calendar():
    calendar = export_ics.build_calendar({"updated_at": UPDATED_AT, "events": []})
    assert vevents(calendar) == []
    parsed = Calendar.from_ical(calendar)
    assert list(parsed.walk("VEVENT")) == []


# --- the file parses back -------------------------------------------------

def test_icalendar_reads_it_back_with_the_right_rides(calendar, rides):
    parsed = Calendar.from_ical(calendar)
    events = list(parsed.walk("VEVENT"))
    assert len(events) == len(rides)
    assert [str(event["SUMMARY"]) for event in events] == [ride["title"] for ride in rides]
    # The TZID form round-trips to the same instant the payload names.
    assert events[0]["DTSTART"].dt.isoformat() == rides[0]["start"]


def test_the_published_file_is_a_valid_calendar():
    """site/rides.ics is generated onto the `data` branch, not committed here,
    so a plain code checkout has none of it until scripts/pull_data.sh runs."""
    path = REPO_ROOT / "site" / "rides.ics"
    if not path.exists():
        pytest.skip("site/rides.ics lives on the data branch (scripts/pull_data.sh)")
    parsed = Calendar.from_ical(path.read_bytes())
    assert str(parsed["PRODID"]) == export_ics.PRODID
    for event in parsed.walk("VEVENT"):
        assert event["UID"] and event["DTSTART"]


# --- no secret ever reaches the file --------------------------------------

def test_only_public_partiful_and_maps_links_appear(calendar):
    urls = [
        word
        for line in content_lines(calendar)
        for word in line.split()
        if word.startswith("http")
    ]
    assert urls, "the fixture rides carry RSVP links"
    for url in urls:
        assert url.startswith(("https://partiful.com/", "https://maps.app.goo.gl/"))


def test_the_exporter_never_reads_the_environment(monkeypatch, payload):
    """The secret feed URL lives in the environment; this script never looks."""
    monkeypatch.setenv("PARTIFUL_ICS_URL", "https://secret.example/private.ics")
    assert "secret.example" not in export_ics.build_calendar(payload)
    source = (REPO_ROOT / "scripts" / "export_ics.py").read_text(encoding="utf-8")
    for forbidden in ("import os", "os.environ", "getenv", "requests"):
        assert forbidden not in source, forbidden


# --- the file write: no churn --------------------------------------------

def write_payload(tmp_path: Path, payload: dict) -> Path:
    source = tmp_path / "events.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    return source


def test_export_writes_the_file_the_first_time(tmp_path, payload):
    source = write_payload(tmp_path, payload)
    out = tmp_path / "rides.ics"
    assert export_ics.export(source, out) is True
    assert out.read_bytes() == export_ics.build_calendar(payload).encode("utf-8")


def test_export_leaves_an_unchanged_file_alone(tmp_path, payload):
    source = write_payload(tmp_path, payload)
    out = tmp_path / "rides.ics"
    export_ics.export(source, out)
    before = out.stat().st_mtime_ns
    assert export_ics.export(source, out) is False
    assert out.stat().st_mtime_ns == before


def test_export_rewrites_when_the_rides_changed(tmp_path, payload, rides):
    source = write_payload(tmp_path, payload)
    out = tmp_path / "rides.ics"
    export_ics.export(source, out)
    write_payload(tmp_path, {"updated_at": UPDATED_AT, "events": rides[:1]})
    assert export_ics.export(source, out) is True
    # read_bytes, not read_text: universal newlines would eat the CRLFs.
    assert len(vevents(out.read_bytes().decode("utf-8"))) == 1


def test_export_creates_the_output_directory(tmp_path, payload):
    source = write_payload(tmp_path, payload)
    out = tmp_path / "nested" / "rides.ics"
    assert export_ics.export(source, out) is True
    assert out.exists()


def test_the_written_file_keeps_crlf_endings(tmp_path, payload):
    source = write_payload(tmp_path, payload)
    out = tmp_path / "rides.ics"
    export_ics.export(source, out)
    raw = out.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\r\n") == raw.count(b"\n")


# --- CLI ------------------------------------------------------------------

def test_cli_writes_and_then_reports_unchanged(tmp_path, payload, capsys):
    source = write_payload(tmp_path, payload)
    out = tmp_path / "rides.ics"
    assert export_ics.main([str(source), "--out", str(out)]) == 0
    assert "Wrote" in capsys.readouterr().out
    assert export_ics.main([str(source), "--out", str(out)]) == 0
    assert "Unchanged" in capsys.readouterr().out


def test_cli_fails_on_a_missing_source(tmp_path, capsys):
    code = export_ics.main([str(tmp_path / "nope.json"), "--out", str(tmp_path / "o.ics")])
    assert code == 1
    assert "export_ics" in capsys.readouterr().err


def test_cli_fails_on_malformed_json(tmp_path, capsys):
    source = tmp_path / "events.json"
    source.write_text("{not json", encoding="utf-8")
    assert export_ics.main([str(source), "--out", str(tmp_path / "o.ics")]) == 1
    assert "not valid JSON" in capsys.readouterr().err
