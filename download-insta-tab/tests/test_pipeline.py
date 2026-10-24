import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import instaloader
import pytest

import pipeline
from helpers import FakeLoader, make_downloader, make_post, refs_for, wrapped_429
from pipeline import Downloader, RequestRefused, Settings, is_stop_error


def log_entries(tmp_path):
    path = tmp_path / pipeline.LOG_NAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ── stop-signal detection ───────────────────────────────────────────────────

def test_is_stop_error_covers_direct_and_wrapped_429():
    assert is_stop_error(instaloader.TooManyRequestsException("x"))
    assert is_stop_error(instaloader.AbortDownloadException("checkpoint_required"))
    assert is_stop_error(instaloader.LoginRequiredException("x"))
    # instaloader re-raises an exhausted 429 as a plain ConnectionException with the 429 as __cause__
    assert is_stop_error(wrapped_429())


def test_is_stop_error_ignores_ordinary_failures():
    assert not is_stop_error(instaloader.ConnectionException("plain network error"))
    assert not is_stop_error(ValueError("nope"))
    assert not is_stop_error(None)


# ── run_batch ────────────────────────────────────────────────────────────────

def test_batch_downloads_logs_and_stamps_download_order(tmp_path):
    dl, loader = make_downloader(tmp_path)
    refs, _ = refs_for("AAA", "BBB", "CCC")

    result = dl.run_batch(refs, "_saved", total=3)

    assert (result["downloaded"], result["skipped"], result["failed"]) == (3, 0, 0)
    assert loader.downloaded == ["AAA", "BBB", "CCC"]
    assert [e["status"] for e in log_entries(tmp_path)] == ["downloaded"] * 3
    mtimes = [os.path.getmtime(tmp_path / "_saved" / f"bob_{c}.jpg") for c in ("AAA", "BBB", "CCC")]
    assert mtimes == sorted(mtimes) and len(set(mtimes)) == 3


def test_batch_skips_posts_already_on_disk_without_fetching_them(tmp_path):
    dl, loader = make_downloader(tmp_path)
    (tmp_path / "bob").mkdir()
    (tmp_path / "bob" / "bob_BBB.jpg").write_bytes(b"x")
    refs, fetched = refs_for("AAA", "BBB", "CCC")

    result = dl.run_batch(refs, None, total=3)

    assert (result["downloaded"], result["skipped"]) == (2, 1)
    assert fetched == ["AAA", "CCC"]  # no request spent on the one we already have
    assert [e["status"] for e in log_entries(tmp_path)] == ["downloaded", "skipped", "downloaded"]


def test_force_redownloads_existing_posts(tmp_path):
    dl, _ = make_downloader(tmp_path)
    (tmp_path / "bob").mkdir()
    (tmp_path / "bob" / "bob_AAA.jpg").write_bytes(b"x")
    refs, fetched = refs_for("AAA")

    result = dl.run_batch(refs, None, total=1, force=True)

    assert result["downloaded"] == 1 and fetched == ["AAA"]


def test_exhausted_429_stops_the_batch_and_starts_a_persisted_cooldown(tmp_path):
    dl, _ = make_downloader(tmp_path)
    refs, fetched = refs_for("AAA", "BBB", "CCC", fail={"BBB": wrapped_429()})

    result = dl.run_batch(refs, None, total=3)

    assert fetched == ["AAA", "BBB"]  # CCC is never requested
    assert result["downloaded"] == 1 and "aborted" in result
    assert dl.cooldown_remaining() > 0
    # survives a server restart
    restarted = Downloader(FakeLoader(tmp_path), dl.settings)
    assert restarted.cooldown_remaining() > 0
    with pytest.raises(RequestRefused, match="Cooling down"):
        restarted.check_request(1)


