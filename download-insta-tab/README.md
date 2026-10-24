# download-insta-tab

Download Instagram posts, reels, saved items, profiles and hashtags from the pages you are already looking at, using your logged-in Chrome session. A thin layer on top of [Instaloader](https://instaloader.github.io/): the parent package does the hard parts, so upstream fixes are picked up automatically.

```
Chrome extension ──(reads your cookies, adds buttons)──▶ server.py ──▶ instaloader ──▶ files on disk
                      ws://127.0.0.1:7432                  (this folder)
```

No password, no admin rights, no copying tokens out of DevTools.

## Quick start

1. **Install dependencies:** `pip install instaloader websockets Pillow` (plus `ffmpeg` on your PATH for videos).
2. **Load the extension:** `chrome://extensions` → enable *Developer mode* → *Load unpacked* → pick `download-insta-tab/extension`.
3. **Start the server:** `python download-insta-tab/server.py --output C:\Users\you\Downloads`
4. **Browse Instagram** (logged in) and click the buttons below.

To stop typing flags every time, edit `config.toml` (versioned; `config.example.toml` is the annotated template) to set your defaults; then it is just `python download-insta-tab/server.py`.

## Using the extension

| Where | What you get |
|---|---|
| Any post / reel (feed, grid tile, post page) | A round **⬇** button. Click to download that post. |
| Saved page | **⬇ Download all visible** pill: checks what's new, asks how many, downloads them. |
| Profile or hashtag page | **⬇ Download top posts** pill: asks how many of the newest, downloads them. |

- **Already downloaded** posts get a green outline and a green ✓, and are never fetched twice.
- A running batch shows a live `12/40` counter; **click the pill again to cancel** (the post in flight finishes first).
- The **toolbar icon** shows `ON` (server up, logged in), `OFF` (server not running) or `!` (not logged in to Instagram). Click it for details.
- Posts clicked on the Saved page go to `<output>/_saved/` and are stamped in download order; everywhere else they go to `<output>/<username>/`.

## What you get on disk

```
<output>/
├── <username>/
│   ├── username_shortcode_snapshot.jpg   ← image + caption/comments panel (videos: _snapshot.mp4)
│   └── username_shortcode_carousel_snapshot.jpg   ← carousels: collage + panel
├── _saved/            saved posts (":saved" on Linux/Mac)
├── #<tag>/            hashtag posts
├── downloads.jsonl    one line per post: downloaded / skipped / failed / aborted, with timestamp
└── .ig-dl-state.json  persisted cooldown (see below)
```

By default the original media and `.txt` stay next to each snapshot; start with `--delete-originals` to keep only the `_snapshot` file per post. Every file's modified time is set to its download order, so sorting a folder by *Date Modified* reproduces the order posts appeared on the source page (handy for working through Saved posts and unsaving them as you go).

The caption panel is drawn from the post's caption and its top 5 comments. It sits to the right of portrait images (max 400 px), below square/landscape ones, and below videos (ffmpeg, audio kept).

## Server options

Every option can be a command-line flag or a key in `config.toml` (flag wins). Config keys are the flag names with underscores.

| Flag | Default | Description |
|---|---|---|
| `--output`, `-o` | `.` | Root folder for downloads |
| `--port`, `-p` | `7432` | WebSocket port |
| `--delete-originals` | off | Keep only the final `_snapshot` file per post |
| `--no-post-process` | off | Skip collages and caption graphics entirely |
| `--no-collage` | off | Skip carousel collages only |
| `--no-graphic` | off | Skip caption snapshots only |
| `--post-delay MIN MAX` | `6 15` | Random pause (seconds) between posts in a run; `0 0` disables |
| `--max-batch` | `50` | Refuse requests for more posts than this at once |
| `--daily-cap` | `100` | Most posts per rolling 24 h; `0` disables |
| `--cooldown-minutes` | `120` | After Instagram pushes back, refuse all downloads for this long |
| `--extension-id` | any | Only accept this Chrome extension (ID shown on `chrome://extensions`) |
| `--filename-pattern` | `{owner_username}_{shortcode}` | Custom patterns require `--no-post-process`: snapshots, collages and the already-downloaded check rely on the default |
| `--sessionid`, `--csrftoken` | — | Optional. Only for use without the extension (see Legacy) |
| `--config` | `config.toml` | Config file path |

## Staying under Instagram's radar

Nothing makes bulk downloading risk-free: **Instagram restricted this account once** ("You can't create multiple sessions", 2026-10-24) after a ~12-post run during which it was already answering "Please wait a few minutes before you try again". The layers below are the result of that, and are all on by default.

- **Abort on the first soft block.** Instaloader is told to abort immediately on HTTP **401** ("please wait a few minutes") and **429** instead of retrying. Messages like `login_required`, `checkpoint_required`, `challenge_required` and `feedback_required` are treated the same way, including when they come from the comments fetch. Two posts failing back to back also stops the run (a deleted post, a plain 404, doesn't count).
- **Cooldown.** Any of the above stops the batch and **refuses all downloads for `--cooldown-minutes` (120)**. The cooldown is saved in the output folder (`.ig-dl-state.json`), so restarting the server doesn't clear it; delete that file only if you really mean to override it.
- **Same device, not a new one every time.** The extension sends your browser's device cookies (`mid`, `ig_did`, `datr`, …) along with the session, plus Chrome's real user-agent, client hints, `Accept-Language` and `sec-fetch-*` headers (toward `www.instagram.com` only). Without those, each server start looked like a brand-new device using your session. **Keep the server running instead of restarting it repeatedly.**
- **Pacing:** instaloader's own random 0–15 s sleep before every request, plus `--post-delay` (6–15 s) between posts.
- **Caps:** `--max-batch` (50) per request and `--daily-cap` (100) per rolling 24 h, counted from `downloads.jsonl`.
- **No repeat traffic:** posts already on disk are never re-fetched. Status icon, duplicate checks and badges talk only to your local server.
- The TLS fingerprint of Python's requests still differs from Chrome's; that can't be fixed from here.

Habits that matter more than any setting: small batches (a few dozen at most), a few days apart rather than several runs a day, no other Instagram automation on the account, and no heavy Instagram app use while a batch runs. After a restriction, wait it out, use Instagram normally for a day or two, and only then start again with small batches.

## Offline tools

### `postprocess.py` — one command for already-downloaded folders

Runs the same steps the server runs after each download: carousel collages, then caption snapshots. Use it for posts downloaded with `--no-post-process`, or before a feature existed. Safe to re-run.

```bash
python download-insta-tab/postprocess.py C:\Users\you\Downloads\_saved
python download-insta-tab/postprocess.py <dir> --delete-originals         # keep only *_snapshot files
python download-insta-tab/postprocess.py <dir> --move-to <archive-dir>    # archive instead of delete
python download-insta-tab/postprocess.py <dir> --overwrite                # regenerate existing snapshots
python download-insta-tab/postprocess.py <dir> --shortcode AbCdEfG        # one post only
python download-insta-tab/postprocess.py <dir> --no-collage | --no-graphic --keep-slides --cell-size 800
```

(`carousel_processor.py` and `caption_graphic.py` still work as separate CLIs if you want only one step.)

### `scrape_comments.py` — every comment on one post

Dumps the full comment thread (with replies) to a text file. This one needs cookies on the command line, since it runs without the extension: DevTools → Application → Cookies → `https://www.instagram.com` → copy `sessionid` and `csrftoken`.

```bash
python download-insta-tab/scrape_comments.py AbCdEfG --sessionid "..." --csrftoken "..." [--output out.txt] [--max-comments 150]
```

> Those cookies give full access to your account. Never commit or share them. `keys.txt` in this folder is git-ignored for that purpose.

## Legacy: no extension

`legacy/` keeps the pre-extension flow working and unchanged:

- `legacy/console.js` — paste into the DevTools console on an Instagram page, then `await igdl()`, `await igdl(30)`, `await igdl(10, 5)` (count, skip) or `await igdl({ count: 5, skip: 2 })`. Start the server with `--sessionid` and `--csrftoken` for this flow.
- `legacy/inject_and_run.py` — injects `console.js` into a Chrome started with `--remote-debugging-port=9222` (needs `pip install playwright`): `python download-insta-tab/legacy/inject_and_run.py --count 10`.
- `legacy/instagram-page-downloader.js` — the original browser-only script (deprecated).

## Files

| File | Purpose |
|---|---|
| `server.py` | WebSocket server, command line and config file |
| `pipeline.py` | The download engine: one pipeline for every route, plus all safety rails |
| `session.py` | Cookie login and browser-identity headers (shared with `scrape_comments.py`) |
| `extension/` | The Chrome extension (Manifest V3) |
| `postprocess.py` | Offline collage + snapshot command |
| `carousel_processor.py`, `caption_graphic.py` | The collage builder and the caption panel renderer |
| `scrape_comments.py` | Full comment thread dump |
| `config.example.toml` | Template for `config.toml` |
| `tests/` | Server/pipeline tests: `python -m pytest download-insta-tab/tests` |
| `extension/tests/` | Extension page-script tests in a simulated page: `cd download-insta-tab && npm install && npm test` |
| `legacy/` | Pre-extension scripts |

## Where the logs are

| Where | What it shows |
|---|---|
| **Server console** | Every request (`[ig-dl] COLLECTION …`), each post (`[3/40] AbC…`), `Refused: …` reasons (batch too big, cooldown, daily cap), login failures, and errors with tracebacks |
| `<output>/downloads.jsonl` | One line per post: `downloaded` / `skipped` / `failed` / `aborted`, with time and error |
| `<output>/.ig-dl-state.json` | Cooldown end time, if Instagram pushed back |
| **The pill / button** | The error text itself; hover for the full message |
| **Instagram tab DevTools → Console** | `[igdl] failed: …` lines from the page script |
| **`chrome://extensions` → Instaloader Downloader → *service worker*** | The background worker's console (`[igdl] request failed: …`) and its Network/WS frames |

## Troubleshooting

- **Icon shows `OFF`:** the server isn't running (or isn't on port 7432).
- **Icon shows `!`:** you're not logged in to instagram.com in this Chrome profile; the tooltip says which cookie is missing.
- **"Cooling down…":** Instagram pushed back recently; wait it out (the message says how long).
- **Pill stuck on "downloading…":** reload the extension and the tab; the server stops a batch whose browser connection is gone.
- **A failed WebSocket error on the extension's page:** harmless; Chrome logs it whenever the server was down at that moment.

## Notes

- The server listens on `127.0.0.1` only, and rejects WebSocket connections from any origin except `instagram.com` and Chrome extensions (pin yours with `--extension-id`). Your cookies never leave your machine.
- Session cookies last weeks to months; if Instagram logs you out, just log in again in Chrome.
- If `--no-collage` is set but `--no-graphic` is not, carousels get no graphic (there is no composite to attach it to).
- `--delete-originals` only deletes a post's pre-snapshot files *after* its snapshot is created, so a post never loses its files if snapshot generation fails; it has no effect with `--no-post-process` or `--no-graphic`.
- Python 3.10+ (3.11+ for `config.toml`).
