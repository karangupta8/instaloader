// Drives extension/content.js inside jsdom: fake Instagram pages and a stubbed chrome.* API.
// Run: npm test   (from download-insta-tab/; needs `npm install` once for jsdom)
const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const CONTENT = fs.readFileSync(process.env.CONTENT_JS || path.join(__dirname, "..", "content.js"), "utf8");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function page({ path, tiles, have = [], serverUp = true, html: custom }) {
  const html = custom || `<body>${tiles.map((c) => `<a href="/p/${c}/"><img src="x.jpg"></a>`).join("")}</body>`;
  const dom = new JSDOM(html, { url: `https://www.instagram.com${path}`, runScripts: "outside-only", pretendToBeVisual: true });
  const sent = [];
  let onMessage = null;
  dom.window.chrome = {
    runtime: {
      onMessage: { addListener: (fn) => { onMessage = fn; } },
      sendMessage: async (msg) => {
        sent.push(msg);
        if (!serverUp) return { ok: false, error: "Server not reachable. Is server.py running?" };
        if (msg.type === "check") return { ok: true, downloaded: msg.shortcodes.filter((c) => have.includes(c)) };
        if (msg.type === "download-many") return { ok: true, downloaded: msg.shortcodes.length, skipped: 0, failed: 0, target: "C:/out/_saved" };
        if (msg.type === "download-page") return { ok: true, downloaded: msg.count, skipped: 0, failed: 0, target: "C:/out/x" };
        return { ok: true };
      },
    },
  };
  dom.window.eval(CONTENT);
  return { dom, sent, win: dom.window, doc: dom.window.document, progress: (m) => onMessage && onMessage(m) };
}

const pill = (p) => p.doc.querySelector(".igdl-bulk");
const panel = (p) => p.doc.querySelector(".igdl-panel");

