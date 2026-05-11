#!/usr/bin/env python3
"""
Carousel post-processing: combine multi-slide Instagram posts into single composite files.

Supports:
- Image-only carousels → static collage (PIL)
- Video-only carousels → concatenated video (ffmpeg)
- Mixed carousels → video grid with images frozen, videos playing (ffmpeg xstack)
"""

import logging
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

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
            is_video = m.group(3) == "mp4"
            matched.append((slide_num, f, is_video))
    matched.sort(key=lambda t: t[0])
    return [SlideInfo(path=f, is_video=is_video, index=idx) for idx, f, is_video in matched]


# ── Image collage (PIL) ────────────────────────────────────────────────────

def _grid_dims(n: int) -> tuple[int, int]:
    """Calculate responsive grid dimensions: cols = ceil(sqrt(n)), rows = ceil(n / cols)."""
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return cols, rows


def _build_image_collage(
    slides: list[SlideInfo],
    out_path: Path,
    cell_size: int = 640,
) -> Path:
    """
    Create a static image collage from slide images.
    Each image is fit to cell_size x cell_size (thumbnail, preserve aspect ratio),
    centered in its grid cell, with dark background filling empty space.
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise CarouselProcessingError(
            "PIL/Pillow required for image collage. Install: pip install Pillow"
        ) from exc

    n = len(slides)
    cols, rows = _grid_dims(n)
    canvas_w = cols * cell_size
    canvas_h = rows * cell_size

    # Dark background for letterbox/pillarbox areas
    canvas = Image.new("RGB", (canvas_w, canvas_h), color=(20, 20, 20))

    for i, slide in enumerate(slides):
        if slide.is_video:
            # Skip video slides in image-only collage (shouldn't happen if called correctly)
            continue

        try:
            img = Image.open(slide.path).convert("RGB")
        except Exception as exc:
            logger.warning(f"Failed to open image {slide.path}: {exc}")
            continue

        # Fit within cell, preserve aspect ratio
        img.thumbnail((cell_size, cell_size), Image.LANCZOS)

        # Center within grid cell
        col = i % cols
        row = i // cols
        cell_x = col * cell_size
        cell_y = row * cell_size
        paste_x = cell_x + (cell_size - img.width) // 2
        paste_y = cell_y + (cell_size - img.height) // 2

        canvas.paste(img, (paste_x, paste_y))

    try:
        canvas.save(out_path, "JPEG", quality=92, optimize=True)
    except Exception as exc:
        raise CarouselProcessingError(f"Failed to save collage to {out_path}: {exc}") from exc

    return out_path


# ── ffmpeg helpers ────────────────────────────────────────────────────────

def _check_ffmpeg() -> bool:
    """Check if ffmpeg and ffprobe are available."""
    return (
        shutil.which("ffmpeg") is not None
        and shutil.which("ffprobe") is not None
    )


def _run_ffmpeg(cmd: list[str]) -> None:
    """Execute ffmpeg command, raising CarouselProcessingError on failure."""
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
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
    """Get video duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return float(result.stdout.strip())
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as exc:
        raise CarouselProcessingError(
            f"Failed to get duration of {path.name}: {exc}"
        ) from exc


# ── Video concatenation ────────────────────────────────────────────────────

def _build_video_concat(
    slides: list[SlideInfo],
    out_path: Path,
    cell_size: int = 640,
) -> Path:
    """
    Concatenate video slides into a single video.
    Each video is scaled to cell_size x cell_size with letterbox padding,
    frame rate normalized to 30fps.
    """
    n = len(slides)
    inputs: list[str] = []
    for slide in slides:
        inputs += ["-i", str(slide.path)]

    # Build filter_complex for scale+pad, fps, and concat
    filter_parts: list[str] = []

    # Per-video normalization
    for i in range(n):
        filter_parts.append(
            f"[{i}:v]scale={cell_size}:{cell_size}:force_original_aspect_ratio=decrease,"
            f"pad={cell_size}:{cell_size}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1[v{i}];"
        )
        filter_parts.append(
            f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}];"
        )

    # Concat all video streams
    v_labels = "".join(f"[v{i}]" for i in range(n))
    a_labels = "".join(f"[a{i}]" for i in range(n))
    filter_parts.append(f"{v_labels}concat=n={n}:v=1:a=0[outv];")
    filter_parts.append(f"{a_labels}concat=n={n}:v=0:a=1[outa]")

    filter_complex = " ".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]

    _run_ffmpeg(cmd)
    return out_path


