// "Where we've been" — the café list on cafes.html, rendered from the same
// events-past.json the calendar dims its history with. Nothing is derived on
// this page: the café name, its address, the ride's year, the date and the
// RSVP link are all fields the sync already wrote (see scripts/ride_fields.py).
//
// It also draws the map above that list: one pin per café, from the sync's
// geocode cache (cafe-points.json), on MapLibre GL JS + OpenFreeMap's vector
// tiles. That is the whole reason this page loads a library at all — see
// buildMap() below.
//
// Standalone on purpose: this page has no calendar, no modal and no ride
// cards, so it does NOT join the window.BCB namespace the three rides scripts
// share — it is one small file instead of three, and nothing on the rides page
// depends on it.
(function () {
  "use strict";

  // Public profile — the fallback target whenever a ride has no event link,
  // and the CTA on the "couldn't load it" note.
  const PARTIFUL = "https://partiful.com/u/Hs47uq5mucZyXLBJZCda";

  // Month names by number so the "since September 2025" line can be written
  // straight off the ISO start string. Same no-`Date` rule as the rest of the
  // site: `start` carries the Eastern offset, and re-parsing it in the
  // visitor's timezone is how a ride lands on the wrong day.
  const MONTHS = ["January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"];

  // A Location field holding nothing but a link (fetch_rides.py stores it as
  // location_url). Name the link instead of printing the URL — same rule and
  // wording as ride-card.js meetingPointText().
  const MAPS_URL_RE =
    /^https?:\/\/(?:maps\.app\.goo\.gl\/|maps\.google\.[a-z.]+\/|(?:www\.)?goo\.gl\/maps|(?:www\.)?google\.[a-z.]+\/maps)/i;

  // OpenFreeMap's "positron" vector style: keyless, unlimited, and fetched by
  // the visitor's own browser only when they open this page. The data is
  // OpenStreetMap's, served through OpenMapTiles' schema; attribution comes
  // back with the tiles and MapLibre prints it on the map.
  //
  // Why a vector style and not the raster tiles the route-map SVGs embed: the
  // organizer asked for the red/orange highways, the hill markers and some of
  // the neighbourhood labels to go, and a raster tile is a finished picture —
  // nothing in it can be removed. A vector style is a list of layers, so the
  // ones we don't want are simply dropped before the map is built (DROP_LAYERS).
  const STYLE_URL = "https://tiles.openfreemap.org/styles/positron";
  // MapLibre v6 ships as a small ES-module graph. Keep it same-origin (the
  // verified npm tarball is vendored under site/vendor/) and import it only
  // after the café data proves there is a map to draw. A module/WebGL failure
  // therefore leaves the list — the actual page content — intact.
  const MAPLIBRE_MODULE_URL = "../vendor/maplibre-gl-6.10.0/maplibre-gl.mjs";
  // Layers deleted from that style, by id. Positron is already grey-on-grey
  // with no motorway colour, no hillshade and no POI symbols; these are what
  // was left of the clutter:
  //   label_other  — every place label that isn't a city/town/village/state/
  //                  country, i.e. the italic uppercase neighbourhood and
  //                  suburb names ("EAGLE HILL", "JEFFRIES POINT"). Boston,
  //                  Cambridge and Somerville still get their names.
  //   *shield*     — the little route badges (the US interstate one is red,
  //                  white and blue: the last loud colour on the map).
  //   airport      — an aeroplane icon plus a three-line "Boston Harbor
  //                  Seaplane Base (Cape Air Seaplanes)": the wordiest thing
  //                  left, and a symbol at the same size as our café pins.
  //                  Logan's runways still draw, so the airport is still there.
  const DROP_LAYERS = [
    "label_other",
    "highway-shield-us-interstate",
    "highway-shield-non-us",
    "road_shield_us",
    "airport"
  ];
  // Deep enough to read a street, shallow enough that one café doesn't open
  // the map on somebody's front door.
  const FIT_MAX_ZOOM = 15;
  const FIT_PADDING = 28;
  // How far from the middle of the pack a café can sit and still be worth
  // framing on open. ~0.6° is about 65km — the whole of greater Boston, and
  // not the one ride that ended at somebody's place in New Haven. See fitPoints().
  const FIT_OUTLIER_DEGREES = .6;
  // How far above the pin its popup floats. The pin is an 18px circle centred
  // on the café (see .cafe-pin), so half of it plus a hair clears the bubble.
  const PIN_OFFSET = 13;

  const list = document.getElementById("cafe-list");
  const countLine = document.getElementById("cafe-count");
  const mapBox = document.getElementById("cafe-map");
  if (!list) { return; }

  // Everything is built with createElement/textContent, never innerHTML, so
  // café names and ride titles out of the feed can't inject markup.
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
  }

  // The café name and its address are split in the sync, not here:
  // ride_fields.derive writes `place_name` ("Localito Cafe") and `address`
  // ("30 Riverside Ave, Medford, MA") onto every archived ride. The fallbacks
  // are for a deploy that lands ahead of the data — an event from before the
  // fields existed still shows something.
  function cafeName(ev) {
    return ev.place_name || ev.location || "";
  }

  function meetingPointText(url) {
    return MAPS_URL_RE.test(String(url || "")) ? "Meeting point on Google Maps" : "Meeting point";
  }

  // date_display has no year and the archive already spans two of them, so the
  // ride carries `year` alongside it: "Sunday, August 16" + "2026".
  function rideYear(ev) {
    return ev.year || String(ev.start || "").slice(0, 4);
  }

  function rideDate(ev) {
    const year = rideYear(ev);
    const day = ev.date_display || "";
    if (day && year) { return day + ", " + year; }
    return day || year;
  }

  function cafeItem(ev) {
    const item = el("li", "cafe");
    const name = cafeName(ev);
    const title = ev.title || "Café ride";
    // With no address at all there is no café name to head the card with, so
    // the ride's own name does the job.
    const heading = name || title;
    item.appendChild(el("h3", "cafe-name", heading));

    if (ev.address) {
      item.appendChild(el("p", "cafe-address", ev.address));
    } else if (ev.location_url) {
      // The organizer pasted the meeting point's map link in place of an
      // address: show it as a named link, not a raw URL.
      const line = el("p", "cafe-address");
      const link = el("a", null, meetingPointText(ev.location_url));
      link.href = ev.location_url;
      link.target = "_blank";
      link.rel = "noopener";
      line.appendChild(link);
      item.appendChild(line);
    } else if (!name) {
      item.appendChild(el("p", "cafe-address is-unknown", "Location wasn't published"));
    }

    item.appendChild(el("p", "cafe-when", rideDate(ev)));
    // Only when it isn't already the heading — no card says the same thing twice.
    if (title !== heading) { item.appendChild(el("p", "cafe-ride", title)); }

    const go = el("p", "cafe-go");
    const link = el("a", null, "See the ride on Partiful");
    link.href = ev.rsvp_url || PARTIFUL;
    link.rel = "noopener";
    go.appendChild(link);
    item.appendChild(go);
    return item;
  }

  // "34 rides to 19 named cafés and stops, since September 2025." — every
  // number is counted from the archive, nothing is hardcoded.
  function summary(events) {
    const names = new Set();
    events.forEach((ev) => {
      const name = cafeName(ev);
      if (name) { names.add(name.toLowerCase()); }
    });
    const rides = events.length + (events.length === 1 ? " ride" : " rides");
    const cafes = names.size === 1
      ? "1 named café or stop"
      : names.size + " named cafés and stops";
    // events is newest-first, so the oldest ride is last.
    const oldest = events[events.length - 1] || {};
    const iso = String(oldest.start || "");
    const month = MONTHS[Number(iso.slice(5, 7)) - 1];
    const since = month ? ", since " + month + " " + rideYear(oldest) : "";
    return rides + " to " + cafes + since + ".";
  }

  function note(message) {
    list.textContent = "";
    list.appendChild(el("p", "note", message));
    const link = el("a", "btn", "See all rides on Partiful");
    link.href = PARTIFUL;
    link.rel = "noopener";
    list.appendChild(link);
  }

  function render(events) {
    list.textContent = "";
    let year = null;
    let group = null;
    events.forEach((ev) => {
      const evYear = rideYear(ev) || "Earlier";
      if (evYear !== year) {
        year = evYear;
        list.appendChild(el("h2", "year", year));
        group = el("ul", "cafe-list");
        list.appendChild(group);
      }
      group.appendChild(cafeItem(ev));
    });
    if (countLine) {
      countLine.textContent = summary(events);
      countLine.hidden = false;
    }
  }

  // ------------------------------------------------------------------
  // The map

  // cafe-points.json stores each café as [lat, lon]; MapLibre speaks
  // [lng, lat]. Every point that crosses into the library goes through here,
  // so the two orders can't quietly get mixed up.
  function lngLat(point) {
    return [point[1], point[0]];
  }

  // One pin per *coordinate*, not per location string. The organizer typed
  // "Aeronaut Brewery, 14 Tyler St…" one week and "Aeronaut, 14 Tyler St…" the
  // next; keyed on the string those would be two markers stacked exactly on
  // top of each other, and the second would be unclickable.
  function pinGroups(events, points) {
    const groups = [];
    const byKey = new Map();
    events.forEach((ev) => {
      const location = ev.location;
      const point = location && points[String(location).trim()];
      if (!point) { return; }
      const key = point[0] + "," + point[1];
      let group = byKey.get(key);
      if (!group) {
        // events arrive newest-first, so the first ride to claim a pin is the
        // most recent one — its spelling of the name is the one to show.
        group = { point: point, name: cafeName(ev) || ev.title || "Café stop",
          address: ev.address, rides: [] };
        byKey.set(key, group);
        groups.push(group);
      }
      group.rides.push(ev);
    });
    return groups;
  }

  // Both halves of the question the backlog asked: the rides that got here,
  // and a way to go there yourself.
  function pinPopup(group) {
    const box = document.createElement("div");
    box.appendChild(el("h3", "pin-name", group.name));
    if (group.address) {
      box.appendChild(el("p", "pin-address", group.address));
    }

    const rides = el("ul", "pin-rides");
    group.rides.forEach((ev) => {
      const item = document.createElement("li");
      const link = el("a", null, rideDate(ev));
      link.href = ev.rsvp_url || PARTIFUL;
      link.rel = "noopener";
      // The visible text is the date so several rides at one café stay
      // readable; the accessible name still says where the link goes.
      link.setAttribute("aria-label",
        "See the " + (ev.title || "café ride") + " ride on Partiful");
      item.appendChild(link);
      rides.appendChild(item);
    });
    box.appendChild(rides);

    const directions = el("p", "pin-directions");
    const go = el("a", null, "Directions");
    // Coordinates, not the name: the address strings are exactly what
    // Nominatim struggled with, and these points are already resolved.
    go.href = "https://www.google.com/maps/search/?api=1&query="
      + encodeURIComponent(group.point[0] + "," + group.point[1]);
    go.target = "_blank";
    go.rel = "noopener";
    directions.appendChild(go);
    box.appendChild(directions);
    return box;
  }

  // Which pins the opening view is framed around. Every café is on the map;
  // this only decides where it starts. One ride went to New Haven, 200km away,
  // and fitting all of them meant opening on the whole of New England with the
  // 17 Boston cafés stacked in a single unreadable dot. So the fit uses the
  // pins within FIT_OUTLIER_DEGREES of the median point — the middle of the
  // pack, which no single far-flung ride can drag — and the outlier is one
  // zoom-out away.
  function fitPoints(groups) {
    const all = groups.map((group) => group.point);
    if (all.length < 3) { return all; }
    const median = (values) => {
      const sorted = values.slice().sort((a, b) => a - b);
      return sorted[Math.floor(sorted.length / 2)];
    };
    const midLat = median(all.map((p) => p[0]));
    const midLon = median(all.map((p) => p[1]));
    const near = all.filter((p) =>
      Math.abs(p[0] - midLat) <= FIT_OUTLIER_DEGREES &&
      Math.abs(p[1] - midLon) <= FIT_OUTLIER_DEGREES);
    return near.length >= 2 ? near : all;
  }

  // The style JSON, minus the layers we don't want. Fetching it here rather
  // than handing MapLibre the URL is what makes the deletion possible at all:
  // the library takes a style *object* just as happily, and the layers are
  // gone before the first frame is drawn (removing them afterwards would show
  // them for a moment first).
  //
  // A style we can't fetch means the tile host is unreachable, so there is no
  // map to build: null, and the box stays hidden. Same bargain as a CDN
  // outage — the list below is the page.
  async function loadStyle() {
    try {
      const res = await fetch(STYLE_URL);
      if (!res.ok) { throw new Error("HTTP " + res.status); }
      const style = await res.json();
      if (!style || !Array.isArray(style.layers)) { throw new Error("no layers"); }
      style.layers = style.layers.filter((layer) =>
        DROP_LAYERS.indexOf(layer.id) === -1);
      return style;
    } catch (err) {
      return null;
    }
  }

  async function buildMap(groups) {
    if (!mapBox || !groups.length) { return; }
    const gl = await import(MAPLIBRE_MODULE_URL);
    const style = await loadStyle();
    if (!style) { return; }

    const bounds = new gl.LngLatBounds();
    fitPoints(groups).forEach((point) => bounds.extend(lngLat(point)));

    // Unhide BEFORE MapLibre measures the container. A display:none box is
    // 0×0, and the map would mount at zero size — the same trap the rides
    // page hits when FullCalendar renders into a detached node.
    mapBox.hidden = false;

    let map;
    try {
      map = new gl.Map({
        container: mapBox,
        style: style,
        // Framed on open, so the map never flashes the whole world first.
        bounds: bounds,
        fitBoundsOptions: { padding: FIT_PADDING, maxZoom: FIT_MAX_ZOOM },
        // A map that eats the wheel is a map you can't scroll past. Click it
        // first and wheel zoom turns on, which is the usual bargain for an
        // embedded map; the +/− buttons and pinch work from the start.
        scrollZoom: false,
        // A café map that can end up facing south-west is just confusing.
        dragRotate: false,
        pitchWithRotate: false,
        attributionControl: { compact: false }
      });
    } catch (err) {
      // No WebGL (an old phone, a locked-down browser) and the constructor
      // throws. Put the box back the way it was rather than leave an empty one.
      mapBox.hidden = true;
      return;
    }
    map.touchZoomRotate.disableRotation();
    map.on("click", function () { map.scrollZoom.enable(); });
    map.addControl(new gl.NavigationControl({ showCompass: false }), "top-left");

    groups.forEach((group) => {
      // A real <button>, so the pin is reachable by keyboard and announces
      // itself; MapLibre positions whatever element it is handed.
      const pin = el("button", "cafe-pin");
      pin.type = "button";
      pin.setAttribute("aria-label", group.name);
      // setDOMContent takes a real element, so café names out of the feed go
      // in as text nodes — never as a concatenated HTML string.
      const popup = new gl.Popup({ offset: PIN_OFFSET, maxWidth: "260px" })
        .setDOMContent(pinPopup(group));
      new gl.Marker({ element: pin })
        .setLngLat(lngLat(group.point))
        .setPopup(popup)
        .addTo(map);
    });
  }

  // A missing or malformed cache is not an error: no coordinates, no map.
  async function loadPoints() {
    try {
      const res = await fetch("cafe-points.json", { cache: "no-cache" });
      if (!res.ok) { throw new Error("HTTP " + res.status); }
      const data = await res.json();
      const points = data && data.points;
      return (points && typeof points === "object") ? points : {};
    } catch (err) {
      return {};
    }
  }

  async function load() {
    let data;
    try {
      const res = await fetch("events-past.json", { cache: "no-cache" });
      if (!res.ok) { throw new Error("HTTP " + res.status); }
      data = await res.json();
    } catch (err) {
      note("Couldn't load the café list just now. Try again in a moment.");
      return;
    }
    const events = (data && data.events) || [];
    if (!events.length) {
      note("No café stops logged yet — the first rides are still ahead.");
      return;
    }
    // Newest first. The ISO strings sort lexically (they all carry the Eastern
    // offset), which is exactly the no-`Date` rule the rest of the site keeps.
    const sorted = events.slice().sort((a, b) =>
      String(b.start || "").localeCompare(String(a.start || "")));
    render(sorted);

    // The list is the page; the map is the decoration on top of it, so it is
    // built last and every failure along the way is silent.
    const points = await loadPoints();
    const groups = pinGroups(sorted, points);
    if (groups.length) {
      buildMap(groups).catch(function () {
        if (mapBox) { mapBox.hidden = true; }
      });
    }
  }

  load();
})();
