/* Boston Café Bikers — the shared top nav's phone hamburger.
 *
 * Loaded by all seven pages as a plain <script src="js/nav.js"> in <head> —
 * NOT defer'd, unlike every other script here. Two reasons:
 *
 *   1. It adds `has-js` to <html> as its first statement, and the stylesheet
 *      only collapses the tabs into the hamburger under `html.has-js`. Running
 *      before the body is parsed means the class is set before first paint, so
 *      a phone never flashes the expanded tab row. If this file fails to load,
 *      the class never lands and the nav degrades to the plain wrapping row of
 *      tabs it has always been — nothing is hidden behind a dead button.
 *   2. It is ~1KB of local, dependency-free code; blocking on it costs nothing.
 *
 * The wiring itself still waits for DOMContentLoaded, because the nav markup
 * is parsed after <head>.
 *
 * Deliberately standalone: it never touches window.BCB, so it works the same
 * on donate/shopify/meta-business/gallery, which load no other script.
 */
(() => {
  "use strict";

  document.documentElement.classList.add("has-js");

  // Keep in sync with the `@media (max-width: 559.98px)` block in styles.css:
  // above this width the tabs are a row again and the button is display:none.
  const PHONE = "(max-width: 559.98px)";

  const init = () => {
    const nav = document.querySelector(".nav");
    const toggle = nav && nav.querySelector(".nav-toggle");
    const tabs = nav && nav.querySelector(".tabs");
    if (!nav || !toggle || !tabs) { return; }

    const isOpen = () => nav.classList.contains("is-open");
    const setOpen = (open) => {
      // classList.add/remove rather than toggle(name, force): the same two
      // calls, and no reliance on the two-argument form.
      if (open) { nav.classList.add("is-open"); } else { nav.classList.remove("is-open"); }
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    };
    const close = () => { if (isOpen()) { setOpen(false); } };

    toggle.addEventListener("click", (event) => {
      event.preventDefault();
      setOpen(!isOpen());
    });

    // A tab click navigates away; close first so the panel isn't still open if
    // the browser restores this page from the back/forward cache.
    tabs.addEventListener("click", (event) => {
      if (event.target && event.target.closest("a")) { close(); }
    });

    // Anywhere outside the bar dismisses it. The toggle's own click bubbles to
    // here too, but it is inside .nav, so it never re-closes what it opened.
    document.addEventListener("click", (event) => {
      if (!isOpen()) { return; }
      if (event.target && event.target.closest(".nav")) { return; }
      close();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !isOpen()) { return; }
      close();
      // Focus is inside the panel that just disappeared; put it back on the
      // control that owns it. (index.html's modal has its own Escape handler;
      // this one is a no-op whenever the panel is already closed.)
      toggle.focus();
    });

    // Rotating a phone to landscape can cross the breakpoint: the tabs become
    // a row again, so an `is-open` left behind would show the × over a bar
    // that is no longer collapsible.
    if (typeof window.matchMedia === "function") {
      const mq = window.matchMedia(PHONE);
      const onChange = (event) => { if (!event.matches) { close(); } };
      if (typeof mq.addEventListener === "function") {
        mq.addEventListener("change", onChange);
      } else if (typeof mq.addListener === "function") {
        mq.addListener(onChange);  // Safari < 14
      }
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
