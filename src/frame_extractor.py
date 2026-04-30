# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Video frame extraction and grid composition.

Ported from Tagger v1.2.4 with cross-platform ffmpeg search and
without BRAW support (deferred for a future release).

Produces a single 5760x4320 JPEG containing:
  - 64px metadata header strip (filename, duration, fps, resolution, codec)
  - N frame tiles in a 3-column grid where N = clamp(int(dur/24)+1, 4, 20)
  - each tile annotated with its source timecode (HH:MM:SS:FF)

Adaptive JPEG quality keeps file size under 4.8 MB for Claude's 5 MB limit.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


_FFMPEG_SEARCH_PATHS = [
    "/opt/homebrew/bin",                # macOS Apple Silicon Homebrew
    "/usr/local/bin",                   # macOS Intel Homebrew, Linux local
    "/usr/bin",                         # Linux distro packages
    r"C:\ffmpeg\bin",                   # Windows manual install
    r"C:\Program Files\ffmpeg\bin",     # Windows installer default
    r"C:\ProgramData\chocolatey\bin",   # Windows Chocolatey
]


def _find_tool(name: str) -> str:
    """Return the full path to an ffmpeg tool, or the bare name as fallback."""
    if platform.system() == "Windows" and not name.endswith(".exe"):
        name_with_ext = name + ".exe"
    else:
        name_with_ext = name
    for directory in _FFMPEG_SEARCH_PATHS:
        candidate = os.path.join(directory, name_with_ext)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which(name) or shutil.which(name_with_ext)
    return found if found else name


_FFPROBE = _find_tool("ffprobe")
_FFMPEG = _find_tool("ffmpeg")


def _seconds_to_timecode(seconds: float, fps: Optional[float] = None) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if fps and fps > 0:
        frame = int((seconds % 1) * fps)
        return f"{h:02d}:{m:02d}:{s:02d}:{frame:02d}"
    frac = int((seconds % 1) * 100)
    return f"{h:02d}:{m:02d}:{s:02d}.{frac:02d}"


