"""
Download engine for download-insta-tab: ONE pipeline every route (profile, saved, hashtag,
single post, collection of shortcodes) runs through, plus the safety rails around it.

Per post:   skip if already on disk -> pause -> download -> metadata .txt -> caption snapshot
            -> carousel collage -> stamp download order -> log to downloads.jsonl
Safety:     random pause between posts, batch cap, rolling daily cap, stop-and-cooldown when
            Instagram pushes back (429 / checkpoint / login redirect), cancel support.

Everything is written to the output folder: the media, <output>/downloads.jsonl (what was
downloaded/skipped/failed) and <output>/.ig-dl-state.json (the persisted cooldown).
"""

import itertools
import json
import os
import platform
import random
import re
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Import from parent instaloader package without modifying it
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import instaloader

import caption_graphic
import carousel_processor

MEDIA_EXTS = (".jpg", ".jpeg", ".webp", ".mp4", ".mov")
DEFAULT_FILENAME_PATTERN = "{owner_username}_{shortcode}"
LOG_NAME = "downloads.jsonl"
STATE_NAME = ".ig-dl-state.json"
DEFAULT_COUNT = 10
ROUTES = ("profile", "saved", "hashtag", "post", "collection")

# Instagram telling us to stop: repeated 429s, checkpoint/feedback_required, redirected to login.
STOP_ERRORS = (
    instaloader.TooManyRequestsException,
    instaloader.AbortDownloadException,
    instaloader.LoginRequiredException,
)

_SHORTCODE_RE = re.compile(r"[A-Za-z0-9_-]+")
_NAME_RE = re.compile(r"[^\s/\\?#]{1,100}")  # usernames / hashtags: anything that can't escape a folder

ProgressFn = Callable[[dict], None]


class RequestRefused(Exception):
    """A request rejected up front: bad input, cooldown, batch cap or daily cap."""


# HTTP status codes instaloader should abort on immediately instead of retrying: 401 is Instagram's
# "Please wait a few minutes before you try again" soft block, 429 is rate limiting. Retrying either
# only adds more requests to an account that is already being throttled.
FATAL_STATUS_CODES = [401, 429]

# Message fragments that mean the same thing when they arrive wrapped in a plain ConnectionException.
STOP_MARKERS = (
    "please wait a few minutes",
    "too many requests",
    "login_required",
    "checkpoint_required",
    "challenge_required",
    "feedback_required",
    "consent_required",
)


def is_stop_error(exc: BaseException | None) -> bool:
    """True if Instagram is telling us to stop: a stop exception, or a ConnectionException whose
    message carries a stop marker (401 "please wait", login_required, a challenge...), at any
    level of the __cause__ chain (instaloader re-raises exhausted retries as a plain ConnectionException)."""
    for _ in range(5):
        if exc is None:
            return False
        if isinstance(exc, STOP_ERRORS):
            return True
        if isinstance(exc, instaloader.ConnectionException) and any(m in str(exc).lower() for m in STOP_MARKERS):
            return True
        exc = exc.__cause__
    return False


def saved_dir_name() -> str:
    """Saved posts directory name (Windows-safe: ':' isn't allowed in directory names)."""
    return "_saved" if platform.system() == "Windows" else ":saved"


@dataclass(frozen=True)
class Settings:
    output_dir: Path
    skip_post_process: bool = False
    skip_collage: bool = False
    skip_graphic: bool = False
    delete_originals: bool = False
    post_delay: tuple[float, float] = (6.0, 15.0)  # random pause (s) between posts in a run
    max_batch: int = 50                            # most posts accepted in one request
    daily_cap: int = 100                           # most posts downloaded per rolling 24h (0 = off)
    cooldown_seconds: int = 120 * 60               # refuse downloads this long after Instagram pushes back
    max_consecutive_failures: int = 2              # stop (and cool down) after this many failed posts in a row


@dataclass(frozen=True)
class PostRef:
    """A post to process: its shortcode is known up front, so the on-disk check costs no request."""
    shortcode: str
    fetch: Callable[[], Any]


@dataclass(frozen=True)
class _Plan:
    refs: Iterable[PostRef]
    folder: str | None  # None => each post goes in its owner's folder
    total: int


