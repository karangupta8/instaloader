# download-insta-tab

Download from any Instagram page in your browser using your active session.
Built as a thin layer on top of [Instaloader](https://instaloader.github.io/) — the parent package handles all the hard parts.
When upstream Instaloader updates, this picks up the changes automatically.

## How it works

```
Extract cookies from DevTools
         │
         ▼
python server.py ──(via WebSocket)──▶ Chrome console (any IG page)
         │                                ▲
         │                                │
         └────────── instaloader ────────┘
                                    (downloads to disk)
```

1. Get your Instagram `sessionid` and `csrftoken` from DevTools.
2. Start `server.py` with these cookies.
3. Open any Instagram page in Chrome.
4. Paste `console.js` into DevTools console and call `await igdl(10)`.
5. Instaloader downloads everything at full resolution using your authenticated session.

**No password, no admin, no browser_cookie3. WebSocket bypasses Instagram's Content Security Policy.**

---

## Usage

**Step 1 — Get your cookies from DevTools**

1. Open any Instagram page and press `F12` (DevTools).
2. Go to **Application** tab → **Cookies** → `https://www.instagram.com`.
3. Find and copy the values of:
   - `sessionid`
   - `csrftoken`

**Step 2 — Start the server**

```bash
python download-insta-tab/server.py --sessionid <YOUR_SESSIONID> --csrftoken <YOUR_CSRFTOKEN>

# With options
python download-insta-tab/server.py \
  --sessionid <YOUR_SESSIONID> \
  --csrftoken <YOUR_CSRFTOKEN> \
  --output ~/Downloads \
  --port 7432
```

The server will verify your credentials and display:
```
[ig-dl] Authenticated as @yourusername
[ig-dl] Output : /path/to/output
[ig-dl] Listening on ws://localhost:7432
```

**Step 3 — Open any Instagram page in Chrome** (must be logged in).

Supported pages:
| URL pattern | Downloads |
|---|---|
| `instagram.com/natgeo/` | Profile posts |
| `instagram.com/you/saved/` | Your saved posts |
| `instagram.com/explore/tags/cats/` | Hashtag posts |
| `instagram.com/p/AbCdEfG/` or `/reel/…` | Single post |

**Step 4 — Open DevTools console and run**

Press `F12` → **Console**, paste `console.js`, then call:

```js
await igdl()          // top 10 posts from current page
await igdl(30)        // top 30 posts
await igdl({ count: 5, port: 7432 })   // explicit options
```

---

## Files

| File | Purpose |
|---|---|
| `server.py` | Local WebSocket server — handles all downloads via instaloader |
| `console.js` | Paste into DevTools on any Instagram page |
| `instagram-page-downloader.js` | Legacy standalone browser-only script (deprecated) |

---

## Notes

- **Server cookies are persistent** — extract once, use until they expire (typically weeks/months).
- If your Instagram session expires, extract new cookies and restart the server.
- Files are saved to `<output>/<profile>/` using instaloader's default naming (`2024-01-15_AbCdEfG.jpg`).
- Saved posts go to `<output>/:saved/`, hashtag posts to `<output>/#tagname/`.
- The server only listens on `127.0.0.1` — your cookies never leave your machine.
- Session cookies have normal Instagram expiration timelines; refresh them periodically if server is left running long-term.
