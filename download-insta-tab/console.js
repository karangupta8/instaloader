/*
  download-insta-tab console snippet
  ───────────────────────────────────
  Paste this into DevTools console on any Instagram page, then call:

      await igdl()           // download 10 posts from current page
      await igdl(30)         // download 30 posts
      await igdl(10, 5)      // download 10 posts, skipping first 5
      await igdl({ count: 5, skip: 2, port: 7432 })

  Works on:
    - Profile page      instagram.com/natgeo/
    - Saved posts       instagram.com/you/saved/
    - Hashtag           instagram.com/explore/tags/cats/
    - Single post       instagram.com/p/AbCdEfG/ or /reel/AbCdEfG/

  Requires server.py running locally:
      python download-insta-tab/server.py

  Uses WebSocket (ws://) to talk to localhost — allowed by Instagram's CSP.
  Auth is automatic: sends your session cookies on first call.
*/

async function igdl(countOrOptions = 10, skip = 0) {
  const opts  = typeof countOrOptions === "object" ? countOrOptions : { count: countOrOptions, skip: skip };
  const count = opts.count ?? 10;
  const skipVal = opts.skip ?? 0;
  const port  = opts.port  ?? 7432;

  // ── Detect page type ────────────────────────────────────────────────────────

  const path = location.pathname.replace(/\/$/, "");
  let pageInfo = null;

  const hashtagMatch = path.match(/^\/explore\/tags\/([^/]+)$/);
  if (hashtagMatch) pageInfo = { page_type: "hashtag", identifier: hashtagMatch[1] };

  if (!pageInfo && path.match(/\/saved(\/|$)/))
    pageInfo = { page_type: "saved" };

  if (!pageInfo) {
    const postMatch = path.match(/^\/(p|reel)\/([A-Za-z0-9_-]+)$/);
    if (postMatch) pageInfo = { page_type: "post", identifier: postMatch[2] };
  }

  const RESERVED = new Set([
    "explore", "accounts", "direct", "stories", "reels",
    "tv", "locations", "directory", "login", "challenge",
  ]);
  if (!pageInfo) {
    const parts = path.split("/").filter(Boolean);
    if (parts.length === 1 && !RESERVED.has(parts[0]))
      pageInfo = { page_type: "profile", identifier: parts[0] };
  }

  if (!pageInfo) {
    console.error(
      `[ig-dl] Cannot detect page type from: ${path}\n` +
      "Supported: profile, saved (/saved/), hashtag (/explore/tags/…), post (/p/… or /reel/…)"
    );
    return null;
  }

  console.log(
    `[ig-dl] Detected: ${pageInfo.page_type}` +
    (pageInfo.identifier ? ` → ${pageInfo.identifier}` : "") +
    `  (count=${count}, skip=${skipVal})`
  );

  // ── Connect via WebSocket (allowed by Instagram's CSP) ────────────────────

  return new Promise((resolve) => {
    let ws;
    try {
      ws = new WebSocket(`ws://localhost:${port}`);
    } catch {
      console.error("[ig-dl] Could not connect. Is server.py running?");
      resolve(null);
      return;
    }

    ws.onerror = () => {
      console.error(
        `[ig-dl] Cannot reach server at ws://localhost:${port}.\n` +
        "Start it with:  python download-insta-tab/server.py"
      );
      resolve(null);
    };

    ws.onmessage = async ({ data }) => {
      const msg = JSON.parse(data);

      if (msg.type === "status") {
        console.log(`[ig-dl] Server ready — @${msg.logged_in_as}  →  ${msg.output}`);
        ws.send(JSON.stringify({ type: "download", ...pageInfo, count, skip: skipVal }));
      }

      else if (msg.type === "done") {
        console.log(`[ig-dl] Done — ${msg.downloaded} post(s) saved to ${msg.target}`);
        ws.close();
        resolve(msg);
      }

      else if (msg.type === "error") {
        console.error(`[ig-dl] Error: ${msg.error}`);
        ws.close();
        resolve(null);
      }
    };
  });
}
