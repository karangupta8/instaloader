// Adds a small download button over every post/reel on the page (feed articles,
// profile/saved grid tiles) plus a floating one on single post/reel pages.

const SHORTCODE_RE = /\/(?:p|reel|reels)\/([A-Za-z0-9_-]+)/;
const MARK = "data-igdl-attached";
const STATES = { idle: "⬇", busy: "…", done: "✓", fail: "✕" };

function shortcodeFrom(href) {
  const match = (href || "").match(SHORTCODE_RE);
  return match ? match[1] : null;
}

function setState(btn, state, title) {
  btn.textContent = STATES[state];
  btn.dataset.state = state;
  btn.title = title;
}

function makeButton(getShortcode, extraClass = "") {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `igdl-btn ${extraClass}`.trim();
  setState(btn, "idle", "Download with instaloader");

  btn.addEventListener("click", async (event) => {
    // The button sits inside links; don't let Instagram navigate or react to the click.
    event.preventDefault();
    event.stopPropagation();
    const shortcode = getShortcode();
    if (!shortcode || btn.dataset.state === "busy") return;

    setState(btn, "busy", `Downloading ${shortcode}…`);
    try {
      const fromSavedPage = /\/saved(\/|$)/.test(location.pathname);
      const res = await chrome.runtime.sendMessage({ type: "download", shortcode, fromSavedPage });
      if (res?.ok) setState(btn, "done", `Saved to ${res.target}`);
      else setState(btn, "fail", res?.error || "Download failed");
    } catch (err) {
      setState(btn, "fail", err.message);
    }
  }, true);
  return btn;
}

function ensurePositioned(el) {
  if (getComputedStyle(el).position === "static") el.style.position = "relative";
}

function attachToArticles() {
  for (const article of document.querySelectorAll(`article:not([${MARK}])`)) {
    const link = article.querySelector('a[href*="/p/"], a[href*="/reel/"]');
    if (!link) continue; // post link not rendered yet; retry on next mutation
    article.setAttribute(MARK, "1");
    ensurePositioned(article);
    article.appendChild(makeButton(() => shortcodeFrom(
      article.querySelector('a[href*="/p/"], a[href*="/reel/"]')?.getAttribute("href")
    ), "igdl-corner"));
  }
}

function attachToTiles() {
  const tiles = document.querySelectorAll(
    `a[href*="/p/"]:not([${MARK}]), a[href*="/reel/"]:not([${MARK}])`
  );
  for (const tile of tiles) {
    // Only media tiles; skip timestamp/caption links and anything inside an article.
    if (!tile.querySelector("img, video") || tile.closest("article")) continue;
    tile.setAttribute(MARK, "1");
    ensurePositioned(tile);
    tile.appendChild(makeButton(() => shortcodeFrom(tile.getAttribute("href")), "igdl-corner"));
  }
}

let floating = null;

function syncFloatingButton() {
  const onPostPage = SHORTCODE_RE.test(location.pathname) && !document.querySelector("article");
  if (onPostPage && !floating) {
    floating = makeButton(() => shortcodeFrom(location.pathname), "igdl-floating");
    document.body.appendChild(floating);
  } else if (!onPostPage && floating) {
    floating.remove();
    floating = null;
  }
}

// ── "Download all visible" (saved page only, mirrors igdl() on /saved/) ─────────

let bulk = null;

function visibleShortcodes() {
  const codes = Array.from(document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]'))
    .filter((a) => a.querySelector("img, video") && !a.closest("article"))
    .map((a) => shortcodeFrom(a.getAttribute("href")))
    .filter(Boolean);
  return [...new Set(codes)];
}

function makeBulkButton() {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "igdl-bulk";
  btn.textContent = "⬇ Download all visible";
  btn.addEventListener("click", async () => {
    if (btn.dataset.state === "busy") return;
    const shortcodes = visibleShortcodes();
    if (!shortcodes.length) { btn.title = "No posts found on this page"; return; }
    btn.dataset.state = "busy";
    btn.textContent = `… downloading ${shortcodes.length}`;
    try {
      const res = await chrome.runtime.sendMessage({ type: "download-many", shortcodes });
      btn.dataset.state = res?.ok ? "done" : "fail";
      btn.textContent = res?.ok
        ? `✓ ${res.downloaded}/${shortcodes.length} saved`
        : "✕ Failed (hover)";
      btn.title = res?.ok ? `Saved to ${res.target}` : res?.error || "Download failed";
    } catch (err) {
      btn.dataset.state = "fail";
      btn.textContent = "✕ Failed (hover)";
      btn.title = err.message;
    }
  });
  return btn;
}

function syncBulkButton() {
  const onSaved = /\/saved\/[^/]+/.test(location.pathname) || /\/saved\/?$/.test(location.pathname);
  if (onSaved && !bulk) {
    bulk = makeBulkButton();
    document.body.appendChild(bulk);
  } else if (!onSaved && bulk) {
    bulk.remove();
    bulk = null;
  }
}

function scan() {
  attachToArticles();
  attachToTiles();
  syncFloatingButton();
  syncBulkButton();
}

let scheduled = false;
new MutationObserver(() => {
  if (scheduled) return;
  scheduled = true;
  setTimeout(() => { scheduled = false; scan(); }, 300);
}).observe(document.documentElement, { childList: true, subtree: true });

scan();
