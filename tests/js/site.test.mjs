// Verification for site/js/ride-card.js, calendar.js and app.js.
//
//   node --test tests/js/*.test.mjs
//
// (Shell-globbed, not `node --test tests/js/`: Node 26 stopped accepting a
// bare directory there. The glob works on every version CI or a laptop has.)
//
// Node built-ins only (node:test, node:assert/strict, and the hand-rolled DOM
// in ./dom-shim.mjs, which uses node:vm/fs/path). There is no package.json and
// no npm dependency, matching site/'s no-build-step rule.
//
// Every fixture ride carries an Eastern offset in its `start`, exactly like
// site/events.json, and every date is in 2030 so the fixtures don't rot. Run
// the suite under TZ=Asia/Tokyo too: nothing here may depend on the system
// timezone.

import test from "node:test";
import assert from "node:assert/strict";

import {
  createHarness, createContactHarness, createNavHarness, makeFullCalendarStub
} from "./dom-shim.mjs";

const PARTIFUL = "https://partiful.com/u/Hs47uq5mucZyXLBJZCda";

// Every fixture carries the fields scripts/ride_fields.py derives — grace_until,
// place_name, address, year, and each route's start_name/end_name — because
// that is what the sync writes and all the JS does now is read them.
const RIDE_A = {
  uid: "abc123",
  title: "O'Some Sunday ☕",
  start: "2030-09-07T10:00:00-04:00",
  end: "2030-09-07T12:00:00-04:00",
  grace_until: "2030-09-07T11:00:00-04:00",
  date_display: "Saturday, September 7",
  time_display: "10:00 am",
  year: "2030",
  location: "O'Some Café, 100 Main St, Watertown, MA 02472",
  place_name: "O'Some Café",
  address: "100 Main St, Watertown, MA 02472",
  location_hidden: false,
  location_url: null,
  description: "Easy four flat miles, then coffee.",
  rsvp_url: "https://partiful.com/e/abc123",
  image: null,
  map_image: "maps/abc123.svg",
  routes: [{
    label: "Estimated Route",
    url: "https://maps.app.goo.gl/short1",
    start: "Bluebikes, Cleveland Circle, Boston, MA 02135",
    start_name: "Cleveland Circle",
    end: "O'Some Café, 100 Main St, Watertown, MA 02472",
    end_name: "O'Some Café",
    distance_display: "~4.0 mi"
  }]
};

const RIDE_B = {
  uid: "def456",
  title: "Minuteman morning",
  start: "2030-09-21T11:00:00-04:00",
  end: null,
  grace_until: "2030-09-21T12:00:00-04:00",
  date_display: "Saturday, September 21",
  time_display: "11:00 am",
  year: "2030",
  location: null,
  place_name: null,
  address: null,
  location_hidden: true,
  location_url: null,
  description: "Bring a lock.",
  rsvp_url: null,
  image: "https://example.invalid/poster.jpg",
  map_image: null,
  routes: []
};

const PAST_RIDE = {
  uid: "old789",
  title: "Charles River loop",
  start: "2025-11-16T10:00:00-05:00",
  end: null,
  grace_until: "2025-11-16T11:00:00-05:00",
  date_display: "Sunday, November 16",
  time_display: "10:00 am",
  year: "2025",
  location: "Tatte Bakery, 1003 Beacon St, Brookline, MA 02446",
  place_name: "Tatte Bakery",
  address: "1003 Beacon St, Brookline, MA 02446",
  location_hidden: false,
  location_url: null,
  description: "A cold one.",
  rsvp_url: "https://partiful.com/e/old789",
  image: null,
  map_image: null,
  routes: []
};

const UPDATED_AT = "2026-08-28T15:04:00-04:00";

function payload(events, updatedAt = UPDATED_AT) {
  return { updated_at: updatedAt, count: events.length, events };
}

const FULL_ROUTES = {
  "events.json": { body: payload([RIDE_A, RIDE_B]) },
  "events-past.json": { body: { events: [PAST_RIDE] } }
};

async function booted(options = {}) {
  const h = createHarness({ routes: FULL_ROUTES, ...options });
  await h.flush();
  return h;
}

/* ================================================================== *
 * Bootstrap: the happy path
 * ================================================================== */

