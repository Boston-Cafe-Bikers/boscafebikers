#!/usr/bin/env python3
"""Re-export every ride as a public, subscribable calendar.

    python scripts/export_ics.py <events.json> --past <events-past.json> \
        --out site/rides.ics

The site already lets a visitor download one ride at a time (the browser builds
that .ics in site/js/ride-card.js). This writes the *whole* list once, so a
visitor can subscribe to the URL and let their calendar app keep itself up to
date. It reads only the committed events payloads — it never touches the
private feed URL (PARTIFUL_ICS_URL), and nothing derived from it is in the
payload either.

Past rides come from the archive too. A ride leaves events.json as soon as it
starts, so exporting only that file made the event disappear from every
subscriber's calendar the next morning. The archive is append-only, so once a
ride is on the calendar it stays there.

Two rules keep the file from churning in git every six hours:

* DTSTAMP comes from the payload's `updated_at`, never datetime.now(), so the
  same payload always produces byte-identical output.
* The file is rewritten only when its bytes actually changed.

Both matter because the sync workflow commits whatever this leaves behind — see
the "Order matters" notes in CLAUDE.md; the workflow runs this on the *promoted*
site/events.json, which only changes when the rides do.

The UID of each VEVENT matches BCB.buildIcs() in site/js/ride-card.js exactly,
so a visitor who subscribes *and* downloaded a single ride sees one event, not
two. Keep the two in step.

Stdlib only. Exits 0 whether or not it rewrote the file; nonzero only when the
source is missing or unparseable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PRODID = "-//Boston Café Bikers//boscafebikers//EN"
CALNAME = "Boston Café Bikers rides"
TZID = "America/New_York"
# The sync runs every 6 hours; tell subscribers so they don't poll harder.
# REFRESH-INTERVAL is RFC 7986; X-PUBLISHED-TTL is what Outlook/Google read.
REFRESH = "PT6H"

# Copied verbatim from what Partiful ships in its own export (and from
# tests/fixtures/sample.ics): the US rules since 2007. Clients that already
# know America/New_York ignore it; the ones that don't need it to place a
# floating "20260823T110000" on the right hour.
VTIMEZONE = """BEGIN:VTIMEZONE
TZID:America/New_York
BEGIN:DAYLIGHT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE""".split("\n")

# A payload with no usable `updated_at` still has to produce stable bytes, so
# fall back to a constant rather than to the clock. fetch_rides always stamps
# one, so this is belt-and-braces.
FALLBACK_DTSTAMP = "19700101T000000Z"

_WALL_CLOCK_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?")


class ExportError(Exception):
    """The source payload can't be read."""


def ics_escape(text: object) -> str:
    """RFC 5545 TEXT escaping — same rule as icsEscape() in ride-card.js."""
    value = "" if text is None else str(text)
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold_line(line: str) -> str:
    """Fold one content line to 75 **octets**, continuations led by a space.

    The browser twin folds on JS chars because that's all it has; here the
    encoded length is what RFC 5545 actually limits, and a café name full of
    `é`/`☕` would otherwise fold too late — worse, a naive byte slice would
    split a multibyte sequence and hand the subscriber mojibake. So cut on
    bytes and back up over any UTF-8 continuation byte (10xxxxxx).
    """
    data = line.encode("utf-8")
    if len(data) <= 75:
        return line
    chunks = []
    limit = 75  # the first line has no leading space
    while len(data) > limit:
        cut = limit
        while cut > 0 and (data[cut] & 0xC0) == 0x80:
            cut -= 1
        if cut == 0:  # a single code point wider than the limit: impossible today
            cut = limit
        chunks.append(data[:cut])
        data = data[cut:]
        limit = 74  # the leading space costs one of the 75 octets
    chunks.append(data)
    return "\r\n ".join(chunk.decode("utf-8") for chunk in chunks)


def wall_clock(iso: object) -> str | None:
    """"2026-08-23T11:00:00-04:00" → "20260823T110000".

    The ISO string already carries the Eastern offset, so its date/time prefix
    *is* the Eastern wall clock — the same shortcut the site's JS takes. Never
    re-parse it into a datetime and re-render: that would drag the runner's
    timezone into the output.
    """
    match = _WALL_CLOCK_RE.match(str(iso or ""))
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    return f"{year}{month}{day}T{hour}{minute}{second or '00'}"


def fallback_uid(event: dict) -> str:
    """The UID ride-card.js invents for an event with none. Keep in step."""
    stamp = wall_clock(event.get("start")) or "undated"
    slug = re.sub(r"[^a-z0-9]+", "-", str(event.get("title") or "ride").lower())
    return f"boscafebikers-{stamp}-{slug}"


