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
  --output C:\Users\karan\Downloads \
  --port 7432 \
  --filename-pattern "{date}_{owner_username}_{shortcode}"
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
await igdl(10, 5)     // download 10 posts, skipping first 5
await igdl({ count: 5, skip: 2, port: 7432 })   // explicit options
```

---

## Output structure

For each downloaded post you get:

```
<output>/
└── <username>/
    ├── username_shortcode.jpg          ← original media
    ├── username_shortcode.txt          ← caption + top 5 comments (JSON)
    ├── username_shortcode_snapshot.jpg ← caption graphic appended to image
    └── username_shortcode_carousel.jpg ← grid composite (carousel posts only)
        username_shortcode_carousel_snapshot.jpg
```

- **Saved posts** go to `<output>/_saved/` (Windows) or `<output>/:saved/` (Linux/Mac).
- **Hashtag posts** go to `<output>/#tagname/`.
- **Videos** produce `_snapshot.mp4` (panel appended via ffmpeg).
- **Carousel posts** produce a collage image/video grid AND a snapshot of that collage.

---

## Server flags

| Flag | Default | Description |
|---|---|---|
| `--sessionid` | *(required)* | Instagram `sessionid` cookie value |
| `--csrftoken` | *(required)* | Instagram `csrftoken` cookie value |
| `--output`, `-o` | `.` | Root directory for all downloads |
| `--port`, `-p` | `7432` | WebSocket port to listen on |
| `--filename-pattern` | `{owner_username}_{shortcode}` | Instaloader filename template |
| `--no-post-process` | off | Skip all post-processing (collage + caption graphic) |
| `--no-collage` | off | Skip carousel collage/concat only |
| `--no-graphic` | off | Skip caption graphic snapshot only |

**Filename pattern tokens:** `{date}`, `{owner_username}`, `{shortcode}`, `{mediaid}`, etc.

```bash
# Download files only, no post-processing at all
python download-insta-tab/server.py ... --no-post-process

# Collages yes, but skip graphic panels
python download-insta-tab/server.py ... --no-graphic

# Graphics yes, but no carousel collages
python download-insta-tab/server.py ... --no-collage
```

---

## Caption graphic

After each download, `caption_graphic.py` generates an Instagram-style panel from the saved `.txt` metadata and appends it to the media file:

```
┌──────────────────────────────────────┐
│  Caption text, word-wrapped…         │
│  #hashtag and @mention in blue       │
│  ────────────────────────────────    │
│  @user1  First comment text…         │
│    ♥ 142                             │
│  @user2  Another comment…            │
└──────────────────────────────────────┘
```

- **Portrait images** (height > width): panel appended as a right-hand sidebar (max 400 px wide).
- **Square / landscape images**: panel appended as a strip below.
- **Videos**: panel composited below via ffmpeg (`vstack`), audio preserved.

Requires **Pillow** (`pip install Pillow`) and **ffmpeg** on your PATH for video posts.

### Retroactive application

Caption graphics are generated automatically during download. If you downloaded posts before this feature existed, or ran with `--no-graphic`, apply them retroactively with the standalone CLI:

```bash
# Apply caption graphics to everything in a folder that doesn't have one yet
python download-insta-tab/caption_graphic.py C:\Users\karan\Downloads\_saved

# Re-generate snapshots that already exist
python download-insta-tab/caption_graphic.py C:\Users\karan\Downloads\_saved --overwrite

# Delete original media and .txt files after creating snapshot
python download-insta-tab/caption_graphic.py C:\Users\karan\Downloads\_saved --delete

# Move original files to a folder instead of deleting them
python download-insta-tab/caption_graphic.py C:\Users\karan\Downloads\_saved --move-to C:\Users\karan\Downloads\_trash
```

This scans for any `{name}.txt` + `{name}.{jpg|mp4|…}` pair without a matching `{name}_snapshot.*`, generates the snapshot, and can optionally **delete the original media and `.txt`** files using the `--delete` flag. Works on single posts and already-built `_carousel.*` composites alike.

---

## Carousel processor

`carousel_processor.py` detects multi-image/video carousel posts and:

1. Assembles individual slides into a single composite grid image (images) or concatenated video.
2. Optionally appends the caption graphic to the composite.

It can also be run standalone on a directory of already-downloaded files to build collages from raw slides (`_1.jpg`, `_2.jpg`, …).

> **Note:** If the server already processed a carousel, the individual slides are deleted and only `_carousel.jpg` remains — `carousel_processor.py` will correctly report "No carousels found" in that case. To retroactively apply caption graphics to already-processed files, use `caption_graphic.py` instead (see below).

```bash
# Process all carousels found in a directory
python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved

# One specific shortcode only
python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved --shortcode AbCdEfG

# Skip the caption graphic
python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved --no-graphic

# Skip the collage entirely (just rename/tidy files)
python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved --no-collage

# Keep individual slide files after compositing
python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved --keep-slides

# Move individual slide files to a folder instead of deleting them
python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved --move-to C:\Users\karan\Downloads\_slides_backup

# Custom cell size for the grid (default 640 px)
python download-insta-tab/carousel_processor.py C:\Users\karan\Downloads\_saved --cell-size 800
```

Requires **Pillow** and **ffmpeg**.

---

## Automated Injection (`inject_and_run.py`)

Instead of manually copying and pasting `console.js` into the browser console, you can use `inject_and_run.py` to automate the process.

This script connects to your running Chrome instance, lists your open tabs, injects the script, and runs the `igdl` command with parameters.

Requires starting Chrome with remote debugging enabled:
```powershell
chrome.exe --remote-debugging-port=9222
```

Then run:
```powershell
python download-insta-tab/inject_and_run.py --count 10 --skip 0
```

See `COMMANDS.md` for full details and arguments.

---

## Files

| File | Purpose |
|---|---|
| `server.py` | Local WebSocket server — authenticates, downloads, post-processes |
| `console.js` | Paste into DevTools on any Instagram page |
| `carousel_processor.py` | Carousel collage builder; also runnable as a standalone CLI |
| `caption_graphic.py` | Generates Instagram-style caption+comments panel and appends it to media |
| `inject_and_run.py` | Automates script injection and execution via CDP |
| `instagram-page-downloader.js` | Legacy standalone browser-only script (deprecated) |

---

## Dependencies

| Package | Required for |
|---|---|
| `instaloader` | Core download engine (parent package) |
| `websockets` | WebSocket server |
| `Pillow` | Image collage and caption panel |
| `ffmpeg` (system) | Video concat and caption panel on videos |
| `playwright` | Automated script injection |

Install Python dependencies:
```bash
pip install instaloader websockets Pillow playwright
```

---

## Notes

- The server only listens on `127.0.0.1` — your cookies never leave your machine.
- Session cookies typically last weeks to months; restart the server with fresh cookies if authentication fails.
- `--no-post-process` overrides `--no-collage` and `--no-graphic` — when set, nothing extra runs.
- If `--no-collage` is set but `--no-graphic` is not, no graphic is generated for carousel posts (there is no composite to attach it to).
- Saved posts use `_saved` as the folder name on Windows (`:` is not allowed in Windows directory names).