# ── Mixed grid (images + videos) ──────────────────────────────────────────

def _build_xstack_layout(cols: int, rows: int, cell_size: int) -> str:
    """
    Generate xstack layout string with absolute pixel positions.
    For a 2x2 grid at cell_size=640: "0_0|640_0|0_640|640_640"
    """
    positions: list[str] = []
    for idx in range(cols * rows):
        col = idx % cols
        row = idx // cols
        x = col * cell_size
        y = row * cell_size
        positions.append(f"{x}_{y}")
    return "|".join(positions)


def _build_mixed_grid(
    slides: list[SlideInfo],
    out_path: Path,
    cell_size: int = 640,
    max_dur: float = 0,
) -> Path:
    """
    Create a video grid with images as static frames and videos playing.
    All content is synchronized: images display for max_dur seconds,
    videos play from start and loop if shorter than max_dur.
    Grid is responsive based on slide count.
    """
    n = len(slides)
    cols, rows = _grid_dims(n)

    if max_dur <= 0:
        # Calculate max_dur from video slides
        video_durations = []
        for slide in slides:
            if slide.is_video:
                try:
                    video_durations.append(_get_video_duration(slide.path))
                except CarouselProcessingError:
                    logger.warning(f"Could not determine duration of {slide.path}")
        if not video_durations:
            raise CarouselProcessingError("No video durations found for mixed grid")
        max_dur = max(video_durations)

    # Build ffmpeg inputs with -loop flag for images
    inputs: list[str] = []
    for slide in slides:
        if slide.is_video:
            inputs += ["-i", str(slide.path)]
        else:
            inputs += ["-loop", "1", "-i", str(slide.path)]

    # Build filter_complex
    filter_parts: list[str] = []

    # Per-stream normalization
    for i, slide in enumerate(slides):
        if slide.is_video:
            # Video: scale+pad+fps (no trim, plays full duration)
            filter_parts.append(
                f"[{i}:v]scale={cell_size}:{cell_size}:force_original_aspect_ratio=decrease,"
                f"pad={cell_size}:{cell_size}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1[v{i}];"
            )
        else:
            # Image: scale+pad+fps+trim to max_dur
            filter_parts.append(
                f"[{i}:v]scale={cell_size}:{cell_size}:force_original_aspect_ratio=decrease,"
                f"pad={cell_size}:{cell_size}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1,"
                f"trim=duration={max_dur:.2f}[v{i}];"
            )

    # xstack layout
    layout = _build_xstack_layout(cols, rows, cell_size)
    v_labels = "".join(f"[v{i}]" for i in range(n))
    filter_parts.append(
        f"{v_labels}xstack=inputs={n}:layout={layout}:fill=black[outv];"
    )

    # Audio: mix from video slides, silence from image slots
    audio_inputs: list[str] = []

    for i, slide in enumerate(slides):
        if slide.is_video:
            filter_parts.append(
                f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}];"
            )
            audio_inputs.append(f"[a{i}]")
        else:
            # Silence for image slots, trimmed to max_dur
            filter_parts.append(
                f"anullsrc=r=44100:cl=stereo,atrim=duration={max_dur:.2f}[a{i}];"
            )
            audio_inputs.append(f"[a{i}]")

    a_all = "".join(audio_inputs)
    filter_parts.append(f"{a_all}amix=inputs={n}:duration=longest[outa]")

    filter_complex = " ".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-crf", "23", "-preset", "fast",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]

    _run_ffmpeg(cmd)
    return out_path


