#!/usr/bin/env python3
"""
download-insta-tab server — bridges your browser to instaloader via WebSocket.

Instagram's CSP blocks HTTP/HTTPS to localhost but allows ws://localhost:*.
This server uses WebSocket so console.js can communicate from an Instagram tab.

Usage:
    python download-insta-tab/server.py [options]

Options:
    --output    Directory to save downloads into (default: current dir)
    --port      Port to listen on (default: 7432)
"""

import argparse
import asyncio
import http
import itertools
import json
import logging
import os
import platform
import random
import re
import sys
import threading
import time
import traceback
import urllib.parse
from pathlib import Path

# Suppress "opening handshake failed" noise from browsers probing the port
logging.getLogger("websockets").setLevel(logging.CRITICAL)

# Import from parent instaloader package without modifying it
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instaloader
from websockets.asyncio.server import serve as ws_serve

import caption_graphic
import carousel_processor

# ── Globals set once at startup ─────────────────────────────────────────────

_loader: instaloader.Instaloader | None = None
_output_dir: Path = Path(".")
_authenticated: bool = False
_skip_post_process: bool = False
_skip_collage: bool = False
_skip_graphic: bool = False
_delete_originals: bool = False
_credentials: tuple[str, str, str | None] | None = None  # (sessionid, csrftoken, user_agent)

# ── Rate-limit safety (on top of instaloader's own per-request sleeps and 429 backoff) ──
_post_delay: tuple[float, float] = (3.0, 8.0)   # random pause between posts in a multi-post run
_max_batch: int = 100                           # most posts accepted in one request
_COOLDOWN_SECONDS = 15 * 60                     # refuse new downloads this long after Instagram pushes back
_cooldown_until: float = 0.0
# Instagram telling us to stop: repeated 429s, checkpoint/feedback_required, redirected to login.
_STOP_ERRORS = (
    instaloader.TooManyRequestsException,
    instaloader.AbortDownloadException,
    instaloader.LoginRequiredException,
)
# instaloader's RateController and session are not thread-safe; serialize auth + downloads.
_work_lock = threading.Lock()

_ALLOWED_ORIGIN_PREFIXES = ("chrome-extension://", "https://www.instagram.com")


def _origin_allowed(origin: str | None) -> bool:
    """Browsers always send Origin on WebSocket handshakes. Reject arbitrary web pages so a
    random site can't drive downloads (or push credentials) through this localhost server."""
    return origin is None or origin.startswith(_ALLOWED_ORIGIN_PREFIXES)


def _get_saved_dir_name() -> str:
    """Return saved posts directory name (Windows-safe: no colons allowed)."""
    # Windows doesn't allow ':' in directory names, so use '_saved' on Windows
    return "_saved" if platform.system() == "Windows" else ":saved"


# ── Session management ───────────────────────────────────────────────────────

def _user_id_from_sessionid(sessionid: str) -> int | None:
    """Instagram's sessionid cookie is "<user_id>:<token>:<version>:<hash>" (colons may be
    percent-encoded), so the numeric user id can be read straight out of it with no extra request."""
    match = re.match(r"^(\d+)[:%]", urllib.parse.unquote(sessionid))
    return int(match.group(1)) if match else None


def _valid_user_agent(user_agent: object) -> bool:
    """Accept only a plain browser-looking UA string (it ends up in an HTTP header)."""
    return (isinstance(user_agent, str) and user_agent.startswith("Mozilla/5.0")
            and len(user_agent) <= 300 and user_agent.isprintable())


def _authenticate(sessionid: str, csrftoken: str, user_agent: str | None = None) -> str:
    """Build an instaloader session from cookie values.

    user_agent, when given, is the real browser's UA so requests match the browser that owns the
    session instead of instaloader's generic one. It must be set *before* load_session(), which
    bakes the UA into the session headers.
    """
    global _authenticated, _credentials

    cookies = {
        "sessionid": sessionid,
        "csrftoken": csrftoken,
    }

    _authenticated = False
    if user_agent:
        _loader.context.user_agent = user_agent
    _loader.context.load_session("unknown", cookies)

    test_user = _loader.test_login()
    if not test_user:
        raise ValueError("Session cookies are invalid or expired. Re-copy from DevTools and try again.")

    _loader.context.username = test_user
    # login() normally sets this from Instagram's response; load_session() (raw cookies) doesn't,
    # so the iPhone comments endpoint (used for posts with many comments) sends "ig-intended-user-id: None"
    # and Instagram rejects it with a generic "fail" response. Parse it from the sessionid cookie instead
    # of querying the profile endpoint, which is a separate call that gets rate-limited (429) quickly.
    user_id = _user_id_from_sessionid(sessionid)
    if user_id is None:
        raise ValueError("Could not parse a user id out of --sessionid; make sure you copied the full cookie value.")
    _loader.context.user_id = user_id
    _authenticated = True
    _credentials = (sessionid, csrftoken, user_agent)
    print(f"[ig-dl] Authenticated as @{test_user}")
    return test_user