test("happy path renders the next-ride card, the stamp and the calendar", async () => {
  const h = await booted();

  // events[0] of the *upcoming* list, never the archive.
  const card = h.nextRideCard.querySelector(".ride");
  assert.ok(card, "a ride card was rendered into #next-ride-card");
  assert.ok(card.classList.contains("next-ride"));
  assert.ok(!card.classList.contains("is-past"));
  assert.equal(card.querySelector("h3").textContent, RIDE_A.title);
  assert.equal(card.querySelector(".when").textContent, "Saturday, September 7 · 10:00 am");
  assert.equal(card.querySelector(".where").textContent, RIDE_A.location);
  assert.equal(card.querySelector(".ride-actions .btn").textContent, "RSVP on Partiful");
  assert.equal(card.querySelector(".ride-actions .btn").href, RIDE_A.rsvp_url);

  // A drawn route map wins over the poster, links to the route, opens a tab.
  const img = card.querySelector(".ride-img");
  assert.ok(img.classList.contains("is-map"));
  assert.equal(img.src, "maps/abc123.svg");
  assert.equal(img.loading, "lazy");
  const imgLink = card.querySelector(".ride-img-link");
  assert.equal(imgLink.href, RIDE_A.routes[0].url);
  assert.equal(imgLink.target, "_blank");

  // Route line: label, measured distance pill, "from <route.start_name>".
  const route = card.querySelector(".route");
  assert.equal(route.querySelector("a").textContent, "Estimated Route");
  assert.equal(route.querySelector(".route-distance").textContent, "~4.0 mi");
  assert.equal(route.querySelector(".route-start").textContent, "from Cleveland Circle");

  // "Last updated" is regex-sliced off the ISO string, never re-formatted.
  assert.equal(h.updated.hidden, false);
  assert.equal(h.updatedLink.textContent, "Last updated August 28, 2026 at 3:04 pm ET.");

  // Fallback month grid (no FullCalendar global): one .calendar per month.
  const months = h.schedule.querySelectorAll(".calendar");
  assert.equal(months.length, 2);
  assert.equal(months[0].querySelector("h3").textContent, "November 2025");
  assert.equal(months[1].querySelector("h3").textContent, "September 2030");
  assert.equal(h.schedule.querySelectorAll(".ride-chip").length, 3);
});

test("archived rides join the calendar dimmed, and never the next-ride card", async () => {
  const h = await booted();

  const chips = h.schedule.querySelectorAll(".ride-chip");
  assert.equal(chips[0].textContent, PAST_RIDE.title);
  assert.ok(chips[0].classList.contains("is-past"), "the archived ride's chip is dimmed");
  assert.equal(chips[0].getAttribute("aria-label"),
    "View details for past ride " + PAST_RIDE.title);
  assert.ok(!chips[1].classList.contains("is-past"));

  // The archive is copied with past: true, so the source object is untouched.
  assert.equal(PAST_RIDE.past, undefined);
  assert.equal(h.nextRideCard.querySelector("h3").textContent, RIDE_A.title);
});

test("the archived ride's card drops RSVP and both calendar exports", async () => {
  const h = await booted();
  h.schedule.querySelectorAll(".ride-chip")[0].click();

  const card = h.modalContent.querySelector(".ride");
  assert.ok(card.classList.contains("is-past"));
  assert.equal(card.querySelector(".ride-tag").textContent, "Past ride");
  const actions = card.querySelector(".ride-actions");
  assert.equal(actions.children.length, 1, "one action only — the ride is over");
  assert.equal(actions.querySelector(".btn").textContent, "See it on Partiful");
});

test("a ride with no rsvp_url falls back to the Partiful profile", async () => {
  const h = await booted({});
  // RIDE_B has rsvp_url: null; open it from the calendar.
  const chip = h.schedule.querySelectorAll(".ride-chip")[2];
  assert.equal(chip.textContent, RIDE_B.title);
  chip.click();

  const card = h.modalContent.querySelector(".ride");
  assert.equal(card.querySelector(".ride-actions .btn").href, PARTIFUL);
  // No map, so the poster banner links at the RSVP target — here the profile.
  const imgLink = card.querySelector(".ride-img-link");
  assert.equal(imgLink.href, PARTIFUL);
  assert.equal(imgLink.target, undefined, "a poster banner does not open a new tab");
  assert.ok(!card.querySelector(".ride-img").classList.contains("is-map"));
});

test("the banner is map_image, then poster, then image", async () => {
  // Three sources, in the order the site prefers them: the drawn route map
  // (about this ride), our own resized copy of the photo (same bytes, served
  // from here), and the multi-megabyte Firebase original as the last resort.
  const h = await booted();
  const rideCard = h.BCB.rideCard;
  const base = Object.assign({}, RIDE_A, {
    map_image: "maps/abc123.svg",
    poster: "posters/abc123.jpg",
    image: "https://firebasestorage.googleapis.invalid/original.jpg"
  });

  const withMap = rideCard(base);
  assert.equal(withMap.querySelector(".ride-img").src, "maps/abc123.svg");
  assert.ok(withMap.querySelector(".ride-img").classList.contains("is-map"));

  const withPoster = rideCard(Object.assign({}, base, { map_image: null }));
  const posterImg = withPoster.querySelector(".ride-img");
  assert.equal(posterImg.src, "posters/abc123.jpg");
  assert.equal(posterImg.loading, "lazy");
  assert.ok(!posterImg.classList.contains("is-map"), "a photo is cropped, not contained");
  assert.equal(posterImg.alt, RIDE_A.title);
  // Same-origin and relative — the site is served from /boscafebikers/.
  assert.ok(!posterImg.src.startsWith("/") && !/^https?:/.test(posterImg.src));
  // A poster links at RSVP and stays in the tab, exactly like the original did.
  const posterLink = withPoster.querySelector(".ride-img-link");
  assert.equal(posterLink.href, RIDE_A.rsvp_url);
  assert.equal(posterLink.target, undefined);

  const withImage = rideCard(Object.assign({}, base, { map_image: null, poster: null }));
  assert.equal(withImage.querySelector(".ride-img").src, base.image);

  // A ride the mirror gave up on carries poster: null, which must fall through
  // to the original rather than blanking the banner.
  const none = rideCard(Object.assign({}, base, {
    map_image: null, poster: null, image: null
  }));
  assert.equal(none.querySelector(".ride-img"), null, "no banner at all");
});

