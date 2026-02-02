"""
Video Concatenation Processor Module

Handles all FFmpeg operations for concatenating video pairs.
"""

import os
import re
import subprocess
import tempfile
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

# Video extensions to recognize
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg', '.3gp'}

_DRAWTEXT_AVAILABLE: Optional[bool] = None
_FONT_INDEX: Optional[dict[str, Path]] = None


class ConcatOrder(Enum):
    A_THEN_B = "A_then_B"
    B_THEN_A = "B_then_A"


@dataclass
class TextOverlayConfig:
    """Configuration for a text overlay."""
    text: str
    x: int
    y: int
    duration: float  # seconds; 0 = full length
    font_size: int
    font_color: str
    font_family: Optional[str] = None
    font_style: str = "Normal"
    align: str = "top_center"  # "top_center" or "manual"
    max_width_ratio: float = 0.85
    stroke_width: int = 4
    stroke_color: str = "black"
    line_spacing: int = 6
    box_width: int = 0  # 0 = auto
    box_height: int = 0  # 0 = auto

    def is_enabled(self) -> bool:
        return bool(self.text and self.text.strip())

@dataclass
class VideoMatch:
    """Represents a matched pair of videos."""
    basename: str
    file_a: Optional[Path]
    file_b: Optional[Path]

    @property
    def is_matched(self) -> bool:
        return self.file_a is not None and self.file_b is not None

    @property
    def status(self) -> str:
        if self.is_matched:
            return "Matched"
        elif self.file_a is None:
            return "Only in B"
        else:
            return "Only in A"


@dataclass
class ProcessingResult:
    """Result of processing a single video pair."""
    basename: str
    success: bool
    used_fast_copy: bool
    error_message: Optional[str] = None


def natural_sort_key(s: str):
    """Generate a key for natural sorting (2 before 10)."""
    return [int(text) if text.isdigit() else text.lower()
            for text in re.split(r'(\d+)', s)]


def _escape_drawtext_value(value: str) -> str:
    """Escape text values for ffmpeg drawtext."""
    return (value.replace("\\", "\\\\")
                 .replace(":", "\\:")
                 .replace("'", "\\'"))


def _normalize_drawtext_color(value: str) -> str:
    """Normalize common color inputs for ffmpeg drawtext."""
    color = (value or "").strip()
    if not color:
        return "white"

    if color.startswith("#"):
        hex_part = color[1:]
        if len(hex_part) in (6, 8) and all(c in "0123456789abcdefABCDEF" for c in hex_part):
            return f"0x{hex_part}"
        return "white"

    return color


def _check_drawtext_available() -> bool:
    """Return True if ffmpeg has drawtext support."""
    global _DRAWTEXT_AVAILABLE
    if _DRAWTEXT_AVAILABLE is not None:
        return _DRAWTEXT_AVAILABLE

    try:
        result = subprocess.run(
            ['ffmpeg', '-filters'],
            capture_output=True,
            text=True,
            timeout=10
        )
        output = result.stdout or ""
        _DRAWTEXT_AVAILABLE = "drawtext" in output
    except Exception:
        _DRAWTEXT_AVAILABLE = False
    return _DRAWTEXT_AVAILABLE


def _build_font_index() -> dict[str, Path]:
    """Index available font files for quick lookup."""
    global _FONT_INDEX
    if _FONT_INDEX is not None:
        return _FONT_INDEX

    index: dict[str, Path] = {}
    font_dirs = [
        "/System/Library/Fonts",
        "/System/Library/Fonts/Supplemental",
        "/Library/Fonts",
        str(Path.home() / "Library/Fonts"),
        "/usr/share/fonts",
        "/usr/local/share/fonts"
    ]
    for font_dir in font_dirs:
        path = Path(font_dir)
        if not path.exists():
            continue
        for file in path.rglob("*"):
            if file.suffix.lower() in {".ttf", ".otf", ".ttc"}:
                key = file.stem.lower()
                if key not in index:
                    index[key] = file

    _FONT_INDEX = index
    return index


def _resolve_font_path(font_family: Optional[str], font_style: str) -> Optional[Path]:
    """Resolve a font file path from a font family/style."""
    index = _build_font_index()
    if not index:
        return None

    candidates = []
    if font_family:
        base = font_family.strip()
        if base:
            style = (font_style or "").strip()
            if style and style.lower() != "normal":
                candidates.append(f"{base} {style}".lower())
            candidates.append(base.lower())
            candidates.append(base.replace(" ", "").lower())

    fallback = ["Helvetica", "Arial", "SF Pro", "SF Pro Text", "Menlo", "Verdana"]
    for name in fallback:
        candidates.append(name.lower())

    for candidate in candidates:
        if candidate in index:
            return index[candidate]

    # Try partial match
    for candidate in candidates:
        for key, value in index.items():
            if candidate in key:
                return value

    return None