# ── Post metadata (caption + comments) ────────────────────────────────────

def _save_post_metadata(post, txt_path: Path) -> None:
    """Write caption and top 5 comments as JSON to txt_path."""
    caption = post.caption or ""

    top_comments = []
    try:
        for comment in itertools.islice(post.get_comments(), 5):
            top_comments.append({
                "username": comment.owner.username,
                "text": comment.text,
                "likes": comment.likes_count,
                "timestamp": comment.created_at_utc.isoformat(),
            })
    except Exception as exc:
        print(f"  [meta] Could not fetch comments: {exc}", file=sys.stderr)

    metadata = {
        "caption": caption,
        "top_comments": top_comments,
    }
    try:
        txt_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"  [meta] Could not write {txt_path.name}: {exc}", file=sys.stderr)


# ── Post-processing helpers ────────────────────────────────────────────────

_MEDIA_EXTS = [".jpg", ".jpeg", ".webp", ".mp4", ".mov"]


def _pause_between_posts() -> None:
    """Sleep a random human-ish interval so multi-post runs don't fire at machine cadence."""
    low, high = _post_delay
    if high > 0:
        time.sleep(random.uniform(low, high))


def _start_cooldown(reason: str) -> None:
    global _cooldown_until
    _cooldown_until = time.time() + _COOLDOWN_SECONDS
    print(f"[ig-dl] Instagram pushed back ({reason}). Pausing all downloads for "
          f"{_COOLDOWN_SECONDS // 60} minutes; avoid using Instagram heavily meanwhile.", file=sys.stderr)


def _downloaded_shortcodes(shortcodes: list[str]) -> list[str]:
    """Return the subset of shortcodes that already have output on disk.

    Looks one folder level under the output dir (<user>/, _saved/, #tag/). When snapshots are
    enabled a post only counts once its snapshot exists, so an interrupted download is retried;
    otherwise any media file for the shortcode counts.
    """
    need_snapshot = not (_skip_post_process or _skip_graphic)
    names: list[str] = []
    try:
        for sub in _output_dir.iterdir():
            if sub.is_dir():
                names.extend(p.name for p in sub.iterdir() if p.is_file())
    except OSError as exc:
        print(f"  [check] Could not scan {_output_dir}: {exc}", file=sys.stderr)
        return []

    def has_output(code: str) -> bool:
        if need_snapshot:
            snaps = (f"_{code}_snapshot.", f"_{code}_carousel_snapshot.")
            return any(s in n for n in names for s in snaps)
        return any(f"_{code}" in n and Path(n).suffix.lower() in _MEDIA_EXTS for n in names)

    return [code for code in shortcodes if has_output(code)]