test("location_hidden renders the friendly note, not the feed's placeholder", async () => {
  const h = await booted();
  h.schedule.querySelectorAll(".ride-chip")[2].click();
  assert.equal(h.modalContent.querySelector(".where").textContent,
    "Location shared after you RSVP");
});

test("a link-only location renders as a named Google Maps link", async () => {
  const ride = Object.assign({}, RIDE_A, {
    location: null,
    location_hidden: false,
    location_url: "https://maps.app.goo.gl/meetinghere"
  });
  const h = createHarness({ routes: { "events.json": { body: payload([ride]) } } });
  await h.flush();

  const where = h.nextRideCard.querySelector(".where");
  const link = where.querySelector("a");
  assert.equal(link.textContent, "Meeting point on Google Maps");
  assert.equal(link.href, ride.location_url);
  assert.equal(link.target, "_blank");
});

test("a non-Google meeting-point link is named plainly", async () => {
  const ride = Object.assign({}, RIDE_A, {
    location: null, location_hidden: false, location_url: "https://osm.org/go/abcd"
  });
  const h = createHarness({ routes: { "events.json": { body: payload([ride]) } } });
  await h.flush();
  assert.equal(h.nextRideCard.querySelector(".where a").textContent, "Meeting point");
});

/* ================================================================== *
 * Empty and failing loads
 * ================================================================== */

test("no rides at all → notes plus the Partiful profile button", async () => {
  const h = createHarness({ routes: { "events.json": { body: payload([]) } } });
  await h.flush();

  assert.equal(h.nextRideCard.querySelector(".note").textContent,
    "No rides scheduled yet — check Partiful for the next one.");
  assert.equal(h.nextRideCard.querySelector(".btn").href, PARTIFUL);
  assert.equal(h.schedule.querySelector(".note").textContent,
    "No rides on the calendar right now — check Partiful for the next one.");
  assert.equal(h.schedule.querySelector(".btn").href, PARTIFUL);
  // updated_at is still present, so the stamp still shows.
  assert.equal(h.updated.hidden, false);
});

test("a failed events.json is an error, and leaves no empty focusable link", async () => {
  const h = createHarness({ routes: { "events-past.json": { body: { events: [PAST_RIDE] } } } });
  await h.flush();

  assert.equal(h.schedule.querySelector(".note").textContent,
    "Couldn't load the ride schedule just now.");
  // Not "here are last month's rides": the archive must not stand in.
  assert.equal(h.schedule.querySelectorAll(".ride-chip").length, 0);
  assert.equal(h.nextRideCard.querySelector(".note").textContent,
    "No rides scheduled yet — check Partiful for the next one.");
  assert.equal(h.updated.hidden, true, "#updated stays hidden");
  assert.equal(h.updatedLink.textContent, "", "no empty focusable link");
});

test("a missing events-past.json is fail-soft — the upcoming rides still render", async () => {
  const h = createHarness({ routes: { "events.json": { body: payload([RIDE_A, RIDE_B]) } } });
  await h.flush();

  assert.equal(h.nextRideCard.querySelector("h3").textContent, RIDE_A.title);
  assert.equal(h.schedule.querySelectorAll(".ride-chip").length, 2);
  assert.equal(h.schedule.querySelectorAll(".calendar").length, 1);
  // Both files are always requested, with cache: no-cache.
  const urls = h.fetchImpl.calls.map((c) => c.url);
  assert.deepEqual(urls.sort(), ["events-past.json", "events.json"]);
  assert.equal(h.fetchImpl.calls[0].options.cache, "no-cache");
});

test("malformed events.json JSON lands in the same error state", async () => {
  const h = createHarness({ routes: { "events.json": { badJson: true } } });
  await h.flush();
  assert.equal(h.schedule.querySelector(".note").textContent,
    "Couldn't load the ride schedule just now.");
});

/* ================================================================== *
 * The ride-detail modal
 * ================================================================== */

test("a calendar chip opens the modal and moves focus to the close button", async () => {
  const h = await booted();
  const chip = h.schedule.querySelectorAll(".ride-chip")[1];
  chip.focus();
  chip.click();

  assert.equal(h.modal.hidden, false);
  assert.equal(h.modalDialog.getAttribute("aria-label"), RIDE_A.title);
  assert.equal(h.doc.activeElement, h.modalClose);
  assert.equal(h.doc.body.style.overflow, "hidden");
  // Same builder as the featured card, so the details can't drift.
  const card = h.modalContent.querySelector(".ride");
  assert.ok(card.classList.contains("modal-ride"));
  assert.equal(card.querySelector("h3").textContent, RIDE_A.title);
});

test("Escape closes the modal and restores focus to the opener", async () => {
  const h = await booted();
  const chip = h.schedule.querySelectorAll(".ride-chip")[1];
  chip.focus();
  chip.click();

  h.doc.dispatchEvent({ type: "keydown", key: "Escape" });
  assert.equal(h.modal.hidden, true);
  assert.equal(h.doc.body.style.overflow, "");
  assert.equal(h.doc.activeElement, chip);
});

