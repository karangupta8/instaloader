#!/usr/bin/env python3
"""
download-insta-tab server: bridges your browser to instaloader via WebSocket.

The Chrome extension (extension/) talks to this server; so does legacy/console.js.
Instagram's CSP blocks HTTP/HTTPS to localhost but allows ws://localhost:*, hence WebSocket.

Usage:
    python download-insta-tab/server.py [--output DIR] [options]

Options can also live in download-insta-tab/config.toml (see config.example.toml);
command-line flags override the file.
"""

import argparse
import asyncio
import http
import json
import logging
import sys
import threading
import traceback
from pathlib import Path

# Suppress "opening handshake failed" noise from browsers probing the port
logging.getLogger("websockets").setLevel(logging.CRITICAL)

from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed

import session
from pipeline import DEFAULT_FILENAME_PATTERN, Downloader, RequestRefused, Settings

import instaloader  # noqa: E402  (importable once pipeline has put the parent package on sys.path)

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.toml"
INSTAGRAM_ORIGIN = "https://www.instagram.com"
EXTENSION_ORIGIN_PREFIX = "chrome-extension://"


def origin_allowed(origin: str | None, extension_id: str | None = None) -> bool:
    """Browsers always send Origin on WebSocket handshakes. Reject arbitrary web pages so a
    random site can't drive downloads (or push credentials) through this localhost server.
    With extension_id set, only that one extension is accepted."""
    if origin is None or origin == INSTAGRAM_ORIGIN:
        return True
    if origin.startswith(EXTENSION_ORIGIN_PREFIX):
        return extension_id is None or origin == f"{EXTENSION_ORIGIN_PREFIX}{extension_id}"
    return False


# ── Login state ──────────────────────────────────────────────────────────────

class LoginState:
    """Tracks which cookies/browser identity the shared instaloader session was built from."""

    def __init__(self, loader, downloader: Downloader):
        self.loader = loader
        self.downloader = downloader
        self.authenticated = False
        self._key: tuple | None = None

    @property
    def username(self) -> str | None:
        return self.loader.context.username if self.authenticated else None

    def login(self, sessionid: str, csrftoken: str, user_agent: str | None = None,
              client_headers: dict[str, str] | None = None) -> str:
        key = (sessionid, csrftoken, user_agent, tuple(sorted((client_headers or {}).items())))
        # Same cookies + browser identity => already logged in; skips an Instagram request.
        if self.authenticated and self._key == key:
            return self.loader.context.username
        with self.downloader.lock:
            self.authenticated = False
            username = session.authenticate(self.loader, sessionid, csrftoken, user_agent, client_headers)
            self.authenticated, self._key = True, key
        print(f"[ig-dl] Authenticated as @{username}")
        return username


# ── WebSocket handler ────────────────────────────────────────────────────────