(async () => {
  // 1. Saved page: pill appears, click -> check -> inline box (no window.prompt) -> download-many
  let p = page({ path: "/karangupta8/saved/", tiles: ["AAA", "BBB", "CCC", "DDD"], have: ["AAA"] });
  await sleep(450);
  assert(pill(p), "pill should exist on the saved page");
  assert.strictEqual(pill(p).textContent, "⬇ Download all visible");
  let prompted = false;
  p.win.prompt = () => { prompted = true; return null; }; // would be the old, suppressible path
  pill(p).click();
  await sleep(50);
  assert(panel(p), "inline count box should open");
  assert(panel(p).textContent.includes("3 new post(s)"), panel(p).textContent);
  assert.strictEqual(pill(p).dataset.state, "asking");
  pill(p).click(); // clicking the pill while the box is open must not cancel or restart anything
  assert.strictEqual(pill(p).dataset.state, "asking");
  const input = panel(p).querySelector("input");
  input.value = "2";
  [...panel(p).querySelectorAll("button")].find((b) => b.textContent === "Download").click();
  await sleep(50);
  const many = p.sent.find((m) => m.type === "download-many");
  assert.strictEqual(JSON.stringify(many.shortcodes), JSON.stringify(["BBB", "CCC"]), "first 2 of the 3 new posts, skipping the one already on disk");
  assert(!prompted, "must not use window.prompt");
  assert.match(pill(p).textContent, /2 saved/);
  assert(!panel(p), "box closes after answering");
  console.log("ok 1  saved pill: check -> inline box -> download-many [BBB, CCC]");

  // 2. Blank answer = all new posts
  p = page({ path: "/karangupta8/saved/", tiles: ["AAA", "BBB", "CCC"] });
  await sleep(450);
  pill(p).click(); await sleep(50);
  panel(p).querySelector("input").value = "";
  [...panel(p).querySelectorAll("button")].find((b) => b.textContent === "Download").click();
  await sleep(50);
  assert.strictEqual(JSON.stringify(p.sent.find((m) => m.type === "download-many").shortcodes), JSON.stringify(["AAA", "BBB", "CCC"]));
  console.log("ok 2  blank answer downloads all new posts");

  // 3. Cancel button: nothing is sent, and the pill says so
  p = page({ path: "/karangupta8/saved/", tiles: ["AAA", "BBB"] });
  await sleep(450);
  pill(p).click(); await sleep(50);
  [...panel(p).querySelectorAll("button")].find((b) => b.textContent === "Cancel").click();
  await sleep(20);
  assert(!p.sent.some((m) => m.type === "download-many"));
  assert.strictEqual(pill(p).textContent, "Cancelled");
  console.log("ok 3  cancelling the box sends nothing and says 'Cancelled'");

  // 4. No tiles: the pill says why instead of silently doing nothing
  p = page({ path: "/karangupta8/saved/", tiles: [] });
  await sleep(450);
  pill(p).click(); await sleep(20);
  assert.match(pill(p).textContent, /No posts found/);
  console.log("ok 4  no tiles ->", JSON.stringify(pill(p).textContent));

  // 5. Everything already downloaded
  p = page({ path: "/karangupta8/saved/", tiles: ["AAA", "BBB"], have: ["AAA", "BBB"] });
  await sleep(450);
  pill(p).click(); await sleep(50);
  assert.match(pill(p).textContent, /All 2 already downloaded/);
  console.log("ok 5  all downloaded ->", JSON.stringify(pill(p).textContent));

  // 6. Server down: the error text is on the pill, not hidden in a tooltip
  p = page({ path: "/karangupta8/saved/", tiles: ["AAA"], serverUp: false });
  await sleep(450);
  pill(p).click(); await sleep(50);
  assert.match(pill(p).textContent, /Server not reachable/);
  console.log("ok 6  server down ->", JSON.stringify(pill(p).textContent));

  // 7. Profile page: 'Download top posts' -> inline box -> download-page
  p = page({ path: "/natgeo/", tiles: ["AAA", "BBB"] });
  await sleep(450);
  assert.strictEqual(pill(p).textContent, "⬇ Download top posts");
  pill(p).click(); await sleep(50);
  panel(p).querySelector("input").value = "5";
  [...panel(p).querySelectorAll("button")].find((b) => b.textContent === "Download").click();
  await sleep(50);
  const pg = p.sent.find((m) => m.type === "download-page");
  assert.deepStrictEqual([pg.page_type, pg.identifier, pg.count], ["profile", "natgeo", 5]);
  console.log("ok 7  profile pill -> download-page natgeo x5");

  // 8. Live progress + click-to-cancel while busy
  p = page({ path: "/karangupta8/saved/", tiles: ["AAA", "BBB"] });
  p.win.chrome.runtime.sendMessage = async (msg) => {
    p.sent.push(msg);
    if (msg.type === "check") return { ok: true, downloaded: [] };
    if (msg.type === "download-many") { await sleep(300); return { ok: true, downloaded: 1, skipped: 0, failed: 0, cancelled: true, target: "t" }; }
    return { ok: true };
  };
  await sleep(450);
  pill(p).click(); await sleep(50);
  [...panel(p).querySelectorAll("button")].find((b) => b.textContent === "Download").click();
  await sleep(50);
  p.progress({ type: "progress", processed: 1, total: 2, shortcode: "AAA", status: "downloaded" });
  assert.match(pill(p).textContent, /1\/2 · click to cancel/);
  pill(p).click(); // cancel while busy
  assert(p.sent.some((m) => m.type === "cancel-batch"), "cancel-batch should be sent");
  await sleep(350);
  assert.match(pill(p).textContent, /Cancelled: 1 saved/);
  console.log("ok 8  live counter, click-to-cancel ->", JSON.stringify(pill(p).textContent));

  // 9. THE REAL SAVED PAGE: the whole grid inside one <article> (what the screenshot showed)
  const tile = (c) => `<a href="/p/${c}/"><img src="x.jpg"></a>`;
  p = page({ path: "/karangupta8/saved/all-posts/", html: `<body><article>${["AAA", "BBB", "CCC"].map(tile).join("")}</article></body>`, have: ["AAA"] });
  await sleep(450);
  assert.strictEqual(p.doc.querySelectorAll(".igdl-btn").length, 3, "one button per tile");
  assert.strictEqual(p.doc.querySelector("article > .igdl-btn"), null, "no single button on the grid container");
  assert.strictEqual(p.doc.querySelectorAll("a .igdl-btn").length, 3, "buttons sit on the tiles");
  pill(p).click(); await sleep(60);
  assert(panel(p), "pill finds tiles inside the article and asks how many");
  assert(panel(p).textContent.includes("2 new post(s)"), panel(p).textContent);
  [...panel(p).querySelectorAll("button")].find((b) => b.textContent === "Download").click();
  await sleep(50);
  assert.strictEqual(JSON.stringify(p.sent.find((m) => m.type === "download-many").shortcodes), JSON.stringify(["BBB", "CCC"]));
  console.log("ok 9  grid wrapped in one <article>: 3 tile buttons, pill finds the tiles");

  // 10. A real feed post <article> (two links, same post) still gets exactly one button
  p = page({ path: "/", html: `<body><article><a href="/p/POST1/"><img src="x.jpg"></a><a href="/p/POST1/">2h</a></article></body>` });
  await sleep(450);
  assert.strictEqual(p.doc.querySelectorAll(".igdl-btn").length, 1);
  assert(p.doc.querySelector("article > .igdl-btn"), "button on the article itself");
  assert.strictEqual(p.doc.querySelectorAll("a .igdl-btn").length, 0, "not on the inner links");
  console.log("ok 10 feed post article: exactly one button on the article");

  // 11. Article first looks like one post, then more tiles load in -> container button hands over to tiles
  p = page({ path: "/karangupta8/saved/all-posts/", html: `<body><article id="g">${tile("AAA")}</article></body>` });
  await sleep(450);
  assert(p.doc.querySelector("article > .igdl-btn"), "starts as a single-post-looking article");
  p.doc.getElementById("g").insertAdjacentHTML("beforeend", tile("BBB") + tile("CCC"));
  await sleep(500);
  assert.strictEqual(p.doc.querySelector("article > .igdl-btn"), null, "container button removed once it is a grid");
  assert.strictEqual(p.doc.querySelectorAll("a .igdl-btn").length, 3, "all three tiles have their own button");
  console.log("ok 11 progressively loaded grid: container button replaced by per-tile buttons");

  // 12. Truly unmatched layout: pill says so, and the console gets link diagnostics
  p = page({ path: "/karangupta8/saved/", html: `<body><a href="/p/AAA/"><div style="background-image:url(x)"></div></a></body>` });
  await sleep(450);
  const logs = [];
  p.win.console.info = (...a) => logs.push(a);
  pill(p).click(); await sleep(30);
  assert(logs.some((a) => String(a[0]).includes("no tiles matched") && a[1].postLinks === 1 && a[1].withMedia === 0), JSON.stringify(logs));
  console.log("ok 12 unmatched layout logs link diagnostics for the console");

  console.log("\nall pill scenarios passed");
  process.exit(0);
})().catch((err) => { console.error("FAILED:", err.message); process.exit(1); });