test("a backdrop click closes the modal; a click inside the dialog does not", async () => {
  const h = await booted();
  const chip = h.schedule.querySelectorAll(".ride-chip")[1];
  chip.click();

  // Inside .modal-dialog: e.target.closest(".modal-dialog") is truthy.
  h.modalContent.dispatchEvent({ type: "click" });
  assert.equal(h.modal.hidden, false);

  h.backdrop.dispatchEvent({ type: "click" });
  assert.equal(h.modal.hidden, true);
});

test("the × button closes the modal exactly once", async () => {
  const h = await booted();
  const chip = h.schedule.querySelectorAll(".ride-chip")[1];
  chip.focus();
  chip.click();

  // The close button is inside .modal-dialog, so the overlay's own click
  // handler must not fire a second close on the way up.
  h.modalClose.click();
  assert.equal(h.modal.hidden, true);
  assert.equal(h.doc.activeElement, chip);

  // Escape on an already-closed modal is a no-op.
  h.doc.dispatchEvent({ type: "keydown", key: "Escape" });
  assert.equal(h.modal.hidden, true);
});

/* ================================================================== *
 * FullCalendar
 * ================================================================== */

test("FullCalendar is mounted before render and configured for Eastern", async () => {
  const stub = makeFullCalendarStub();
  const h = await booted({ FullCalendar: { Calendar: stub.Calendar } });

  assert.equal(stub.instances.length, 1);
  const cal = stub.instances[0];
  assert.equal(cal.rendered, true);
  assert.equal(cal.mountedBeforeRender, true,
    "the holder must be in the document before FullCalendar.render()");
  assert.equal(cal.holder.id, "ride-calendar");
  assert.equal(cal.holder.parentNode.className, "calendar");
  assert.equal(cal.holder.parentNode.parentNode, h.schedule);

  const opts = cal.options;
  assert.equal(opts.initialView, "dayGridMonth");
  assert.equal(opts.timeZone, "America/New_York");
  assert.equal(opts.displayEventTime, false, "the time prefix eats a phone chip");
  assert.equal(opts.height, "auto");
  assert.equal(opts.dayMaxEvents, 3);
  assert.equal(opts.eventDisplay, "block");
  // Opens on the next *upcoming* ride's month, not the oldest archived one.
  assert.equal(opts.initialDate, "2030-09-07");

  assert.equal(opts.events.length, 3);
  // Spread first: the array is created in the vm realm, so a strict
  // deepEqual against a host array would fail on the prototype.
  assert.deepEqual([...opts.events[0].classNames], ["is-past"]);
  assert.equal(opts.events[0].start, PAST_RIDE.start);
  assert.equal(opts.events[1].url, RIDE_A.rsvp_url);
  assert.equal(opts.events[1].end, RIDE_A.end);
  assert.equal(opts.events[2].url, PARTIFUL, "no rsvp_url → the profile");
  assert.equal(opts.events[2].end, undefined, "no DTEND → no end key");
  assert.equal(opts.events[1].extendedProps.data.uid, RIDE_A.uid);
  assert.equal(opts.events[2].classNames, undefined);
});

test("eventClick opens the modal instead of navigating", async () => {
  const stub = makeFullCalendarStub();
  const h = await booted({ FullCalendar: { Calendar: stub.Calendar } });

  let prevented = false;
  stub.instances[0].options.eventClick({
    jsEvent: { preventDefault() { prevented = true; } },
    event: { extendedProps: { data: RIDE_A } }
  });
  assert.equal(prevented, true, "the left click must not navigate to RSVP");
  assert.equal(h.modal.hidden, false);
  assert.equal(h.modalContent.querySelector("h3").textContent, RIDE_A.title);
});

test("a late CDN re-renders, and the stale calendar instance is destroyed", async () => {
  const h = await booted();
  // No FullCalendar at boot: the hand-rolled grid ran.
  assert.equal(h.schedule.querySelectorAll(".ride-chip").length, 3);

  const stub = makeFullCalendarStub();
  h.setFullCalendar({ Calendar: stub.Calendar });
  h.fcScript.dispatchEvent({ type: "load" });
  assert.equal(stub.instances.length, 1);
  assert.equal(h.schedule.querySelectorAll(".ride-chip").length, 0, "grid replaced");

  h.fcScript.dispatchEvent({ type: "load" });
  assert.equal(stub.instances.length, 2);
  assert.equal(stub.instances[0].destroyed, true, "destroyCalendar() ran first");
  assert.equal(stub.instances[1].destroyed, false);
});

/* ================================================================== *
 * Deferred [data-bg] photos
 * ================================================================== */

test("[data-bg] photos are applied only after window load", async () => {
  const h = await booted();
  const section = h.doc.getElementById("next-ride");

  assert.ok(!section.style.backgroundImage, "no background before load fires");
  assert.ok(!section.classList.contains("bg-loaded"));

  h.fireWindowEvent("load");

  assert.equal(section.style.backgroundImage, 'url("images/jess-b-gracies-bikes.jpeg")');
  assert.ok(section.classList.contains("bg-loaded"));
});

