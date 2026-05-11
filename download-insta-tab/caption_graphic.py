#!/usr/bin/env python3
"""
caption_graphic.py — Instagram-style caption+comments panel, appended to media.

Creates a white panel with caption text and top comments, then composites it
alongside a downloaded image or video to produce a "snapshot" file.
"""

import json
import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Color palette (Instagram UI) ────────────────────────────────────────────

BG_COLOR        = (255, 255, 255)   # white panel background
TEXT_COLOR      = (38, 38, 38)      # #262626 body text
MUTED_COLOR     = (142, 142, 142)   # #8E8E8E likes / secondary text
DIVIDER_COLOR   = (219, 219, 219)   # #DBDBDB rule between caption and comments
HASHTAG_COLOR   = (0, 53, 105)      # #003569 hashtags and @mentions in caption
MENTION_COLOR   = (0, 53, 105)      # same blue for @user prefix in comments

PADDING         = 10    # px margin inside panel on all sides (reduced to save space)
CAPTION_SIZE    = 20    # pt caption font (larger)
COMMENT_SIZE    = 20    # pt comment body font (larger)
LIKES_SIZE      = 6    # pt likes counter (slightly larger)
LINE_SPACING    = 3     # px between wrapped lines within a block
COMMENT_GAP     = 6    # px between individual comments
DIVIDER_MARGIN  = 7    # px above and below the horizontal rule
MAX_CAPTION_LINES = 6   # reduced from 8 to prevent panel from becoming too tall with larger font

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".webp", ".png"}

# ── Errors ───────────────────────────────────────────────────────────────────

class CaptionGraphicError(Exception):
    """Non-fatal; caller should log and continue."""


# ── Font resolution ───────────────────────────────────────────────────────────

_font_cache: dict[tuple[str, int], object] = {}

_FONT_CANDIDATES = [
    # Windows
    ("C:/Windows/Fonts/arial.ttf",    "C:/Windows/Fonts/arialbd.ttf"),
    # Linux
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    # Mac
    ("/Library/Fonts/Arial.ttf",      "/Library/Fonts/Arial Bold.ttf"),
    ("/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Helvetica.ttc"),
]


def _load_font(size: int, bold: bool = False):
    """Return a PIL ImageFont at the given size, with or without bold."""
    from PIL import ImageFont
    key = (f"{'bold' if bold else 'regular'}", size)
    if key in _font_cache:
        return _font_cache[key]

    for regular_path, bold_path in _FONT_CANDIDATES:
        path = bold_path if bold else regular_path
        if Path(path).exists():
            try:
                font = ImageFont.truetype(path, size)
                _font_cache[key] = font
                return font
            except Exception:
                continue

    # PIL built-in default (no bold distinction, fixed size ~11px)
    font = ImageFont.load_default()
    _font_cache[key] = font
    return font


# ── Metadata loading ─────────────────────────────────────────────────────────

def load_metadata(txt_path: Path) -> dict:
    """
    Read JSON from txt_path.
    Returns {"caption": str, "top_comments": list} or {} on failure.
    """
    try:
        return json.loads(txt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"Could not load metadata from {txt_path.name}: {exc}")
        return {}


# ── Text measurement helpers ─────────────────────────────────────────────────

def _text_width(text: str, font) -> int:
    """Measure rendered width of text with the given font."""
    try:
        from PIL import ImageDraw, Image as _Image
        # Use a throw-away draw context for measurement
        img = _Image.new("RGB", (1, 1))
        draw = ImageDraw.Draw(img)
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except AttributeError:
        # Older Pillow fallback
        return font.getsize(text)[0]


def _font_height(font) -> int:
    """Return the line height of the font."""
    try:
        from PIL import ImageDraw, Image as _Image
        img = _Image.new("RGB", (1, 1))
        draw = ImageDraw.Draw(img)
        bbox = draw.textbbox((0, 0), "Ay", font=font)
        return bbox[3] - bbox[1]
    except AttributeError:
        return font.getsize("Ay")[1]


