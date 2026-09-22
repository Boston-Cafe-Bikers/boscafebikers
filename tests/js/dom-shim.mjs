// A minimal DOM for verifying site/js/*.js under `node --test`.
//
// There is no browser in this repo and no npm (no package.json, no jsdom) —
// site/ is deliberately buildless — so the ride scripts are checked by
// evaluating them, unmodified, against the smallest document that satisfies
// what they actually call. Everything here is node built-ins only.
//
// The three ride scripts share `window.BCB`, so they must run in ONE vm
// context whose `window` IS the context global; ride-card.js -> calendar.js ->
// app.js, the same order site/index.html loads them in.
//
// The shim is deliberately partial. It implements the selectors the scripts
// use (`#id`, `.class`, `tag`, `[attr]`, `tag[attr*="v"]`) rather than a real
// selector engine, and it leaves `href`/`src`/`alt`/... as plain properties
// (which is how the scripts set them) instead of reflecting them into
// attributes. Query by class or id, read the property.

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = path.resolve(HERE, "..", "..");
export const SITE_JS = path.join(REPO_ROOT, "site", "js");

// The CDN tag site/index.html carries in <head>; app.js looks it up with
// `script[src*="fullcalendar"]` to re-render if the script lands late.
const FC_SRC = "https://cdn.jsdelivr.net/npm/fullcalendar@6.1.15/index.global.min.js";

/* ------------------------------------------------------------------ *
 * Selectors
 * ------------------------------------------------------------------ */

function parseAttrTest(body) {
  const eq = body.indexOf("=");
  if (eq === -1) { return { name: body.trim(), op: null, value: null }; }
  let name = body.slice(0, eq);
  let op = "=";
  if (/[*^$~|]$/.test(name)) {
    op = name.slice(-1) + "=";
    name = name.slice(0, -1);
  }
  let value = body.slice(eq + 1).trim();
  if (/^".*"$/.test(value) || /^'.*'$/.test(value)) { value = value.slice(1, -1); }
  return { name: name.trim(), op, value };
}

function parseCompound(sel) {
  const m = /^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)((?:\[[^\]]*\])*)$/.exec(sel);
  if (!m) {
    throw new Error("dom-shim: unsupported selector " + JSON.stringify(sel));
  }
  const tag = m[1] ? m[1].toUpperCase() : null;
  const classes = [];
  let id = null;
  for (const bit of (m[2] || "").match(/[.#][\w-]+/g) || []) {
    if (bit[0] === ".") { classes.push(bit.slice(1)); } else { id = bit.slice(1); }
  }
  const attrs = ((m[3] || "").match(/\[[^\]]*\]/g) || [])
    .map((raw) => parseAttrTest(raw.slice(1, -1)));
  return { tag, id, classes, attrs };
}

// Compound selectors joined by the descendant combinator (a space) — enough
// for ".ride-actions .btn" and 'script[src*="fullcalendar"]'. No child (>),
// sibling or comma combinators; those throw rather than quietly mis-match.
function parseSelector(selector) {
  const sel = String(selector).trim();
  if (/[>~+,]/.test(sel)) {
    throw new Error("dom-shim: unsupported selector " + JSON.stringify(selector));
  }
  const compounds = sel.split(/\s+/).filter(Boolean).map(parseCompound);
  if (!compounds.length) {
    throw new Error("dom-shim: empty selector");
  }
  return compounds;
}

function matchesCompound(node, parsed) {
  if (!node || node.nodeType !== 1) { return false; }
  if (parsed.tag && node.tagName !== parsed.tag) { return false; }
  if (parsed.id && node.id !== parsed.id) { return false; }
  for (const cls of parsed.classes) {
    if (!node.classList.contains(cls)) { return false; }
  }
  for (const test of parsed.attrs) {
    const actual = node.getAttribute(test.name);
    if (actual === null || actual === undefined) { return false; }
    if (test.op === null) { continue; }
    const value = String(actual);
    if (test.op === "=" && value !== test.value) { return false; }
    if (test.op === "*=" && !value.includes(test.value)) { return false; }
    if (test.op === "^=" && !value.startsWith(test.value)) { return false; }
    if (test.op === "$=" && !value.endsWith(test.value)) { return false; }
  }
  return true;
}