/* ================================================================== *
 * BCB.isRolling — the one sanctioned use of the clock
 * ================================================================== */

test("isRolling reads grace_until, inclusive at both ends", async () => {
  const h = await booted();
  const startMs = Date.parse(RIDE_A.start);
  const minute = 60 * 1000;

  assert.equal(h.BCB.isRolling(RIDE_A, startMs - minute), false, "a minute before start");
  assert.equal(h.BCB.isRolling(RIDE_A, startMs), true, "at start");
  assert.equal(h.BCB.isRolling(RIDE_A, startMs + 30 * minute), true, "mid-ride");
  assert.equal(h.BCB.isRolling(RIDE_A, startMs + 60 * minute), true,
    "exactly at grace_until, still rolling");
  assert.equal(h.BCB.isRolling(RIDE_A, startMs + 61 * minute), false, "past grace_until");
  assert.equal(h.BCB.isRolling({ start: "not a date", grace_until: RIDE_A.grace_until }, startMs),
    false);
  assert.equal(h.BCB.isRolling({}, startMs), false);
});

test("the window is whatever grace_until says, not an hour hardcoded here", async () => {
  // The length of the grace hour lives in scripts/ride_fields.py; if it ever
  // moves, the data moves with it and this file has nothing to keep in step.
  const h = await booted();
  const startMs = Date.parse(RIDE_A.start);
  const wide = Object.assign({}, RIDE_A, { grace_until: "2030-09-07T13:00:00-04:00" });
  assert.equal(h.BCB.isRolling(wide, startMs + 150 * 60 * 1000), true, "two and a half hours in");
  assert.equal(h.BCB.isRolling(wide, startMs + 181 * 60 * 1000), false, "past the wider window");
});

test("a ride with no grace_until is never rolling", async () => {
  // A deploy can land minutes before the data that feeds it; a missing window
  // must read as "not rolling", never as an hour guessed in the browser.
  const h = await booted();
  const startMs = Date.parse(RIDE_A.start);
  const old = Object.assign({}, RIDE_A);
  delete old.grace_until;
  assert.equal(h.BCB.isRolling(old, startMs + 10 * 60 * 1000), false);
  assert.equal(h.BCB.isRolling(Object.assign({}, RIDE_A, { grace_until: null }), startMs), false);
});

test("a rolling ride wears the pill and keeps RSVP and both exports", async () => {
  // rideCard() reads the real clock for a non-archived ride, so this fixture
  // has to be pinned to "now" rather than to a fixed date.
  const now = Date.now();
  const iso = (ms) => {
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, "0");
    // Hand-built as an Eastern-offset string; the value only has to parse back
    // to `ms`, which is what isRolling compares.
    return d.getUTCFullYear() + "-" + p(d.getUTCMonth() + 1) + "-" + p(d.getUTCDate()) +
      "T" + p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds()) + "+00:00";
  };
  const ride = Object.assign({}, RIDE_A, {
    start: iso(now - 10 * 60 * 1000),
    grace_until: iso(now + 50 * 60 * 1000),
    end: null
  });
  const h = createHarness({ routes: { "events.json": { body: payload([ride]) } } });
  await h.flush();

  const card = h.nextRideCard.querySelector(".ride");
  const tag = card.querySelector(".ride-tag");
  assert.equal(tag.textContent, "Rolling now");
  assert.ok(tag.classList.contains("is-rolling"));
  assert.equal(card.querySelectorAll(".ride-actions .btn").length, 3);
});

/* ================================================================== *
 * The route line reads its names, it doesn't derive them
 * ================================================================== */
// The Bluebikes-dock rule lives in scripts/ride_fields.py (and is tested in
// tests/test_ride_fields.py); the card's job is to print what the sync wrote.

test("the route line prints route.start_name verbatim", async () => {
  // A name no rule in this file could have produced from route.start: if the
  // card still shows it, it is reading the field rather than re-deriving one.
  const ride = Object.assign({}, RIDE_A, {
    routes: [Object.assign({}, RIDE_A.routes[0], { start_name: "The usual corner" })]
  });
  const h = createHarness({ routes: { "events.json": { body: payload([ride]) } } });
  await h.flush();

  const route = h.nextRideCard.querySelector(".route");
  assert.equal(route.querySelector(".route-start").textContent, "from The usual corner");
  // The untrimmed pair is still the link's title.
  assert.equal(route.querySelector("a").title,
    RIDE_A.routes[0].start + " → " + RIDE_A.routes[0].end);
});

test("a route with no start_name falls back to its raw start", async () => {
  // A deploy that lands ahead of the data still names the meeting point.
  const route = Object.assign({}, RIDE_A.routes[0]);
  delete route.start_name;
  const h = createHarness({
    routes: { "events.json": { body: payload([Object.assign({}, RIDE_A, { routes: [route] })]) } }
  });
  await h.flush();

  assert.equal(h.nextRideCard.querySelector(".route-start").textContent,
    "from " + route.start);
});

test("a route with neither start nor start_name shows no from-line", async () => {
  const route = { label: "Estimated Route", url: "https://maps.app.goo.gl/short1" };
  const h = createHarness({
    routes: { "events.json": { body: payload([Object.assign({}, RIDE_A, { routes: [route] })]) } }
  });
  await h.flush();

  const line = h.nextRideCard.querySelector(".route");
  assert.equal(line.querySelector(".route-start"), null);
  assert.equal(line.querySelector("a").title, "Open this route in Google Maps");
});