def _maybe_create_snapshot(post, target_dir: Path) -> None:
    """Non-fatal: find the downloaded file and append a caption graphic."""
    if _skip_post_process or _skip_graphic:
        return
    txt_path = target_dir / f"{post.owner_username}_{post.shortcode}.txt"
    if not txt_path.exists():
        return
    for ext in _MEDIA_EXTS:
        candidate = target_dir / f"{post.owner_username}_{post.shortcode}{ext}"
        if candidate.exists():
            snap_suffix = ".mp4" if ext in (".mp4", ".mov") else ".jpg"
            snapshot_path = target_dir / f"{post.owner_username}_{post.shortcode}_snapshot{snap_suffix}"
            try:
                result = caption_graphic.create_snapshot(candidate, txt_path, snapshot_path)
                if result:
                    print(f"  Snapshot: {result.name}")
                    if _delete_originals:
                        try:
                            candidate.unlink()
                            txt_path.unlink()
                        except OSError as exc:
                            print(f"  [snapshot] Could not delete originals: {exc}", file=sys.stderr)
            except caption_graphic.CaptionGraphicError as exc:
                print(f"  [snapshot] ERROR: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"  [snapshot] Unexpected error: {exc}", file=sys.stderr)
            return


def _stamp_download_order(target_dir: Path, post, order_ts: float) -> None:
    """Set mtime of every file belonging to this post to order_ts.

    download_post() (and our own snapshot/carousel steps) leave each file's mtime set to
    the post's original Instagram creation date, which has nothing to do with the order
    posts were saved/downloaded in. Overwriting it with a strictly increasing per-download
    timestamp means sorting a folder by "Date Modified" in a file explorer reproduces the
    exact download order (== the order posts appeared on the source page at download time).
    """
    for f in target_dir.glob(f"{post.owner_username}_{post.shortcode}*"):
        try:
            os.utime(f, (order_ts, order_ts))
        except OSError as exc:
            print(f"  [order] Could not set mtime on {f.name}: {exc}", file=sys.stderr)


def _maybe_process_carousel(post, target_dir: Path) -> None:
    """Non-fatal carousel post-processing step."""
    if _skip_post_process:
        return
    try:
        result = carousel_processor.process_carousel(
            post, target_dir,
            make_collage=not _skip_collage,
            make_graphic=not _skip_graphic,
            delete_originals=_delete_originals,
        )
        if result:
            print(f"  Carousel: {result.name}")
        elif post.typename == "GraphSidecar":
            print(f"  [carousel] Skipped (single item or no files found)")
    except carousel_processor.CarouselProcessingError as exc:
        print(f"  [carousel] ERROR: {exc}", file=sys.stderr)
    except Exception as exc:
        print(f"  [carousel] Unexpected error: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)


# ── Download handlers ────────────────────────────────────────────────────────

def _handle_profile(identifier: str, count: int, skip: int = 0) -> dict:
    profile = instaloader.Profile.from_username(_loader.context, identifier)
    available = max(0, profile.mediacount - skip)
    actual = min(count, available)
    target_dir = _output_dir / profile.username
    print(f"  Profile : {profile.full_name} (@{profile.username}), {profile.mediacount} posts")
    print(f"  Target  : {target_dir}  ({actual} posts, skipped {skip})")
    downloaded = 0
    order_ts = time.time()
    for post in itertools.islice(profile.get_posts(), skip, skip + count):
        if downloaded:
            _pause_between_posts()
        downloaded += 1
        print(f"  [{downloaded}/{actual}] {post.shortcode}", end="  ")
        _loader.download_post(post, target=profile.username)
        txt_path = target_dir / f"{post.owner_username}_{post.shortcode}.txt"
        _save_post_metadata(post, txt_path)
        _maybe_create_snapshot(post, target_dir)
        _maybe_process_carousel(post, target_dir)
        _stamp_download_order(target_dir, post, order_ts + downloaded)
        print()
    return {"downloaded": downloaded, "target": str(target_dir)}


def _handle_saved(count: int, skip: int = 0) -> dict:
    profile = instaloader.Profile.own_profile(_loader.context)
    folder_name = _get_saved_dir_name()
    target_dir = _output_dir / folder_name
    print(f"  Saved posts for @{profile.username} (count={count}, skip={skip})")
    downloaded = 0
    order_ts = time.time()
    for post in itertools.islice(profile.get_saved_posts(), skip, skip + count):
        if downloaded:
            _pause_between_posts()
        downloaded += 1
        print(f"  [{downloaded}] {post.shortcode}", end="  ")
        _loader.download_post(post, target=folder_name)
        txt_path = target_dir / f"{post.owner_username}_{post.shortcode}.txt"
        _save_post_metadata(post, txt_path)
        _maybe_create_snapshot(post, target_dir)
        _maybe_process_carousel(post, target_dir)
        # Saved posts are downloaded in the same order they appear on your Saved page
        # (most recently saved first), so stamping mtime by download sequence means
        # sorting the folder by "Date Modified" reproduces that same order.
        _stamp_download_order(target_dir, post, order_ts + downloaded)
        print()
    return {"downloaded": downloaded, "target": str(target_dir)}


def _handle_collection(identifiers: list, count: int, skip: int = 0, force: bool = False) -> dict:
    folder_name = _get_saved_dir_name()
    target_dir = _output_dir / folder_name
    to_download = identifiers[skip : skip + count]
    already = set() if force else set(_downloaded_shortcodes(to_download))
    skipped = len(already)
    to_download = [code for code in to_download if code not in already]
    print(f"  Collection: downloading {len(to_download)} shortcodes to {target_dir} "
          f"(skipped {skip}, {skipped} already downloaded)")
    downloaded = 0
    aborted = None
    order_ts = time.time()
    for index, shortcode in enumerate(to_download):
        if index:
            _pause_between_posts()
        try:
            post = instaloader.Post.from_shortcode(_loader.context, shortcode)
            print(f"  [{downloaded+1}] {post.shortcode}", end="  ")
            _loader.download_post(post, target=folder_name)
            txt_path = target_dir / f"{post.owner_username}_{post.shortcode}.txt"
            _save_post_metadata(post, txt_path)
            _maybe_create_snapshot(post, target_dir)
            _maybe_process_carousel(post, target_dir)
            downloaded += 1
            _stamp_download_order(target_dir, post, order_ts + downloaded)
            print()
        except _STOP_ERRORS as e:
            # Don't keep hammering Instagram once it says stop.
            aborted = f"{type(e).__name__}: {e}"
            print(f"\nStopping batch at {shortcode}: {aborted}", file=sys.stderr)
            _start_cooldown(type(e).__name__)
            break
        except Exception as e:
            print(f"\nError downloading {shortcode}: {e}")
    result = {"downloaded": downloaded, "skipped": skipped, "target": str(target_dir)}
    if aborted:
        result["aborted"] = aborted
    return result


def _handle_hashtag(identifier: str, count: int, skip: int = 0) -> dict:
    tag = identifier.lstrip("#")
    folder_name = f"#{tag}"
    hashtag = instaloader.Hashtag.from_name(_loader.context, tag)
    target_dir = _output_dir / folder_name
    print(f"  Hashtag : #{tag}  ->  {target_dir} (count={count}, skip={skip})")
    downloaded = 0
    order_ts = time.time()
    for post in itertools.islice(hashtag.get_posts(), skip, skip + count):
        if downloaded:
            _pause_between_posts()
        downloaded += 1
        print(f"  [{downloaded}/{count}] {post.shortcode}", end="  ")
        _loader.download_post(post, target=folder_name)
        txt_path = target_dir / f"{post.owner_username}_{post.shortcode}.txt"
        _save_post_metadata(post, txt_path)
        _maybe_create_snapshot(post, target_dir)
        _maybe_process_carousel(post, target_dir)
        _stamp_download_order(target_dir, post, order_ts + downloaded)
        print()
    return {"downloaded": downloaded, "target": str(target_dir)}


def _handle_post(identifier: str, force: bool = False) -> dict:
    if not force and _downloaded_shortcodes([identifier]):
        print(f"  Post    : {identifier} already downloaded, skipping")
        return {"downloaded": 0, "skipped": 1, "target": str(_output_dir)}
    post = instaloader.Post.from_shortcode(_loader.context, identifier)
    folder_name = post.owner_username
    target_dir = _output_dir / folder_name
    print(f"  Post    : {identifier}  ({post.typename})")
    _loader.download_post(post, target=folder_name)
    txt_path = target_dir / f"{post.owner_username}_{post.shortcode}.txt"
    _save_post_metadata(post, txt_path)
    _maybe_create_snapshot(post, target_dir)
    _maybe_process_carousel(post, target_dir)
    _stamp_download_order(target_dir, post, time.time())
    return {"downloaded": 1, "skipped": 0, "target": str(target_dir)}


ROUTES = {
    "profile":  lambda d, c, s: _handle_profile(d["identifier"], c, s),
    "saved":    lambda d, c, s: _handle_saved(c, s),
    "hashtag":  lambda d, c, s: _handle_hashtag(d["identifier"], c, s),
    "post":     lambda d, c, s: _handle_post(d["identifier"], bool(d.get("force"))),
    "collection": lambda d, c, s: _handle_collection(d["identifiers"], c, s, bool(d.get("force"))),
}


# ── WebSocket handler ────────────────────────────────────────────────────────

async def handle_client(websocket):
    async def send(payload: dict):
        await websocket.send(json.dumps(payload))

    # Send initial status
    await send({
        "type": "status",
        "authenticated": _authenticated,
        "logged_in_as": _loader.context.username if _authenticated else None,
        "output": str(_output_dir.resolve()),
    })

    async for raw in websocket:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await send({"type": "error", "error": "Invalid JSON"})
            continue

        msg_type = msg.get("type")

        # ── Auth (sent by the browser extension, straight from the cookie jar) ──
        if msg_type == "auth":
            sessionid, csrftoken = msg.get("sessionid"), msg.get("csrftoken")
            if not (isinstance(sessionid, str) and sessionid and isinstance(csrftoken, str) and csrftoken):
                await send({"type": "error", "error": "auth needs sessionid and csrftoken"})
                continue

            user_agent = msg.get("user_agent")
            if not _valid_user_agent(user_agent):
                user_agent = None  # keep instaloader's default rather than send something odd

            def do_auth():
                with _work_lock:
                    # Same cookies + UA => already authenticated; avoids a needless Instagram call.
                    if _authenticated and _credentials == (sessionid, csrftoken, user_agent):
                        return _loader.context.username
                    return _authenticate(sessionid, csrftoken, user_agent)

            try:
                user = await asyncio.get_event_loop().run_in_executor(None, do_auth)
                await send({"type": "auth_ok", "logged_in_as": user, "output": str(_output_dir)})
            except Exception as exc:
                await send({"type": "error", "error": str(exc)})

        # ── Which of these shortcodes are already on disk? (no auth needed) ────
        elif msg_type == "check":
            codes = msg.get("shortcodes")
            if not (isinstance(codes, list) and len(codes) <= 2000
                    and all(isinstance(c, str) and re.fullmatch(r"[A-Za-z0-9_-]+", c) for c in codes)):
                await send({"type": "error", "error": "check needs a list of shortcodes"})
                continue
            found = await asyncio.get_event_loop().run_in_executor(None, _downloaded_shortcodes, codes)
            await send({"type": "checked", "downloaded": found})

        # ── Download ──────────────────────────────────────────────────────────
        elif msg_type == "download":
            if not _authenticated:
                await send({"type": "error", "error": "Not authenticated. Log in to Instagram in Chrome "
                                                      "and use the extension, or start with --sessionid/--csrftoken."})
                continue

            page_type = msg.get("page_type")
            count = int(msg.get("count", 10))
            skip = int(msg.get("skip", 0))

            if page_type not in ROUTES:
                await send({"type": "error", "error": f"Unknown page type: {page_type!r}"})
                continue

            wait_left = _cooldown_until - time.time()
            if wait_left > 0:
                await send({"type": "error", "error": f"Cooling down after an Instagram rate-limit/"
                                                      f"challenge response; try again in {int(wait_left // 60) + 1} min."})
                continue

            batch = len(msg["identifiers"][skip:skip + count]) if page_type == "collection" \
                and isinstance(msg.get("identifiers"), list) else count
            if batch > _max_batch:
                await send({"type": "error", "error": f"Batch of {batch} exceeds --max-batch {_max_batch}; "
                                                      f"download fewer posts at a time."})
                continue

            print(f"\n[ig-dl] {page_type.upper()}"
                  + (f" -> {msg.get('identifier')}" if msg.get("identifier") else "")
                  + f"  (count={count}, skip={skip})")

            try:
                def run_route():
                    with _work_lock:
                        return ROUTES[page_type](msg, count, skip)

                result = await asyncio.get_event_loop().run_in_executor(None, run_route)
                await send({"type": "done", **result})
            except Exception as exc:
                traceback.print_exc()
                if isinstance(exc, _STOP_ERRORS):
                    _start_cooldown(type(exc).__name__)
                await send({"type": "error", "error": str(exc)})

        else:
            await send({"type": "error", "error": f"Unknown message type: {msg_type!r}"})


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _loader, _output_dir

    parser = argparse.ArgumentParser(
        description="download-insta-tab WebSocket server",
        epilog=(
            "Get cookie values from Chrome DevTools:\n"
            "  F12 → Application → Cookies → https://www.instagram.com\n"
            "  Copy the values of 'sessionid' and 'csrftoken'"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--sessionid",
                        help="Instagram sessionid cookie value (optional when using the Chrome "
                             "extension, which sends cookies itself)")
    parser.add_argument("--csrftoken",
                        help="Instagram csrftoken cookie value (optional with the extension)")
    parser.add_argument("--output", "-o", default=".",
                        help="Root directory for downloads (default: current dir)")
    parser.add_argument("--port", "-p", type=int, default=7432,
                        help="Port to listen on (default: 7432)")
    parser.add_argument("--filename-pattern", default="{owner_username}_{shortcode}",
                        help="Filename pattern for downloads (default: {owner_username}_{shortcode}). "
                             "Available: {date}, {owner_username}, {shortcode}, etc.")
    parser.add_argument("--no-post-process", action="store_true",
                        help="Skip all post-processing (collage + caption graphic)")
    parser.add_argument("--no-collage", action="store_true",
                        help="Skip carousel collage/concat")
    parser.add_argument("--no-graphic", action="store_true",
                        help="Skip caption graphic snapshot")
    parser.add_argument("--delete-originals", action="store_true",
                        help="Delete original media/composite and .txt once a snapshot is "
                             "created, keeping only the final _snapshot file per post")
    parser.add_argument("--post-delay", nargs=2, type=float, metavar=("MIN", "MAX"), default=(3.0, 8.0),
                        help="Random pause in seconds between posts in a multi-post run "
                             "(default: 3 8; use 0 0 to disable). Added on top of instaloader's own "
                             "per-request sleeps.")
    parser.add_argument("--max-batch", type=int, default=100,
                        help="Refuse requests for more than this many posts at once (default: 100)")
    args = parser.parse_args()

    global _skip_post_process, _skip_collage, _skip_graphic, _delete_originals
    global _post_delay, _max_batch
    if args.post_delay[0] < 0 or args.post_delay[1] < args.post_delay[0]:
        parser.error("--post-delay needs 0 <= MIN <= MAX")
    if args.max_batch < 1:
        parser.error("--max-batch must be at least 1")
    _post_delay = (args.post_delay[0], args.post_delay[1])
    _max_batch = args.max_batch
    _skip_post_process = args.no_post_process
    _skip_collage      = args.no_collage
    _skip_graphic      = args.no_graphic
    _delete_originals  = args.delete_originals

    if _delete_originals and (_skip_post_process or _skip_graphic):
        print("[ig-dl] WARNING: --delete-originals has no effect without snapshots "
              "(--no-post-process/--no-graphic disables snapshot creation)", file=sys.stderr)

    _output_dir = Path(args.output).expanduser().resolve()
    _output_dir.mkdir(parents=True, exist_ok=True)

    # Debug: show what path was resolved
    print(f"[ig-dl] Input path: {args.output}")
    print(f"[ig-dl] Resolved to: {_output_dir}")

    # Embed the absolute output path in dirname_pattern so instaloader never
    # sanitizes the base path (it only sanitizes the {target} substitution).
    # On Windows, passing a full path as `target` causes colons and backslashes
    # to be replaced with full-width lookalikes, which breaks path resolution.
    _loader = instaloader.Instaloader(
        dirname_pattern=str(_output_dir / "{target}"),
        filename_pattern=args.filename_pattern,
        download_video_thumbnails=False,
        save_metadata=False,
        post_metadata_txt_pattern="",  # we write our own JSON txt
    )

    if bool(args.sessionid) != bool(args.csrftoken):
        parser.error("--sessionid and --csrftoken must be given together")

    if args.sessionid:
        try:
            _authenticate(args.sessionid, args.csrftoken)
        except Exception as exc:
            print(f"[ig-dl] ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        print("[ig-dl] No cookies given; waiting for the Chrome extension to authenticate.")

    print(f"[ig-dl] Output : {_output_dir}")
    print(f"[ig-dl] Listening on ws://localhost:{args.port}")
    print(f"[ig-dl] Use the Chrome extension, or paste console.js in an Instagram tab and call igdl()\n")

    async def process_request(connection, request):
        """Handle Chrome's Private Network Access preflight, and reject foreign origins."""
        origin = request.headers.get("Origin")
        if not _origin_allowed(origin):
            return connection.respond(http.HTTPStatus.FORBIDDEN, "Origin not allowed\n")
        if request.headers.get("Access-Control-Request-Private-Network") == "true":
            return connection.respond(
                http.HTTPStatus.NO_CONTENT,
                [
                    ("Access-Control-Allow-Origin", origin or "*"),
                    ("Access-Control-Allow-Private-Network", "true"),
                ],
            )

    async def run():
        async with ws_serve(handle_client, "127.0.0.1", args.port,
                            process_request=process_request):
            # Run indefinitely, but allow cancellation
            try:
                await asyncio.sleep(float('inf'))
            except asyncio.CancelledError:
                pass

    import signal

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task = None

    def handle_shutdown(sig=None, frame=None):
        if main_task and not main_task.done():
            main_task.cancel()

    signal.signal(signal.SIGINT, handle_shutdown)

    try:
        main_task = loop.create_task(run())
        loop.run_until_complete(main_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[ig-dl] Stopped.")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