function matchesParsed(node, compounds) {
  if (!node || node.nodeType !== 1) { return false; }
  let i = compounds.length - 1;
  if (!matchesCompound(node, compounds[i])) { return false; }
  i -= 1;
  let ancestor = node.parentNode;
  while (i >= 0 && ancestor && ancestor.nodeType === 1) {
    if (matchesCompound(ancestor, compounds[i])) { i -= 1; }
    ancestor = ancestor.parentNode;
  }
  return i < 0;
}

/* ------------------------------------------------------------------ *
 * Nodes
 * ------------------------------------------------------------------ */

class ShimNode {
  constructor(tagName, ownerDocument) {
    this.nodeType = 1;
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument || null;
    this.childNodes = [];
    this.parentNode = null;
    this.className = "";
    this.id = "";
    this.hidden = false;
    this.style = {};
    this.dataset = {};
    this._attrs = new Map();
    this._text = "";
    this._listeners = new Map();
    // Test-visible bookkeeping: downloadIcs() calls a.click() on a detached
    // anchor, and there is nothing else to observe about it.
    this.clickCount = 0;
    this.focusCount = 0;

    const self = this;
    this.classList = {
      add(...names) {
        const have = self._classes();
        names.forEach((n) => { if (n && !have.includes(n)) { have.push(n); } });
        self.className = have.join(" ");
      },
      remove(...names) {
        self.className = self._classes().filter((c) => !names.includes(c)).join(" ");
      },
      contains(name) { return self._classes().includes(name); },
      toString() { return self.className; }
    };
  }

  _classes() {
    return String(this.className || "").split(/\s+/).filter(Boolean);
  }

  get children() {
    return this.childNodes.filter((n) => n.nodeType === 1);
  }

  get textContent() {
    return this._text + this.childNodes.map((n) => n.textContent).join("");
  }

  set textContent(value) {
    this.childNodes.forEach((n) => { n.parentNode = null; });
    this.childNodes = [];
    this._text = value === null || value === undefined ? "" : String(value);
  }