/* ================================================================== *
 * The .ics export
 * ================================================================== */

function unfold(ics) {
  return ics.replace(/\r\n /g, "");
}
function icsLine(ics, prefix) {
  return unfold(ics).split("\r\n").find((l) => l.startsWith(prefix));
}

test("buildIcs writes Eastern wall-clock times, never a re-parsed Date", async () => {
  const h = await booted();
  const ics = h.BCB.buildIcs(RIDE_A);

  assert.ok(ics.startsWith("BEGIN:VCALENDAR\r\n"));
  assert.ok(ics.endsWith("END:VCALENDAR\r\n"));
  assert.equal(icsLine(ics, "UID:"), "UID:abc123");
  assert.equal(icsLine(ics, "DTSTART"), "DTSTART;TZID=America/New_York:20300907T100000");
  assert.equal(icsLine(ics, "DTEND"), "DTEND;TZID=America/New_York:20300907T120000");
  assert.equal(icsLine(ics, "URL:"), "URL:" + RIDE_A.rsvp_url);
  assert.match(icsLine(ics, "DTSTAMP:"), /^DTSTAMP:\d{8}T\d{6}Z$/);
  assert.equal(icsLine(ics, "LOCATION:"),
    "LOCATION:O'Some Café\\, 100 Main St\\, Watertown\\, MA 02472");
});

test("buildIcs escapes text and folds every line at 75 characters", async () => {
  const h = await booted();
  const ride = Object.assign({}, RIDE_A, {
    title: "Beans, cranks; and a very long café name that will certainly need folding",
    description: "Line one\nLine two, with a comma; and a semicolon\\backslash"
  });
  const ics = h.BCB.buildIcs(ride);

  for (const line of ics.split("\r\n")) {
    assert.ok(line.length <= 75, "line over 75 chars: " + JSON.stringify(line));
  }
  for (const line of ics.split("\r\n").slice(1)) {
    if (line && !/^[A-Z]/.test(line)) {
      assert.ok(line.startsWith(" "), "a continuation line leads with a space");
    }
  }
  assert.equal(icsLine(ics, "SUMMARY:"),
    "SUMMARY:Beans\\, cranks\\; and a very long café name that will certainly need folding");
  assert.equal(icsLine(ics, "DESCRIPTION:"),
    "DESCRIPTION:Line one\\nLine two\\, with a comma\\; and a semicolon\\\\backslash" +
    "\\n\\nRSVP: https://partiful.com/e/abc123");
});

test("buildIcs falls back to a derived UID and omits what the ride lacks", async () => {
  const h = await booted();
  const ride = { title: "O'Some Sunday", start: "2030-09-07T10:00:00-04:00" };
  const ics = h.BCB.buildIcs(ride);

  assert.equal(icsLine(ics, "UID:"), "UID:boscafebikers-20300907T100000-o-some-sunday");
  assert.equal(icsLine(ics, "DTEND"), undefined);
  assert.equal(icsLine(ics, "LOCATION:"), undefined);
  assert.equal(icsLine(ics, "DESCRIPTION:"), undefined);
  assert.equal(icsLine(ics, "URL:"), undefined);
});

test("exportDetails orders description, meeting point, RSVP — and drops the meeting point when there is an address", async () => {
  const h = await booted();

  const linkOnly = Object.assign({}, RIDE_A, {
    location: null, location_url: "https://maps.app.goo.gl/meetinghere"
  });
  assert.equal(icsLine(h.BCB.buildIcs(linkOnly), "DESCRIPTION:"),
    "DESCRIPTION:Easy four flat miles\\, then coffee." +
    "\\n\\nMeeting point: https://maps.app.goo.gl/meetinghere" +
    "\\n\\nRSVP: https://partiful.com/e/abc123");
  // A link-only location never becomes an ICS LOCATION — calendar apps
  // geocode a bare URL badly.
  assert.equal(icsLine(h.BCB.buildIcs(linkOnly), "LOCATION:"), undefined);

  const both = Object.assign({}, RIDE_A, { location_url: "https://maps.app.goo.gl/meetinghere" });
  assert.equal(icsLine(h.BCB.buildIcs(both), "DESCRIPTION:"),
    "DESCRIPTION:Easy four flat miles\\, then coffee.\\n\\nRSVP: https://partiful.com/e/abc123");
});

test("the Add-to-calendar button downloads a .ics blob", async () => {
  const h = await booted();
  const button = h.nextRideCard.querySelector("button.btn-ghost");
  assert.equal(button.textContent, "Add to calendar");
  assert.equal(button.getAttribute("aria-label"), "Download .ics for " + RIDE_A.title);

  button.click();

  assert.equal(h.blobs.length, 1);
  assert.equal(h.blobs[0].type, "text/calendar;charset=utf-8");
  assert.ok(h.blobs[0].text.includes("DTSTART;TZID=America/New_York:20300907T100000"));
  assert.equal(h.objectUrls.length, 1);
  // The anchor is created, clicked, and taken back out of the document.
  assert.equal(h.doc.body.querySelectorAll("a").filter((a) => a.download).length, 0);
  await h.flush(1);
  assert.equal(h.objectUrls[0].revoked, true);
});