def dtstamp(updated_at: object) -> str:
    """The payload's `updated_at` as a UTC stamp.

    Deriving it from the data rather than the clock is what makes the export
    byte-stable: run it twice on the same events.json and git sees nothing.
    """
    try:
        moment = datetime.fromisoformat(str(updated_at))
    except (TypeError, ValueError):
        return FALLBACK_DTSTAMP
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def export_details(event: dict) -> str:
    """The DESCRIPTION body — the twin of exportDetails() in ride-card.js.

    A Location field that held nothing but a map link becomes a line in here
    rather than a LOCATION value: calendar apps geocode a bare URL badly.
    """
    parts = []
    if event.get("description"):
        parts.append(str(event["description"]))
    if not event.get("location") and event.get("location_url"):
        parts.append("Meeting point: " + str(event["location_url"]))
    if event.get("rsvp_url"):
        parts.append("RSVP: " + str(event["rsvp_url"]))
    return "\n\n".join(parts)


def event_lines(event: dict, stamp: str) -> list[str]:
    """One VEVENT, as unfolded content lines."""
    lines = [
        "BEGIN:VEVENT",
        "UID:" + ics_escape(event.get("uid") or fallback_uid(event)),
        "DTSTAMP:" + stamp,
    ]
    start = wall_clock(event.get("start"))
    if start:
        lines.append(f"DTSTART;TZID={TZID}:{start}")
    end = wall_clock(event.get("end"))
    if end:
        lines.append(f"DTEND;TZID={TZID}:{end}")
    lines.append("SUMMARY:" + ics_escape(event.get("title") or "Café ride"))
    # location_hidden ("Location available once RSVP'd") and a link-only
    # location both leave `location` null — neither belongs in LOCATION.
    if event.get("location"):
        lines.append("LOCATION:" + ics_escape(event["location"]))
    details = export_details(event)
    if details:
        lines.append("DESCRIPTION:" + ics_escape(details))
    if event.get("rsvp_url"):
        # URL is a URI value, not TEXT: it is not escaped (nor is it in the JS).
        lines.append("URL:" + str(event["rsvp_url"]))
    lines.append("END:VEVENT")
    return lines


def combine_events(upcoming: list, past: list | None = None) -> list:
    """The upcoming rides plus the archived ones, each UID exactly once.

    A ride can briefly be in both files around its start time, and a duplicate
    UID is an update to the same calendar event rather than a second one. The
    upcoming copy wins (it is the one the feed just refreshed), and the result
    is chronological so the file reads like the calendar it represents.
    """
    merged: dict[str, dict] = {}
    for event in list(past or []) + list(upcoming or []):
        if not isinstance(event, dict):
            continue
        merged[event.get("uid") or fallback_uid(event)] = event
    return sorted(
        merged.values(),
        key=lambda event: (
            str(event.get("start") or ""),
            str(event.get("uid") or fallback_uid(event)),
        ),
    )


def build_calendar(payload: dict, past: list | None = None) -> str:
    """The whole VCALENDAR as a CRLF string. Zero rides is still valid.

    `past` is the archive's ride list (events-past.json's `events`); `payload`
    supplies the upcoming rides and the DTSTAMP. The stamp stays tied to the
    upcoming payload so a quiet run leaves the bytes alone.
    """
    stamp = dtstamp(payload.get("updated_at"))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:" + PRODID,
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:" + CALNAME,
        "X-WR-TIMEZONE:" + TZID,
        "REFRESH-INTERVAL;VALUE=DURATION:" + REFRESH,
        "X-PUBLISHED-TTL:" + REFRESH,
    ]
    lines.extend(VTIMEZONE)
    for event in combine_events(payload.get("events") or [], past):
        lines.extend(event_lines(event, stamp))
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"


def load_payload(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise ExportError(f"can't read {path}: {exc.strerror or type(exc).__name__}")
    except ValueError:
        raise ExportError(f"{path} is not valid JSON")
    if not isinstance(payload, dict):
        raise ExportError(f"{path} is not an events payload")
    return payload


def export(source: Path, out: Path, past_source: Path | None = None) -> bool:
    """Write the calendar for `source` to `out`. True when the bytes changed."""
    past = None
    if past_source is not None:
        past = load_payload(past_source).get("events") or []
    calendar = build_calendar(load_payload(source), past).encode("utf-8")
    try:
        if out.read_bytes() == calendar:
            return False
    except OSError:
        pass  # missing or unreadable: write it
    out.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so the CRLFs above survive; text mode would double them.
    with out.open("w", encoding="utf-8", newline="") as handle:
        handle.write(calendar.decode("utf-8"))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("source", help="an events.json payload (upcoming rides)")
    parser.add_argument(
        "--past",
        help="an events-past.json payload; its rides stay on the calendar",
    )
    parser.add_argument("--out", default="site/rides.ics", help="calendar file to write")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        changed = export(
            Path(args.source),
            Path(args.out),
            Path(args.past) if args.past else None,
        )
    except ExportError as exc:
        print(f"export_ics: {exc}", file=sys.stderr)
        return 1
    print(f"{'Wrote' if changed else 'Unchanged;'} {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