def _wrap_text(text: str, font, max_width: int) -> list[str]:
    """Word-wrap text into lines that fit within max_width pixels."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split(" ") if paragraph.strip() else [""]
        current = ""
        for word in words:
            test = f"{current} {word}".strip() if current else word
            if _text_width(test, font) <= max_width:
                current = test
            else:
                if current:
                    lines.append(current)
                current = word
        if current is not None:
            lines.append(current)
    return lines


# ── Panel generation ─────────────────────────────────────────────────────────

def build_caption_panel(metadata: dict, panel_width: int):
    """
    Render a white Instagram-style panel of exactly panel_width pixels wide.
    Height is calculated dynamically based on content.
    Returns an RGB PIL Image.
    """
    from PIL import Image, ImageDraw

    caption: str = metadata.get("caption") or ""
    comments: list[dict] = metadata.get("top_comments") or []

    cap_font    = _load_font(CAPTION_SIZE)
    cap_font_b  = _load_font(CAPTION_SIZE, bold=True)
    cmt_font    = _load_font(COMMENT_SIZE)
    cmt_font_b  = _load_font(COMMENT_SIZE, bold=True)
    lk_font     = _load_font(LIKES_SIZE)

    cap_h   = _font_height(cap_font)
    cmt_h   = _font_height(cmt_font)
    lk_h    = _font_height(lk_font)
    inner_w = panel_width - 2 * PADDING

    # ── Build draw commands (measure pass) ──────────────────────────────────
    # Each command: (type, y, payload)
    # We accumulate y to know total height, then draw in a second pass.

    DrawCmd = tuple  # (kind: str, y: int, data: dict)
    cmds: list[DrawCmd] = []
    y = PADDING

    # Caption
    if caption.strip():
        cap_lines = _wrap_text(caption, cap_font, inner_w)
        if len(cap_lines) > MAX_CAPTION_LINES:
            cap_lines = cap_lines[:MAX_CAPTION_LINES]
            cap_lines[-1] = cap_lines[-1].rstrip() + "…"
        for line in cap_lines:
            cmds.append(("caption_line", y, {"text": line}))
            y += cap_h + LINE_SPACING
        y += DIVIDER_MARGIN - LINE_SPACING

    # Divider (only if there are comments)
    if comments:
        cmds.append(("divider", y, {}))
        y += 1 + DIVIDER_MARGIN

    # Comments
    for comment in comments:
        username = comment.get("username", "")
        text     = comment.get("text", "")
        likes    = comment.get("likes", 0)

        # "@username" in bold inline with comment text
        prefix = f"@{username}  "
        prefix_w = _text_width(prefix, cmt_font_b)
        remaining_w = inner_w - prefix_w

        # Wrap comment body
        body_lines = _wrap_text(text, cmt_font, remaining_w) if text.strip() else [""]

        # First line shares row with the @username prefix
        cmds.append(("comment_first", y, {
            "prefix": prefix,
            "line": body_lines[0] if body_lines else "",
        }))
        y += cmt_h + LINE_SPACING

        # Continuation lines indented by prefix width
        for cont in body_lines[1:]:
            cmds.append(("comment_cont", y, {"text": cont, "indent": prefix_w}))
            y += cmt_h + LINE_SPACING

        # Likes row
        if likes:
            cmds.append(("likes", y, {"text": f"\u2665 {likes:,}"}))
            y += lk_h + LINE_SPACING

        y += COMMENT_GAP

    y += PADDING

    # ── Draw pass ────────────────────────────────────────────────────────────
    canvas = Image.new("RGB", (panel_width, y), BG_COLOR)
    draw   = ImageDraw.Draw(canvas)

    for kind, cy, data in cmds:
        x = PADDING

        if kind == "caption_line":
            # Draw word by word to colour #hashtag and @mentions
            _draw_colored_line(draw, data["text"], x, cy, cap_font, TEXT_COLOR,
                               hashtag_color=HASHTAG_COLOR, mention_color=HASHTAG_COLOR)

        elif kind == "divider":
            draw.line([(x, cy), (panel_width - PADDING, cy)],
                      fill=DIVIDER_COLOR, width=1)

        elif kind == "comment_first":
            # Bold @username prefix
            draw.text((x, cy), data["prefix"], font=cmt_font_b, fill=MENTION_COLOR)
            prefix_w = _text_width(data["prefix"], cmt_font_b)
            # Regular comment text
            draw.text((x + prefix_w, cy), data["line"], font=cmt_font, fill=TEXT_COLOR)

        elif kind == "comment_cont":
            draw.text((x + data["indent"], cy), data["text"],
                      font=cmt_font, fill=TEXT_COLOR)

        elif kind == "likes":
            draw.text((x, cy), data["text"], font=lk_font, fill=MUTED_COLOR)

    return canvas


def _draw_colored_line(draw, text: str, x: int, y: int, font,
                        default_color, hashtag_color, mention_color) -> None:
    """Draw a line of text word by word, colouring #hashtags and @mentions."""
    for word in text.split(" "):
        if not word:
            x += _text_width(" ", font)
            continue
        if word.startswith("#"):
            color = hashtag_color
        elif word.startswith("@"):
            color = mention_color
        else:
            color = default_color
        draw.text((x, y), word, font=font, fill=color)
        x += _text_width(word + " ", font)


