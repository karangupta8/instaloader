/*
  ig-dl console snippet
  ─────────────────────
  Paste this into DevTools console on any Instagram page, then call:

      await igdl()           // download 10 posts from current page
      await igdl(30)         // download 30 posts
      await igdl({ count: 5, port: 7432 })   // explicit options

  Works on:
    - Profile page      instagram.com/natgeo/
    - Saved posts       instagram.com/you/saved/
    - Hashtag           instagram.com/explore/tags/cats/
    - Single post       instagram.com/p/AbCdEfG/ or /reel/AbCdEfG/

  Requires server.py to be running locally first.
*/

async function igdl(countOrOptions = 10) {
  const opts = typeof countOrOptions === "object" ? countOrOptions : { count: countOrOptions };
  const count = opts.count ?? 10;
  const port  = opts.port  ?? 7432;
  const base  = `http://localhost:${port}`;

  // ── Detect page type from current URL ──────────────────────────────────────

  const path = location.pathname.replace(/\/$/, ""); // strip trailing slash

  let pageInfo = null;

  // /explore/tags/<hashtag>
  const hashtagMatch = path.match(/^\/explore\/tags\/([^/]+)$/);
  if (hashtagMatch) {
    pageInfo = { type: "hashtag", identifier: hashtagMatch[1] };
  }

  // /<username>/saved  or  /<username>/saved/all-posts
  if (!pageInfo && path.match(/\/saved(\/|$)/)) {
    pageInfo = { type: "saved" };
  }

  // /p/<shortcode>  or  /reel/<shortcode>
  if (!pageInfo) {
    const postMatch = path.match(/^\/(p|reel)\/([A-Za-z0-9_-]+)$/);
    if (postMatch) pageInfo = { type: "post", identifier: postMatch[2] };
  }

  // /<username>  — anything not matching a known Instagram reserved path
  const RESERVED = new Set([
    "explore", "accounts", "direct", "stories", "reels",
    "tv", "locations", "directory", "login", "challenge",
  ]);
  if (!pageInfo) {
    const parts = path.split("/").filter(Boolean);
    if (parts.length === 1 && !RESERVED.has(parts[0])) {
      pageInfo = { type: "profile", identifier: parts[0] };
    }
  }

  if (!pageInfo) {
    console.error(
      `[ig-dl] Cannot detect page type from path: ${path}\n` +
      "Supported pages: profile, saved, hashtag (/explore/tags/…), single post (/p/… or /reel/…)"
    );
    return null;
  }

  console.log(
    `[ig-dl] Detected: ${pageInfo.type}` +
    (pageInfo.identifier ? ` → ${pageInfo.identifier}` : "") +
    `  (count=${count})`
  );

  // ── Check server is up ────────────────────────────────────────────────────

  let status;
  try {
    const res = await fetch(`${base}/status`);
    status = await res.json();
  } catch {
    console.error(
      `[ig-dl] Cannot reach server at ${base}.\n` +
      "Start it with:  python ig-dl/server.py"
    );
    return null;
  }

  console.log(`[ig-dl] Server ready — logged in as @${status.logged_in_as}  →  ${status.output}`);

  // ── Send download request ─────────────────────────────────────────────────

  const res = await fetch(`${base}/download`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...pageInfo, count }),
  });

  const result = await res.json();

  if (result.ok) {
    console.log(`[ig-dl] Done — ${result.downloaded} post(s) saved to ${result.target}`);
  } else {
    console.error(`[ig-dl] Error: ${result.error}`);
  }

  return result;
}