def make_handler(downloader: Downloader, state: LoginState):
    output = str(downloader.settings.output_dir)

    async def handle_client(websocket):
        loop = asyncio.get_running_loop()
        cancel: threading.Event | None = None  # of the download running on this connection
        active: asyncio.Task | None = None

        async def send(payload: dict) -> None:
            try:
                await websocket.send(json.dumps(payload))
            except ConnectionClosed:
                pass  # client went away; the batch is cancelled in the finally below

        def progress(update: dict) -> None:  # called from the download thread
            asyncio.run_coroutine_threadsafe(send({"type": "progress", **update}), loop)

        async def run_download(msg: dict, token: threading.Event) -> None:
            try:
                result = await loop.run_in_executor(None, lambda: downloader.download(msg, token, progress))
                await send({"type": "done", **result})
            except RequestRefused as exc:
                print(f"[ig-dl] Refused: {exc}", file=sys.stderr)
                await send({"type": "error", "error": str(exc)})
            except Exception as exc:
                traceback.print_exc()
                await send({"type": "error", "error": str(exc)})

        await send({"type": "status", "authenticated": state.authenticated,
                    "logged_in_as": state.username, "output": output})
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await send({"type": "error", "error": "Invalid JSON"})
                    continue
                if not isinstance(msg, dict):
                    await send({"type": "error", "error": "Expected a JSON object"})
                    continue

                msg_type = msg.get("type")

                if msg_type == "auth":  # sent by the extension, straight from Chrome's cookie jar
                    sessionid, csrftoken = msg.get("sessionid"), msg.get("csrftoken")
                    if not (isinstance(sessionid, str) and sessionid and isinstance(csrftoken, str) and csrftoken):
                        await send({"type": "error", "error": "auth needs sessionid and csrftoken"})
                        continue
                    user_agent = msg.get("user_agent")
                    if not session.valid_user_agent(user_agent):
                        user_agent = None  # keep instaloader's default rather than send something odd
                    hints = session.clean_client_headers(msg.get("client_headers"))
                    try:
                        user = await loop.run_in_executor(
                            None, lambda: state.login(sessionid, csrftoken, user_agent, hints))
                        await send({"type": "auth_ok", "logged_in_as": user, "output": output})
                    except Exception as exc:
                        print(f"[ig-dl] Login failed: {exc}", file=sys.stderr)
                        await send({"type": "error", "error": str(exc)})

                elif msg_type == "check":  # which shortcodes are already on disk? (no login needed)
                    codes = msg.get("shortcodes")
                    if not (isinstance(codes, list) and len(codes) <= 2000
                            and all(isinstance(c, str) and c for c in codes)):
                        await send({"type": "error", "error": "check needs a list of shortcodes"})
                        continue
                    found = await loop.run_in_executor(None, downloader.downloaded_shortcodes, codes)
                    await send({"type": "checked", "downloaded": found})

                elif msg_type == "download":
                    if not state.authenticated:
                        print("[ig-dl] Refused: download before login", file=sys.stderr)
                        await send({"type": "error", "error": "Not authenticated. Log in to Instagram in "
                                    "Chrome and use the extension, or start with --sessionid/--csrftoken."})
                    elif active is not None and not active.done():
                        await send({"type": "error", "error": "A download is already running on this connection."})
                    else:
                        cancel = threading.Event()
                        active = asyncio.create_task(run_download(msg, cancel))

                elif msg_type == "cancel":
                    if cancel is not None:
                        cancel.set()

                else:
                    await send({"type": "error", "error": f"Unknown message type: {msg_type!r}"})
        finally:
            # Client closed (or its service worker died): don't keep downloading for nobody.
            if cancel is not None:
                cancel.set()

    return handle_client


# ── Command line + config file ───────────────────────────────────────────────