def _get_font(size: int = 36):
    from PIL import ImageFont
    candidates = [
        "/System/Library/Fonts/Monaco.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/Library/Fonts/Courier New.ttf",
        r"C:\Windows\Fonts\cour.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _is_braw(video_path: str) -> bool:
    return Path(video_path).suffix.lower() == ".braw"


# ffprobe color_transfer values that indicate log encoding (footage will
# look flat / desaturated by design and Claude should not be told the
# scene is dim or gloomy on that basis).
_LOG_TRANSFER_TAGS = {
    "log316":          "Sony S-Log3",
    "smpte428":        "SMPTE 428-1 Cinema",
    "smpte2084":       "PQ (HDR)",
    "arib-std-b67":    "HLG (HDR)",
    "bt1361e":         "BT.1361",
    "iec61966-2-4":    "xvYCC",
    "apple-log":       "Apple Log",
}

# Camera-vendor strings sometimes show up in container tags (Format
# Description, Encoder, Compression Name) when the color_transfer field
# is missing. Used as a secondary signal.
_LOG_TAG_HINTS = (
    ("s-log3",   "Sony S-Log3"),
    ("slog3",    "Sony S-Log3"),
    ("logc4",    "ARRI LogC4"),
    ("logc3",    "ARRI LogC3"),
    ("log-c",    "ARRI LogC"),
    ("v-log",    "Panasonic V-Log"),
    ("vlog",     "Panasonic V-Log"),
    ("n-log",    "Nikon N-Log"),
    ("clog2",    "Canon Log 2"),
    ("clog3",    "Canon Log 3"),
    ("c-log",    "Canon Log"),
    ("apple log","Apple Log"),
)


def _detect_log_color_space(stream_info: dict) -> Optional[str]:
    """Return a friendly label for the source color space if log/HDR detected.

    Reads color_transfer first (most reliable), falls back to scanning
    tag strings for camera-vendor log markers. Returns None for plain
    Rec.709 / sRGB / unmarked footage.
    """
    transfer = (stream_info.get("color_transfer") or "").lower()
    if transfer and transfer in _LOG_TRANSFER_TAGS:
        return _LOG_TRANSFER_TAGS[transfer]

    tags = stream_info.get("tags") or {}
    haystack = " ".join(str(v).lower() for v in tags.values())
    for hint, label in _LOG_TAG_HINTS:
        if hint in haystack:
            return label
    return None


class FrameExtractor:
    CANVAS_W = 5760
    CANVAS_H = 4320
    CANVAS_COLS = 3
    CANVAS_CELL_W = CANVAS_W // CANVAS_COLS
    HEADER_H = 64

    @staticmethod
    def get_framerate(video_path: str) -> Optional[float]:
        if _is_braw(video_path):
            from braw_extractor import get_braw_info
            info = get_braw_info(video_path)
            return info.get("fps") if info else None
        try:
            r = subprocess.run(
                [_FFPROBE, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=r_frame_rate",
                 "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return None
            raw = r.stdout.strip()
            if "/" in raw:
                num, den = raw.split("/")
                return float(num) / float(den) if float(den) else None
            return float(raw)
        except Exception as e:
            logger.error(f"Framerate error: {e}")
            return None

    @staticmethod
    def get_duration(video_path: str) -> Optional[float]:
        if _is_braw(video_path):
            from braw_extractor import get_braw_duration
            return get_braw_duration(video_path)
        try:
            r = subprocess.run(
                [_FFPROBE, "-v", "error",
                 "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                return float(r.stdout.strip())
            return None
        except Exception as e:
            logger.error(f"Duration error: {e}")
            return None

    @staticmethod
    def _get_video_info(video_path: str) -> dict:
        if _is_braw(video_path):
            from braw_extractor import get_braw_info
            return get_braw_info(video_path) or {}
        try:
            r = subprocess.run(
                [_FFPROBE, "-v", "error", "-select_streams", "v:0",
                 "-show_entries",
                 "stream=codec_name,width,height,color_space,color_transfer,color_primaries:stream_tags",
                 "-of", "json", video_path],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return {}
            streams = json.loads(r.stdout).get("streams", [])
            s = streams[0] if streams else {}
            color_label = _detect_log_color_space(s)
            info = {
                "codec":         s.get("codec_name", "unknown"),
                "width":         s.get("width", 0),
                "height":        s.get("height", 0),
                "color_space":   s.get("color_space"),
                "color_transfer":s.get("color_transfer"),
            }
            if color_label:
                info["color_label"] = color_label
            return info
        except Exception as e:
            logger.warning(f"Video info error: {e}")
            return {}

    @staticmethod
    def _compute_frame_count(duration: float) -> int:
        return max(4, min(20, int(duration / 24) + 1))

    @staticmethod
    def _compute_cell_height(n: int) -> int:
        rows = (n + FrameExtractor.CANVAS_COLS - 1) // FrameExtractor.CANVAS_COLS
        return (FrameExtractor.CANVAS_H - FrameExtractor.HEADER_H) // rows

    @staticmethod
    def _compute_percentages(n: int, offset: float = 0.0) -> List[float]:
        start = min(max(0.10 + offset, 0.05), 0.90)
        end = min(0.85 + offset, 0.95)
        if n == 1:
            return [(start + end) / 2]
        step = (end - start) / (n - 1)
        return [start + i * step for i in range(n)]

    @staticmethod
    def _annotate_timecode(image, seconds: float, fps: Optional[float] = None):
        from PIL import ImageDraw
        img = image.copy()
        draw = ImageDraw.Draw(img)
        tc = _seconds_to_timecode(seconds, fps)
        font_size = max(24, img.width // 64)
        font = _get_font(font_size)
        padding = max(8, img.width // 240)
        try:
            bbox = draw.textbbox((0, 0), tc, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except AttributeError:
            tw, th = len(tc) * (font_size // 2), font_size
        x = padding
        y = img.height - th - padding * 2
        draw.rectangle(
            [x - padding, y - padding, x + tw + padding, y + th + padding],
            fill=(20, 20, 20),
        )
        draw.text((x, y), tc, font=font, fill=(255, 255, 255))
        return img

    @staticmethod
    def _build_header_strip(video_path, duration, fps, video_info, grid_width):
        try:
            from PIL import Image as PILImage, ImageDraw
        except ImportError:
            return None
        height = 64
        bg = (30, 35, 41)
        fg = (255, 255, 255)
        img = PILImage.new("RGB", (grid_width, height), bg)
        draw = ImageDraw.Draw(img)
        font = _get_font(22)
        name = Path(video_path).name
        dur_m = int(duration // 60)
        dur_s = int(duration % 60)
        fps_str = f"{fps:.2f} fps" if fps else "? fps"
        res_str = (f"{video_info['width']}x{video_info['height']}"
                   if video_info.get("width") else "? res")
        codec = video_info.get("codec", "?")
        color_label = video_info.get("color_label")
        if color_label:
            text = (
                f"{name}  |  {dur_m}:{dur_s:02d}  |  {fps_str}  |  {res_str}  "
                f"|  {codec}  |  COLOR: {color_label} (log/HDR -- flat by design)"
            )
        else:
            text = f"{name}  |  {dur_m}:{dur_s:02d}  |  {fps_str}  |  {res_str}  |  {codec}"
        draw.text((16, (height - 22) // 2), text, font=font, fill=fg)
        return img

    @staticmethod
    def _extract_opencv_cells(video_path, duration, fps, percentages):
        try:
            import cv2
            from PIL import Image as PILImage
        except ImportError as e:
            logger.error(f"opencv-python and Pillow required: {e}")
            return []
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            logger.error(f"OpenCV could not open: {video_path}")
            return []
        cells = []
        try:
            for pct in percentages:
                cap.set(cv2.CAP_PROP_POS_MSEC, duration * pct * 1000)
                ret, frame = cap.read()
                if not ret:
                    continue
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                cells.append(PILImage.fromarray(rgb))
        finally:
            cap.release()
        return cells

    @staticmethod
    def _extract_braw_cells(video_path, percentages):
        """Extract PIL Images from a .braw file via the Blackmagic RAW SDK."""
        from braw_extractor import extract_frames_braw
        images, _ = extract_frames_braw(video_path, percentages)
        return images or []

    @staticmethod
    def _stitch_from_images(cells, video_path, duration, fps, video_info):
        try:
            from PIL import Image as PILImage
        except ImportError:
            return None, None
        if not cells:
            return None, None

        COLS = FrameExtractor.CANVAS_COLS
        CELL_W = FrameExtractor.CANVAS_CELL_W
        CELL_H = FrameExtractor._compute_cell_height(len(cells))
        resized = [c.resize((CELL_W, CELL_H), PILImage.LANCZOS) for c in cells]
        grid_w = FrameExtractor.CANVAS_W

        header = None
        if video_path and duration and video_info is not None:
            header = FrameExtractor._build_header_strip(
                video_path, duration, fps, video_info, grid_w
            )

        canvas = PILImage.new("RGB", (FrameExtractor.CANVAS_W, FrameExtractor.CANVAS_H), (0, 0, 0))
        y_offset = 0
        if header:
            canvas.paste(header, (0, 0))
            y_offset = header.height
        for i, cell in enumerate(resized):
            canvas.paste(cell, ((i % COLS) * CELL_W, y_offset + (i // COLS) * CELL_H))

        temp_dir = tempfile.mkdtemp(prefix="stitch_")
        out_path = os.path.join(temp_dir, "grid.jpg")

        # Anthropic enforces a 5 MB limit on the base64-encoded image. Base64
        # inflates by ~33%, so the JPEG must stay below ~3.6 MB to fit safely.
        _MAX_BYTES = 3_600_000
        quality = 85
        canvas.save(out_path, "JPEG", quality=quality)
        while os.path.getsize(out_path) > _MAX_BYTES and quality > 40:
            quality -= 5
            canvas.save(out_path, "JPEG", quality=quality)

        logger.info(
            f"Stitched {len(resized)} frames -> {out_path} "
            f"(quality={quality}, {os.path.getsize(out_path):,} bytes)"
        )
        return out_path, temp_dir

    @staticmethod
    def extract_and_stitch(
        video_path: str,
        pct_offset: float = 0.0,
    ) -> Tuple[Optional[str], Optional[str]]:
        if not os.path.exists(video_path):
            logger.error(f"Video not found: {video_path}")
            return None, None
        duration = FrameExtractor.get_duration(video_path)
        if not duration:
            return None, None
        fps = FrameExtractor.get_framerate(video_path)
        info = FrameExtractor._get_video_info(video_path)
        n = FrameExtractor._compute_frame_count(duration)
        percentages = FrameExtractor._compute_percentages(n, offset=pct_offset)
        if _is_braw(video_path):
            cells = FrameExtractor._extract_braw_cells(video_path, percentages)
        else:
            cells = FrameExtractor._extract_opencv_cells(video_path, duration, fps, percentages)
        if not cells:
            return None, None
        annotated = [
            FrameExtractor._annotate_timecode(cell, duration * pct, fps)
            for cell, pct in zip(cells, percentages)
        ]
        return FrameExtractor._stitch_from_images(annotated, video_path, duration, fps, info)

    @staticmethod
    def cleanup_frames(frame_paths: List[str], temp_dir: Optional[str] = None) -> None:
        for p in frame_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass


def test_ffmpeg() -> bool:
    for tool in (_FFMPEG, _FFPROBE):
        try:
            r = subprocess.run([tool, "-version"], capture_output=True, timeout=5)
            if r.returncode != 0:
                return False
        except Exception:
            return False
    return True
