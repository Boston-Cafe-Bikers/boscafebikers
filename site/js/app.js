// App bootstrap: fetch events.json, render the featured next-ride card + the
// calendar, the ride-detail modal, and the "Last updated …" stamp. Loads LAST
// in the dependency order (defer) and reads the shared helpers off the `BCB`
// namespace (window.BCB) that ride-card.js / calendar.js populate — see the
// "JS module split" note in CLAUDE.md.
(function (BCB) {
  "use strict";

  const { el, rideCard, isOver, PARTIFUL, MONTHS } = BCB;
  const { renderCalendarFull, destroyCalendar, groupByMonth, monthGrid } = BCB;

  const schedule = document.getElementById("schedule");
  const updated = document.getElementById("updated");
  const updatedLink = document.getElementById("updated-link");
  const nextRideCard = document.getElementById("next-ride-card");

  let currentData = null;
  // Rides that already happened, from the accumulating events-past.json. They
  // go on the calendar (dimmed) but never into the next-ride card.
  let pastEvents = [];
  let lastFocused = null;

  // Both lists are sorted by start already, and every past ride precedes every
  // upcoming one, so this concatenation is chronological. Each archived ride is
  // copied with `past: true` — the flag drives the dimmed calendar chip and the
  // ride card's "See it on Partiful" variant. `over` is the slice of
  // events.json the visitor's clock says has already finished (see split());
  // it sits between the two, because it started after everything archived and
  // before everything still to come.
  function calendarEvents(over, upcoming) {
    return pastEvents
      .concat(over)
      .map((ev) => Object.assign({}, ev, { past: true }))
      .concat(upcoming);
  }

  // events.json is only as fresh as the last sync, up to 6 hours ago, so the
  // ride at events[0] may have ended hours before the page loaded. Decide
  // against the visitor's clock instead: everything whose grace hour has
  // passed is `over` (dimmed on the calendar, no RSVP, never the next ride),
  // the rest is `upcoming`. Order is preserved, so upcoming[0] is the next
  // ride. isOver is the one sanctioned read of the clock — see ride-card.js.
  function split(events) {
    const over = [];
    const upcoming = [];
    events.forEach((ev) => { (isOver(ev) ? over : upcoming).push(ev); });
    return { over, upcoming };
  }

  function emptyState(message) {
    schedule.textContent = "";
    schedule.appendChild(el("p", "note", message));
    const link = el("a", "btn", "See all rides on Partiful");
    link.href = PARTIFUL;
    link.rel = "noopener";
    schedule.appendChild(link);
  }

  // events.json timestamps already carry an Eastern offset and the display
  // strings are precomputed in Python — never re-format with Date(), or
  // visitors outside Eastern see the wrong time.
  function stamp(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso || "");
    if (!m) { return ""; }
    const hour = parseInt(m[4], 10);
    const suffix = hour < 12 ? "am" : "pm";
    const hour12 = hour % 12 === 0 ? 12 : hour % 12;
    return "Last updated " + MONTHS[parseInt(m[2], 10) - 1] + " " +
      parseInt(m[3], 10) + ", " + m[1] + " at " + hour12 + ":" + m[5] +
      " " + suffix + " ET.";
  }

  // --- ride detail modal ---
  // Clicking a calendar event (FullCalendar or the fallback grid) opens this
  // instead of navigating straight to the RSVP page — the BCU detail-page
  // pattern adapted for a static site. The content reuses the rideCard
  // builder, so details and the add-to-calendar exports match the featured
  // next-ride card exactly. Exposed on BCB so calendar.js can open it.
  const rideModal = document.getElementById("ride-modal");
  const rideModalContent = document.getElementById("ride-modal-content");
  const rideModalDialog = rideModal ? rideModal.querySelector(".modal-dialog") : null;

  function openRideModal(ev) {
    lastFocused = document.activeElement;
    rideModalContent.textContent = "";
    rideModalContent.appendChild(rideCard(ev, "modal-ride"));
    if (rideModalDialog) {
      rideModalDialog.setAttribute("aria-label", ev.title || "Ride details");
    }
    rideModal.hidden = false;
    document.body.style.overflow = "hidden";
    const close = rideModal.querySelector(".modal-close");
    if (close) { close.focus(); }
  }

  function closeRideModal() {
    if (!rideModal || rideModal.hidden) { return; }
    rideModal.hidden = true;
    document.body.style.overflow = "";
    if (lastFocused && lastFocused.focus) { lastFocused.focus(); }
    lastFocused = null;
  }

  if (rideModal) {
    const modalCloseBtn = rideModal.querySelector(".modal-close");
    if (modalCloseBtn) { modalCloseBtn.addEventListener("click", closeRideModal); }
    // Clicking the backdrop (or anywhere outside the dialog) closes it.
    rideModal.addEventListener("click", (e) => {
      if (!e.target.closest(".modal-dialog")) { closeRideModal(); }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !rideModal.hidden) { closeRideModal(); }
    });
  }
  BCB.openRideModal = openRideModal;

  // The next-ride section shows the first ride that hasn't finished: events
  // are the `upcoming` half of split(), sorted by start. A ride inside its
  // grace hour still counts (a latecomer lands on it and the card wears
  // rideCard's "Rolling now" pill); one past it does not, however stale
  // events.json is. Nothing left → an empty state with a Partiful link.
  function setNextRide(events) {
    nextRideCard.textContent = "";
    if (!events.length) {
      nextRideCard.appendChild(el("p", "note", "No rides scheduled yet — check Partiful for the next one."));
      const link = el("a", "btn", "See all rides on Partiful");
      link.href = PARTIFUL;
      link.rel = "noopener";
      nextRideCard.appendChild(link);
      return;
    }
    nextRideCard.appendChild(rideCard(events[0], "next-ride"));
  }

  // The schedule section is calendar-only now: FullCalendar 6 when its CDN
  // script has loaded, otherwise the hand-rolled month grid. The list view
  // was removed in favor of the featured next-ride card above.
  // A page left open across the end of a grace hour switches on its own: the
  // timer fires one second after upcoming[0]'s grace_until and re-renders,
  // which moves that ride into `over` and the next one into the card. One
  // timer at a time (render() can run more than once — the late-CDN hook), and
  // none past setTimeout's 32-bit ceiling (~24.8 days): a ride further out
  // than that is left to the next page load.
  const MAX_TIMER_MS = 2147483647;
  let switchTimer = null;
  function scheduleSwitch(next) {
    if (switchTimer !== null) {
      clearTimeout(switchTimer);
      switchTimer = null;
    }
    const endMs = Date.parse((next && next.grace_until) || "");
    if (isNaN(endMs)) { return; }
    const delay = endMs - Date.now() + 1000;
    if (delay <= 0 || delay > MAX_TIMER_MS) { return; }
    switchTimer = setTimeout(() => {
      switchTimer = null;
      render();
    }, delay);
  }

  function render() {
    destroyCalendar();
    const events = (currentData && currentData.events) || [];
    const { over, upcoming } = split(events);
    const shown = calendarEvents(over, upcoming);
    // Open on the next ride's month; with nothing upcoming, on the most recent
    // one — never on the oldest archived ride, which is where shown[0] sits.
    const finished = pastEvents.concat(over);
    const focus = upcoming.length
      ? upcoming[0].start
      : (finished.length ? finished[finished.length - 1].start : null);
    schedule.textContent = "";
    if (!shown.length) {
      emptyState("No rides on the calendar right now — check Partiful for the next one.");
    } else if (typeof FullCalendar !== "undefined") {
      // renderCalendarFull mounts the box into #schedule itself before it
      // calls FullCalendar.render() — the holder must be in the document so FC
      // measures a real container width (a detached element → 0-width columns).
      renderCalendarFull(shown, schedule, focus);
    } else {
      groupByMonth(shown).forEach((month) => {
        schedule.appendChild(monthGrid(month));
      });
    }
    setNextRide(upcoming);
    scheduleSwitch(upcoming[0]);
    // The "Last updated …" stamp is the link to the sync workflow. Hide the
    // whole line (not just the text) when there's no updated_at, so there's
    // no empty focusable link.
    const text = stamp(currentData && currentData.updated_at);
    if (updatedLink) { updatedLink.textContent = text; }
    if (updated) { updated.hidden = !text; }
  }

  // The deferred FullCalendar script usually finishes before the events fetch
  // resolves, so the first render already uses it. This hook covers the edge
  // case where the CDN script lands after an earlier fallback render.
  if (typeof FullCalendar === "undefined") {
    const fcScript = document.querySelector('script[src*="fullcalendar"]');
    if (fcScript && fcScript.addEventListener) {
      fcScript.addEventListener("load", () => {
        if (currentData) { render(); }
      });
    }
  }

  // --- deferred background images ([data-bg]) ---
  // The hero photo (and any future background image) is held in the data-bg
  // attribute; JS copies it into background-image only after window.load so it
  // never blocks first paint / LCP. The element's own CSS background (a café
  // gradient/color) is the fallback that renders before this — and stays for
  // anyone without JS. ride <img> photos keep loading="lazy" in rideCard.
  function applyBackgrounds() {
    document.querySelectorAll("[data-bg]").forEach((el) => {
      const src = el.getAttribute("data-bg");
      if (src) {
        el.style.backgroundImage = 'url("' + src + '")';
        el.classList.add("bg-loaded");
      }
    });
  }
  // defer scripts run before the load event; the readyState guard covers the
  // edge case where a page is restored from bfcache and load has already fired.
  if (document.readyState === "complete") {
    applyBackgrounds();
  } else {
    window.addEventListener("load", applyBackgrounds);
  }

  async function loadData(url) {
    const res = await fetch(url, { cache: "no-cache" });
    if (!res.ok) { throw new Error("HTTP " + res.status); }
    return res.json();
  }

  // events-past.json is optional: a fresh checkout (or a fork whose sync has
  // never archived anything) simply has no past rides, and a 404 must not take
  // the upcoming schedule down with it.
  const pastLoaded = loadData("events-past.json")
    .then((data) => { pastEvents = (data && data.events) || []; })
    .catch(() => { pastEvents = []; });

  loadData("events.json")
    .then((data) => pastLoaded.then(() => {
      currentData = data;
      render();
    }))
    .catch(() => {
      // A failed schedule fetch stays an error, not a silent fall back to the
      // archive: "here are last month's rides" would read as if they were the
      // upcoming ones.
      setNextRide([]);
      emptyState("Couldn't load the ride schedule just now.");
    });
})(window.BCB = window.BCB || {});