def load_config(path: Path) -> dict:
    try:
        import tomllib
    except ImportError:
        raise SystemExit("Config files need Python 3.11+ (tomllib). Use command-line flags instead.")
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"Could not read config {path}: {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="download-insta-tab WebSocket server",
        epilog=("Cookies are optional: the Chrome extension sends them itself.\n"
                "Defaults can be set in config.toml (see config.example.toml)."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, help=f"TOML config file (default: {DEFAULT_CONFIG.name} next to this script)")
    parser.add_argument("--sessionid", help="Instagram sessionid cookie (optional with the extension)")
    parser.add_argument("--csrftoken", help="Instagram csrftoken cookie (optional with the extension)")
    parser.add_argument("--output", "-o", default=".", help="Root directory for downloads (default: current dir)")
    parser.add_argument("--port", "-p", type=int, default=7432, help="Port to listen on (default: 7432)")
    parser.add_argument("--filename-pattern", default=DEFAULT_FILENAME_PATTERN,
                        help="instaloader filename pattern. Anything but the default requires --no-post-process, "
                             "because snapshots/collages/duplicate checks rely on {owner_username}_{shortcode}")
    parser.add_argument("--no-post-process", action="store_true", help="Skip all post-processing (collage + caption graphic)")
    parser.add_argument("--no-collage", action="store_true", help="Skip carousel collage/concat")
    parser.add_argument("--no-graphic", action="store_true", help="Skip caption graphic snapshot")
    parser.add_argument("--delete-originals", action="store_true",
                        help="Delete original media/composite and .txt once a snapshot is created, "
                             "keeping only the final _snapshot file per post")
    parser.add_argument("--post-delay", nargs=2, type=float, metavar=("MIN", "MAX"), default=(3.0, 8.0),
                        help="Random pause in seconds between posts in a multi-post run (default: 3 8; "
                             "0 0 disables), on top of instaloader's own per-request sleeps")
    parser.add_argument("--max-batch", type=int, default=100,
                        help="Refuse requests for more than this many posts at once (default: 100)")
    parser.add_argument("--daily-cap", type=int, default=300,
                        help="Most posts downloaded per rolling 24 hours (default: 300; 0 disables)")
    parser.add_argument("--extension-id", help="Only accept connections from this Chrome extension ID "
                        "(chrome://extensions shows it); default: any extension")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=Path)
    config_arg = pre.parse_known_args(argv)[0].config

    parser = build_parser()
    config_path = config_arg or (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None)
    if config_path:
        config = load_config(config_path)
        unknown = sorted(set(config) - {a.dest for a in parser._actions} - {"help", "config"})
        if unknown:
            parser.error(f"unknown key(s) in {config_path}: {', '.join(unknown)}")
        if "post_delay" in config:
            config["post_delay"] = tuple(config["post_delay"])
        parser.set_defaults(**config)
    args = parser.parse_args(argv)

    if len(args.post_delay) != 2 or args.post_delay[0] < 0 or args.post_delay[1] < args.post_delay[0]:
        parser.error("post_delay needs 0 <= MIN <= MAX")
    if args.max_batch < 1:
        parser.error("max_batch must be at least 1")
    if args.daily_cap < 0:
        parser.error("daily_cap must be 0 or more")
    if bool(args.sessionid) != bool(args.csrftoken):
        parser.error("--sessionid and --csrftoken must be given together")
    if args.filename_pattern != DEFAULT_FILENAME_PATTERN and not args.no_post_process:
        parser.error(f"a custom --filename-pattern requires --no-post-process (snapshots, collages and the "
                     f"already-downloaded check rely on {DEFAULT_FILENAME_PATTERN})")
    if args.delete_originals and (args.no_post_process or args.no_graphic):
        print("[ig-dl] WARNING: --delete-originals has no effect without snapshots "
              "(--no-post-process/--no-graphic disables snapshot creation)", file=sys.stderr)
    args.config_path = config_path
    return args


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        output_dir=output_dir,
        skip_post_process=args.no_post_process,
        skip_collage=args.no_collage,
        skip_graphic=args.no_graphic,
        delete_originals=args.delete_originals,
        post_delay=(args.post_delay[0], args.post_delay[1]),
        max_batch=args.max_batch,
        daily_cap=args.daily_cap,
    )

    # Embed the absolute output path in dirname_pattern so instaloader never sanitizes the base
    # path (it only sanitizes the {target} substitution). On Windows, passing a full path as
    # `target` replaces colons and backslashes with full-width lookalikes, breaking path resolution.
    loader = instaloader.Instaloader(
        dirname_pattern=str(output_dir / "{target}"),
        filename_pattern=args.filename_pattern,
        download_video_thumbnails=False,
        save_metadata=False,
        post_metadata_txt_pattern="",  # we write our own JSON txt
    )
    downloader = Downloader(loader, settings)
    state = LoginState(loader, downloader)

    if args.config_path:
        print(f"[ig-dl] Config : {args.config_path}")
    if args.sessionid:
        try:
            state.login(args.sessionid, args.csrftoken)
        except Exception as exc:
            print(f"[ig-dl] ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        print("[ig-dl] No cookies given; waiting for the Chrome extension to authenticate.")
    if downloader.cooldown_remaining() > 0:
        print(f"[ig-dl] Still cooling down from an earlier Instagram pushback "
              f"({int(downloader.cooldown_remaining() // 60) + 1} min left).")

    print(f"[ig-dl] Output : {output_dir}")
    print(f"[ig-dl] Log    : {output_dir / 'downloads.jsonl'}")
    print(f"[ig-dl] Listening on ws://localhost:{args.port}\n")

    async def process_request(connection, request):
        """Handle Chrome's Private Network Access preflight, and reject foreign origins."""
        origin = request.headers.get("Origin")
        if not origin_allowed(origin, args.extension_id):
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
        async with ws_serve(make_handler(downloader, state), "127.0.0.1", args.port,
                            process_request=process_request):
            try:
                await asyncio.sleep(float("inf"))
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
