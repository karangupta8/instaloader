#!/usr/bin/env python3
"""
postprocess.py: the one offline command for already-downloaded folders.

Runs, in order:
  1. carousel collages  (carousel_processor): slides  user_CODE_1.jpg, _2.jpg ... -> user_CODE_carousel.jpg/.mp4
  2. caption snapshots  (caption_graphic):    every media + .txt pair             -> *_snapshot.jpg/.mp4

This is the same code the server runs after each download, so it's the way to apply the
pipeline retroactively (posts downloaded with --no-post-process, or before a feature existed).

    python download-insta-tab/postprocess.py C:\\Users\\you\\Downloads\\_saved
    python download-insta-tab/postprocess.py <dir> --delete-originals     # keep only the *_snapshot files
    python download-insta-tab/postprocess.py <dir> --move-to <backup-dir> # archive instead of delete
"""

import argparse
import logging
import sys
from pathlib import Path

import caption_graphic
import carousel_processor


def run(target_dir: Path, *, shortcode: str | None = None, no_collage: bool = False,
        no_graphic: bool = False, overwrite: bool = False, keep_slides: bool = False,
        move_to: Path | None = None, delete_originals: bool = False, cell_size: int = 640) -> int:
    """Returns the number of failures (0 = clean run). Raises KeyError for an unknown --shortcode."""
    failures = 0
    if not no_collage:
        print("Carousels:")
        # The caption step below owns snapshots, so the collage step never makes them itself.
        failures += carousel_processor.process_directory(
            target_dir, shortcode=shortcode, cell_size=cell_size, make_collage=True,
            make_graphic=False, keep_slides=keep_slides, move_to=move_to)
    if not no_graphic:
        print("Caption snapshots:")
        processed, skipped, errors = caption_graphic.process_directory(
            target_dir, overwrite=overwrite, delete=delete_originals, move_to=move_to, shortcode=shortcode)
        print(f"  {processed} generated, {skipped} already existed, {errors} errors.")
        failures += errors
    return failures


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_dir", type=Path, help="Folder of downloaded media (+ .txt metadata)")
    parser.add_argument("--shortcode", metavar="CODE", help="Only process this post (default: everything)")
    parser.add_argument("--no-collage", action="store_true", help="Skip carousel collage/concat")
    parser.add_argument("--no-graphic", action="store_true", help="Skip caption snapshots")
    parser.add_argument("--overwrite", action="store_true", help="Re-generate snapshots that already exist")
    parser.add_argument("--keep-slides", action="store_true", help="Keep individual carousel slides after compositing")
    parser.add_argument("--delete-originals", action="store_true",
                        help="Delete the media + .txt once its snapshot exists (keeps only *_snapshot files)")
    parser.add_argument("--move-to", type=Path, metavar="DIR",
                        help="Move retired slides/originals here instead of deleting them")
    parser.add_argument("--cell-size", type=int, default=640, metavar="N",
                        help="Carousel grid cell size in pixels (default: 640)")
    args = parser.parse_args()

    target_dir = args.input_dir.expanduser().resolve()
    if not target_dir.is_dir():
        sys.exit(f"ERROR: {target_dir} is not a directory")
    try:
        failures = run(
            target_dir, shortcode=args.shortcode, no_collage=args.no_collage, no_graphic=args.no_graphic,
            overwrite=args.overwrite, keep_slides=args.keep_slides,
            move_to=args.move_to.expanduser().resolve() if args.move_to else None,
            delete_originals=args.delete_originals, cell_size=args.cell_size)
    except KeyError:
        sys.exit(f"ERROR: shortcode '{args.shortcode}' not found (or has < 2 slides)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