# ── Cleanup ────────────────────────────────────────────────────────────

def _delete_slides(slides: list[SlideInfo]) -> None:
    """Delete individual slide files after a successful composite was created."""
    for slide in slides:
        try:
            slide.path.unlink()
        except OSError as exc:
            logger.warning(f"Could not delete slide {slide.path.name}: {exc}")


# ── Main entry point ────────────────────────────────────────────────────

def process_carousel(
    post,
    target_dir,
    *,
    cell_size: int = 640,
) -> Path | None:
    """
    Post-process a carousel post.

    Args:
        post: instaloader.Post object
        target_dir: Path or str where instaloader saved the files
        cell_size: px per grid cell (default 640)

    Returns:
        Path to the generated composite file, or None if skipped.

    Raises:
        CarouselProcessingError: on fatal errors that caller should log.
    """
    target_dir = Path(target_dir)

    # Debug info
    logger.debug(f"process_carousel called: post.typename={post.typename}, mediacount={post.mediacount}, shortcode={post.shortcode}")

    # Only process carousels with multiple slides
    if post.typename != "GraphSidecar" or post.mediacount <= 1:
        logger.debug(f"Skipping: not a carousel or single item")
        return None

    # Discover downloaded slide files
    logger.debug(f"Looking for carousel files in {target_dir}")
    slides = find_carousel_files(target_dir, post.shortcode)
    logger.debug(f"Found {len(slides)} slide files")
    if not slides:
        logger.warning(
            f"Carousel post {post.shortcode} expected {post.mediacount} slides "
            f"but no files found in {target_dir}"
        )
        return None

    if len(slides) < post.mediacount:
        logger.warning(
            f"Carousel post {post.shortcode}: found {len(slides)}/{post.mediacount} "
            f"slides (some may still be downloading)"
        )

    # Classify by content type
    has_images = any(not s.is_video for s in slides)
    has_videos = any(s.is_video for s in slides)

    # Check ffmpeg availability for video/mixed cases
    if has_videos and not _check_ffmpeg():
        logger.warning(
            f"ffmpeg not found — skipping video processing for {post.shortcode}. "
            f"Install from https://ffmpeg.org/download.html"
        )
        # Fallback: if images present, still produce collage
        image_slides = [s for s in slides if not s.is_video]
        if len(image_slides) >= 2:
            out_path = target_dir / f"{post.shortcode}_carousel.jpg"
            if out_path.exists():
                logger.debug(f"Carousel output already exists: {out_path.name}")
                return out_path
            try:
                result = _build_image_collage(image_slides, out_path, cell_size)
                _delete_slides(image_slides)
                return result
            except CarouselProcessingError as exc:
                logger.warning(f"Image collage failed: {exc}")
        return None

    out_path_jpg = target_dir / f"{post.shortcode}_carousel.jpg"
    out_path_mp4 = target_dir / f"{post.shortcode}_carousel.mp4"

    # Images only
    if not has_videos:
        if out_path_jpg.exists():
            logger.debug(f"Carousel output already exists: {out_path_jpg.name}")
            return out_path_jpg
        result = _build_image_collage(slides, out_path_jpg, cell_size)
        _delete_slides(slides)
        return result

    # Videos only
    if not has_images:
        if out_path_mp4.exists():
            logger.debug(f"Carousel output already exists: {out_path_mp4.name}")
            return out_path_mp4
        result = _build_video_concat(slides, out_path_mp4, cell_size)
        _delete_slides(slides)
        return result

    # Mixed: images + videos
    if out_path_mp4.exists():
        logger.debug(f"Carousel output already exists: {out_path_mp4.name}")
        return out_path_mp4

    max_dur = max(_get_video_duration(s.path) for s in slides if s.is_video)
    result = _build_mixed_grid(slides, out_path_mp4, cell_size, max_dur=max_dur)
    _delete_slides(slides)
    return result