# ── Appending to images ───────────────────────────────────────────────────────

def append_panel_to_image(img_path: Path, txt_path: Path, out_path: Path) -> Path:
    """
    Composite a caption panel alongside the image.
    Portrait (h > w): panel goes RIGHT.
    Square/landscape (w >= h): panel goes BELOW.
    """
    from PIL import Image

    metadata = load_metadata(txt_path)
    if not metadata:
        raise CaptionGraphicError(f"No metadata found in {txt_path.name}")

    img = Image.open(img_path).convert("RGB")
    portrait = img.height > img.width

    if portrait:
        # Sidebar: at most half the image width, max 400px
        panel_w = min(img.width // 2, 400)
        panel   = build_caption_panel(metadata, panel_w)
        canvas_w = img.width + panel_w
        canvas_h = max(img.height, panel.height)
        canvas   = Image.new("RGB", (canvas_w, canvas_h), BG_COLOR)
        canvas.paste(img, (0, 0))
        # Vertically centre panel if it is shorter than the image
        panel_y  = (canvas_h - panel.height) // 2
        canvas.paste(panel, (img.width, panel_y))
    else:
        # Strip below
        panel_w = img.width
        panel   = build_caption_panel(metadata, panel_w)
        canvas_w = img.width
        canvas_h = img.height + panel.height
        canvas   = Image.new("RGB", (canvas_w, canvas_h), BG_COLOR)
        canvas.paste(img, (0, 0))
        canvas.paste(panel, (0, img.height))

    canvas.save(out_path, "JPEG", quality=92, optimize=True)
    return out_path


# ── Appending to videos ───────────────────────────────────────────────────────

def _get_video_info(path: Path) -> tuple[int, int, float]:
    """Return (width, height, duration_seconds) of a video using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    import json as _json
    data = _json.loads(result.stdout)
    w   = int(data["streams"][0]["width"])
    h   = int(data["streams"][0]["height"])
    dur = float(data["format"]["duration"])
    return w, h, dur


def append_panel_to_video(video_path: Path, txt_path: Path, out_path: Path) -> Path:
    """
    Composite a static caption panel below the video using ffmpeg.
    The panel image is saved as a temp PNG, then vstack'd under the video.
    """
    from PIL import Image as _Image

    metadata = load_metadata(txt_path)
    if not metadata:
        raise CaptionGraphicError(f"No metadata found in {txt_path.name}")

    try:
        video_w, video_h, duration = _get_video_info(video_path)
    except (subprocess.CalledProcessError, ValueError, FileNotFoundError, KeyError) as exc:
        raise CaptionGraphicError(f"Could not get video info: {exc}") from exc

    # libx264 requires even dimensions — round video width up if needed
    even_w = video_w if video_w % 2 == 0 else video_w + 1

    panel = build_caption_panel(metadata, even_w)

    with tempfile.TemporaryDirectory() as tmp:
        panel_png = Path(tmp) / "_panel.png"
        panel.save(panel_png, "PNG")

        panel_h = panel.height
        # Ensure panel height is also even for the vstack total
        even_panel_h = panel_h if panel_h % 2 == 0 else panel_h + 1

        # Use explicit -t <duration> instead of -shortest to avoid the
        # "Could not open encoder before EOF" race between the looped
        # image stream and the video stream on short clips.
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(panel_png),
            "-filter_complex",
            (
                f"[0:v]scale={even_w}:-2,setsar=1[vid];"
                f"[1:v]loop=loop=-1:size=1:start=0,"
                f"scale={even_w}:{even_panel_h},setsar=1[panel];"
                f"[vid][panel]vstack=inputs=2,format=yuv420p[outv]"
            ),
            "-map", "[outv]",
            "-map", "0:a?",
            "-c:v", "libx264", "-crf", "23", "-preset", "superfast",
            "-c:a", "copy",
            "-t", str(duration),
            "-movflags", "+faststart",
            str(out_path),
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
        except FileNotFoundError as exc:
            raise CaptionGraphicError("ffmpeg not found") from exc
        except subprocess.CalledProcessError as exc:
            # Remove any partial/zero-byte output file ffmpeg may have created
            if out_path.exists():
                out_path.unlink(missing_ok=True)
            raise CaptionGraphicError(
                f"ffmpeg failed: {exc.stderr[-500:]}"
            ) from exc

    return out_path


# ── Public entry point ────────────────────────────────────────────────────────

def create_snapshot(media_path: Path, txt_path: Path, out_path: Path) -> "Path | None":
    """
    Load metadata from txt_path, build a caption panel, and append it to
    media_path, saving the result to out_path.

    Returns out_path on success, None if metadata is missing or empty.
    Raises CaptionGraphicError on unrecoverable failures.
    """
    metadata = load_metadata(txt_path)
    if not metadata or (not metadata.get("caption") and not metadata.get("top_comments")):
        logger.debug(f"Skipping snapshot — no usable metadata in {txt_path.name}")
        return None

    suffix = media_path.suffix.lower()

    if suffix in IMAGE_EXTS:
        return append_panel_to_image(media_path, txt_path, out_path)
    elif suffix in VIDEO_EXTS:
        return append_panel_to_video(media_path, txt_path, out_path)
    else:
        raise CaptionGraphicError(f"Unsupported media type: {suffix}")


# ── Standalone CLI ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Retroactively apply caption graphics to already-downloaded posts.",
        epilog=(
            "Scans input_dir for any media file that has a matching .txt and no\n"
            "existing _snapshot file, then generates the snapshot.\n\n"
            "Works on single posts AND already-built carousel composites."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input_dir", type=Path,
                        help="Directory containing downloaded media and .txt files")
    parser.add_argument("--overwrite", action="store_true",
                        help="Re-generate snapshots that already exist")
    parser.add_argument("--delete", action="store_true",
                        help="Delete original media and .txt files after snapshot")
    args = parser.parse_args()

    target_dir = args.input_dir.expanduser().resolve()
    if not target_dir.is_dir():
        print(f"ERROR: {target_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    all_exts = IMAGE_EXTS | VIDEO_EXTS
    processed = skipped = errors = 0

    for txt_path in sorted(target_dir.glob("*.txt")):
        stem = txt_path.stem  # e.g. "natgeo_ABC123" or "user_ABC123_carousel"
        # Find a matching media file
        media_path = None
        for ext in all_exts:
            candidate = target_dir / f"{stem}{ext}"
            if candidate.exists():
                media_path = candidate
                break
        if media_path is None:
            continue  # no media for this txt

        # Determine snapshot output path
        snap_ext = ".mp4" if media_path.suffix.lower() in VIDEO_EXTS else ".jpg"
        snapshot_path = target_dir / f"{stem}_snapshot{snap_ext}"

        if snapshot_path.exists() and not args.overwrite:
            skipped += 1
            continue

        try:
            print(f"  {media_path.name} ... ", end="", flush=True)
            result = create_snapshot(media_path, txt_path, snapshot_path)
            if result:
                print(f"-> {result.name}")
                processed += 1
                if args.delete:
                    media_path.unlink()
                    txt_path.unlink()
            else:
                print("skipped (no usable metadata)")
        except CaptionGraphicError as exc:
            print(f"\n  ERROR {media_path.name}: {exc}", file=sys.stderr)
            errors += 1

    print(f"\nDone: {processed} generated, {skipped} already existed, {errors} errors.")