def test_checkpoint_during_comment_fetch_also_stops_the_batch(tmp_path):
    def comments_blocked():
        raise instaloader.AbortDownloadException("feedback_required")

    dl, _ = make_downloader(tmp_path)
    post = make_post("AAA", get_comments=comments_blocked)
    refs = [pipeline.PostRef("AAA", lambda: post), pipeline.PostRef("BBB", lambda: make_post("BBB"))]

    result = dl.run_batch(refs, None, total=2)

    assert "aborted" in result and result["downloaded"] == 0
    assert dl.cooldown_remaining() > 0


def test_ordinary_failure_is_skipped_and_the_batch_continues(tmp_path):
    dl, _ = make_downloader(tmp_path)
    refs, fetched = refs_for("AAA", "BBB", "CCC", fail={"BBB": instaloader.ConnectionException("boom")})

    result = dl.run_batch(refs, None, total=3)

    assert fetched == ["AAA", "BBB", "CCC"]
    assert (result["downloaded"], result["failed"]) == (2, 1)
    assert "aborted" not in result and "error" not in result
    assert dl.cooldown_remaining() == 0
    assert [e["status"] for e in log_entries(tmp_path)] == ["downloaded", "failed", "downloaded"]


def test_single_failure_surfaces_its_error(tmp_path):
    dl, _ = make_downloader(tmp_path)
    refs, _ = refs_for("AAA", fail={"AAA": instaloader.ConnectionException("post is gone")})

    result = dl.run_batch(refs, None, total=1)

    assert result["downloaded"] == 0 and "post is gone" in result["error"]


def test_cancel_stops_between_posts(tmp_path):
    dl, loader = make_downloader(tmp_path)
    refs, _ = refs_for("AAA", "BBB", "CCC")
    cancel = threading.Event()

    result = dl.run_batch(refs, None, total=3, cancel=cancel,
                          on_progress=lambda update: cancel.set())

    assert result["cancelled"] is True and result["downloaded"] == 1
    assert loader.downloaded == ["AAA"]
    assert dl.cooldown_remaining() == 0  # a user cancel is not Instagram pushback


def test_progress_reports_each_post_with_its_status(tmp_path):
    dl, _ = make_downloader(tmp_path)
    (tmp_path / "bob").mkdir()
    (tmp_path / "bob" / "bob_AAA.jpg").write_bytes(b"x")
    refs, _ = refs_for("AAA", "BBB")
    events = []

    dl.run_batch(refs, None, total=2, on_progress=events.append)

    assert [(e["shortcode"], e["status"], e["processed"], e["total"]) for e in events] == [
        ("AAA", "skipped", 1, 2), ("BBB", "downloaded", 2, 2)]


def test_metadata_txt_records_the_username_even_with_underscores(tmp_path):
    dl, _ = make_downloader(tmp_path)
    refs, _ = refs_for("AAA", owner="nat_geo")

    dl.run_batch(refs, None, total=1)

    meta = json.loads((tmp_path / "nat_geo" / "nat_geo_AAA.txt").read_text(encoding="utf-8"))
    assert meta["username"] == "nat_geo" and meta["caption"] == "caption AAA"


# ── daily cap ────────────────────────────────────────────────────────────────

def write_log(tmp_path, *entries):
    with (tmp_path / pipeline.LOG_NAME).open("w", encoding="utf-8") as fh:
        for status, age_hours in entries:
            ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(timespec="seconds")
            fh.write(json.dumps({"ts": ts, "status": status, "shortcode": "X"}) + "\n")


def test_daily_cap_counts_only_recent_downloads(tmp_path):
    write_log(tmp_path, ("downloaded", 1), ("downloaded", 5), ("downloaded", 30), ("skipped", 1), ("failed", 1))
    dl, _ = make_downloader(tmp_path, daily_cap=5)

    assert dl.downloaded_last_24h() == 2
    assert dl.check_request(1) == 3


def test_daily_cap_blocks_a_request_once_reached(tmp_path):
    write_log(tmp_path, ("downloaded", 1), ("downloaded", 2))
    dl, _ = make_downloader(tmp_path, daily_cap=2)

    with pytest.raises(RequestRefused, match="Daily cap"):
        dl.check_request(1)


