#!/usr/bin/env python3
"""
Carousel post-processing: combine multi-slide Instagram posts into single composite files.

Supports:
- Image-only carousels → static collage (PIL)
- Video-only carousels → concatenated video (ffmpeg)
- Mixed carousels → video grid with images frozen, videos playing (ffmpeg xstack)

Can also be run standalone:
    python carousel_processor.py <input_dir> [options]
"""

import logging
import math
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Errors ──────────────────────────────────────────────────────────────────

class CarouselProcessingError(Exception):
    """Carousel processing failed — non-fatal."""
    pass


# ── Data structures ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SlideInfo:
    """Metadata for a downloaded carousel slide."""
    path: Path
    is_video: bool
    index: int  # 1-based, matches instaloader suffix


# ── File discovery ─────────────────────────────────────────────────────────

SLIDE_PATTERN = re.compile(r"^.+_([A-Za-z0-9_-]+)_(\d+)\.(jpg|jpeg|webp|mp4|mov)$")


def find_carousel_files(target_dir: Path, shortcode: str) -> list[SlideInfo]:
    """
    Discover carousel slide files in target_dir matching the given shortcode.
    Returns slides sorted by index (1, 2, 3, ...).
    """
    matched: list[tuple[int, Path, bool]] = []
    for f in target_dir.iterdir():
        if not f.is_file():
            continue
        m = SLIDE_PATTERN.match(f.name)
        if m and m.group(1) == shortcode:
            slide_num = int(m.group(2))
            is_video = m.group(3) in ("mp4", "mov")
            matched.append((slide_num, f, is_video))
    matched.sort(key=lambda t: t[0])
    return [SlideInfo(path=f, is_video=is_video, index=idx) for idx, f, is_video in matched]


def _scan_directory_for_carousels(target_dir: Path) -> dict[str, list[SlideInfo]]:
    """
    Scan target_dir for all slide files. Returns a dict mapping
    shortcode → sorted list[SlideInfo], only for shortcodes with 2+ slides.
    """
    groups: dict[str, list[tuple[int, Path, bool]]] = defaultdict(list)
    for f in target_dir.iterdir():
        if not f.is_file():
            continue
        m = SLIDE_PATTERN.match(f.name)
        if m:
            shortcode = m.group(1)
            slide_num = int(m.group(2))
            is_video  = m.group(3) in ("mp4", "mov")
            groups[shortcode].append((slide_num, f, is_video))

    result: dict[str, list[SlideInfo]] = {}
    for shortcode, items in groups.items():
        if len(items) >= 2:
            items.sort(key=lambda t: t[0])
            result[shortcode] = [
                SlideInfo(path=f, is_video=iv, index=idx)
                for idx, f, iv in items
            ]
    return result


def _extract_owner_username(shortcode: str, slides: list[SlideInfo]) -> str:
    """
    Derive owner_username from a slide filename.
    Filename stem: "{owner_username}_{shortcode}_{N}"
    Uses rfind("_{shortcode}_") to find the exact boundary.
    """
    stem = slides[0].path.stem  # e.g. "john_doe_ABC123_1"
    marker = f"_{shortcode}_"
    pos = stem.rfind(marker)
    if pos > 0:
        return stem[:pos]
    # Fallback: strip trailing _{N} then try again
    stem2 = "_".join(stem.split("_")[:-1])
    pos2 = stem2.rfind(f"_{shortcode}")
    return stem2[:pos2] if pos2 > 0 else "unknown"


# ── Image collage (PIL) ────────────────────────────────────────────────────

def _grid_dims(n: int) -> tuple[int, int]:
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return cols, rows