/* ================================================================== *
 * The Google Calendar link
 * ================================================================== */

function gcalParams(url) {
  const out = {};
  url.split("?")[1].split("&").forEach((pair) => {
    const i = pair.indexOf("=");
    out[pair.slice(0, i)] = decodeURIComponent(pair.slice(i + 1));
  });
  return out;
}

test("googleCalUrl mirrors the .ics export in Eastern", async () => {
  const h = await booted();
  const url = h.BCB.googleCalUrl(RIDE_A);
  assert.ok(url.startsWith("https://calendar.google.com/calendar/render?"));

  const p = gcalParams(url);
  assert.equal(p.action, "TEMPLATE");
  assert.equal(p.text, RIDE_A.title);
  assert.equal(p.dates, "20300907T100000/20300907T120000");
  assert.equal(p.ctz, "America/New_York");
  assert.equal(p.location, RIDE_A.location);
  assert.equal(p.details,
    "Easy four flat miles, then coffee.\n\nRSVP: https://partiful.com/e/abc123");
  assert.ok(url.includes("ctz=America%2FNew_York"));
});

test("googleCalUrl defaults a missing end to +1h, rolling past midnight", async () => {
  const h = await booted();
  const nearMidnight = Object.assign({}, RIDE_A, {
    start: "2030-09-07T23:30:00-04:00", end: null
  });
  assert.equal(gcalParams(h.BCB.googleCalUrl(nearMidnight)).dates,
    "20300907T233000/20300908T003000");

  const noon = Object.assign({}, RIDE_A, { start: "2030-09-07T10:00:00-04:00", end: null });
  assert.equal(gcalParams(h.BCB.googleCalUrl(noon)).dates, "20300907T100000/20300907T110000");
});

test("googleCalUrl omits a link-only location and carries it in the details", async () => {
  const h = await booted();
  const linkOnly = Object.assign({}, RIDE_A, {
    location: null, location_url: "https://maps.app.goo.gl/meetinghere"
  });
  const p = gcalParams(h.BCB.googleCalUrl(linkOnly));
  assert.equal(p.location, undefined);
  assert.ok(p.details.includes("Meeting point: https://maps.app.goo.gl/meetinghere"));
});

/* ================================================================== *
 * The calendar's Eastern weekday math
 * ================================================================== */

test("the month grid places rides on their Eastern weekday, whatever TZ node runs in", async () => {
  // 2030-09-01 is a Sunday, so a ride on the 7th is the second Saturday and
  // lands in the second row, last column: cell index 6 + 7 = 13 after the
  // 7 weekday headers.
  const h = await booted();
  const september = h.schedule.querySelectorAll(".calendar")[1];
  const cells = september.querySelectorAll(".cal-day");
  const withRides = cells.filter((c) => c.classList.contains("has-ride"));
  assert.deepEqual(withRides.map((c) => c.querySelector(".num").textContent), ["7", "21"]);

  const dows = september.querySelectorAll(".cal-dow").map((n) => n.textContent);
  assert.deepEqual(dows, ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]);
  // Day 1 is a Sunday in September 2030 → no leading empty cells.
  assert.equal(cells[0].classList.contains("empty"), false);
  assert.equal(cells[0].querySelector(".num").textContent, "1");
  // A near-midnight Eastern ride keeps its Eastern date (23:30 on the 7th is
  // 03:30 UTC on the 8th — a naive new Date() would move it a day).
  assert.equal(cells[6].querySelector(".num").textContent, "7");
});

test("a ride at 23:30 Eastern stays on its own Eastern day", async () => {
  const lateRide = Object.assign({}, RIDE_A, { start: "2030-09-07T23:30:00-04:00", end: null });
  const h = createHarness({ routes: { "events.json": { body: payload([lateRide]) } } });
  await h.flush();

  const cells = h.schedule.querySelector(".calendar").querySelectorAll(".cal-day");
  const withRide = cells.find((c) => c.classList.contains("has-ride"));
  assert.equal(withRide.querySelector(".num").textContent, "7");
});

/* ================================================================== *
 * No markup injection
 * ================================================================== */

test("feed text is set as text, never parsed as markup", async () => {
  const nasty = Object.assign({}, RIDE_A, {
    title: "<img src=x onerror=alert(1)>",
    description: "</p><script>bad()</script>"
  });
  const h = createHarness({ routes: { "events.json": { body: payload([nasty]) } } });
  await h.flush();

  const card = h.nextRideCard.querySelector(".ride");
  assert.equal(card.querySelector("h3").textContent, nasty.title);
  assert.equal(card.querySelectorAll("script").length, 0);
  assert.equal(card.querySelectorAll("img").length, 1, "only the ride banner");
});

/* ================================================================== *
 * nav.js — the phone hamburger (standalone, runs from <head>)
 * ================================================================== */

