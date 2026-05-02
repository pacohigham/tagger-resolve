# Copyright 2026 Tagger, LLC -- support@tagger.mov
"""Video frame extraction and grid composition.

Ported from Tagger v1.2.4 with cross-platform ffmpeg search and
without BRAW support (deferred for a future release).

Produces a single 5760x4320 JPEG containing:
  - 64px metadata header strip (filename, duration, fps, resolution, codec)
  - N frame tiles in a 3-column grid where N = clamp(int(dur/24)+1, 6, 20)
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


# Substrings in ffmpeg stderr that indicate the input contains a codec
# the local ffmpeg cannot decode. These are vendor-locked formats (ARRIRAW,
# REDCODE, etc) that require their respective paid SDKs. We probe once
# per file and log a friendly message instead of hammering with N calls.
_VENDOR_LOCKED_SIGNATURES = (
    "could not resolve file descriptor strong ref",   # ARRIRAW in MXF
    "no decoder found for: none",                     # ARRIRAW (paired with above)
    "Could not find codec parameters for stream",     # generic missing decoder
)


def _ffmpeg_can_decode(video_path: str) -> bool:
    """Probe whether ffmpeg has any decoder for this file's video stream.

    Runs a no-op transcode of one packet to /dev/null. Returns False (and
    logs once at info level) if the probe hits a known vendor-lock signal.
    """
    try:
        r = subprocess.run(
            [
                _FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-i", video_path,
                "-map", "0:v:0",
                "-frames:v", "1",
                "-f", "null", os.devnull,
            ],
            capture_output=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return True   # don't block on a slow file; let the real extractor try
    except Exception:
        return True
    if r.returncode == 0:
        return True
    err = r.stderr.decode(errors="replace")
    for sig in _VENDOR_LOCKED_SIGNATURES:
        if sig in err:
            logger.info(
                f"{Path(video_path).name}: vendor-locked codec detected "
                f"(ARRIRAW / REDCODE / similar). ffmpeg cannot decode. "
                f"Skipping; native support requires the camera vendor's SDK."
            )
            return False
    # Unknown error -- let the per-frame extractor retry and surface it
    return True


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


def _detect_log_color_space(stream_info: dict, filename: str = "") -> Optional[str]:
    """Return a friendly label for the source color space if log/HDR detected.

    Detection priority:
      1. color_transfer field (most reliable)
      2. stream tag strings for camera-vendor log markers
      3. filename hints (last resort -- some cameras tag color incorrectly,
         e.g. Sony FX30 mp4 marks itself bt709 even when shooting S-Log3)

    Returns None for plain Rec.709 / sRGB / unmarked footage.
    """
    transfer = (stream_info.get("color_transfer") or "").lower()
    if transfer and transfer in _LOG_TRANSFER_TAGS:
        return _LOG_TRANSFER_TAGS[transfer]

    tags = stream_info.get("tags") or {}
    tag_haystack = " ".join(str(v).lower() for v in tags.values())
    for hint, label in _LOG_TAG_HINTS:
        if hint in tag_haystack:
            return label

    # Filename fallback for cameras that strip or mistag color metadata
    if filename:
        fn = filename.lower().replace("_", " ").replace("-", " ")
        for hint, label in _LOG_TAG_HINTS:
            if hint in fn:
                return label
    return None


# Brand-name normalization. Container tags are inconsistent ("Sony" vs
# "SONY" vs "Sony Corporation" vs "Sony Imaging"); fold to canonical names
# so the Resolve Keyword bin gets a single sub-bin per manufacturer.
_BRAND_CANONICAL = {
    "blackmagic":          "Blackmagic",
    "blackmagic design":   "Blackmagic",
    "bmd":                 "Blackmagic",
    "arri":                "ARRI",
    "arnold & richter":    "ARRI",
    "sony":                "Sony",
    "canon":               "Canon",
    "panasonic":           "Panasonic",
    "nikon":               "Nikon",
    "apple":               "Apple",
    "apple inc":           "Apple",
    "apple computer":      "Apple",
    "dji":                 "DJI",
    "sz dji":              "DJI",
    "gopro":               "GoPro",
    "red":                 "RED",
    "red digital cinema":  "RED",
    "fujifilm":            "Fujifilm",
    "leica":               "Leica",
    "samsung":             "Samsung",
    "google":              "Google",
}

# Compressor / encoder strings that strongly imply a manufacturer when
# the make tag is missing. Matched substring, case-insensitive.
_BRAND_HINTS_BY_TAG = (
    ("blackmagic",       "Blackmagic"),
    ("apple prores",     "Apple"),
    ("apple log",        "Apple"),
    ("xavc",             "Sony"),
    ("xdcam",            "Sony"),
    ("s-log",            "Sony"),
    ("logc",             "ARRI"),
    ("alexa",            "ARRI"),
    ("arriraw",          "ARRI"),
    ("v-log",            "Panasonic"),
    ("vlog",             "Panasonic"),
    ("n-log",            "Nikon"),
    ("clog",             "Canon"),
    ("canon log",        "Canon"),
    ("dji",              "DJI"),
    ("gopro",            "GoPro"),
    ("redcode",          "RED"),
)


# Filename-based fallback. Many cameras (especially Sony's pro-consumer
# bodies) strip ALL camera metadata from the H.264/H.265 export, so the
# only signal we have is the filename. Editors often name files with the
# camera body / picture profile in the filename intentionally for this
# reason. Order matters: longer/more-specific tokens first.
_FILENAME_MODEL_HINTS = (
    # Sony bodies
    ("fx30",   "Sony", "FX30"),
    ("fx3",    "Sony", "FX3"),
    ("fx6",    "Sony", "FX6"),
    ("fx9",    "Sony", "FX9"),
    ("a7s",    "Sony", "A7S"),
    ("a7iv",   "Sony", "A7 IV"),
    ("a7iii",  "Sony", "A7 III"),
    ("a7riv",  "Sony", "A7R IV"),
    ("venice", "Sony", "Venice"),
    # ARRI
    ("alexa35",     "ARRI", "ALEXA 35"),
    ("alexa-mini",  "ARRI", "ALEXA Mini"),
    ("alexa",       "ARRI", "ALEXA"),
    ("amira",       "ARRI", "AMIRA"),
    # Blackmagic
    ("ursa",        "Blackmagic", "URSA"),
    ("bmpcc",       "Blackmagic", "Pocket Cinema Camera"),
    ("pocket",      "Blackmagic", "Pocket Cinema Camera"),
    # Canon
    ("c70",   "Canon", "EOS C70"),
    ("c300",  "Canon", "EOS C300"),
    ("c500",  "Canon", "EOS C500"),
    ("r5c",   "Canon", "EOS R5 C"),
    ("r5",    "Canon", "EOS R5"),
    # Panasonic
    ("gh6",   "Panasonic", "GH6"),
    ("gh5",   "Panasonic", "GH5"),
    ("s1h",   "Panasonic", "S1H"),
    # DJI
    ("mavic",   "DJI", "Mavic"),
    ("inspire", "DJI", "Inspire"),
    ("ronin",   "DJI", "Ronin"),
    # GoPro
    ("hero",  "GoPro", "Hero"),
    # RED
    ("v-raptor", "RED", "V-Raptor"),
    ("komodo",   "RED", "Komodo"),
)


def _detect_from_filename(filename: str) -> tuple[Optional[str], Optional[str]]:
    """Best-effort camera (make, model) inferred from the filename.

    Used when container metadata is missing -- common on Sony bodies that
    strip make/model from H.264/H.265 exports. Filenames frequently
    encode the camera body and picture profile by convention.
    """
    if not filename:
        return None, None
    norm = filename.lower().replace("_", " ").replace("-", " ")
    for token, brand, model in _FILENAME_MODEL_HINTS:
        if token in norm:
            return brand, model
    return None, None


def _normalize_make(raw: Optional[str]) -> Optional[str]:
    """Map a raw 'make' tag to a canonical brand name. None on no match."""
    if not raw:
        return None
    norm = str(raw).strip().lower()
    if not norm:
        return None
    if norm in _BRAND_CANONICAL:
        return _BRAND_CANONICAL[norm]
    # Partial-prefix match for verbose vendor strings
    for key, canonical in _BRAND_CANONICAL.items():
        if norm.startswith(key) or key in norm:
            return canonical
    return None


def _clean_model(raw: Optional[str], make: Optional[str] = None) -> Optional[str]:
    """Light cleanup of camera model string. None on empty.

    Collapses multiple whitespace and strips a redundant leading brand
    prefix when it matches the detected make (so we get 'AMIRA' not
    'ARRI AMIRA' and 'Pocket Cinema Camera 4K' not 'Blackmagic Pocket
    Cinema Camera 4K' -- the brand is captured separately on camera_make
    and the editor sees both sub-bins in Resolve's Keyword tree).
    """
    if not raw:
        return None
    s = " ".join(str(raw).split())  # collapse whitespace
    if make:
        prefix = make.lower()
        if s.lower().startswith(prefix + " "):
            s = s[len(prefix) + 1:].lstrip()
    return s or None


def _detect_camera_metadata(format_info: dict, stream_info: dict, filename: str = "") -> dict:
    """Extract {camera_make, camera_model} from ffprobe output.

    Detection priority (high -> low confidence):
      1. Vendor-specific tags from cameras that embed full metadata:
         - ARRI .mov: com.arri.camera.CameraModel
         - ARRI .mxf descriptor: company_name + product_name
         - Sony, Canon, Panasonic equivalents
      2. Standard make/model tags (most camera apps + iPhones):
         - format.tags.make + .model
         - com.apple.quicktime.make + .model
      3. Compressor / encoder substring scan (last resort)

    Apple's QuickTime container often shows vendor_id='appl' or encoder
    'Apple ProRes' even on ARRI footage, so those are explicitly NOT
    treated as a Make signal. Only matches as last resort if nothing
    else fires.

    Either field returns 'Unknown' when undetectable.
    """
    fmt_tags = (format_info.get("tags") or {})
    str_tags = (stream_info.get("tags") or {})
    all_tags = {**str_tags, **fmt_tags}    # fmt_tags wins on conflict

    make: Optional[str] = None
    model: Optional[str] = None

    # --- Tier 1: vendor-specific camera-metadata atoms -----------------------

    # ARRI .mov files (CameraModel, CameraIndex, etc as com.arri.camera.* keys)
    arri_model = all_tags.get("com.arri.camera.CameraModel")
    if arri_model:
        make = "ARRI"
        model = _clean_model(arri_model, make=make)

    # ARRI .mxf descriptor: company_name + product_name
    if not make:
        company = all_tags.get("company_name")
        product = all_tags.get("product_name")
        if company:
            normalised = _normalize_make(company)
            if normalised:
                make = normalised
                if product:
                    model = _clean_model(product, make=make)

    # Sony XAVC: vendor-specific atoms vary; rarely useful
    # Canon EOS atoms: com.canon.* (some models)
    # iPhone / Apple Camera: com.apple.quicktime.{make,model} -- handled in tier 2

    # --- Tier 2: standard make/model tags ------------------------------------

    if not make:
        for k in ("make", "manufacturer", "MAKE",
                 "com.apple.quicktime.make"):
            v = all_tags.get(k)
            if v:
                normalised = _normalize_make(v)
                if normalised:
                    make = normalised
                    break

    if not model:
        for k in ("model", "MODEL", "camera_model",
                 "com.apple.quicktime.model"):
            v = all_tags.get(k)
            if v:
                model = _clean_model(v, make=make)
                if model:
                    break

    # --- Tier 3: compressor/encoder substring scan (lowest confidence) ------
    #
    # Skip this when we already have a make. Note: Apple ProRes / vendor_id=appl
    # is never a reliable signal for make -- ARRI cameras export ProRes via
    # Apple's encoder, so the container shows Apple even though the camera
    # was an ALEXA. Brand hints here only help when no other signal exists
    # AND the hint isn't a generic codec name.
    if not make:
        haystack_parts = []
        for k in ("compressor_name", "encoder", "comment", "handler_name"):
            v = all_tags.get(k)
            if v:
                haystack_parts.append(str(v).lower())
        haystack = " ".join(haystack_parts)
        if haystack:
            for hint, brand in _BRAND_HINTS_BY_TAG:
                if hint in haystack:
                    make = brand
                    break

    # --- Tier 4: filename heuristics ----------------------------------------
    # Last-resort signal when the camera stripped its metadata (common on
    # Sony pro-consumer mp4 exports). Editors frequently encode the body
    # in the filename intentionally.
    if not make or not model:
        fn_make, fn_model = _detect_from_filename(filename)
        if not make and fn_make:
            make = fn_make
        if not model and fn_model:
            model = fn_model

    return {
        "camera_make":  make or "Unknown",
        "camera_model": model or "Unknown",
    }


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
                [_FFPROBE, "-v", "error",
                 "-select_streams", "v:0",
                 "-show_entries",
                 "stream=codec_name,width,height,color_space,color_transfer,color_primaries:"
                 "stream_tags:format_tags",
                 "-show_format",
                 "-of", "json", video_path],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return {}
            parsed = json.loads(r.stdout)
            streams = parsed.get("streams", [])
            s = streams[0] if streams else {}
            f = parsed.get("format", {}) or {}
            fn = Path(video_path).name
            color_label = _detect_log_color_space(s, filename=fn)
            cam = _detect_camera_metadata(f, s, filename=fn)
            info = {
                "codec":         s.get("codec_name", "unknown"),
                "width":         s.get("width", 0),
                "height":        s.get("height", 0),
                "color_space":   s.get("color_space"),
                "color_transfer":s.get("color_transfer"),
                "camera_make":   cam["camera_make"],
                "camera_model":  cam["camera_model"],
            }
            if color_label:
                info["color_label"] = color_label
            return info
        except Exception as e:
            logger.warning(f"Video info error: {e}")
            return {}

    @staticmethod
    def _compute_frame_count(duration: float) -> int:
        return max(6, min(20, int(duration / 24) + 1))

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
    def _extract_ffmpeg_cells(video_path, duration, fps, percentages):
        """Extract frames at given video percentages using system ffmpeg.

        Replaces the previous OpenCV-based implementation. System ffmpeg
        has substantially broader codec support than OpenCV's bundled
        ffmpeg -- it handles ProRes/MXF, DNxHR variants, certain HEVC
        profiles, and many ARRI/Sony/Panasonic wrappers that OpenCV
        cannot decode. (BRAW and ARRIRAW still require their respective
        vendor SDKs; see the project notes for the pro-codec roadmap.)

        Each frame is extracted in its own subprocess with `-ss BEFORE -i`
        for fast keyframe seek (sufficient for our "approximately at this
        percentage" use case -- we are not doing frame-accurate cuts).
        Workers run in parallel with a small thread pool to amortize the
        ffmpeg startup overhead across the N target frames.
        """
        try:
            from PIL import Image as PILImage
        except ImportError as e:
            logger.error(f"Pillow required: {e}")
            return []
        import concurrent.futures

        timestamps = [duration * pct for pct in percentages]

        def _extract_one(ts: float):
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
                out_path = tf.name
            # The colorspace filter forces a bt709 interpretation on input
            # AND output, which lets ffmpeg's swscaler handle ARRI ProRes
            # files whose color metadata reads as "csp:gbr / prim:reserved
            # / trc:reserved" -- without this filter, swscale rejects the
            # 10-bit yuv422p source with "Operation not supported".
            # `-map 0:v:0` selects only the first video stream so we don't
            # trip on data tracks (timecode, ARRI metadata) that some MXF
            # / MOV files include as separate streams with no decoder.
            try:
                r = subprocess.run(
                    [
                        _FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-ss", f"{ts:.3f}",
                        "-i", video_path,
                        "-map", "0:v:0",
                        "-frames:v", "1",
                        "-q:v", "5",
                        "-vf", "colorspace=all=bt709:iall=bt709:format=yuv420p",
                        out_path,
                    ],
                    capture_output=True, timeout=60,
                )
                if r.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
                    err = r.stderr.decode(errors="replace")[-400:]
                    logger.warning(f"ffmpeg frame at t={ts:.2f}s failed: {err}")
                    return None
                with PILImage.open(out_path) as im:
                    return im.convert("RGB").copy()
            except subprocess.TimeoutExpired:
                logger.warning(f"ffmpeg frame at t={ts:.2f}s timed out")
                return None
            finally:
                try:
                    if os.path.exists(out_path):
                        os.remove(out_path)
                except OSError:
                    pass

        # Pre-flight probe: try a single frame at the first timestamp. If
        # it fails with "no decoder found" / "could not resolve file
        # descriptor strong ref" the codec is vendor-locked (ARRIRAW,
        # REDCODE, etc) and there is no point hammering ffmpeg N more
        # times. Log one clear message and bail.
        if not _ffmpeg_can_decode(video_path):
            return []

        cells: list = []
        # Parallel ffmpeg processes, capped to keep memory reasonable on
        # large files. ThreadPoolExecutor.map preserves the input order
        # so frames come back chronologically.
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            for img in ex.map(_extract_one, timestamps):
                if img is not None:
                    cells.append(img)
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
            cells = FrameExtractor._extract_ffmpeg_cells(video_path, duration, fps, percentages)
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
