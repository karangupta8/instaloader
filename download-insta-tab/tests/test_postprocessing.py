"""The snapshot + carousel steps with real Pillow images (images only, so no ffmpeg needed)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import postprocess
import pipeline
from helpers import FakeLoader, make_post
from pipeline import Downloader, PostRef, Settings


def write_image(path: Path, size=(400, 300), color=(200, 30, 30)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG")


def write_meta(path: Path, username="bob", caption="hello #world", comments=()):
    path.write_text(json.dumps({"username": username, "caption": caption,
                                "top_comments": list(comments)}), encoding="utf-8")


class ImageLoader(FakeLoader):
    """Writes real JPEGs (single post -> one file; carousel -> _1.._N slides like instaloader does)."""

    def download_post(self, post, target):
        folder = self.output_dir / target
        stem = f"{post.owner_username}_{post.shortcode}"
        if post.mediacount > 1:
            for i in range(1, post.mediacount + 1):
                write_image(folder / f"{stem}_{i}.jpg", color=(40 * i, 90, 160))
        else:
            write_image(folder / f"{stem}.jpg")
        self.downloaded.append(post.shortcode)


def run_one(tmp_path, post, **settings):
    loader = ImageLoader(tmp_path)
    dl = Downloader(loader, Settings(output_dir=tmp_path, post_delay=(0.0, 0.0), daily_cap=0, **settings),
                    sleep=lambda _s: None)
    result = dl.run_batch([PostRef(post.shortcode, lambda: post)], None, total=1)
    return dl, result


def comment(user, text, likes=3):
    return SimpleNamespace(owner=SimpleNamespace(username=user), text=text, likes_count=likes,
                           created_at_utc=__import__("datetime").datetime(2026, 1, 1))


def test_single_post_gets_a_caption_snapshot(tmp_path):
    post = make_post("AAA", comments=[comment("fan", "love it")])
    _, result = run_one(tmp_path, post)

    assert result["downloaded"] == 1
    snapshot = tmp_path / "bob" / "bob_AAA_snapshot.jpg"
    assert snapshot.exists()
    original = Image.open(tmp_path / "bob" / "bob_AAA.jpg")
    assert Image.open(snapshot).height > original.height  # panel appended below a landscape image


def test_delete_originals_keeps_only_the_snapshot(tmp_path):
    _, result = run_one(tmp_path, make_post("AAA"), delete_originals=True)

    assert result["downloaded"] == 1
    assert sorted(p.name for p in (tmp_path / "bob").iterdir()) == ["bob_AAA_snapshot.jpg"]


def test_a_post_with_a_snapshot_counts_as_downloaded(tmp_path):
    dl, _ = run_one(tmp_path, make_post("AAA"), delete_originals=True)
    assert dl.downloaded_shortcodes(["AAA", "ZZZ"]) == ["AAA"]


def test_carousel_slides_become_one_collage_with_a_snapshot(tmp_path):
    post = make_post("CCC")
    post.typename, post.mediacount = "GraphSidecar", 3

    _, result = run_one(tmp_path, post)

    assert result["downloaded"] == 1
    files = sorted(p.name for p in (tmp_path / "bob").iterdir())
    assert "bob_CCC_carousel.jpg" in files and "bob_CCC_carousel_snapshot.jpg" in files
    assert not any(name.startswith("bob_CCC_") and name[-5:-4].isdigit() for name in files)  # slides removed


def test_no_graphic_flag_skips_the_snapshot(tmp_path):
    run_one(tmp_path, make_post("AAA"), skip_graphic=True)
    assert not (tmp_path / "bob" / "bob_AAA_snapshot.jpg").exists()


# ── postprocess.py: the retroactive command ──────────────────────────────────

@pytest.fixture
def old_download(tmp_path):
    """A folder downloaded with --no-post-process: a single post plus a 2-slide carousel."""
    folder = tmp_path / "_saved"
    write_image(folder / "bob_AAA.jpg")
    write_meta(folder / "bob_AAA.txt")
    for i in (1, 2):
        write_image(folder / f"bob_BBB_{i}.jpg", color=(10, 20 * i, 200))
    write_meta(folder / "bob_BBB.txt", caption="carousel!")
    return folder


def names(folder):
    return sorted(p.name for p in folder.iterdir())


def test_postprocess_builds_collages_and_snapshots(old_download):
    assert postprocess.run(old_download) == 0

    result = names(old_download)
    assert "bob_AAA_snapshot.jpg" in result and "bob_BBB_carousel_snapshot.jpg" in result
    assert "bob_BBB_1.jpg" not in result  # slides consumed by the collage
    assert "bob_AAA.jpg" in result  # originals kept unless asked otherwise


def test_postprocess_delete_originals_leaves_only_snapshots(old_download):
    assert postprocess.run(old_download, delete_originals=True) == 0
    assert names(old_download) == ["bob_AAA_snapshot.jpg", "bob_BBB_carousel_snapshot.jpg"]


def test_postprocess_move_to_archives_instead_of_deleting(old_download, tmp_path):
    archive = tmp_path / "archive"

    assert postprocess.run(old_download, move_to=archive) == 0

    assert names(old_download) == ["bob_AAA_snapshot.jpg", "bob_BBB_carousel_snapshot.jpg"]
    assert "bob_AAA.jpg" in names(archive) and "bob_BBB_1.jpg" in names(archive)


def test_postprocess_respects_no_collage_and_shortcode(old_download):
    assert postprocess.run(old_download, no_collage=True, shortcode="AAA") == 0

    result = names(old_download)
    assert "bob_AAA_snapshot.jpg" in result
    assert "bob_BBB_1.jpg" in result and not any("carousel" in n for n in result)


def test_postprocess_unknown_shortcode_raises(old_download):
    with pytest.raises(KeyError):
        postprocess.run(old_download, shortcode="NOPE")


def test_postprocess_is_idempotent(old_download):
    postprocess.run(old_download)
    before = names(old_download)

    assert postprocess.run(old_download) == 0
    assert names(old_download) == before
