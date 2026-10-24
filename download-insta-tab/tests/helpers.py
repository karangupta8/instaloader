"""Fakes so the pipeline can be exercised without touching Instagram."""

import time
from pathlib import Path
from types import SimpleNamespace

import instaloader

from pipeline import Downloader, PostRef, Settings


def make_post(code: str, owner: str = "bob", comments=None, get_comments=None):
    return SimpleNamespace(
        shortcode=code, owner_username=owner, caption=f"caption {code}",
        typename="GraphImage", mediacount=1,
        get_comments=get_comments or (lambda: iter(comments or [])),
    )


class FakeLoader:
    """Stands in for instaloader.Instaloader: writes a fake media file per downloaded post."""

    def __init__(self, output_dir: Path, delay: float = 0.0):
        self.output_dir = output_dir
        self.delay = delay
        self.downloaded: list[str] = []
        self.context = SimpleNamespace(username="tester")

    def download_post(self, post, target):
        time.sleep(self.delay)
        folder = self.output_dir / target
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{post.owner_username}_{post.shortcode}.jpg").write_bytes(b"x")
        self.downloaded.append(post.shortcode)


def make_downloader(tmp_path: Path, **overrides) -> tuple[Downloader, FakeLoader]:
    settings = Settings(output_dir=tmp_path, skip_post_process=True, post_delay=(0.0, 0.0),
                        **{"daily_cap": 0, **overrides})
    loader = FakeLoader(tmp_path)
    return Downloader(loader, settings, sleep=lambda _s: None), loader


def refs_for(*codes: str, fail: dict | None = None, owner: str = "bob"):
    """PostRefs for the given codes. `fail` maps code -> exception to raise when that post is fetched."""
    fetched: list[str] = []

    def fetcher(code):
        def fetch():
            fetched.append(code)
            if fail and code in fail:
                raise fail[code]
            return make_post(code, owner)
        return fetch

    return [PostRef(code, fetcher(code)) for code in codes], fetched


def wrapped_429() -> Exception:
    """What instaloader raises once its own 429 retries are exhausted."""
    try:
        try:
            raise instaloader.TooManyRequestsException("429 Too Many Requests")
        except instaloader.TooManyRequestsException as inner:
            raise instaloader.ConnectionException("JSON Query failed") from inner
    except instaloader.ConnectionException as outer:
        return outer