def _as_int(value: object, name: str, minimum: int) -> int:
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        raise RequestRefused(f"{name} must be a whole number") from None
    if number < minimum:
        raise RequestRefused(f"{name} must be at least {minimum}")
    return number


def _name(msg: dict, key: str = "identifier") -> str:
    value = msg.get(key)
    if not (isinstance(value, str) and _NAME_RE.fullmatch(value)):
        raise RequestRefused(f"Missing or invalid {key}")
    return value


def _shortcodes(values: object) -> list[str]:
    if not (isinstance(values, list) and values
            and all(isinstance(v, str) and _SHORTCODE_RE.fullmatch(v) for v in values)):
        raise RequestRefused("Missing or invalid shortcode list")
    return values


class Downloader:
    """Owns the instaloader session, the safety state and the per-post pipeline."""

    def __init__(self, loader, settings: Settings, *, sleep: Callable[[float], None] = time.sleep):
        self.loader = loader
        self.settings = settings
        self.lock = threading.Lock()  # instaloader's session/RateController aren't thread-safe
        self._sleep = sleep
        self._cooldown_until = self._load_cooldown()

    # ── Safety state (persisted in the output folder) ────────────────────────

    def _load_cooldown(self) -> float:
        try:
            data = json.loads((self.settings.output_dir / STATE_NAME).read_text(encoding="utf-8"))
            return float(data.get("cooldown_until", 0.0))
        except (OSError, ValueError, AttributeError):
            return 0.0

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.time())

    def start_cooldown(self, reason: str) -> None:
        self._cooldown_until = time.time() + self.settings.cooldown_seconds
        try:
            (self.settings.output_dir / STATE_NAME).write_text(
                json.dumps({"cooldown_until": self._cooldown_until}), encoding="utf-8")
        except OSError as exc:
            print(f"[ig-dl] Could not persist cooldown: {exc}", file=sys.stderr)
        print(f"[ig-dl] Instagram pushed back ({reason}). Pausing all downloads for "
              f"{self.settings.cooldown_seconds // 60} minutes; avoid using Instagram heavily meanwhile.",
              file=sys.stderr)

    def _log(self, status: str, shortcode: str, *, owner: str | None = None,
             folder: str | None = None, error: str | None = None) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "status": status, "shortcode": shortcode}
        for key, value in (("owner", owner), ("folder", folder), ("error", error)):
            if value:
                entry[key] = value
        try:
            with (self.settings.output_dir / LOG_NAME).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"  [log] Could not write {LOG_NAME}: {exc}", file=sys.stderr)

    def downloaded_last_24h(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        count = 0
        try:
            with (self.settings.output_dir / LOG_NAME).open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        entry = json.loads(line)
                        if entry.get("status") == "downloaded" and datetime.fromisoformat(entry["ts"]) >= cutoff:
                            count += 1
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            return 0
        return count

    def check_request(self, batch: int) -> int | None:
        """Raise RequestRefused if this request shouldn't run. Returns how many more posts may be
        downloaded today (None = no daily cap)."""
        s = self.settings
        wait = self.cooldown_remaining()
        if wait > 0:
            raise RequestRefused("Cooling down after an Instagram rate-limit/challenge response; "
                                 f"try again in {int(wait // 60) + 1} min.")
        if batch > s.max_batch:
            raise RequestRefused(f"Batch of {batch} exceeds --max-batch {s.max_batch}; "
                                 "download fewer posts at a time.")
        if s.daily_cap <= 0:
            return None
        remaining = s.daily_cap - self.downloaded_last_24h()
        if remaining <= 0:
            raise RequestRefused(f"Daily cap of {s.daily_cap} posts reached; it frees up as the "
                                 "oldest downloads pass 24 hours.")
        return remaining

    # ── What's already on disk ───────────────────────────────────────────────

    def downloaded_shortcodes(self, shortcodes: list[str]) -> list[str]:
        """Subset of shortcodes that already have output on disk.

        Looks one folder level under the output dir (<user>/, _saved/, #tag/). When snapshots are
        enabled a post only counts once its snapshot exists, so an interrupted download is retried;
        otherwise any media file for the shortcode counts.
        """
        s = self.settings
        need_snapshot = not (s.skip_post_process or s.skip_graphic)
        names: list[str] = []
        try:
            for sub in s.output_dir.iterdir():
                if sub.is_dir():
                    names.extend(p.name for p in sub.iterdir() if p.is_file())
        except OSError as exc:
            print(f"  [check] Could not scan {s.output_dir}: {exc}", file=sys.stderr)
            return []

        def has_output(code: str) -> bool:
            if need_snapshot:
                snaps = (f"_{code}_snapshot.", f"_{code}_carousel_snapshot.")
                return any(snap in n for n in names for snap in snaps)
            return any(f"_{code}" in n and Path(n).suffix.lower() in MEDIA_EXTS for n in names)

        return [code for code in shortcodes if has_output(code)]

    # ── Per-post steps ───────────────────────────────────────────────────────

    def _save_metadata(self, post, txt_path: Path) -> None:
        """Write username, caption and top 5 comments as JSON to txt_path."""
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
            if is_stop_error(exc):
                raise  # the comments endpoint is the most tightly limited one; honour its pushback
            print(f"  [meta] Could not fetch comments: {exc}", file=sys.stderr)

        metadata = {"username": post.owner_username, "caption": post.caption or "",
                    "top_comments": top_comments}
        try:
            txt_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"  [meta] Could not write {txt_path.name}: {exc}", file=sys.stderr)

    def _snapshot(self, post, target_dir: Path) -> None:
        """Non-fatal: find the downloaded file and append a caption graphic."""
        s = self.settings
        if s.skip_post_process or s.skip_graphic:
            return
        stem = f"{post.owner_username}_{post.shortcode}"
        txt_path = target_dir / f"{stem}.txt"
        if not txt_path.exists():
            return
        for ext in MEDIA_EXTS:
            candidate = target_dir / f"{stem}{ext}"
            if not candidate.exists():
                continue
            snap_suffix = ".mp4" if ext in (".mp4", ".mov") else ".jpg"
            try:
                result = caption_graphic.create_snapshot(candidate, txt_path,
                                                         target_dir / f"{stem}_snapshot{snap_suffix}")
                if result:
                    print(f"  Snapshot: {result.name}")
                    if s.delete_originals:
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

    def _carousel(self, post, target_dir: Path) -> None:
        """Non-fatal carousel post-processing step."""
        s = self.settings
        if s.skip_post_process:
            return
        try:
            result = carousel_processor.process_carousel(
                post, target_dir,
                make_collage=not s.skip_collage,
                make_graphic=not s.skip_graphic,
                delete_originals=s.delete_originals,
            )
            if result:
                print(f"  Carousel: {result.name}")
            elif post.typename == "GraphSidecar":
                print("  [carousel] Skipped (single item or no files found)")
        except carousel_processor.CarouselProcessingError as exc:
            print(f"  [carousel] ERROR: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"  [carousel] Unexpected error: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

    @staticmethod
    def _stamp_download_order(target_dir: Path, post, order_ts: float) -> None:
        """Set mtime of every file belonging to this post to order_ts.

        download_post() (and our own snapshot/carousel steps) leave each file's mtime set to the
        post's original Instagram creation date, which has nothing to do with the order posts were
        saved/downloaded in. A strictly increasing per-download timestamp means sorting a folder by
        "Date Modified" reproduces the download order (== the order posts appeared on the source page).
        """
        for f in target_dir.glob(f"{post.owner_username}_{post.shortcode}*"):
            try:
                os.utime(f, (order_ts, order_ts))
            except OSError as exc:
                print(f"  [order] Could not set mtime on {f.name}: {exc}", file=sys.stderr)

    def _process_post(self, post, folder: str | None) -> Path:
        """The whole per-post pipeline after the post is fetched. Returns the folder it landed in."""
        folder = folder or post.owner_username
        target_dir = self.settings.output_dir / folder
        self.loader.download_post(post, target=folder)
        self._save_metadata(post, target_dir / f"{post.owner_username}_{post.shortcode}.txt")
        self._snapshot(post, target_dir)
        self._carousel(post, target_dir)
        return target_dir

    # ── The batch runner ─────────────────────────────────────────────────────

    def _pause(self) -> None:
        low, high = self.settings.post_delay
        if high > 0:
            self._sleep(random.uniform(low, high))

    def run_batch(self, refs: Iterable[PostRef], folder: str | None, *, total: int, force: bool = False,
                  cancel: threading.Event | None = None, on_progress: ProgressFn | None = None,
                  allowance: int | None = None) -> dict:
        """Process every ref through the pipeline; stops cleanly on cancel, the daily cap, or when
        Instagram pushes back. Individual post failures are logged and skipped."""
        cancel = cancel or threading.Event()
        counts = {"downloaded": 0, "skipped": 0, "failed": 0}
        extra: dict[str, Any] = {}
        last_error = None
        consecutive_failures = 0
        target = self.settings.output_dir / (folder or "")
        order_ts = time.time()
        handled = 0
        fetched_any = False

        def abort(reason: str, **flags) -> None:
            extra["aborted"] = reason
            extra.update(flags)

        def emit(shortcode: str, status: str) -> None:
            if on_progress:
                on_progress({"processed": handled, "total": total, "shortcode": shortcode,
                             "status": status, **counts})

        try:
            for ref in refs:
                if cancel.is_set():
                    abort("Cancelled by user.", cancelled=True)
                    break
                if allowance is not None and counts["downloaded"] >= allowance:
                    abort(f"Daily cap of {self.settings.daily_cap} posts reached.")
                    break

                print(f"  [{handled + 1}/{total}] {ref.shortcode}")
                if not force and self.downloaded_shortcodes([ref.shortcode]):
                    counts["skipped"] += 1
                    handled += 1
                    print("  already downloaded, skipping")
                    self._log("skipped", ref.shortcode)
                    emit(ref.shortcode, "skipped")
                    continue

                if fetched_any:
                    self._pause()
                fetched_any = True
                try:
                    post = ref.fetch()
                    target = self._process_post(post, folder)
                    counts["downloaded"] += 1
                    self._stamp_download_order(target, post, order_ts + counts["downloaded"])
                    self._log("downloaded", ref.shortcode, owner=post.owner_username, folder=target.name)
                except Exception as exc:
                    if is_stop_error(exc):
                        reason = f"{type(exc).__name__}: {exc}"
                        print(f"  Stopping batch at {ref.shortcode}: {reason}", file=sys.stderr)
                        self._log("aborted", ref.shortcode, error=reason)
                        self.start_cooldown(type(exc).__name__)
                        abort(reason)
                        break
                    last_error = str(exc)
                    counts["failed"] += 1
                    print(f"  Error downloading {ref.shortcode}: {exc}", file=sys.stderr)
                    self._log("failed", ref.shortcode, error=last_error)
                    status = "failed"
                    # A deleted post is just a 404; anything else failing back to back usually means
                    # Instagram is throttling us, and carrying on makes it worse.
                    if not isinstance(exc, instaloader.QueryReturnedNotFoundException):
                        consecutive_failures += 1
                    if consecutive_failures >= self.settings.max_consecutive_failures:
                        reason = f"{consecutive_failures} posts failed in a row (last: {last_error})"
                        print(f"  Stopping batch: {reason}", file=sys.stderr)
                        self._log("aborted", ref.shortcode, error=reason)
                        self.start_cooldown("consecutive failures")
                        abort(reason)
                        handled += 1
                        emit(ref.shortcode, status)
                        break
                else:
                    status = "downloaded"
                    consecutive_failures = 0
                handled += 1
                emit(ref.shortcode, status)
        except Exception as exc:  # raised by the iterator itself (e.g. a pagination request)
            if not is_stop_error(exc):
                raise
            reason = f"{type(exc).__name__}: {exc}"
            print(f"  Stopping batch: {reason}", file=sys.stderr)
            self.start_cooldown(type(exc).__name__)
            abort(reason)

        result: dict[str, Any] = {**counts, "target": str(target)}
        if last_error and not counts["downloaded"]:
            result["error"] = last_error  # lets a single-post caller see why nothing was saved
        return {**result, **extra}

    # ── Routes: each turns a request into (posts to process, folder, total) ───

    def _plan_profile(self, msg: dict, count: int, skip: int) -> _Plan:
        profile = instaloader.Profile.from_username(self.loader.context, _name(msg))
        available = max(0, profile.mediacount - skip)
        print(f"  Profile : {profile.full_name} (@{profile.username}), {profile.mediacount} posts")
        return _Plan(_refs_from_posts(itertools.islice(profile.get_posts(), skip, skip + count)),
                     profile.username, min(count, available))

    def _plan_saved(self, msg: dict, count: int, skip: int) -> _Plan:
        profile = instaloader.Profile.own_profile(self.loader.context)
        print(f"  Saved posts for @{profile.username}")
        return _Plan(_refs_from_posts(itertools.islice(profile.get_saved_posts(), skip, skip + count)),
                     saved_dir_name(), count)

    def _plan_hashtag(self, msg: dict, count: int, skip: int) -> _Plan:
        raw = msg.get("identifier")
        tag = _name({"identifier": raw.lstrip("#") if isinstance(raw, str) else raw})
        hashtag = instaloader.Hashtag.from_name(self.loader.context, tag)
        print(f"  Hashtag : #{tag}")
        return _Plan(_refs_from_posts(itertools.islice(hashtag.get_posts(), skip, skip + count)),
                     f"#{tag}", count)

    def _plan_post(self, msg: dict, count: int, skip: int) -> _Plan:
        code = _shortcodes([msg.get("identifier")])[0]
        return _Plan(self._refs_from_codes([code]), None, 1)

    def _plan_collection(self, msg: dict, count: int, skip: int) -> _Plan:
        codes = _shortcodes(msg.get("identifiers"))[skip:skip + count]
        print(f"  Collection: {len(codes)} shortcodes (skipped {skip})")
        return _Plan(self._refs_from_codes(codes), saved_dir_name(), len(codes))

    def _refs_from_codes(self, codes: list[str]) -> Iterator[PostRef]:
        for code in codes:
            yield PostRef(code, lambda c=code: instaloader.Post.from_shortcode(self.loader.context, c))

    @staticmethod
    def _batch_size(page_type: str, msg: dict, count: int, skip: int) -> int:
        if page_type == "post":
            return 1
        if page_type == "collection":
            return len(_shortcodes(msg.get("identifiers"))[skip:skip + count])
        return count

    def download(self, msg: dict, cancel: threading.Event | None = None,
                 on_progress: ProgressFn | None = None) -> dict:
        """Handle one {"type": "download", ...} request end to end. Raises RequestRefused for
        requests that must not run; blocks while another request holds the session."""
        cancel = cancel or threading.Event()
        page_type = msg.get("page_type")
        if page_type not in ROUTES:
            raise RequestRefused(f"Unknown page type: {page_type!r}")
        count = _as_int(msg.get("count", DEFAULT_COUNT), "count", 1)
        skip = _as_int(msg.get("skip", 0), "skip", 0)

        with self.lock:
            if cancel.is_set():
                return {"downloaded": 0, "skipped": 0, "failed": 0, "target": str(self.settings.output_dir),
                        "aborted": "Cancelled by user.", "cancelled": True}
            print(f"\n[ig-dl] {page_type.upper()}"
                  + (f" -> {msg['identifier']}" if msg.get("identifier") else "")
                  + f"  (count={count}, skip={skip})")
            allowance = self.check_request(self._batch_size(page_type, msg, count, skip))
            try:
                plan = getattr(self, f"_plan_{page_type}")(msg, count, skip)
            except Exception as exc:
                if is_stop_error(exc):
                    self.start_cooldown(type(exc).__name__)
                raise
            return self.run_batch(plan.refs, plan.folder, total=plan.total, force=bool(msg.get("force")),
                                  cancel=cancel, on_progress=on_progress, allowance=allowance)


def _refs_from_posts(posts: Iterable) -> Iterator[PostRef]:
    for post in posts:
        yield PostRef(post.shortcode, lambda p=post: p)
