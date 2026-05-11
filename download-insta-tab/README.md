# download-insta-tab

Download from any Instagram page in your browser using your active session.
Built as a thin layer on top of [Instaloader](https://instaloader.github.io/) — the parent package handles all the hard parts.
When upstream Instaloader updates, this picks up the changes automatically.

## How it works

```
Chrome (logged in) ──cookies──▶ server.py ──instaloader──▶ files on disk
      │                              ▲
      └── console.js ──POST /download┘
```

1. `server.py` starts once, reads your Chrome session via `browser_cookie3`, authenticates with Instagram.
2. You open any Instagram page in Chrome.
3. Paste `console.js` into DevTools console — it detects the page type and calls the server.
4. Instaloader downloads everything.

---

## Setup

```bash
pip install browser-cookie3   # already in Pipfile if using pipenv
```

---

## Usage

**Step 1 — Start the server** (once per session):

```bash
# From repo root
python download-insta-tab/server.py

# Options
python download-insta-tab/server.py --browser firefox --output ~/Downloads --port 7432
```

**Step 2 — Open any Instagram page in Chrome and log in.**

Supported pages:
| URL pattern | Downloads |
|---|---|
| `instagram.com/natgeo/` | Profile posts |
| `instagram.com/you/saved/` | Your saved posts |
| `instagram.com/explore/tags/cats/` | Hashtag posts |
| `instagram.com/p/AbCdEfG/` or `/reel/…` | Single post |

**Step 3 — Open DevTools console** (`F12` → Console), paste `console.js`, then call:

```js
await igdl()          // top 10 posts from current page
await igdl(30)        // top 30 posts
await igdl({ count: 5, port: 7432 })   // explicit options
```

---

## Files

| File | Purpose |
|---|---|
| `server.py` | Local HTTP server — run once, handles all downloads via instaloader |
| `console.js` | Paste into DevTools on any Instagram page |
| `instagram-page-downloader.js` | Standalone browser-only alternative (no Python needed) |

---

## Notes

- `server.py` must be running before you call `igdl()` in the console.
- Session is loaded once at startup — if your Chrome session expires, restart the server.
- Files are saved to `<output>/<profile>/` using instaloader's default naming (`2024-01-15_AbCdEfG.jpg`).
- Saved posts go to `<output>/:saved/`, hashtag posts to `<output>/#cats/`.

---

## Standalone alternative: `instagram-page-downloader.js`

No Python needed — paste directly in DevTools, downloads via browser fetch.

```js
await runInstagramPageDownloader({
  maxPosts: 10,
  scrollDelayMs: 900,
  requestDelayMs: 350,
  downloadDelayMs: 250,
});
```

| | `server.py` + `console.js` | `instagram-page-downloader.js` |
|---|---|---|
| Requires Python | Yes | No |
| Works on saved/hashtag pages | Yes | No (profile only) |
| Download quality | Full res via Instaloader | CDN URL from page |
| Deduplication | Yes | No |
| Rate limiting | Instaloader RateController | Manual delays |
