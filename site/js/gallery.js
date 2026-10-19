(() => {
  "use strict";

  const grid = document.querySelector(".gallery-grid");
  if (!grid || typeof fetch !== "function") { return; }

  const instagramLink = (value) => {
    if (typeof value !== "string") { return null; }
    return /^https:\/\/(?:www\.)?instagram\.com\//i.test(value) ? value : null;
  };

  const localImage = (value) => {
    if (typeof value !== "string") { return null; }
    return /^instagram\/[A-Za-z0-9_-]+\.jpg$/.test(value) ? value : null;
  };

  const normalizedPost = (post) => {
    if (!post || typeof post !== "object") { return null; }
    const permalink = instagramLink(post.permalink);
    const image = localImage(post.image);
    if (!permalink || !image) { return null; }
    const kinds = ["Post", "Carousel", "Reel", "Video"];
    const kind = kinds.includes(post.kind) ? post.kind : "Post";
    const caption = typeof post.caption === "string" ? post.caption.trim() : "";
    return { permalink, image, kind, caption };
  };

  const tile = (post) => {
    const figure = document.createElement("figure");
    figure.className = "gallery-item instagram-item";

    const link = document.createElement("a");
    link.className = "gallery-link";
    link.href = post.permalink;
    link.target = "_blank";
    link.rel = "noopener";
    const summary = post.caption ? `: ${post.caption.slice(0, 120)}` : "";
    link.setAttribute("aria-label", `View Instagram ${post.kind.toLowerCase()}${summary}`);

    const image = document.createElement("img");
    image.src = post.image;
    image.alt = `${post.kind} from Boston Café Bikers on Instagram`;
    image.loading = "lazy";
    image.decoding = "async";
    link.appendChild(image);

    const caption = document.createElement("figcaption");
    const text = document.createElement("span");
    text.className = "instagram-caption";
    text.textContent = post.caption || `View this ${post.kind.toLowerCase()} on Instagram`;
    const kind = document.createElement("span");
    kind.className = "instagram-kind";
    kind.textContent = post.kind;
    caption.append(text, kind);
    link.appendChild(caption);
    figure.appendChild(link);
    return figure;
  };

  fetch("instagram-posts.json", { cache: "no-cache", credentials: "same-origin" })
    .then((response) => {
      if (!response.ok) { throw new Error(`Instagram cache: HTTP ${response.status}`); }
      return response.json();
    })
    .then((payload) => {
      if (!payload || payload.account !== "bostoncafebikers" || !Array.isArray(payload.posts)) {
        throw new Error("Instagram cache: invalid payload");
      }
      const posts = payload.posts.slice(0, 9).map(normalizedPost).filter(Boolean);
      if (!posts.length) { throw new Error("Instagram cache: no renderable posts"); }

      // Build everything first. Only replace the hand-written photos after a
      // complete usable cache has arrived, so every failure keeps the fallback.
      const tiles = posts.map(tile);
      grid.textContent = "";
      tiles.forEach((item) => grid.appendChild(item));
      grid.setAttribute("data-source", "instagram");
    })
    .catch(() => {
      // Deliberately quiet: the manual photos already in gallery.html are the
      // designed offline/API-failure state, not an error message for visitors.
    });
})();
