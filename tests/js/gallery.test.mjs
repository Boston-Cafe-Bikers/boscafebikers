// Offline verification for site/js/gallery.js and its manual-photo fallback.

import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import test from "node:test";
import assert from "node:assert/strict";

import { ShimDocument, SITE_JS, makeFetch } from "./dom-shim.mjs";

const POST = {
  id: "post_1",
  caption: "Coffee <strong>after</strong> the ride",
  kind: "Post",
  permalink: "https://www.instagram.com/p/post_1/",
  timestamp: "2026-09-20T14:00:00+0000",
  image: "instagram/post_1.jpg"
};

function createGalleryHarness(route) {
  const doc = new ShimDocument();
  const grid = doc.createElement("div");
  grid.className = "gallery-grid";
  const fallback = doc.createElement("figure");
  fallback.className = "gallery-item manual-item";
  fallback.textContent = "Manual fallback";
  grid.appendChild(fallback);
  doc.body.appendChild(grid);

  const fetchImpl = makeFetch({ "instagram-posts.json": route });
  const context = vm.createContext({
    document: doc,
    fetch: fetchImpl,
    console,
    setTimeout,
    clearTimeout
  });
  vm.runInContext("globalThis.window = globalThis;", context, { filename: "gallery:window" });
  const file = path.join(SITE_JS, "gallery.js");
  vm.runInContext(fs.readFileSync(file, "utf8"), context, { filename: file });

  return {
    doc,
    grid,
    fallback,
    fetchImpl,
    async flush(turns = 5) {
      for (let i = 0; i < turns; i++) {
        await new Promise((resolve) => setTimeout(resolve, 0));
      }
    }
  };
}

test("the Instagram cache replaces the manual gallery with linked local images", async () => {
  const h = createGalleryHarness({
    body: { account: "bostoncafebikers", count: 2, posts: [
      POST,
      {
        ...POST,
        id: "reel_2",
        caption: "A quick ride recap",
        kind: "Reel",
        permalink: "https://www.instagram.com/reel/reel_2/",
        image: "instagram/reel_2.jpg"
      }
    ] }
  });
  await h.flush();

  assert.equal(h.grid.getAttribute("data-source"), "instagram");
  assert.equal(h.grid.querySelectorAll(".instagram-item").length, 2);
  assert.equal(h.grid.querySelector(".manual-item"), null);

  const first = h.grid.querySelector(".instagram-item");
  const link = first.querySelector(".gallery-link");
  const image = first.querySelector("img");
  assert.equal(link.href, POST.permalink);
  assert.equal(link.target, "_blank");
  assert.equal(link.rel, "noopener");
  assert.equal(image.src, "instagram/post_1.jpg");
  assert.equal(image.loading, "lazy");
  assert.equal(first.querySelector(".instagram-kind").textContent, "Post");

  // Captions are text, never markup supplied by Instagram.
  assert.equal(first.querySelector(".instagram-caption").textContent, POST.caption);
  assert.equal(first.querySelector("strong"), null);
  assert.equal(h.fetchImpl.calls.length, 1);
  assert.equal(h.fetchImpl.calls[0].url, "instagram-posts.json");
  assert.equal(h.fetchImpl.calls[0].options.cache, "no-cache");
  assert.equal(h.fetchImpl.calls[0].options.credentials, "same-origin");
});

test("a missing or malformed cache leaves the hand-written photos alone", async () => {
  for (const route of [
    { status: 404, body: {} },
    { badJson: true },
    { body: { account: "somebody-else", posts: [POST] } },
    { body: { account: "bostoncafebikers", posts: [] } }
  ]) {
    const h = createGalleryHarness(route);
    await h.flush();
    assert.equal(h.grid.getAttribute("data-source"), null);
    assert.equal(h.grid.querySelectorAll(".manual-item").length, 1);
    assert.equal(h.grid.querySelector(".instagram-item"), null);
  }
});

test("unsafe remote image and non-Instagram links never reach the page", async () => {
  const h = createGalleryHarness({
    body: { account: "bostoncafebikers", posts: [
      { ...POST, image: "https://cdn.example/post.jpg" },
      { ...POST, id: "bad-link", permalink: "https://example.com/post" },
      { ...POST, id: "safe_3", image: "instagram/safe_3.jpg" }
    ] }
  });
  await h.flush();

  const items = h.grid.querySelectorAll(".instagram-item");
  assert.equal(items.length, 1);
  assert.equal(items[0].querySelector("img").src, "instagram/safe_3.jpg");
});

test("the browser displays at most the latest nine cached posts", async () => {
  const posts = Array.from({ length: 12 }, (_, index) => ({
    ...POST,
    id: `post_${index}`,
    image: `instagram/post_${index}.jpg`
  }));
  const h = createGalleryHarness({
    body: { account: "bostoncafebikers", count: posts.length, posts }
  });
  await h.flush();
  assert.equal(h.grid.querySelectorAll(".instagram-item").length, 9);
});
