// Ride-card rendering and the add-to-calendar exports (.ics download + Google
// Calendar link). Loaded FIRST in the dependency order (defer), so it also owns
// the tiny DOM helper and the constants the later files share. Everything the
// other files use lives on the shared `BCB` namespace (window.BCB) — see the
// "JS module split" note in CLAUDE.md.
(function (BCB) {
  "use strict";

  BCB.PARTIFUL = "https://partiful.com/u/Hs47uq5mucZyXLBJZCda";

  BCB.MONTHS = ["January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December"];

  BCB.DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  // DOM built with createElement/textContent, never innerHTML, so feed text
  // can't inject markup.
  BCB.el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text) { node.textContent = text; }
    return node;
  };

  BCB.pad2 = (n) => (n < 10 ? "0" + n : String(n));

  // --- per-event .ics download ---
  // Generated in the browser from the precomputed events.json data. The start
  // ISO string already carries the Eastern offset, so its Y-M-DTHH:MM prefix IS
  // the Eastern wall-clock time — never new Date(start), mirroring the calendar.
  function icsEscape(text) {
    return String(text == null ? "" : text)
      .replace(/\\/g, "\\\\")
      .replace(/;/g, "\\;")
      .replace(/,/g, "\\,")
      .replace(/\r?\n/g, "\\n");
  }
  function foldIcsLine(line) {
    // RFC 5545: fold at 75 chars, continuation lines lead with a space. Avoid
    // splitting a surrogate pair (emoji in a café name, say).
    const out = [];
    while (line.length > 75) {
      let cut = 75;
      const c = line.charCodeAt(cut);
      if (c >= 0xD800 && c <= 0xDBFF) { cut -= 1; }
      out.push(line.slice(0, cut));
      line = " " + line.slice(cut);
    }
    out.push(line);
    return out.join("\r\n");
  }
  function icsDateTime(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(iso || "");
    if (!m) { return null; }
    return m[1] + m[2] + m[3] + "T" + m[4] + m[5] + (m[6] || "00");
  }
  function utcStamp() {
    const now = new Date();
    return now.getUTCFullYear() + BCB.pad2(now.getUTCMonth() + 1) + BCB.pad2(now.getUTCDate()) +
      "T" + BCB.pad2(now.getUTCHours()) + BCB.pad2(now.getUTCMinutes()) +
      BCB.pad2(now.getUTCSeconds()) + "Z";
  }
  function icsFilename(title) {
    const safe = String(title || "").toLowerCase()
      .replace(/[^\w\- ]+/g, "")
      .trim().replace(/\s+/g, "-").replace(/-+/g, "-").slice(0, 60);
    return (safe || "cafe-ride") + ".ics";
  }
  function fallbackUid(ev) {
    return "boscafebikers-" + (icsDateTime(ev.start) || "undated") + "-" +
      String(ev.title || "ride").toLowerCase().replace(/[^a-z0-9]+/g, "-");
  }
  // The body both exports share: the ride blurb, the meeting-point link when
  // the Location field held one instead of an address (calendar apps geocode a
  // bare URL badly, so it stays in the description and out of LOCATION), and
  // the RSVP link.
  function exportDetails(ev) {
    const parts = [];
    if (ev.description) { parts.push(ev.description); }
    if (!ev.location && ev.location_url) { parts.push("Meeting point: " + ev.location_url); }
    if (ev.rsvp_url) { parts.push("RSVP: " + ev.rsvp_url); }
    return parts.join("\n\n");
  }

  BCB.buildIcs = (ev) => {
    const lines = [
      "BEGIN:VCALENDAR",
      "VERSION:2.0",
      "PRODID:-//Boston Café Bikers//boscafebikers//EN",
      "CALSCALE:GREGORIAN",
      "METHOD:PUBLISH",
      "BEGIN:VEVENT",
      "UID:" + icsEscape(ev.uid || fallbackUid(ev)),
      "DTSTAMP:" + utcStamp()
    ];
    const start = icsDateTime(ev.start);
    if (start) { lines.push("DTSTART;TZID=America/New_York:" + start); }
    const end = icsDateTime(ev.end);
    if (end) { lines.push("DTEND;TZID=America/New_York:" + end); }
    lines.push("SUMMARY:" + icsEscape(ev.title || "Café ride"));
    if (ev.location) { lines.push("LOCATION:" + icsEscape(ev.location)); }
    const desc = exportDetails(ev);
    if (desc) { lines.push("DESCRIPTION:" + icsEscape(desc)); }
    if (ev.rsvp_url) { lines.push("URL:" + ev.rsvp_url); }
    lines.push("END:VEVENT", "END:VCALENDAR");
    return lines.map(foldIcsLine).join("\r\n") + "\r\n";
  };
  BCB.downloadIcs = (ev) => {
    const blob = new Blob([BCB.buildIcs(ev)], { type: "text/calendar;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = icsFilename(ev.title);
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => { URL.revokeObjectURL(url); }, 0);
  };

  // "Add to Google Calendar" link — mirrors the calendar export BCU offers on
  // its event detail pages, but as a static-site-friendly TEMPLATE URL. Like
  // buildIcs, it reuses the precomputed Eastern wall-clock fields (never new
  // Date(start) — see the calendar note), and ctz makes Google interpret the
  // naive times in America/New_York. end is null when the feed omits DTEND, so
  // default the block to an hour.
  function icsDateTimePlusHour(iso) {
    const dt = icsDateTime(iso);
    if (!dt) { return null; }
    // dt is YYYYMMDDTHHMMSS (Eastern wall-clock). Add an hour with Date.UTC
    // arithmetic so a near-midnight ride rolls into the next day correctly;
    // the result is still treated as a naive Eastern time by Google's ctz.
    const y = +dt.slice(0, 4), mo = +dt.slice(4, 6), d = +dt.slice(6, 8);
    const h = +dt.slice(9, 11), mi = +dt.slice(11, 13), s = dt.slice(13, 15);
    const date = new Date(Date.UTC(y, mo - 1, d, h + 1, mi));
    return date.getUTCFullYear() + BCB.pad2(date.getUTCMonth() + 1) + BCB.pad2(date.getUTCDate()) +
      "T" + BCB.pad2(date.getUTCHours()) + BCB.pad2(date.getUTCMinutes()) + s;
  }
  BCB.googleCalUrl = (ev) => {
    const parts = [];
    const add = (key, value) => {
      if (value) { parts.push(key + "=" + encodeURIComponent(value)); }
    };
    add("action", "TEMPLATE");
    add("text", ev.title || "Café ride");
    const start = icsDateTime(ev.start);
    const end = ev.end ? icsDateTime(ev.end) : icsDateTimePlusHour(ev.start);
    add("dates", start && end ? start + "/" + end : start);
    add("location", ev.location);
    add("details", exportDetails(ev));
    add("ctz", "America/New_York");
    return "https://calendar.google.com/calendar/render?" + parts.join("&");
  };

  // Some rides carry a link instead of an address in Partiful's Location field
  // (fetch_rides.py turns that into `location_url`). Name the link for what it
  // is rather than printing the URL, which overflows the card.
  const MAPS_URL_RE =
    /^https?:\/\/(?:maps\.app\.goo\.gl\/|maps\.google\.[a-z.]+\/|(?:www\.)?goo\.gl\/maps|(?:www\.)?google\.[a-z.]+\/maps)/i;
  function meetingPointText(url) {
    return MAPS_URL_RE.test(String(url || "")) ? "Meeting point on Google Maps" : "Meeting point";
  }

  // --- "Rolling now" ---
  // A ride stays in events.json for an hour after it starts (the grace hour),
  // because latecomers can still catch the group. During that hour the card
  // must not read as "upcoming": say the ride is happening.
  //
  // The hour itself is not a number here: ride_fields.py writes the end of the
  // window into the data as `grace_until`, so there is one definition of it
  // (scripts/ride_fields.GRACE_PERIOD) instead of a Python constant and a
  // JavaScript twin drifting apart. No grace_until — an event from before the
  // field existed — means no window, so the ride is simply not rolling.
  //
  // Reading the clock here is the one allowed use of it, and it is NOT the
  // thing the no-Date rule bans. That rule is about *re-formatting* a ride's
  // time — the display strings are precomputed in Eastern, and new Date(start)
  // would re-render them in the visitor's own timezone. Here the question is
  // "has this instant passed?", which is absolute: both strings carry their
  // Eastern offset, so Date.parse gives the correct UTC epoch in every
  // timezone. Nothing below is formatted for display.
  function isRolling(ev, nowMs) {
    const startMs = Date.parse((ev && ev.start) || "");
    const endMs = Date.parse((ev && ev.grace_until) || "");
    if (isNaN(startMs) || isNaN(endMs)) { return false; }
    const now = typeof nowMs === "number" ? nowMs : Date.now();
    // Inclusive at both ends, matching fetch_rides.is_upcoming: the ride is
    // rolling from its start time through the last instant of the grace hour.
    return now >= startMs && now <= endMs;
  }
  BCB.isRolling = isRolling;

  // The other side of the same window: once the grace hour has passed the
  // ride is over, whatever events.json still says. That file is only as fresh
  // as the last sync (every 6 hours), so a ride can sit at events[0] for hours
  // after it ended; app.js asks this before choosing the featured next ride
  // (issue #13). Same rule as fetch_rides.is_upcoming, from the other end:
  // upcoming through the last instant of grace_until, over one tick later. No
  // grace_until means no verdict — the ride is left as the sync published it.
  function isOver(ev, nowMs) {
    const endMs = Date.parse((ev && ev.grace_until) || "");
    if (isNaN(endMs)) { return false; }
    const now = typeof nowMs === "number" ? nowMs : Date.now();
    return now > endMs;
  }
  BCB.isOver = isOver;

  // The shared ride-card builder — used by the featured next-ride card AND the
  // ride-detail modal, so the details and add-to-calendar exports can't drift.
  BCB.rideCard = (ev, extraClass) => {
    const card = BCB.el("div", "ride" + (ev.past ? " is-past" : "") + (extraClass ? " " + extraClass : ""));
    // Date/title lead the card (before the banner) so they're visible above
    // the fold on a phone — a map or poster image is tall enough to push
    // them out of the first viewport otherwise (Backlog 10).
    const when = [ev.date_display, ev.time_display].filter(Boolean).join(" · ");
    // Archived rides are flagged in the data; a ride still in events.json can
    // have started anyway (the grace hour, plus up to 6 hours of cron lag), so
    // that one is decided against the visitor's clock at render time.
    const rolling = !ev.past && isRolling(ev);
    if (when || ev.past || rolling) {
      const whenLine = BCB.el("p", "when", when);
      // A ride pulled off the archive needs to say so: the date alone doesn't
      // read as "already happened" when you land on it from the calendar.
      if (ev.past) {
        whenLine.appendChild(BCB.el("span", "ride-tag", "Past ride"));
      } else if (rolling) {
        whenLine.appendChild(BCB.el("span", "ride-tag is-rolling", "Rolling now"));
      }
      card.appendChild(whenLine);
    }
    card.appendChild(BCB.el("h3", null, ev.title || "Café ride"));
    // The drawn route map wins over the Partiful poster: it says something
    // about *this* ride. It also links to the route rather than to RSVP, and
    // is shown whole (object-fit: contain) instead of cropped like a photo.
    // Failing a map, `poster` is our own resized copy of the ride photo,
    // served from this site (a relative path the sync writes) — `image`, the
    // multi-megabyte original on Firebase, is the last resort. The two behave
    // identically here; only the bytes the visitor fetches differ.
    const firstRoute = (ev.routes || [])[0];
    const banner = ev.map_image || ev.poster || ev.image;
    if (banner) {
      const isMap = Boolean(ev.map_image);
      const imgLink = BCB.el("a", "ride-img-link");
      imgLink.href = (isMap && firstRoute && firstRoute.url) || ev.rsvp_url || BCB.PARTIFUL;
      imgLink.rel = "noopener";
      if (isMap) { imgLink.target = "_blank"; }
      const img = BCB.el("img", "ride-img" + (isMap ? " is-map" : ""));
      img.src = banner;
      img.alt = isMap
        ? "Route map for " + (ev.title || "this café ride")
        : (ev.title || "Café ride");
      img.loading = "lazy";
      imgLink.appendChild(img);
      card.appendChild(imgLink);
    }
    if (ev.location) {
      card.appendChild(BCB.el("p", "where", ev.location));
    } else if (ev.location_url) {
      // The Location field held a map link, not an address — show it as a
      // named link; the raw URL would run off the side of the card.
      const whereLine = BCB.el("p", "where");
      const whereLink = BCB.el("a", null, meetingPointText(ev.location_url));
      whereLink.href = ev.location_url;
      whereLink.target = "_blank";
      whereLink.rel = "noopener";
      whereLine.appendChild(whereLink);
      card.appendChild(whereLine);
    } else if (ev.location_hidden) {
      // Partiful hides the address until RSVP; show a friendly note instead
      // of the feed's "Location available once RSVP'd" template text.
      card.appendChild(BCB.el("p", "where", "Location shared after you RSVP"));
    }
    // Route links come from the host's Partiful custom fields ("Estimated
    // Route", "Team A & C Route", …) and open Google Maps. The card names only
    // where the ride starts ("from Cleveland Circle" — route.start_name, out of
    // the data): the café is already the .where line, and the full start → end
    // pair (with any stops) is one tap away in Maps, and in the link's title.
    (ev.routes || []).forEach((route) => {
      const line = BCB.el("p", "route");
      const link = BCB.el("a", null, route.label || "Route");
      link.href = route.url;
      link.target = "_blank";
      link.rel = "noopener";
      link.title = route.start && route.end
        ? route.start + " → " + route.end
        : "Open this route in Google Maps";
      line.appendChild(link);
      // Distance is measured from the route's own stops (see fetch_rides.py),
      // not quoted from Google — the "~" in the string is doing real work.
      if (route.distance_display) {
        line.appendChild(BCB.el("span", "route-distance", route.distance_display));
      }
      // Precomputed by ride_fields.derive (the Bluebikes-dock rule lives in
      // Python now); route.start is the fallback for a deploy that lands ahead
      // of the data it reads.
      const start = route.start_name || route.start;
      if (start) {
        line.appendChild(BCB.el("span", "route-start", "from " + start));
      }
      card.appendChild(line);
    });
    if (ev.description) { card.appendChild(BCB.el("p", null, ev.description)); }
    const actions = BCB.el("div", "ride-actions");
    const link = BCB.el("a", "btn", ev.past ? "See it on Partiful" : "RSVP on Partiful");
    link.href = ev.rsvp_url || BCB.PARTIFUL;
    link.rel = "noopener";
    actions.appendChild(link);
    if (ev.past) {
      // No RSVP, and nothing to add to a calendar — the ride is over. The
      // Partiful link still works, and that's where the photos are.
      card.appendChild(actions);
      return card;
    }
    const ics = BCB.el("button", "btn btn-ghost", "Add to calendar");
    ics.type = "button";
    ics.setAttribute("aria-label", "Download .ics for " + (ev.title || "this ride"));
    ics.addEventListener("click", () => { BCB.downloadIcs(ev); });
    actions.appendChild(ics);
    const gcal = BCB.el("a", "btn btn-ghost", "Google Calendar");
    gcal.href = BCB.googleCalUrl(ev);
    gcal.target = "_blank";
    gcal.rel = "noopener";
    actions.appendChild(gcal);
    card.appendChild(actions);
    return card;
  };
})(window.BCB = window.BCB || {});