def test_daily_cap_stops_a_batch_midway(tmp_path):
    dl, loader = make_downloader(tmp_path, daily_cap=2)
    refs, _ = refs_for("AAA", "BBB", "CCC")

    result = dl.run_batch(refs, None, total=3, allowance=dl.check_request(3))

    assert result["downloaded"] == 2 and "Daily cap" in result["aborted"]
    assert loader.downloaded == ["AAA", "BBB"]
    assert dl.cooldown_remaining() == 0


# ── request validation + routing ─────────────────────────────────────────────

def test_download_rejects_bad_input(tmp_path):
    dl, _ = make_downloader(tmp_path)
    for bad in (
        {"page_type": "nonsense"},
        {"page_type": "post", "identifier": "../../etc"},
        {"page_type": "post", "identifier": "ABC", "count": "lots"},
        {"page_type": "collection", "identifiers": []},
        {"page_type": "collection", "identifiers": ["ok", "not ok!"]},
        {"page_type": "profile", "identifier": "a/b"},
    ):
        with pytest.raises(RequestRefused):
            dl.download(bad)


def test_download_enforces_max_batch(tmp_path):
    dl, _ = make_downloader(tmp_path, max_batch=2)
    with pytest.raises(RequestRefused, match="max-batch"):
        dl.download({"page_type": "collection", "identifiers": ["A", "B", "C"], "count": 3})


def test_download_collection_goes_to_the_saved_folder(tmp_path, monkeypatch):
    dl, loader = make_downloader(tmp_path)
    monkeypatch.setattr(instaloader.Post, "from_shortcode",
                        staticmethod(lambda _ctx, code: make_post(code)))

    result = dl.download({"page_type": "collection", "identifiers": ["AAA", "BBB"], "count": 2})

    assert result["downloaded"] == 2
    assert (tmp_path / pipeline.saved_dir_name() / "bob_AAA.jpg").exists()


def test_download_single_post_stamps_order_and_uses_owner_folder(tmp_path, monkeypatch):
    dl, _ = make_downloader(tmp_path)
    monkeypatch.setattr(instaloader.Post, "from_shortcode",
                        staticmethod(lambda _ctx, code: make_post(code, owner="alice")))
    before = time.time()

    result = dl.download({"page_type": "post", "identifier": "AAA"})

    assert result["downloaded"] == 1 and result["target"].endswith("alice")
    assert os.path.getmtime(tmp_path / "alice" / "alice_AAA.jpg") >= before - 1


def test_stop_error_while_planning_starts_the_cooldown(tmp_path, monkeypatch):
    dl, _ = make_downloader(tmp_path)

    def boom(_ctx, _name):
        raise wrapped_429()

    monkeypatch.setattr(instaloader.Profile, "from_username", staticmethod(boom))

    with pytest.raises(instaloader.ConnectionException):
        dl.download({"page_type": "profile", "identifier": "someone"})
    assert dl.cooldown_remaining() > 0


# ── on-disk check ────────────────────────────────────────────────────────────

def test_downloaded_shortcodes_needs_a_snapshot_when_snapshots_are_on(tmp_path):
    (tmp_path / "_saved").mkdir()
    (tmp_path / "bob").mkdir()
    for path in ("_saved/bob_AAA_snapshot.jpg", "bob/bob_BBB.jpg", "bob/bob_CCC_carousel_snapshot.jpg",
                 "bob/bob_DDD.txt"):
        (tmp_path / path).write_bytes(b"x")
    codes = ["AAA", "BBB", "CCC", "DDD", "EEE"]

    snapshots_on = Downloader(FakeLoader(tmp_path), Settings(output_dir=tmp_path))
    snapshots_off = Downloader(FakeLoader(tmp_path), Settings(output_dir=tmp_path, skip_graphic=True))

    assert snapshots_on.downloaded_shortcodes(codes) == ["AAA", "CCC"]  # BBB has no snapshot yet
    assert snapshots_off.downloaded_shortcodes(codes) == ["AAA", "BBB", "CCC"]  # txt-only DDD never counts
