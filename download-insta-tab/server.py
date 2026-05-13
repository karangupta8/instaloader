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
import platform
import sys
import traceback
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


def _get_saved_dir_name() -> str:
    """Return saved posts directory name (Windows-safe: no colons allowed)."""
    # Windows doesn't allow ':' in directory names, so use '_saved' on Windows
    return "_saved" if platform.system() == "Windows" else ":saved"


# ── Session management ───────────────────────────────────────────────────────

def _authenticate(sessionid: str, csrftoken: str) -> str:
    """Build an instaloader session from cookie values."""
    global _authenticated

    cookies = {
        "sessionid": sessionid,
        "csrftoken": csrftoken,
    }

    _loader.context.load_session("unknown", cookies)

    test_user = _loader.test_login()
    if not test_user:
        raise ValueError("Session cookies are invalid or expired. Re-copy from DevTools and try again.")

    _loader.context.username = test_user
    _authenticated = True
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
            except caption_graphic.CaptionGraphicError as exc:
                print(f"  [snapshot] ERROR: {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"  [snapshot] Unexpected error: {exc}", file=sys.stderr)
            return


def _maybe_process_carousel(post, target_dir: Path) -> None:
    """Non-fatal carousel post-processing step."""
    if _skip_post_process:
        return
    try:
        result = carousel_processor.process_carousel(
            post, target_dir,
            make_collage=not _skip_collage,
            make_graphic=not _skip_graphic,
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
    for post in itertools.islice(profile.get_posts(), skip, skip + count):
        downloaded += 1
        print(f"  [{downloaded}/{actual}] {post.shortcode}", end="  ")
        _loader.download_post(post, target=profile.username)
        txt_path = target_dir / f"{post.owner_username}_{post.shortcode}.txt"
        _save_post_metadata(post, txt_path)
        _maybe_create_snapshot(post, target_dir)
        _maybe_process_carousel(post, target_dir)
        print()
    return {"downloaded": downloaded, "target": str(target_dir)}


def _handle_saved(count: int, skip: int = 0) -> dict:
    profile = instaloader.Profile.own_profile(_loader.context)
    folder_name = _get_saved_dir_name()
    target_dir = _output_dir / folder_name
    print(f"  Saved posts for @{profile.username} (count={count}, skip={skip})")
    downloaded = 0
    for post in itertools.islice(profile.get_saved_posts(), skip, skip + count):
        downloaded += 1
        print(f"  [{downloaded}] {post.shortcode}", end="  ")
        _loader.download_post(post, target=folder_name)
        txt_path = target_dir / f"{post.owner_username}_{post.shortcode}.txt"
        _save_post_metadata(post, txt_path)
        _maybe_create_snapshot(post, target_dir)
        _maybe_process_carousel(post, target_dir)
        print()
    return {"downloaded": downloaded, "target": str(target_dir)}


def _handle_hashtag(identifier: str, count: int, skip: int = 0) -> dict:
    tag = identifier.lstrip("#")
    folder_name = f"#{tag}"
    hashtag = instaloader.Hashtag.from_name(_loader.context, tag)
    target_dir = _output_dir / folder_name
    print(f"  Hashtag : #{tag}  ->  {target_dir} (count={count}, skip={skip})")
    downloaded = 0
    for post in itertools.islice(hashtag.get_posts(), skip, skip + count):
        downloaded += 1
        print(f"  [{downloaded}/{count}] {post.shortcode}", end="  ")
        _loader.download_post(post, target=folder_name)
        txt_path = target_dir / f"{post.owner_username}_{post.shortcode}.txt"
        _save_post_metadata(post, txt_path)
        _maybe_create_snapshot(post, target_dir)
        _maybe_process_carousel(post, target_dir)
        print()
    return {"downloaded": downloaded, "target": str(target_dir)}


def _handle_post(identifier: str) -> dict:
    post = instaloader.Post.from_shortcode(_loader.context, identifier)
    folder_name = post.owner_username
    target_dir = _output_dir / folder_name
    print(f"  Post    : {identifier}  ({post.typename})")
    _loader.download_post(post, target=folder_name)
    txt_path = target_dir / f"{post.owner_username}_{post.shortcode}.txt"
    _save_post_metadata(post, txt_path)
    _maybe_create_snapshot(post, target_dir)
    _maybe_process_carousel(post, target_dir)
    return {"downloaded": 1, "target": str(target_dir)}


ROUTES = {
    "profile":  lambda d, c, s: _handle_profile(d["identifier"], c, s),
    "saved":    lambda d, c, s: _handle_saved(c, s),
    "hashtag":  lambda d, c, s: _handle_hashtag(d["identifier"], c, s),
    "post":     lambda d, c, s: _handle_post(d["identifier"]),
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

        # ── Download ──────────────────────────────────────────────────────────
        if msg_type == "download":
            if not _authenticated:
                await send({"type": "error", "error": "Not authenticated. Call igdl() from an Instagram page."})
                continue

            page_type = msg.get("page_type")
            count = int(msg.get("count", 10))
            skip = int(msg.get("skip", 0))

            if page_type not in ROUTES:
                await send({"type": "error", "error": f"Unknown page type: {page_type!r}"})
                continue

            print(f"\n[ig-dl] {page_type.upper()}"
                  + (f" -> {msg.get('identifier')}" if msg.get("identifier") else "")
                  + f"  (count={count}, skip={skip})")

            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: ROUTES[page_type](msg, count, skip)
                )
                await send({"type": "done", **result})
            except Exception as exc:
                traceback.print_exc()
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
    parser.add_argument("--sessionid", required=True,
                        help="Instagram sessionid cookie value")
    parser.add_argument("--csrftoken", required=True,
                        help="Instagram csrftoken cookie value")
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
    args = parser.parse_args()

    global _skip_post_process, _skip_collage, _skip_graphic
    _skip_post_process = args.no_post_process
    _skip_collage      = args.no_collage
    _skip_graphic      = args.no_graphic

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

    try:
        _authenticate(args.sessionid, args.csrftoken)
    except Exception as exc:
        print(f"[ig-dl] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"[ig-dl] Output : {_output_dir}")
    print(f"[ig-dl] Listening on ws://localhost:{args.port}")
    print(f"[ig-dl] Paste console.js in any Instagram tab and call igdl()\n")

    async def process_request(connection, request):
        """Handle Chrome's Private Network Access preflight before WebSocket upgrade."""
        if request.headers.get("Access-Control-Request-Private-Network") == "true":
            return connection.respond(
                http.HTTPStatus.NO_CONTENT,
                [
                    ("Access-Control-Allow-Origin", request.headers.get("Origin", "*")),
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