  appendChild(node) {
    if (node.parentNode) { node.parentNode.removeChild(node); }
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  append(...nodes) {
    nodes.forEach((n) => {
      if (typeof n === "string") {
        const text = new ShimNode("span", this.ownerDocument);
        text.textContent = n;
        this.appendChild(text);
      } else {
        this.appendChild(n);
      }
    });
  }

  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i !== -1) {
      this.childNodes.splice(i, 1);
      node.parentNode = null;
    }
    return node;
  }

  setAttribute(name, value) {
    const v = String(value);
    if (name === "class") { this.className = v; return; }
    if (name === "id") { this.id = v; return; }
    if (name === "hidden") { this.hidden = true; }
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[key] = v;
    }
    this._attrs.set(name, v);
  }

  getAttribute(name) {
    if (name === "class") { return this.className || null; }
    if (name === "id") { return this.id || null; }
    return this._attrs.has(name) ? this._attrs.get(name) : null;
  }

  hasAttribute(name) { return this.getAttribute(name) !== null; }

  removeAttribute(name) {
    if (name === "class") { this.className = ""; return; }
    if (name === "id") { this.id = ""; return; }
    this._attrs.delete(name);
  }

  matches(selector) { return matchesParsed(this, parseSelector(selector)); }

  closest(selector) {
    const parsed = parseSelector(selector);
    let node = this;
    while (node && node.nodeType === 1) {
      if (matchesParsed(node, parsed)) { return node; }
      node = node.parentNode;
    }
    return null;
  }

  _descendants(out = []) {
    this.childNodes.forEach((child) => {
      if (child.nodeType !== 1) { return; }
      out.push(child);
      child._descendants(out);
    });
    return out;
  }

  querySelectorAll(selector) {
    const parsed = parseSelector(selector);
    return this._descendants().filter((n) => matchesParsed(n, parsed));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  // A stand-in for HTMLFormElement.elements: named form controls, which is
  // all contact.js reads (`form.elements[name].value`).
  get elements() {
    const out = {};
    this._descendants().forEach((node) => {
      if (!["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(node.tagName)) { return; }
      const name = node.getAttribute("name");
      if (name) { out[name] = node; }
    });
    return out;
  }

  addEventListener(type, handler) {
    if (!this._listeners.has(type)) { this._listeners.set(type, []); }
    this._listeners.get(type).push(handler);
  }

  removeEventListener(type, handler) {
    const list = this._listeners.get(type);
    if (!list) { return; }
    const i = list.indexOf(handler);
    if (i !== -1) { list.splice(i, 1); }
  }

  // Bubbles up the tree (and into the document, which is the root's parent),
  // because app.js listens for clicks on #ride-modal and inspects e.target.
  dispatchEvent(event) {
    const evt = event || {};
    if (!evt.target) { evt.target = this; }
    if (typeof evt.preventDefault !== "function") {
      evt.defaultPrevented = false;
      evt.preventDefault = () => { evt.defaultPrevented = true; };
    }
    let node = this;
    while (node) {
      const list = node._listeners.get(evt.type);
      if (list) { list.slice().forEach((fn) => fn.call(node, evt)); }
      node = node.parentNode;
    }
    return !evt.defaultPrevented;
  }

  click() {
    this.clickCount += 1;
    this.dispatchEvent({ type: "click", bubbles: true });
  }

  focus() {
    this.focusCount += 1;
    if (this.ownerDocument) { this.ownerDocument.activeElement = this; }
  }

  blur() {
    if (this.ownerDocument && this.ownerDocument.activeElement === this) {
      this.ownerDocument.activeElement = this.ownerDocument.body;
    }
  }
}

class ShimDocument extends ShimNode {
  constructor() {
    super("#document", null);
    this.nodeType = 9;
    this.ownerDocument = this;
    this.readyState = "loading";
    this.documentElement = this.createElement("html");
    this.appendChild(this.documentElement);
    this.head = this.createElement("head");
    this.body = this.createElement("body");
    this.documentElement.appendChild(this.head);
    this.documentElement.appendChild(this.body);
    this.activeElement = this.body;
  }

  createElement(tagName) { return new ShimNode(tagName, this); }

  createTextNode(text) {
    const node = new ShimNode("#text", this);
    node.nodeType = 3;
    node.textContent = text;
    return node;
  }

  getElementById(id) {
    return this._descendants().find((n) => n.id === id) || null;
  }
}

/* ------------------------------------------------------------------ *
 * The index.html skeleton the ride scripts expect
 * ------------------------------------------------------------------ */

// Built by hand rather than parsed out of site/index.html: the point is to
// pin the contract (these ids, this nesting), and a regex over the real page
// would only re-encode whatever the page happens to say today. tests/
// test_site_html.py checks the page itself.
export function buildIndexDocument() {
  const doc = new ShimDocument();
  const mk = (tag, opts = {}) => {
    const node = doc.createElement(tag);
    if (opts.id) { node.id = opts.id; }
    if (opts.cls) { node.className = opts.cls; }
    if (opts.attrs) {
      Object.entries(opts.attrs).forEach(([k, v]) => node.setAttribute(k, v));
    }
    if (opts.hidden) { node.hidden = true; }
    (opts.kids || []).forEach((kid) => node.appendChild(kid));
    return node;
  };

  const fcScript = mk("script", { attrs: { src: FC_SRC, defer: "" } });
  doc.head.appendChild(fcScript);

  const nextRideCard = mk("div", { id: "next-ride-card" });
  doc.body.appendChild(mk("section", {
    id: "next-ride",
    attrs: { "data-bg": "images/jess-b-gracies-bikes.jpeg" },
    kids: [mk("div", { cls: "wrap", kids: [nextRideCard] })]
  }));

  const schedule = mk("div", { id: "schedule" });
  const updatedLink = mk("a", {
    id: "updated-link",
    attrs: { href: "https://github.com/Boston-Cafe-Bikers/boscafebikers/actions/workflows/sync.yml" }
  });
  const updated = mk("p", { id: "updated", cls: "note", hidden: true, kids: [updatedLink] });
  const subscribeNote = mk("p", { id: "subscribe-note", cls: "note" });
  doc.body.appendChild(mk("section", {
    id: "rides",
    kids: [mk("div", {
      cls: "wrap",
      kids: [
        schedule,
        updated,
        mk("div", {
          cls: "subscribe ride-actions",
          kids: [mk("a", { cls: "btn", attrs: { href: "rides.ics" } })]
        }),
        subscribeNote
      ]
    })]
  }));

  doc.body.appendChild(mk("section", { id: "first-ride" }));
  doc.body.appendChild(mk("section", { id: "crew" }));

  const modalClose = mk("button", { cls: "modal-close", attrs: { type: "button" } });
  const modalContent = mk("div", { id: "ride-modal-content" });
  const modalDialog = mk("div", {
    cls: "modal-dialog",
    attrs: { role: "dialog", "aria-modal": "true", "aria-label": "Ride details" },
    kids: [modalClose, modalContent]
  });
  const backdrop = mk("div", { cls: "modal-backdrop" });
  doc.body.appendChild(mk("div", {
    id: "ride-modal", cls: "modal", hidden: true, kids: [backdrop, modalDialog]
  }));

  return {
    doc,
    fcScript,
    nextRideCard,
    schedule,
    updated,
    updatedLink,
    subscribeNote,
    modal: doc.getElementById("ride-modal"),
    modalDialog,
    modalContent,
    modalClose,
    backdrop
  };
}

/* ------------------------------------------------------------------ *
 * The harness
 * ------------------------------------------------------------------ */

const RIDE_SCRIPTS = ["ride-card.js", "calendar.js", "app.js"];

// A stand-in for FullCalendar.Calendar that records the options it was built
// with, plus whether the holder was already in the document when render() ran
// (FC measures its container at mount — a detached holder collapses the grid).
export function makeFullCalendarStub() {
  const instances = [];
  class Calendar {
    constructor(holder, options) {
      this.holder = holder;
      this.options = options;
      this.rendered = false;
      this.destroyed = false;
      this.mountedBeforeRender = null;
      instances.push(this);
    }
    render() {
      this.rendered = true;
      // Walk to the root: "in the document" means the holder's chain reaches
      // the ShimDocument, not merely that it has a parent.
      let node = this.holder;
      while (node && node.parentNode) { node = node.parentNode; }
      this.mountedBeforeRender = node ? node.nodeType === 9 : false;
    }
    destroy() { this.destroyed = true; }
  }
  return { Calendar, instances };
}

// `jsonFor` maps a fetched url -> a response description. Anything not listed
// 404s, which is exactly how a fresh checkout behaves for events-past.json.
export function makeFetch(routes) {
  const calls = [];
  const fetchImpl = (url, options) => {
    calls.push({ url, options });
    const route = routes[url];
    if (!route) {
      return Promise.resolve({ ok: false, status: 404, json: () => Promise.reject(new Error("404")) });
    }
    if (typeof route === "function") { return route(url, options); }
    if (route.reject) { return Promise.reject(new Error(route.reject)); }
    const status = route.status === undefined ? 200 : route.status;
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      json: () => (route.badJson
        ? Promise.reject(new SyntaxError("bad json"))
        : Promise.resolve(route.body))
    });
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

export function createHarness({ routes = {}, FullCalendar = undefined } = {}) {
  const dom = buildIndexDocument();
  const windowListeners = new Map();
  const blobs = [];
  const objectUrls = [];

  class Blob {
    constructor(parts, options) {
      this.parts = parts;
      this.type = (options && options.type) || "";
      this.text = parts.join("");
      blobs.push(this);
    }
  }

  const URLStub = {
    createObjectURL(blob) {
      const url = "blob:boscafebikers/" + objectUrls.length;
      objectUrls.push({ url, blob, revoked: false });
      return url;
    },
    revokeObjectURL(url) {
      const entry = objectUrls.find((e) => e.url === url);
      if (entry) { entry.revoked = true; }
    }
  };

  const fetchImpl = makeFetch(routes);

  const sandbox = {
    document: dom.doc,
    fetch: fetchImpl,
    Blob,
    URL: URLStub,
    console,
    setTimeout,
    clearTimeout,
    location: { href: "" },
    navigator: { userAgent: "node-dom-shim" },
    addEventListener(type, handler) {
      if (!windowListeners.has(type)) { windowListeners.set(type, []); }
      windowListeners.get(type).push(handler);
    },
    removeEventListener(type, handler) {
      const list = windowListeners.get(type) || [];
      const i = list.indexOf(handler);
      if (i !== -1) { list.splice(i, 1); }
    }
  };
  if (FullCalendar !== undefined) { sandbox.FullCalendar = FullCalendar; }

  const context = vm.createContext(sandbox);
  // `window` must BE the context global, or `window.BCB = window.BCB || {}` in
  // one file wouldn't be the `BCB` the next file sees.
  vm.runInContext("globalThis.window = globalThis;", context, { filename: "dom-shim:window" });

  RIDE_SCRIPTS.forEach((name) => {
    const file = path.join(SITE_JS, name);
    vm.runInContext(fs.readFileSync(file, "utf8"), context, { filename: file });
  });

  return {
    ...dom,
    context,
    fetchImpl,
    blobs,
    objectUrls,
    get BCB() { return context.BCB; },
    // window "load" is what app.js waits on before painting [data-bg] photos.
    fireWindowEvent(type, event) {
      (windowListeners.get(type) || []).slice().forEach((fn) => fn({ type, ...event }));
    },
    windowListeners,
    setFullCalendar(stub) { context.FullCalendar = stub; },
    // The fetch chain is two promises deep; a few macrotask turns settle it.
    async flush(turns = 6) {
      for (let i = 0; i < turns; i++) {
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
    }
  };
}

// nav.js is the other standalone script (no window.BCB either) and runs from
// <head> on all seven pages, so it gets the shared chrome and nothing else:
// the .nav, its .nav-toggle button and the #site-tabs list. `readyState`
// starts at "loading" exactly as it is when a non-defer'd head script runs,
// so the harness has to call fireReady() to get past DOMContentLoaded — which
// is the point: `has-js` must already be set before that.
export function createNavHarness({ matches = true, withMatchMedia = true } = {}) {
  const doc = new ShimDocument();

  const nav = doc.createElement("nav");
  nav.className = "nav";
  nav.setAttribute("aria-label", "Site");
  const wrap = doc.createElement("div");
  wrap.className = "wrap";

  const toggle = doc.createElement("button");
  toggle.className = "nav-toggle";
  toggle.setAttribute("type", "button");
  toggle.setAttribute("aria-label", "Menu");
  toggle.setAttribute("aria-expanded", "false");
  toggle.setAttribute("aria-controls", "site-tabs");
  const bars = doc.createElement("span");
  bars.className = "bars";
  bars.setAttribute("aria-hidden", "true");
  toggle.appendChild(bars);

  const tabs = doc.createElement("ul");
  tabs.className = "tabs";
  tabs.id = "site-tabs";
  const links = ["index.html", "cafes.html", "gallery.html", "contact.html", "donate.html"]
    .map((href) => {
      const li = doc.createElement("li");
      const a = doc.createElement("a");
      a.setAttribute("href", href);
      li.appendChild(a);
      tabs.appendChild(li);
      return a;
    });

  wrap.appendChild(toggle);
  wrap.appendChild(tabs);
  nav.appendChild(wrap);
  doc.body.appendChild(nav);

  // Somewhere to click that is not the nav.
  const outside = doc.createElement("main");
  doc.body.appendChild(outside);

  const mediaQueries = [];
  const sandbox = {
    document: doc,
    console,
    setTimeout,
    clearTimeout,
    location: { href: "" },
    addEventListener() {},
    removeEventListener() {}
  };
  if (withMatchMedia) {
    sandbox.matchMedia = (query) => {
      const listeners = [];
      const mq = {
        media: query,
        matches,
        addEventListener(type, fn) { if (type === "change") { listeners.push(fn); } },
        removeEventListener(type, fn) {
          const i = listeners.indexOf(fn);
          if (type === "change" && i !== -1) { listeners.splice(i, 1); }
        },
        // Fire the change every registered listener is waiting for.
        _change(nowMatches) {
          mq.matches = nowMatches;
          listeners.slice().forEach((fn) => fn({ matches: nowMatches, media: query }));
        }
      };
      mediaQueries.push(mq);
      return mq;
    };
  }

  const context = vm.createContext(sandbox);
  vm.runInContext("globalThis.window = globalThis;", context, { filename: "dom-shim:window" });
  const file = path.join(SITE_JS, "nav.js");
  vm.runInContext(fs.readFileSync(file, "utf8"), context, { filename: file });

  return {
    doc,
    nav,
    toggle,
    tabs,
    links,
    outside,
    context,
    mediaQueries,
    isOpen: () => nav.classList.contains("is-open"),
    expanded: () => toggle.getAttribute("aria-expanded"),
    // What the browser does once the body has been parsed.
    fireReady() {
      doc.readyState = "interactive";
      doc.dispatchEvent({ type: "DOMContentLoaded" });
    }
  };
}

// contact.js is standalone (it never touches window.BCB), so it gets its own
// tiny page rather than the rides skeleton.
export function createContactHarness({ action = "mailto:boscafebikers@gmail.com" } = {}) {
  const doc = new ShimDocument();
  const form = doc.createElement("form");
  form.id = "contact-form";
  form.setAttribute("action", action);
  ["name", "email", "subject"].forEach((name) => {
    const input = doc.createElement("input");
    input.setAttribute("name", name);
    input.value = "";
    form.appendChild(input);
  });
  const message = doc.createElement("textarea");
  message.setAttribute("name", "message");
  message.value = "";
  form.appendChild(message);

  const status = doc.createElement("p");
  status.id = "contact-status";
  status.hidden = true;
  form.appendChild(status);
  doc.body.appendChild(form);

  const directLine = doc.createElement("p");
  directLine.id = "contact-direct-line";
  directLine.hidden = true;
  const direct = doc.createElement("a");
  direct.id = "contact-direct";
  direct.href = "#";
  directLine.appendChild(direct);
  doc.body.appendChild(directLine);

  const sandbox = {
    document: doc,
    console,
    setTimeout,
    clearTimeout,
    location: { href: "" },
    addEventListener() {},
    removeEventListener() {}
  };
  const context = vm.createContext(sandbox);
  vm.runInContext("globalThis.window = globalThis;", context, { filename: "dom-shim:window" });
  const file = path.join(SITE_JS, "contact.js");
  vm.runInContext(fs.readFileSync(file, "utf8"), context, { filename: file });

  return { doc, form, status, direct, directLine, context, fields: form.elements };
}

export { ShimNode, ShimDocument, parseSelector };
