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
import sys
import traceback
from pathlib import Path

# Suppress "opening handshake failed" noise from browsers probing the port
logging.getLogger("websockets").setLevel(logging.CRITICAL)

# Import from parent instaloader package without modifying it
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instaloader
from websockets.asyncio.server import serve as ws_serve

# ── Globals set once at startup ─────────────────────────────────────────────

_loader: instaloader.Instaloader | None = None
_output_dir: Path = Path(".")
_authenticated: bool = False


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


# ── Download handlers ────────────────────────────────────────────────────────

def _handle_profile(identifier: str, count: int) -> dict:
    profile = instaloader.Profile.from_username(_loader.context, identifier)
    actual = min(count, profile.mediacount)
    target = str(_output_dir / profile.username)
    print(f"  Profile : {profile.full_name} (@{profile.username}), {profile.mediacount} posts")
    print(f"  Target  : {target}  ({actual} posts)")
    downloaded = 0
    for post in itertools.islice(profile.get_posts(), actual):
        downloaded += 1
        print(f"  [{downloaded}/{actual}] {post.shortcode}", end="  ")
        _loader.download_post(post, target=target)
        print()
    return {"downloaded": downloaded, "target": target}


def _handle_saved(count: int) -> dict:
    profile = instaloader.Profile.own_profile(_loader.context)
    target = str(_output_dir / ":saved")
    print(f"  Saved posts for @{profile.username}")
    downloaded = 0
    for post in itertools.islice(profile.get_saved_posts(), count):
        downloaded += 1
        print(f"  [{downloaded}] {post.shortcode}", end="  ")
        _loader.download_post(post, target=target)
        print()
    return {"downloaded": downloaded, "target": target}


def _handle_hashtag(identifier: str, count: int) -> dict:
    tag = identifier.lstrip("#")
    hashtag = instaloader.Hashtag.from_name(_loader.context, tag)
    target = str(_output_dir / f"#{tag}")
    print(f"  Hashtag : #{tag}  →  {target}")
    downloaded = 0
    for post in itertools.islice(hashtag.get_posts(), count):
        downloaded += 1
        print(f"  [{downloaded}/{count}] {post.shortcode}", end="  ")
        _loader.download_post(post, target=target)
        print()
    return {"downloaded": downloaded, "target": target}


def _handle_post(identifier: str) -> dict:
    post = instaloader.Post.from_shortcode(_loader.context, identifier)
    target = str(_output_dir / post.owner_username)
    print(f"  Post    : {identifier}  ({post.typename})")
    _loader.download_post(post, target=target)
    return {"downloaded": 1, "target": target}


ROUTES = {
    "profile":  lambda d, n: _handle_profile(d["identifier"], n),
    "saved":    lambda d, n: _handle_saved(n),
    "hashtag":  lambda d, n: _handle_hashtag(d["identifier"], n),
    "post":     lambda d, n: _handle_post(d["identifier"]),
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

            if page_type not in ROUTES:
                await send({"type": "error", "error": f"Unknown page type: {page_type!r}"})
                continue

            print(f"\n[ig-dl] {page_type.upper()}"
                  + (f" → {msg.get('identifier')}" if msg.get("identifier") else "")
                  + f"  (count={count})")

            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: ROUTES[page_type](msg, count)
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
    args = parser.parse_args()

    _output_dir = Path(args.output).resolve()
    _output_dir.mkdir(parents=True, exist_ok=True)

    _loader = instaloader.Instaloader(
        download_video_thumbnails=False,
        save_metadata=False,
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
            await asyncio.get_running_loop().create_future()  # run forever

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[ig-dl] Stopped.")


if __name__ == "__main__":
    main()