test("has-js lands before DOMContentLoaded, so the phone nav never flashes open", () => {
  const h = createNavHarness();
  // Nothing has fired yet: this is the state of the page while the body is
  // still being parsed, which is when the CSS decides what to paint.
  assert.ok(h.doc.documentElement.classList.contains("has-js"));
  assert.equal(h.isOpen(), false);
  assert.equal(h.expanded(), "false");
});

test("the toggle opens and closes the panel, and says so", () => {
  const h = createNavHarness();
  h.fireReady();

  h.toggle.click();
  assert.equal(h.isOpen(), true);
  assert.equal(h.expanded(), "true");

  h.toggle.click();
  assert.equal(h.isOpen(), false, "the × closes what the bars opened");
  assert.equal(h.expanded(), "false");
});

test("tapping a tab closes the panel before the page navigates", () => {
  const h = createNavHarness();
  h.fireReady();
  h.toggle.click();

  h.links[2].click();
  assert.equal(h.isOpen(), false);
  assert.equal(h.expanded(), "false");
});

test("a click outside the bar dismisses the panel", () => {
  const h = createNavHarness();
  h.fireReady();
  h.toggle.click();

  h.outside.click();
  assert.equal(h.isOpen(), false);

  // …and a click inside it does not: the toggle's own click bubbles to the
  // same document listener, which must not undo the open it just did.
  h.toggle.click();
  assert.equal(h.isOpen(), true);
});

test("Escape closes the panel and hands focus back to the toggle", () => {
  const h = createNavHarness();
  h.fireReady();
  h.toggle.click();

  h.doc.dispatchEvent({ type: "keydown", key: "Escape" });
  assert.equal(h.isOpen(), false);
  assert.equal(h.doc.activeElement, h.toggle);
  assert.equal(h.toggle.focusCount, 1);
});

test("Escape with the panel shut is a no-op — index.html's modal owns that key too", () => {
  const h = createNavHarness();
  h.fireReady();

  h.doc.dispatchEvent({ type: "keydown", key: "Escape" });
  assert.equal(h.isOpen(), false);
  assert.equal(h.toggle.focusCount, 0, "focus must not be stolen from the modal");
});

test("growing past the phone breakpoint closes a panel the tabs no longer need", () => {
  const h = createNavHarness();
  h.fireReady();
  h.toggle.click();
  assert.equal(h.isOpen(), true);

  assert.equal(h.mediaQueries.length, 1);
  assert.equal(h.mediaQueries[0].media, "(max-width: 559.98px)",
    "must match the @media block in styles.css");
  h.mediaQueries[0]._change(false);   // rotated to landscape / resized wider
  assert.equal(h.isOpen(), false);
  assert.equal(h.expanded(), "false");

  // Back to phone width: still closed, and the button still works.
  h.mediaQueries[0]._change(true);
  assert.equal(h.isOpen(), false);
  h.toggle.click();
  assert.equal(h.isOpen(), true);
});

test("no matchMedia (old Safari) still gets a working toggle", () => {
  const h = createNavHarness({ withMatchMedia: false });
  h.fireReady();

  h.toggle.click();
  assert.equal(h.isOpen(), true);
  h.toggle.click();
  assert.equal(h.isOpen(), false);
});

/* ================================================================== *
 * contact.js (standalone — it never touches window.BCB)
 * ================================================================== */

test("contact.js reads the address out of the form's action", () => {
  const h = createContactHarness();
  assert.equal(h.direct.href, "mailto:boscafebikers@gmail.com");
  assert.equal(h.direct.textContent, "boscafebikers@gmail.com");
  assert.equal(h.directLine.hidden, false, "revealed only once it has an address");
});

test("submitting composes a prefilled mailto: with a signature", () => {
  const h = createContactHarness();
  h.fields.name.value = "Ada";
  h.fields.email.value = "ada@example.com";
  h.fields.subject.value = "Ride, please";
  h.fields.message.value = "Can I join Saturday?";

  let prevented = false;
  h.form.dispatchEvent({ type: "submit", preventDefault() { prevented = true; } });

  assert.equal(prevented, true, "the native mailto submission mangles the body");
  const url = h.context.location.href;
  assert.ok(url.startsWith("mailto:boscafebikers@gmail.com?"), url);
  const q = {};
  url.split("?")[1].split("&").forEach((pair) => {
    const i = pair.indexOf("=");
    q[pair.slice(0, i)] = decodeURIComponent(pair.slice(i + 1));
  });
  assert.equal(q.subject, "Ride, please");
  assert.equal(q.body, "Can I join Saturday?\n\n— Ada <ada@example.com>");
  assert.equal(h.status.hidden, false);
});

test("an empty subject falls back to the default line", () => {
  const h = createContactHarness();
  h.fields.name.value = "Ada";
  h.fields.message.value = "Hi";
  h.form.dispatchEvent({ type: "submit" });

  assert.ok(h.context.location.href.includes(
    "subject=" + encodeURIComponent("Hello from the Boston Café Bikers site")));
});

test("no address in the action leaves the native submission alone", () => {
  const h = createContactHarness({ action: "" });
  let prevented = false;
  h.form.dispatchEvent({ type: "submit", preventDefault() { prevented = true; } });

  assert.equal(prevented, false);
  assert.equal(h.context.location.href, "");
  assert.equal(h.directLine.hidden, true, "no empty focusable link with JS off");
});