def _build_image_collage(
    slides: list[SlideInfo],
    out_path: Path,
    cell_size: int = 640,
) -> Path:
    """Create a static image collage from image slides."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise CarouselProcessingError(
            "PIL/Pillow required for image collage. Install: pip install Pillow"
        ) from exc

    n = len(slides)
    cols, rows = _grid_dims(n)
    canvas = Image.new("RGB", (cols * cell_size, rows * cell_size), color=(20, 20, 20))

    for i, slide in enumerate(slides):
        if slide.is_video:
            continue
        try:
            img = Image.open(slide.path).convert("RGB")
        except Exception as exc:
            logger.warning(f"Failed to open image {slide.path}: {exc}")
            continue
        img.thumbnail((cell_size, cell_size), Image.LANCZOS)
        col = i % cols
        row = i // cols
        paste_x = col * cell_size + (cell_size - img.width) // 2
        paste_y = row * cell_size + (cell_size - img.height) // 2
        canvas.paste(img, (paste_x, paste_y))

    try:
        canvas.save(out_path, "JPEG", quality=92, optimize=True)
    except Exception as exc:
        raise CarouselProcessingError(f"Failed to save collage: {exc}") from exc
    return out_path


# ── ffmpeg helpers ────────────────────────────────────────────────────────

def _check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run_ffmpeg(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as exc:
        raise CarouselProcessingError(
            "ffmpeg not found. Install from https://ffmpeg.org/download.html"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr_tail = exc.stderr[-1000:] if exc.stderr else "(no stderr)"
        raise CarouselProcessingError(
            f"ffmpeg failed (exit {exc.returncode}):\n{stderr_tail}"
        ) from exc


def _get_video_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as exc:
        raise CarouselProcessingError(f"Failed to get duration of {path.name}: {exc}") from exc


def _has_audio(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        )
        return len(result.stdout.strip()) > 0
    except Exception:
        return False


# ── Video concatenation ────────────────────────────────────────────────────

def _build_video_concat(slides: list[SlideInfo], out_path: Path, cell_size: int = 640) -> Path:
    n = len(slides)
    inputs: list[str] = []
    for slide in slides:
        inputs += ["-i", str(slide.path)]

    filter_parts: list[str] = []
    for i in range(n):
        filter_parts.append(
            f"[{i}:v]scale={cell_size}:{cell_size}:force_original_aspect_ratio=decrease,"
            f"pad={cell_size}:{cell_size}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1[v{i}];"
        )
        filter_parts.append(
            f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}];"
        )
    v_labels = "".join(f"[v{i}]" for i in range(n))
    a_labels = "".join(f"[a{i}]" for i in range(n))
    filter_parts.append(f"{v_labels}concat=n={n}:v=1:a=0[outv];")
    filter_parts.append(f"{a_labels}concat=n={n}:v=0:a=1[outa]")

    _run_ffmpeg([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", " ".join(filter_parts),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(out_path),
    ])
    return out_path


# ── Mixed grid (images + videos) ──────────────────────────────────────────

def _build_xstack_layout(cols: int, rows: int, cell_size: int) -> str:
    positions: list[str] = []
    for idx in range(cols * rows):
        col = idx % cols
        row = idx // cols
        positions.append(f"{col * cell_size}_{row * cell_size}")
    return "|".join(positions)


def _build_mixed_grid(
    slides: list[SlideInfo], out_path: Path, cell_size: int = 640, max_dur: float = 0,
) -> Path:
    n = len(slides)
    cols, rows = _grid_dims(n)

    if max_dur <= 0:
        durations = []
        for s in slides:
            if s.is_video:
                try:
                    durations.append(_get_video_duration(s.path))
                except CarouselProcessingError:
                    logger.warning(f"Could not determine duration of {s.path}")
        if not durations:
            raise CarouselProcessingError("No video durations found for mixed grid")
        max_dur = max(durations)

    inputs: list[str] = []
    for slide in slides:
        if slide.is_video:
            inputs += ["-i", str(slide.path)]
        else:
            inputs += ["-loop", "1", "-i", str(slide.path)]

    filter_parts: list[str] = []
    for i, slide in enumerate(slides):
        if slide.is_video:
            filter_parts.append(
                f"[{i}:v]scale={cell_size}:{cell_size}:force_original_aspect_ratio=decrease,"
                f"pad={cell_size}:{cell_size}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1[v{i}];"
            )
        else:
            filter_parts.append(
                f"[{i}:v]scale={cell_size}:{cell_size}:force_original_aspect_ratio=decrease,"
                f"pad={cell_size}:{cell_size}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1,"
                f"trim=duration={max_dur:.2f}[v{i}];"
            )

    layout = _build_xstack_layout(cols, rows, cell_size)
    v_labels = "".join(f"[v{i}]" for i in range(n))
    filter_parts.append(f"{v_labels}xstack=inputs={n}:layout={layout}:fill=black[outv];")

    audio_inputs: list[str] = []
    for i, slide in enumerate(slides):
        if slide.is_video and _has_audio(slide.path):
            filter_parts.append(
                f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}];"
            )
        else:
            filter_parts.append(
                f"anullsrc=r=44100:cl=stereo,atrim=duration={max_dur:.2f}[a{i}];"
            )
        audio_inputs.append(f"[a{i}]")
    filter_parts.append(f"{''.join(audio_inputs)}amix=inputs={n}:duration=longest[outa]")

    _run_ffmpeg([
        "ffmpeg", "-y", *inputs,
        "-filter_complex", " ".join(filter_parts),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(out_path),
    ])
    return out_path


# ── Cleanup / rename ────────────────────────────────────────────────────────

def _delete_slides(slides: list[SlideInfo]) -> None:
    for slide in slides:
        try:
            slide.path.unlink()
        except OSError as exc:
            logger.warning(f"Could not delete slide {slide.path.name}: {exc}")


def _rename_companion_txt(target_dir: Path, shortcode: str, composite_path: Path) -> None:
    matches = list(target_dir.glob(f"*_{shortcode}.txt"))
    if not matches:
        return
    dest = composite_path.with_suffix(".txt")
    try:
        matches[0].rename(dest)
    except OSError as exc:
        logger.warning(f"Could not rename companion txt: {exc}")


# ── Core processing function ────────────────────────────────────────────────

def process_carousel_files(
    slides: list[SlideInfo],
    shortcode: str,
    owner_username: str,
    target_dir: Path,
    *,
    cell_size: int = 640,
    make_collage: bool = True,
    make_graphic: bool = True,
    keep_slides: bool = False,
    move_to: Path = None,
) -> "Path | None":
    """
    Process a set of carousel slide files into a composite output.

    Args:
        slides:         Discovered SlideInfo list (sorted by index)
        shortcode:      Post shortcode (used for output filename)
        owner_username: Post owner (used for output filename)
        target_dir:     Directory containing the slides
        cell_size:      Grid cell size in pixels
        make_collage:   Build the collage/video composite
        make_graphic:   Append caption+comments panel to the composite
        keep_slides:    If False, delete individual slides after composite is built

    Returns:
        Path to the composite (or snapshot) on success, None if skipped.
    """
    if not make_collage:
        if make_graphic:
            logger.warning(
                "make_graphic=True requires make_collage=True — skipping graphic"
            )
        return None

    has_images = any(not s.is_video for s in slides)
    has_videos = any(s.is_video for s in slides)

    # ffmpeg check for video/mixed
    if has_videos and not _check_ffmpeg():
        logger.warning(
            f"ffmpeg not found — skipping video processing for {shortcode}. "
            f"Install from https://ffmpeg.org/download.html"
        )
        image_slides = [s for s in slides if not s.is_video]
        if len(image_slides) >= 2:
            out_path = target_dir / f"{owner_username}_{shortcode}_carousel.jpg"
            if out_path.exists():
                return out_path
            try:
                result = _build_image_collage(image_slides, out_path, cell_size)
                if not keep_slides:
                    if move_to:
                        move_to.mkdir(parents=True, exist_ok=True)
                        for s in image_slides:
                            try:
                                shutil.move(str(s.path), str(move_to / s.path.name))
                            except OSError as exc:
                                logger.warning(f"Could not move slide {s.path.name}: {exc}")
                    else:
                        _delete_slides(image_slides)
                _rename_companion_txt(target_dir, shortcode, result)
                return _apply_graphic(result, target_dir, shortcode, make_graphic)
            except CarouselProcessingError as exc:
                logger.warning(f"Image collage failed: {exc}")
        return None

    stem        = f"{owner_username}_{shortcode}_carousel"
    out_path_jpg = target_dir / f"{stem}.jpg"
    out_path_mp4 = target_dir / f"{stem}.mp4"

    if not has_videos:
        # Images only
        if out_path_jpg.exists():
            return out_path_jpg
        composite = _build_image_collage(slides, out_path_jpg, cell_size)
    elif not has_images:
        # Videos only
        if out_path_mp4.exists():
            return out_path_mp4
        composite = _build_video_concat(slides, out_path_mp4, cell_size)
    else:
        # Mixed
        if out_path_mp4.exists():
            return out_path_mp4
        max_dur = max(_get_video_duration(s.path) for s in slides if s.is_video)
        composite = _build_mixed_grid(slides, out_path_mp4, cell_size, max_dur=max_dur)

    if not keep_slides:
        if move_to:
            move_to.mkdir(parents=True, exist_ok=True)
            for slide in slides:
                try:
                    shutil.move(str(slide.path), str(move_to / slide.path.name))
                except OSError as exc:
                    logger.warning(f"Could not move slide {slide.path.name}: {exc}")
        else:
            _delete_slides(slides)
    _rename_companion_txt(target_dir, shortcode, composite)
    return _apply_graphic(composite, make_graphic)


def _apply_graphic(
    composite: Path,
    make_graphic: bool,
) -> Path:
    """Optionally append caption panel to composite. Returns composite path."""
    if not make_graphic:
        return composite

    txt_path = composite.with_suffix(".txt")
    if not txt_path.exists():
        logger.debug(f"No companion txt found for graphic: {txt_path.name}")
        return composite

    try:
        import caption_graphic
        snapshot_path = composite.with_stem(composite.stem + "_snapshot")
        result = caption_graphic.create_snapshot(composite, txt_path, snapshot_path)
        if result:
            logger.debug(f"Snapshot created: {result.name}")
    except Exception as exc:
        logger.warning(f"Caption graphic failed for {composite.name}: {exc}")

    return composite  # always return the composite, snapshot is a bonus


# ── Public API (server.py calls this) ─────────────────────────────────────

def process_carousel(
    post,
    target_dir,
    *,
    cell_size: int = 640,
    make_collage: bool = True,
    make_graphic: bool = True,
    move_to: Path = None,
) -> "Path | None":
    """
    Post-process a carousel instaloader Post.

    Args:
        post:        instaloader.Post object
        target_dir:  Directory where files were saved
        cell_size:   Grid cell size in pixels
        make_collage: Build composite
        make_graphic: Append caption panel

    Returns:
        Path to composite on success, None if skipped.
    Raises:
        CarouselProcessingError on errors the caller should surface.
    """
    target_dir = Path(target_dir)

    logger.debug(
        f"process_carousel: typename={post.typename} "
        f"mediacount={post.mediacount} shortcode={post.shortcode}"
    )

    if post.typename != "GraphSidecar" or post.mediacount <= 1:
        return None

    slides = find_carousel_files(target_dir, post.shortcode)
    if not slides:
        logger.warning(
            f"Carousel {post.shortcode}: expected {post.mediacount} slides "
            f"but found none in {target_dir}"
        )
        return None

    if len(slides) < post.mediacount:
        logger.warning(
            f"Carousel {post.shortcode}: found {len(slides)}/{post.mediacount} slides"
        )

    return process_carousel_files(
        slides,
        shortcode=post.shortcode,
        owner_username=post.owner_username,
        target_dir=target_dir,
        cell_size=cell_size,
        make_collage=make_collage,
        make_graphic=make_graphic,
        keep_slides=False,
        move_to=move_to,
    )


# ── Standalone CLI ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Post-process carousel downloads into composite files.",
        epilog=(
            "Scans input_dir for files with _N suffix (e.g. user_ABC123_1.jpg) "
            "and groups them by shortcode. Processes all carousels found unless "
            "--shortcode is given."
        ),
    )
    parser.add_argument("input_dir", type=Path,
                        help="Directory containing downloaded carousel slides")
    parser.add_argument("--shortcode", metavar="CODE",
                        help="Process only this shortcode (default: all carousels found)")
    parser.add_argument("--no-collage", action="store_true",
                        help="Skip collage/video-concat creation")
    parser.add_argument("--no-graphic", action="store_true",
                        help="Skip caption graphic appending")
    parser.add_argument("--cell-size", type=int, default=640, metavar="N",
                        help="Collage grid cell size in pixels (default: 640)")
    parser.add_argument("--keep-slides", action="store_true",
                        help="Preserve individual slide files after creating composite")
    parser.add_argument("--move-to", type=Path, metavar="DIR",
                        help="Move slide files to this directory instead of deleting them")
    args = parser.parse_args()

    target_dir = args.input_dir.expanduser().resolve()
    move_to = args.move_to.expanduser().resolve() if args.move_to else None
    if not target_dir.is_dir():
        print(f"ERROR: {target_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    carousels = _scan_directory_for_carousels(target_dir)

    if args.shortcode:
        if args.shortcode not in carousels:
            print(f"ERROR: shortcode '{args.shortcode}' not found (or has < 2 slides)",
                  file=sys.stderr)
            sys.exit(1)
        carousels = {args.shortcode: carousels[args.shortcode]}

    if not carousels:
        print("No carousels found (need 2+ slides with same shortcode).")
        sys.exit(0)

    for shortcode, slides in carousels.items():
        owner_username = _extract_owner_username(shortcode, slides)
        print(f"  {shortcode}  ({len(slides)} slides, @{owner_username})")
        try:
            result = process_carousel_files(
                slides, shortcode, owner_username, target_dir,
                cell_size=args.cell_size,
                make_collage=not args.no_collage,
                make_graphic=not args.no_graphic,
                keep_slides=args.keep_slides,
                move_to=move_to,
            )
            if result:
                print(f"    -> {result.name}")
            else:
                print(f"    -> skipped")
        except CarouselProcessingError as exc:
            print(f"    ERROR: {exc}", file=sys.stderr)
