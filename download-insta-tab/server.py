#!/usr/bin/env python3
"""
ig-dl local server — bridges your browser to instaloader.

Run once, leave it running, then paste console.js into any Instagram page.

Usage:
    python ig-dl/server.py [options]

Options:
    --browser   Browser to pull session from (default: chrome)
    --output    Directory to save downloads into (default: current dir)
    --port      Port to listen on (default: 7432)
"""

import argparse
import itertools
import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Import from parent instaloader package without modifying it
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instaloader
from instaloader.__main__ import import_session

# ── Globals set once at startup ─────────────────────────────────────────────

_loader: instaloader.Instaloader | None = None
_output_dir: Path = Path(".")


# ── Page-type handlers ───────────────────────────────────────────────────────

def _handle_profile(identifier: str, count: int) -> dict:
    """Download top N posts from a public or followed private profile."""
    profile = instaloader.Profile.from_username(_loader.context, identifier)
    actual = min(count, profile.mediacount)
    print(f"  Profile : {profile.full_name} (@{profile.username}), {profile.mediacount} posts")
    print(f"  Target  : {_output_dir / profile.username}  ({actual} posts)")
    downloaded = 0
    for post in itertools.islice(profile.get_posts(), actual):
        downloaded += 1
        print(f"  [{downloaded}/{actual}] {post.shortcode}", end="  ")
        _loader.download_post(post, target=str(_output_dir / profile.username))
        print()
    return {"downloaded": downloaded, "target": str(_output_dir / profile.username)}


def _handle_saved(count: int) -> dict:
    """Download top N posts from your own saved collection."""
    profile = instaloader.Profile.own_profile(_loader.context)
    print(f"  Saved posts for @{profile.username}")
    target = str(_output_dir / ":saved")
    downloaded = 0
    posts = profile.get_saved_posts()
    for post in itertools.islice(posts, count):
        downloaded += 1
        print(f"  [{downloaded}] {post.shortcode}", end="  ")
        _loader.download_post(post, target=target)
        print()
    return {"downloaded": downloaded, "target": target}


def _handle_hashtag(identifier: str, count: int) -> dict:
    """Download top N posts from a hashtag page."""
    tag = identifier.lstrip("#")
    hashtag = instaloader.Hashtag.from_name(_loader.context, tag)
    target = str(_output_dir / f"#{tag}")
    print(f"  Hashtag : #{tag}")
    print(f"  Target  : {target}")
    downloaded = 0
    for post in itertools.islice(hashtag.get_posts(), count):
        downloaded += 1
        print(f"  [{downloaded}/{count}] {post.shortcode}", end="  ")
        _loader.download_post(post, target=target)
        print()
    return {"downloaded": downloaded, "target": target}


def _handle_post(identifier: str) -> dict:
    """Download a single post by shortcode."""
    post = instaloader.Post.from_shortcode(_loader.context, identifier)
    target = str(_output_dir / post.owner_username)
    print(f"  Post    : {identifier}  ({post.typename})")
    _loader.download_post(post, target=target)
    return {"downloaded": 1, "target": target}


# ── HTTP handler ─────────────────────────────────────────────────────────────

ROUTES = {
    "profile":  lambda d, n: _handle_profile(d["identifier"], n),
    "saved":    lambda d, n: _handle_saved(n),
    "hashtag":  lambda d, n: _handle_hashtag(d["identifier"], n),
    "post":     lambda d, n: _handle_post(d["identifier"]),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress default access log
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/status":
            username = _loader.context.username if _loader else None
            self._respond(200, {"ok": True, "logged_in_as": username, "output": str(_output_dir.resolve())})
        else:
            self._respond(404, {"error": "Not found"})

    def do_POST(self):
        if self.path != "/download":
            self._respond(404, {"error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._respond(400, {"error": "Invalid JSON body"})
            return

        page_type = body.get("type")
        count = int(body.get("count", 10))

        if page_type not in ROUTES:
            self._respond(400, {"error": f"Unknown page type: {page_type!r}. Supported: {list(ROUTES)}"})
            return

        print(f"\n[ig-dl] {page_type.upper()}"
              + (f" → {body.get('identifier')}" if body.get("identifier") else "")
              + f"  (count={count})")

        try:
            result = ROUTES[page_type](body, count)
            self._respond(200, {"ok": True, **result})
        except Exception as exc:
            traceback.print_exc()
            self._respond(500, {"error": str(exc)})

    def _respond(self, status: int, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _loader, _output_dir

    parser = argparse.ArgumentParser(description="ig-dl local server")
    parser.add_argument("--browser", "-b", default="chrome",
                        choices=["brave", "chrome", "chromium", "edge", "firefox", "librewolf",
                                 "opera", "opera_gx", "safari", "vivaldi"],
                        help="Browser to pull Instagram session from (default: chrome)")
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

    print(f"[ig-dl] Loading session from {args.browser}...")
    try:
        import_session(args.browser, _loader, cookiefile=None)
    except instaloader.LoginException as exc:
        print(f"[ig-dl] ERROR: {exc}", file=sys.stderr)
        print(f"[ig-dl] Make sure you are logged in to Instagram in {args.browser}.", file=sys.stderr)
        sys.exit(1)

    print(f"[ig-dl] Output : {_output_dir}")
    print(f"[ig-dl] Listening on http://localhost:{args.port}  (Ctrl+C to stop)\n")

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ig-dl] Stopped.")


if __name__ == "__main__":
    main()
