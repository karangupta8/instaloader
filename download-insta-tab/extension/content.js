// Adds a small download button over every post/reel on the page (feed articles, grid tiles),
// a floating one on single post/reel pages, and a pill for whole-page downloads on saved,
// profile and hashtag pages. Posts already on disk (per the local server) get a green outline.

const SHORTCODE_RE = /\/(?:p|reel|reels)\/([A-Za-z0-9_-]+)/;
const MARK = "data-igdl-attached";
const HAVE_ATTR = "data-igdl-have";
const STATES = { idle: "⬇", busy: "…", done: "✓", fail: "✕" };
const CHECK_DEBOUNCE_MS = 800;
const CHECK_BATCH = 300;
const CHECK_BACKOFF_MS = 30000;
const DEFAULT_PAGE_COUNT = 10;
const RESERVED_PATHS = new Set([
  "explore", "accounts", "direct", "stories", "reels", "reel", "p",
  "tv", "locations", "directory", "login", "challenge", "about", "legal",
]);

function shortcodeFrom(href) {
  const match = (href || "").match(SHORTCODE_RE);
  return match ? match[1] : null;
}

function setState(btn, state, title) {
  btn.textContent = STATES[state];
  btn.dataset.state = state;
  btn.title = title;
}

async function send(message) {
  try {
    return await chrome.runtime.sendMessage(message);
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

// ── Already-downloaded tracking ─────────────────────────────────────────────

const registry = new Set();      // { btn, getShortcode } for every button we created
const checkedCodes = new Set();  // shortcodes the server has already been asked about
const haveCodes = new Set();     // shortcodes known to be on disk
let checkTimer = null;
let backoffUntil = 0;

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

function onSavedPage() {
  return /\/saved(\/|$)/.test(location.pathname);
}

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
    const res = await send({ type: "download", shortcode, fromSavedPage: onSavedPage() });
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

// ── Whole-page pill: saved / profile / hashtag ──────────────────────────────

let bulk = null;

/** What kind of listing page this is, if any (mirrors the old igdl() page detection). */
function pageContext() {
  const path = location.pathname.replace(/\/$/, "");
  if (onSavedPage()) return { kind: "saved", label: "⬇ Download all visible" };
  const tag = path.match(/^\/explore\/tags\/([^/]+)$/);
  if (tag) {
    const name = decodeURIComponent(tag[1]);
    return { kind: "hashtag", identifier: name, display: `#${name}`, label: "⬇ Download top posts" };
  }
  const parts = path.split("/").filter(Boolean);
  if (parts.length === 1 && !RESERVED_PATHS.has(parts[0])) {
    return { kind: "profile", identifier: parts[0], display: `@${parts[0]}`, label: "⬇ Download top posts" };
  }
  return null;
}

function visibleShortcodes() {
  const codes = Array.from(document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]'))
    .filter((a) => a.querySelector("img, video") && !a.closest("article"))
    .map((a) => shortcodeFrom(a.getAttribute("href")))
    .filter(Boolean);
  return [...new Set(codes)];
}

/** Ask how many posts to fetch; null = cancelled/invalid. `blank` is what an empty answer means. */
function askCount(message, suggestion) {
  const answer = window.prompt(message, String(suggestion));
  if (answer === null) return null;
  if (answer.trim() === "") return suggestion;
  const n = parseInt(answer, 10);
  return Number.isInteger(n) && n > 0 ? n : null;
}

function setBulk(btn, state, text, title = "") {
  btn.dataset.state = state;
  btn.textContent = text;
  btn.title = title;
}

function showResult(btn, res) {
  if (!res?.ok) return setBulk(btn, "fail", "✕ Failed (hover)", res?.error || "Download failed");
  const parts = [`${res.downloaded} saved`];
  if (res.skipped) parts.push(`${res.skipped} already had`);
  if (res.failed) parts.push(`${res.failed} failed`);
  setBulk(btn, res.cancelled ? "idle" : "done", `${res.cancelled ? "■ Cancelled: " : "✓ "}${parts.join(", ")}`,
    `Saved to ${res.target}`);
}

async function refreshBadges(codes) {
  const res = await send({ type: "check", shortcodes: codes });
  if (res?.ok) recordHave(res.downloaded);
}

async function runSavedBulk(btn) {
  const visible = visibleShortcodes();
  if (!visible.length) return setBulk(btn, "idle", pageContext().label, "No posts found on this page");

  setBulk(btn, "busy", "… checking");
  const checked = await send({ type: "check", shortcodes: visible });
  if (!checked?.ok) return setBulk(btn, "fail", "✕ Failed (hover)", checked?.error || "Check failed");
  recordHave(checked.downloaded);

  const have = new Set(checked.downloaded);
  const fresh = visible.filter((code) => !have.has(code));
  if (!fresh.length) return setBulk(btn, "done", `✓ All ${visible.length} already downloaded`);

  const count = askCount(
    `${fresh.length} new post(s) to download (${have.size} already downloaded).\nHow many? (blank = all)`,
    fresh.length);
  if (count === null) return setBulk(btn, "idle", pageContext().label);

  const batch = fresh.slice(0, count);
  setBulk(btn, "busy", `… 0/${batch.length} · click to cancel`);
  showResult(btn, await send({ type: "download-many", shortcodes: batch }));
  refreshBadges(batch);
}

async function runPageBulk(btn, ctx) {
  const count = askCount(
    `Download the newest how many posts from ${ctx.display}?\n(Posts you already have are skipped.)`,
    DEFAULT_PAGE_COUNT);
  if (count === null) return;

  setBulk(btn, "busy", `… 0/${count} · click to cancel`);
  showResult(btn, await send({
    type: "download-page", page_type: ctx.kind, identifier: ctx.identifier, count,
  }));
  refreshBadges(visibleShortcodes());
}

function makeBulkButton() {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "igdl-bulk";
  btn.addEventListener("click", () => {
    if (btn.dataset.state === "busy") {
      btn.textContent = "… cancelling";
      send({ type: "cancel-batch" });
      return;
    }
    const ctx = pageContext();
    if (!ctx) return;
    if (ctx.kind === "saved") runSavedBulk(btn);
    else runPageBulk(btn, ctx);
  });
  return btn;
}

function resetBulk(ctx) {
  bulk.dataset.page = `${ctx.kind}:${ctx.identifier ?? ""}`;
  delete bulk.dataset.state;
  bulk.textContent = ctx.label;
  bulk.title = "";
}

function syncBulkButton() {
  const ctx = pageContext();
  if (ctx && !bulk) {
    bulk = makeBulkButton();
    resetBulk(ctx);
    document.body.appendChild(bulk);
  } else if (!ctx && bulk) {
    bulk.remove();
    bulk = null;
  } else if (ctx && bulk.dataset.state !== "busy" && bulk.dataset.page !== `${ctx.kind}:${ctx.identifier ?? ""}`) {
    resetBulk(ctx); // SPA navigation to a different page while idle: drop the old result text
  }
}

// Live progress from the server: counter on the pill, green outline on each tile as it lands.
chrome.runtime.onMessage.addListener((msg) => {
  if (msg?.type !== "progress") return;
  if (msg.status === "downloaded" || msg.status === "skipped") recordHave([msg.shortcode]);
  if (bulk?.dataset.state === "busy" && bulk.textContent !== "… cancelling") {
    bulk.textContent = `… ${msg.processed}/${msg.total} · click to cancel`;
  }
});

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