def get_video_dimensions(file_path: Path) -> tuple[int, int]:
    """Get video width and height using ffprobe."""
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height',
            '-of', 'json',
            str(file_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = result.stdout
        if not data:
            return 1920, 1080
        import json
        info = json.loads(data)
        stream = info['streams'][0]
        return int(stream['width']), int(stream['height'])
    except Exception:
        return 1920, 1080


def get_video_duration_ms(file_path: Path) -> int:
    """Get video duration in milliseconds using ffprobe."""
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'json',
            str(file_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        duration_sec = float(data['format']['duration'])
        return int(duration_sec * 1000)
    except Exception:
        return 0


def _duration_within_tolerance(actual_ms: int, expected_ms: int) -> bool:
    if actual_ms <= 0 or expected_ms <= 0:
        return True
    tolerance_ms = max(2000, int(expected_ms * 0.05))
    return abs(actual_ms - expected_ms) <= tolerance_ms


def _measure_text(draw, text: str, font) -> tuple[int, int, int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = max(1, bbox[2] - bbox[0])
    height = max(1, bbox[3] - bbox[1])
    return width, height, bbox[0], bbox[1]


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return [text]

    lines: list[str] = []
    current = ""
    for word in words:
        test = f"{current} {word}".strip()
        width, _, _, _ = _measure_text(draw, test, font)
        if width <= max_width or not current:
            current = test
            continue

        lines.append(current)
        current = word

        # Handle a single word longer than max width by splitting chars
        width, _, _, _ = _measure_text(draw, current, font)
        if width > max_width:
            chunk = ""
            for ch in current:
                test_chunk = f"{chunk}{ch}"
                w, _, _, _ = _measure_text(draw, test_chunk, font)
                if w <= max_width or not chunk:
                    chunk = test_chunk
                else:
                    lines.append(chunk)
                    chunk = ch
            if chunk:
                current = chunk

    if current:
        lines.append(current)

    return lines


def _render_text_overlay_image(
    overlay: TextOverlayConfig,
    output_path: Path,
    video_width: int,
    video_height: int
) -> tuple[bool, str]:
    """Render text to a transparent PNG for overlay."""
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageColor
    except Exception:
        return False, "Pillow not installed (pip install pillow)"

    font_path = _resolve_font_path(overlay.font_family, overlay.font_style)
    try:
        if font_path:
            font = ImageFont.truetype(str(font_path), overlay.font_size)
        else:
            font = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    text = overlay.text.strip()
    if not text:
        return False, "Missing overlay text"

    try:
        color_value = overlay.font_color.strip() if overlay.font_color else "white"
        if color_value.startswith("0x"):
            color_value = "#" + color_value[2:]
        fill = ImageColor.getcolor(color_value, "RGBA")
    except Exception:
        fill = (255, 255, 255, 255)

    stroke_color = overlay.stroke_color or "black"
    stroke_width = max(0, int(overlay.stroke_width))

    image = Image.new("RGBA", (video_width, video_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if overlay.align == "top_center":
        box_width = overlay.box_width if overlay.box_width > 0 else int(
            video_width * max(0.1, min(overlay.max_width_ratio, 1.0))
        )
        box_height = overlay.box_height if overlay.box_height > 0 else video_height
        box_left = max(0, int((video_width - box_width) / 2))
        box_top = max(0, int(overlay.y))
    else:
        box_width = overlay.box_width if overlay.box_width > 0 else int(
            video_width * max(0.1, min(overlay.max_width_ratio, 1.0))
        )
        box_height = overlay.box_height if overlay.box_height > 0 else video_height
        box_left = max(0, int(overlay.x))
        box_top = max(0, int(overlay.y))

    max_width = max(1, box_width)
    lines = _wrap_text(draw, text, font, max_width)
    line_heights = []
    line_widths = []
    line_offsets = []
    for line in lines:
        w, h, left_off, top_off = _measure_text(draw, line, font)
        line_widths.append(w)
        line_heights.append(h)
        line_offsets.append((left_off, top_off))

    y = box_top
    for i, line in enumerate(lines):
        w = line_widths[i]
        left_off, top_off = line_offsets[i]
        x = box_left + max(0, int((box_width - w) / 2))
        draw.text(
            (x - left_off, y - top_off),
            line,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_color
        )
        y += line_heights[i] + overlay.line_spacing
        if box_height > 0 and y > box_top + box_height - 1:
            break

    image.save(output_path)
    return True, ""


def _apply_text_overlay_with_image(
    input_video: Path,
    output_path: Path,
    overlay: TextOverlayConfig,
    crf: int,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None
) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        overlay_path = Path(tmpdir) / "overlay.png"
        video_width, video_height = get_video_dimensions(input_video)
        ok, error = _render_text_overlay_image(overlay, overlay_path, video_width, video_height)
        if not ok:
            return False, error

        enable_clause = ""
        if overlay.duration and overlay.duration > 0:
            enable_clause = f":enable='between(t,0,{overlay.duration})'"

        filter_str = (
            f"[1:v]format=rgba[ov];"
            f"[0:v][ov]overlay=x=0:y=0{enable_clause}[v]"
        )

        cmd = [
            'ffmpeg', '-y',
            '-i', str(input_video),
            '-loop', '1', '-i', str(overlay_path),
            '-filter_complex', filter_str,
            '-map', '[v]',
            '-map', '0:a?',
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', str(crf),
            '-c:a', 'copy',
            '-movflags', '+faststart',
            '-shortest',
            str(output_path)
        ]

        return _run_ffmpeg_command(cmd, output_path, cancel_check)


def build_drawtext_filter(config: TextOverlayConfig) -> str:
    """Build an ffmpeg drawtext filter string from a config."""
    text_value = _escape_drawtext_value(config.text.strip())
    color_value = _normalize_drawtext_color(config.font_color)
    parts = [
        f"text='{text_value}'",
        f"x={config.x}",
        f"y={config.y}",
        f"fontsize={config.font_size}",
        f"fontcolor={color_value}"
    ]

    font_family = (config.font_family or "").strip()
    if font_family:
        font_name = font_family
        style = (config.font_style or "").strip()
        if style and style.lower() != "normal":
            font_name = f"{font_name} {style}"
        parts.append(f"font='{_escape_drawtext_value(font_name)}'")

    if config.duration and config.duration > 0:
        parts.append(f"enable='between(t,0,{config.duration})'")

    return "drawtext=" + ":".join(parts)


def check_ffmpeg_available() -> tuple[bool, str]:
    """Check if FFmpeg is available in PATH."""
    try:
        result = subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            # Extract version from first line
            version_line = result.stdout.split('\n')[0]
            return True, version_line
        return False, "FFmpeg returned non-zero exit code"
    except FileNotFoundError:
        return False, "FFmpeg not found in PATH"
    except subprocess.TimeoutExpired:
        return False, "FFmpeg check timed out"
    except Exception as e:
        return False, str(e)


def probe_has_audio(file_path: Path) -> bool:
    """Check if a video file has an audio stream."""
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-select_streams', 'a:0',
            '-show_entries', 'stream=codec_type',
            '-of', 'csv=p=0',
            str(file_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return 'audio' in result.stdout.lower()
    except:
        return False


def scan_video_files(folder: Path) -> dict[str, Path]:
    """
    Scan a folder for video files.
    Returns dict mapping basename (stem) to full path.
    """
    videos = {}
    if not folder.exists() or not folder.is_dir():
        return videos

    for file in folder.iterdir():
        if file.is_file() and file.suffix.lower() in VIDEO_EXTENSIONS:
            stem = file.stem
            # If duplicate stems, keep first found
            if stem not in videos:
                videos[stem] = file

    return videos


def find_matches(folder_a: Path, folder_b: Path) -> list[VideoMatch]:
    """
    Find video matches between two folders.
    Returns sorted list of VideoMatch objects.
    """
    videos_a = scan_video_files(folder_a)
    videos_b = scan_video_files(folder_b)

    all_basenames = set(videos_a.keys()) | set(videos_b.keys())

    matches = []
    for basename in all_basenames:
        match = VideoMatch(
            basename=basename,
            file_a=videos_a.get(basename),
            file_b=videos_b.get(basename)
        )
        matches.append(match)

    # Sort by natural order
    matches.sort(key=lambda m: natural_sort_key(m.basename))

    return matches


def get_match_counts(matches: list[VideoMatch]) -> tuple[int, int, int]:
    """Return (matched_count, only_a_count, only_b_count)."""
    matched = sum(1 for m in matches if m.is_matched)
    only_a = sum(1 for m in matches if m.file_a and not m.file_b)
    only_b = sum(1 for m in matches if m.file_b and not m.file_a)
    return matched, only_a, only_b


def try_fast_copy_concat(
    file1: Path,
    file2: Path,
    output: Path,
    log_callback: Optional[Callable[[str], None]] = None
) -> bool:
    """
    Attempt fast copy concatenation using concat demuxer.
    Returns True if successful, False if failed.
    """
    # Create temporary file list for concat demuxer
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        # Need to escape single quotes in paths
        f.write(f"file '{str(file1).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")
        f.write(f"file '{str(file2).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n")
        list_file = f.name

    try:
        cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', list_file,
            '-c', 'copy',
            str(output)
        ]

        if log_callback:
            log_callback(f"  Trying fast copy: ffmpeg -f concat -c copy ...")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode == 0 and output.exists() and output.stat().st_size > 0:
            dur1 = get_video_duration_ms(file1)
            dur2 = get_video_duration_ms(file2)
            out_dur = get_video_duration_ms(output)
            expected = dur1 + dur2

            if not _duration_within_tolerance(out_dur, expected):
                if log_callback:
                    log_callback(
                        f"  Fast copy duration mismatch (expected ~{expected/1000:.2f}s, got {out_dur/1000:.2f}s)."
                    )
                output.unlink()
                return False

            return True
        else:
            if log_callback and result.stderr:
                log_callback(f"  Fast copy failed: {result.stderr[:200]}")
            # Clean up failed output
            if output.exists():
                output.unlink()
            return False

    except Exception as e:
        if log_callback:
            log_callback(f"  Fast copy exception: {e}")
        if output.exists():
            output.unlink()
        return False
    finally:
        # Clean up temp file
        try:
            os.unlink(list_file)
        except:
            pass


def _run_ffmpeg_concat(
    file1: Path,
    file2: Path,
    output: Path,
    filter_complex: str,
    map_args: list,
    audio_codec: list,
    crf: int,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None
) -> tuple[bool, str]:
    """Helper to run FFmpeg with given filter. Returns (success, error)."""
    cmd = [
        'ffmpeg', '-y',
        '-i', str(file1),
        '-i', str(file2),
        '-filter_complex', filter_complex,
        *map_args,
        '-c:v', 'libx264',
        '-preset', 'medium',
        '-crf', str(crf),
        *audio_codec,
        '-movflags', '+faststart',
        str(output)
    ]

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        while True:
            try:
                stdout, stderr = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if cancel_check and cancel_check():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except:
                        process.kill()
                    if output.exists():
                        output.unlink()
                    return False, "Cancelled by user"

        if process.returncode == 0 and output.exists() and output.stat().st_size > 0:
            return True, ""
        else:
            error = stderr[-500:] if stderr else "Unknown error"
            if output.exists():
                output.unlink()
            return False, error

    except Exception as e:
        if output.exists():
            output.unlink()
        return False, str(e)


def _run_ffmpeg_command(
    cmd: list,
    output: Path,
    cancel_check: Optional[Callable[[], bool]] = None
) -> tuple[bool, str]:
    """Run a generic ffmpeg command with cancellation support."""
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        while True:
            try:
                stdout, stderr = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if cancel_check and cancel_check():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except:
                        process.kill()
                    if output.exists():
                        output.unlink()
                    return False, "Cancelled by user"

        if process.returncode == 0 and output.exists() and output.stat().st_size > 0:
            return True, ""
        else:
            error = stderr[-500:] if stderr else "Unknown error"
            if output.exists():
                output.unlink()
            return False, error

    except Exception as e:
        if output.exists():
            output.unlink()
        return False, str(e)


def apply_text_overlay(
    input_video: Path,
    output_path: Path,
    overlay: TextOverlayConfig,
    crf: int = 18,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None
) -> tuple[bool, str]:
    """Apply a text overlay to a single video using ffmpeg drawtext."""
    if log_callback:
        duration_desc = "full length" if overlay.duration <= 0 else f"{overlay.duration}s"
        log_callback(f"  Overlay text: '{overlay.text}' at ({overlay.x}, {overlay.y}) for {duration_desc}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if _check_drawtext_available():
        if overlay.align == "top_center":
            if log_callback:
                log_callback("  drawtext available, but using image overlay for centered layout.")
        else:
            filter_str = build_drawtext_filter(overlay)

            cmd = [
                'ffmpeg', '-y',
                '-i', str(input_video),
                '-vf', filter_str,
                '-c:v', 'libx264',
                '-preset', 'medium',
                '-crf', str(crf),
                '-c:a', 'copy',
                '-map', '0:v:0',
                '-map', '0:a?',
                '-movflags', '+faststart',
                str(output_path)
            ]

            success, error = _run_ffmpeg_command(cmd, output_path, cancel_check)
            if not success and log_callback:
                log_callback(f"  Overlay failed: {error[:300]}")
            return success, error

    if log_callback:
        log_callback("  using image overlay fallback.")

    success, error = _apply_text_overlay_with_image(
        input_video=input_video,
        output_path=output_path,
        overlay=overlay,
        crf=crf,
        log_callback=log_callback,
        cancel_check=cancel_check
    )
    if not success and log_callback:
        log_callback(f"  Overlay failed: {error[:300]}")
    return success, error


def reencode_concat(
    file1: Path,
    file2: Path,
    output: Path,
    crf: int = 18,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None
) -> tuple[bool, str]:
    """
    Concatenate two videos with re-encoding for maximum compatibility.
    Normalizes: 30fps, 48kHz audio, yuv420p, libx264, AAC 192k

    Always tries WITH audio first, falls back to video-only if audio fails.

    Returns (success, error_message)
    """
    # First attempt: WITH audio
    if log_callback:
        log_callback(f"  Re-encoding with audio: libx264 CRF={crf}, AAC 192k, 30fps, 48kHz")

    filter_with_audio = (
        "[0:v]fps=30,format=yuv420p,scale=trunc(iw/2)*2:trunc(ih/2)*2[v0];"
        "[1:v]fps=30,format=yuv420p,scale=trunc(iw/2)*2:trunc(ih/2)*2[v1];"
        "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a0];"
        "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a1];"
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[outv][outa]"
    )

    success, error = _run_ffmpeg_concat(
        file1, file2, output,
        filter_with_audio,
        ['-map', '[outv]', '-map', '[outa]'],
        ['-c:a', 'aac', '-b:a', '192k'],
        crf, log_callback, cancel_check
    )

    if success:
        return True, ""

    # If audio concat failed, try video-only as fallback
    if log_callback:
        log_callback(f"  Audio concat failed, trying video-only fallback...")
        log_callback(f"  Error was: {error[:200]}")

    filter_video_only = (
        "[0:v]fps=30,format=yuv420p,scale=trunc(iw/2)*2:trunc(ih/2)*2[v0];"
        "[1:v]fps=30,format=yuv420p,scale=trunc(iw/2)*2:trunc(ih/2)*2[v1];"
        "[v0][v1]concat=n=2:v=1:a=0[outv]"
    )

    success, error = _run_ffmpeg_concat(
        file1, file2, output,
        filter_video_only,
        ['-map', '[outv]'],
        ['-an'],
        crf, log_callback, cancel_check
    )

    if success and log_callback:
        log_callback(f"  Note: Output has no audio (source videos lacked compatible audio)")

    return success, error


def simple_video_concat(
    file1: Path,
    file2: Path,
    output: Path,
    crf: int = 18,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None
) -> tuple[bool, str]:
    """
    Simple video concatenation with fixed 1920x1080 output.
    Tries with audio first, falls back to video-only.

    Returns (success, error_message)
    """
    if log_callback:
        log_callback(f"  Simple concat: 1920x1080 with audio")

    # Try WITH audio first
    filter_with_audio = (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v0];"
        "[1:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v1];"
        "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a0];"
        "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a1];"
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[outv][outa]"
    )

    success, error = _run_ffmpeg_concat(
        file1, file2, output,
        filter_with_audio,
        ['-map', '[outv]', '-map', '[outa]'],
        ['-c:a', 'aac', '-b:a', '192k'],
        crf, log_callback, cancel_check
    )

    if success:
        return True, ""

    # Fallback to video-only
    if log_callback:
        log_callback(f"  Audio failed, trying video-only...")

    filter_video_only = (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v0];"
        "[1:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v1];"
        "[v0][v1]concat=n=2:v=1:a=0[outv]"
    )

    return _run_ffmpeg_concat(
        file1, file2, output,
        filter_video_only,
        ['-map', '[outv]'],
        ['-an'],
        crf, log_callback, cancel_check
    )


def process_video_pair(
    match: VideoMatch,
    output_flat: Path,
    output_nested: Path,
    order: ConcatOrder,
    crf: int,
    try_fast_copy: bool,
    overlay_a: Optional[TextOverlayConfig] = None,
    overlay_b: Optional[TextOverlayConfig] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None
) -> ProcessingResult:
    """
    Process a single matched video pair.
    Creates both flat and nested outputs.
    """
    if not match.is_matched:
        return ProcessingResult(
            basename=match.basename,
            success=False,
            used_fast_copy=False,
            error_message="Not a matched pair"
        )

    file_a = match.file_a
    file_b = match.file_b

    overlay_a_enabled = overlay_a.is_enabled() if overlay_a else False
    overlay_b_enabled = overlay_b.is_enabled() if overlay_b else False
    overlay_active = overlay_a_enabled or overlay_b_enabled

    if log_callback:
        log_callback(f"Processing: {match.basename}")
        log_callback(f"  Video A: {file_a.name}")
        log_callback(f"  Video B: {file_b.name}")
        if overlay_active:
            log_callback("  Text overlays: enabled")

    # Ensure output directories exist
    output_flat.parent.mkdir(parents=True, exist_ok=True)
    output_nested.parent.mkdir(parents=True, exist_ok=True)

    # Apply overlays if needed
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_dir = Path(tmpdir)
        if overlay_a_enabled:
            output_a = temp_dir / f"{file_a.stem}_overlay{file_a.suffix}"
            success, error_msg = apply_text_overlay(
                input_video=file_a,
                output_path=output_a,
                overlay=overlay_a,
                crf=crf,
                log_callback=log_callback,
                cancel_check=cancel_check
            )
            if not success:
                if log_callback:
                    log_callback(f"  FAILED overlay for Video A: {error_msg[:300]}")
                return ProcessingResult(
                    basename=match.basename,
                    success=False,
                    used_fast_copy=False,
                    error_message=error_msg
                )
            file_a = output_a

        if overlay_b_enabled:
            output_b = temp_dir / f"{file_b.stem}_overlay{file_b.suffix}"
            success, error_msg = apply_text_overlay(
                input_video=file_b,
                output_path=output_b,
                overlay=overlay_b,
                crf=crf,
                log_callback=log_callback,
                cancel_check=cancel_check
            )
            if not success:
                if log_callback:
                    log_callback(f"  FAILED overlay for Video B: {error_msg[:300]}")
                return ProcessingResult(
                    basename=match.basename,
                    success=False,
                    used_fast_copy=False,
                    error_message=error_msg
                )
            file_b = output_b

        if overlay_active and try_fast_copy:
            if log_callback:
                log_callback("  Text overlays active; disabling fast copy.")
            try_fast_copy = False

        # Determine order
        if order == ConcatOrder.A_THEN_B:
            file1, file2 = file_a, file_b
        else:
            file1, file2 = file_b, file_a

        if log_callback:
            log_callback(f"  First: {file1.name}")
            log_callback(f"  Second: {file2.name}")

        used_fast_copy = False
        success = False
        error_msg = None

        # Try fast copy first if enabled
        if try_fast_copy:
            if try_fast_copy_concat(file1, file2, output_flat, log_callback):
                used_fast_copy = True
                success = True
                if log_callback:
                    log_callback(f"  Fast copy succeeded!")

        # Fall back to re-encode if needed
        if not success:
            if try_fast_copy and log_callback:
                log_callback(f"  Falling back to re-encode...")
            success, error_msg = reencode_concat(
                file1, file2, output_flat, crf, log_callback, cancel_check
            )

            # If that failed too, try simplest possible concat
            if not success:
                if log_callback:
                    log_callback(f"  Trying simple video-only concat...")
                success, error_msg = simple_video_concat(
                    file1, file2, output_flat, crf, log_callback, cancel_check
                )

        if success:
            # Copy to nested location
            try:
                import shutil
                shutil.copy2(output_flat, output_nested)
                if log_callback:
                    log_callback(f"  Success! Output: {output_flat.name}")
            except Exception as e:
                if log_callback:
                    log_callback(f"  Warning: Could not create nested copy: {e}")
        else:
            if log_callback:
                log_callback(f"  FAILED: {error_msg}")

        return ProcessingResult(
            basename=match.basename,
            success=success,
            used_fast_copy=used_fast_copy,
            error_message=error_msg
        )
