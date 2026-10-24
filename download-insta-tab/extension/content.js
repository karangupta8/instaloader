// Adds a small download button over every post/reel on the page (feed articles,
// profile/saved grid tiles) plus a floating one on single post/reel pages. Posts that are
// already on disk (per the local server) are marked with a green ✓ and outline.

const SHORTCODE_RE = /\/(?:p|reel|reels)\/([A-Za-z0-9_-]+)/;
const MARK = "data-igdl-attached";
const HAVE_ATTR = "data-igdl-have";
const STATES = { idle: "⬇", busy: "…", done: "✓", fail: "✕" };
const CHECK_DEBOUNCE_MS = 800;
const CHECK_BATCH = 300;
const CHECK_BACKOFF_MS = 30000;

function shortcodeFrom(href) {
  const match = (href || "").match(SHORTCODE_RE);
  return match ? match[1] : null;
}

function setState(btn, state, title) {
  btn.textContent = STATES[state];
  btn.dataset.state = state;
  btn.title = title;
}

// ── Already-downloaded tracking ─────────────────────────────────────────────

const registry = new Set();      // { btn, getShortcode } for every button we created
const checkedCodes = new Set();  // shortcodes the server has already been asked about
const haveCodes = new Set();     // shortcodes known to be on disk
let checkTimer = null;
let backoffUntil = 0;

async function send(message) {
  try {
    return await chrome.runtime.sendMessage(message);
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

function applyHave(btn) {
  setState(btn, "done", "Already downloaded");
  btn.parentElement?.setAttribute(HAVE_ATTR, "1");
}

function refreshHave() {
  for (const entry of registry) {
    if (!entry.btn.isConnected) { registry.delete(entry); continue; }
    const code = entry.getShortcode();
    if (code && haveCodes.has(code) && entry.btn.dataset.state === "idle") applyHave(entry.btn);
  }
}

function recordHave(codes) {
  for (const code of codes) { checkedCodes.add(code); haveCodes.add(code); }
  refreshHave();
}

async function runCheck() {
  checkTimer = null;
  if (Date.now() < backoffUntil) return;
  const pending = new Set();
  for (const entry of registry) {
    const code = entry.getShortcode();
    if (code && !checkedCodes.has(code)) pending.add(code);
  }
  if (!pending.size) return;

  const batch = [...pending].slice(0, CHECK_BATCH);
  const res = await send({ type: "check", shortcodes: batch });
  if (!res?.ok) {
    // Server probably isn't running; don't hammer it on every DOM mutation.
    backoffUntil = Date.now() + CHECK_BACKOFF_MS;
    setTimeout(scheduleCheck, CHECK_BACKOFF_MS);
    return;
  }
  batch.forEach((code) => checkedCodes.add(code));
  recordHave(res.downloaded);
  if (pending.size > batch.length) scheduleCheck();
}

function scheduleCheck() {
  if (!checkTimer) checkTimer = setTimeout(runCheck, CHECK_DEBOUNCE_MS);
}

// ── Per-post buttons ────────────────────────────────────────────────────────

function makeButton(getShortcode, extraClass = "") {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `igdl-btn ${extraClass}`.trim();
  setState(btn, "idle", "Download with instaloader");
  registry.add({ btn, getShortcode });

  btn.addEventListener("click", async (event) => {
    // The button sits inside links; don't let Instagram navigate or react to the click.
    event.preventDefault();
    event.stopPropagation();
    const shortcode = getShortcode();
    if (!shortcode || btn.dataset.state === "busy") return;

    setState(btn, "busy", `Downloading ${shortcode}…`);
    const fromSavedPage = /\/saved(\/|$)/.test(location.pathname);
    const res = await send({ type: "download", shortcode, fromSavedPage });
    if (!res?.ok) return setState(btn, "fail", res?.error || "Download failed");
    recordHave([shortcode]);
    setState(btn, "done", res.skipped ? "Already downloaded" : `Saved to ${res.target}`);
    btn.parentElement?.setAttribute(HAVE_ATTR, "1");
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

/** Ask how many of the not-yet-downloaded posts to fetch; null = cancelled/invalid. */
function askCount(remaining, alreadyHave) {
  const answer = window.prompt(
    `${remaining} new post(s) to download (${alreadyHave} already downloaded).\nHow many? (blank = all)`,
    String(remaining)
  );
  if (answer === null) return null;
  if (answer.trim() === "") return remaining;
  const n = parseInt(answer, 10);
  return Number.isInteger(n) && n > 0 ? Math.min(n, remaining) : null;
}

function setBulk(btn, state, text, title = "") {
  btn.dataset.state = state;
  btn.textContent = text;
  btn.title = title;
}

async function runBulk(btn) {
  const visible = visibleShortcodes();
  if (!visible.length) return setBulk(btn, "idle", "⬇ Download all visible", "No posts found on this page");

  setBulk(btn, "busy", "… checking");
  const checked = await send({ type: "check", shortcodes: visible });
  if (!checked?.ok) return setBulk(btn, "fail", "✕ Failed (hover)", checked?.error || "Check failed");
  recordHave(checked.downloaded);

  const have = new Set(checked.downloaded);
  const fresh = visible.filter((code) => !have.has(code));
  if (!fresh.length) return setBulk(btn, "done", `✓ All ${visible.length} already downloaded`);

  const count = askCount(fresh.length, have.size);
  if (count === null) return setBulk(btn, "idle", "⬇ Download all visible");

  const batch = fresh.slice(0, count);
  setBulk(btn, "busy", `… downloading ${batch.length}`);
  const res = await send({ type: "download-many", shortcodes: batch });
  if (!res?.ok) return setBulk(btn, "fail", "✕ Failed (hover)", res?.error || "Download failed");

  const after = await send({ type: "check", shortcodes: batch });
  if (after?.ok) recordHave(after.downloaded);
  setBulk(btn, "done", `✓ ${res.downloaded}/${batch.length} saved`, `Saved to ${res.target}`);
}

function makeBulkButton() {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "igdl-bulk";
  btn.textContent = "⬇ Download all visible";
  btn.addEventListener("click", () => {
    if (btn.dataset.state !== "busy") runBulk(btn);
  });
  return btn;
}

function syncBulkButton() {
  const onSaved = /\/saved(\/|$)/.test(location.pathname);
  if (onSaved && !bulk) {
    bulk = makeBulkButton();
    document.body.appendChild(bulk);
  } else if (!onSaved && bulk) {
    bulk.remove();
    bulk = null;
  }
}

// ── Wiring ──────────────────────────────────────────────────────────────────

function scan() {
  attachToArticles();
  attachToTiles();
  syncFloatingButton();
  syncBulkButton();
  refreshHave();
  scheduleCheck();
}

let scheduled = false;
new MutationObserver(() => {
  if (scheduled) return;
  scheduled = true;
  setTimeout(() => { scheduled = false; scan(); }, 300);
}).observe(document.documentElement, { childList: true, subtree: true });

scan();
