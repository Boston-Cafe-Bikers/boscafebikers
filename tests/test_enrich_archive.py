"""Tests for scripts/enrich_archive.py — the bounded archive backfill. Offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import enrich_archive

PAGE = (
    '<script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"event":{"id":"x","image":'
    '"https://firebasestorage.googleapis.com/v0/b/p/o/a.jpg?alt=media"}}}}'
    "</script>"
)


def ride(uid: str, start: str, routes=None) -> dict:
    return {
        "uid": uid,
        "title": "Café ride",
        "start": start,
        "rsvp_url": f"https://partiful.com/e/{uid}",
        "image": None,
        "routes": routes,
    }


def write(path: Path, events: list) -> Path:
    path.write_text(
        json.dumps({"count": len(events), "events": events}), encoding="utf-8"
    )
    return path


def offline(url: str) -> str:
    return PAGE


def no_links(url: str) -> str:  # pragma: no cover - never reached
    raise AssertionError("the fixture page has no custom fields to resolve")


# --- which rides are pending ---


def test_pending_is_only_the_never_checked_rides():
    events = [
        ride("a", "2026-01-01T10:00:00-05:00"),
        ride("b", "2026-02-01T10:00:00-05:00", routes=[]),
        ride("c", "2026-03-01T10:00:00-05:00", routes=[{"label": "R"}]),
    ]
    assert [r["uid"] for r in enrich_archive.pending(events)] == ["a"]


def test_pending_is_newest_first():
    events = [
        ride("old", "2025-01-01T10:00:00-05:00"),
        ride("new", "2026-08-01T10:00:00-04:00"),
        ride("mid", "2026-01-01T10:00:00-05:00"),
    ]
    assert [r["uid"] for r in enrich_archive.pending(events)] == ["new", "mid", "old"]


# --- the bounded run ---


def test_only_limit_rides_are_touched(tmp_path):
    events = [ride(f"r{i}", f"2026-0{i}-01T10:00:00-05:00") for i in range(1, 6)]
    path = write(tmp_path / "past.json", events)
    tried = enrich_archive.enrich_payload(
        path, limit=2, fetch_page=offline, resolve_link=no_links
    )
    assert tried == 2
    written = json.loads(path.read_text(encoding="utf-8"))["events"]
    checked = [r for r in written if r["routes"] is not None]
    assert len(checked) == 2
    # …and it took the newest two.
    assert {r["uid"] for r in checked} == {"r5", "r4"}


def test_a_run_backfills_the_image_too(tmp_path):
    path = write(tmp_path / "past.json", [ride("a", "2026-01-01T10:00:00-05:00")])
    enrich_archive.enrich_payload(path, limit=8, fetch_page=offline, resolve_link=no_links)
    written = json.loads(path.read_text(encoding="utf-8"))["events"][0]
    assert written["image"].endswith("a.jpg?alt=media")
    assert written["routes"] == []


def test_a_fully_checked_archive_is_a_no_op(tmp_path):
    path = write(tmp_path / "past.json", [ride("a", "2026-01-01T10:00:00-05:00", routes=[])])
    before = path.read_text(encoding="utf-8")

    def unreachable(url):
        raise AssertionError("nothing left to check")

    assert enrich_archive.enrich_payload(path, limit=8, fetch_page=unreachable) == 0
    assert path.read_text(encoding="utf-8") == before


def test_repeated_runs_converge(tmp_path):
    events = [ride(f"r{i}", f"2026-0{i}-01T10:00:00-05:00") for i in range(1, 6)]
    path = write(tmp_path / "past.json", events)
    for _ in range(3):
        enrich_archive.enrich_payload(path, limit=2, fetch_page=offline, resolve_link=no_links)
    written = json.loads(path.read_text(encoding="utf-8"))["events"]
    assert all(r["routes"] is not None for r in written)
    assert enrich_archive.enrich_payload(path, limit=2, fetch_page=offline) == 0


def test_limit_zero_does_nothing(tmp_path):
    path = write(tmp_path / "past.json", [ride("a", "2026-01-01T10:00:00-05:00")])

    def unreachable(url):
        raise AssertionError("limit 0 means no fetches")

    assert enrich_archive.enrich_payload(path, limit=0, fetch_page=unreachable) == 0


def test_a_missing_payload_exits_nonzero(tmp_path):
    with pytest.raises(SystemExit):
        enrich_archive.enrich_payload(tmp_path / "nope.json")


def test_a_non_payload_exits_nonzero(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"nope": 1}', encoding="utf-8")
    with pytest.raises(SystemExit):
        enrich_archive.enrich_payload(bad)
