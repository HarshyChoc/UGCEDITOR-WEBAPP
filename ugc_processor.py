"""
UGC Video Processor Module

Handles video processing with:
- AssemblyAI transcription for word-level captions
- Overlay support (PNG and video overlays with opacity)
- End sting concatenation
- Caption burning with custom fonts
"""

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
import json

# Video extensions to recognize
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg', '.3gp'}

# Default assets path
ASSETS_DIR = Path(__file__).parent / "assets"


@dataclass
class TranscriptWord:
    """Represents a single word with timing."""
    text: str
    start_ms: int
    end_ms: int


@dataclass
class UGCProcessingResult:
    """Result of processing a single UGC video."""
    filename: str
    success: bool
    error_message: Optional[str] = None


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
    except Exception as e:
        return 0


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
        data = json.loads(result.stdout)
        stream = data['streams'][0]
        return int(stream['width']), int(stream['height'])
    except Exception:
        return 1920, 1080  # Default fallback


def transcribe_with_assemblyai(
    audio_path: Path,
    api_key: str,
    log_callback: Optional[Callable[[str], None]] = None
) -> list[TranscriptWord]:
    """
    Transcribe audio using AssemblyAI API.
    Returns list of words with timestamps.
    """
    try:
        import assemblyai as aai
        aai.settings.api_key = api_key

        if log_callback:
            log_callback(f"  Uploading to AssemblyAI...")

        transcriber = aai.Transcriber()

        # Configure for word-level timestamps
        config = aai.TranscriptionConfig(
            speech_model=aai.SpeechModel.best,
        )

        transcript = transcriber.transcribe(str(audio_path), config=config)

        if transcript.status == aai.TranscriptStatus.error:
            if log_callback:
                log_callback(f"  Transcription error: {transcript.error}")
            return []

        if log_callback:
            log_callback(f"  Transcription complete: {len(transcript.words)} words")

        words = []
        for word in transcript.words:
            words.append(TranscriptWord(
                text=word.text,
                start_ms=word.start,
                end_ms=word.end
            ))

        return words

    except ImportError:
        if log_callback:
            log_callback("  Error: assemblyai package not installed. Run: pip install assemblyai")
        return []
    except Exception as e:
        if log_callback:
            log_callback(f"  Transcription error: {e}")
        return []


def generate_ass_subtitles(
    words: list[TranscriptWord],
    video_width: int,
    video_height: int,
    font_name: str = "Futura",
    font_size: int = 48,
    output_path: Optional[Path] = None
) -> str:
    """
    Generate ASS subtitle file with word-by-word display.
    Returns the ASS content as string.

    Style: White text, no border/outline, Futura Bold, centered at bottom
    """
    # ASS header with style definition
    # PrimaryColour: &HFFFFFF (white), OutlineColour: &H00000000 (transparent)
    # Outline: 0 (no border), Shadow: 0 (no shadow)
    # Bold: 1 (bold)
    ass_content = f"""[Script Info]
Title: UGC Captions
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},Bold,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,2,10,10,50,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    # Add each word as a separate dialogue event
    for word in words:
        start_time = ms_to_ass_time(word.start_ms)
        end_time = ms_to_ass_time(word.end_ms)
        # Escape special characters in text
        text = word.text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        ass_content += f"Dialogue: 0,{start_time},{end_time},Default,,0,0,0,,{text}\n"

    if output_path:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(ass_content)

    return ass_content


def ms_to_ass_time(ms: int) -> str:
    """Convert milliseconds to ASS time format (H:MM:SS.cc)."""
    total_seconds = ms / 1000
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = total_seconds % 60
    centiseconds = int((seconds % 1) * 100)
    seconds = int(seconds)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def scan_ugc_videos(folder: Path) -> list[Path]:
    """Scan a folder for video files and return sorted list."""
    videos = []
    if not folder.exists() or not folder.is_dir():
        return videos

    for file in folder.iterdir():
        if file.is_file() and file.suffix.lower() in VIDEO_EXTENSIONS:
            videos.append(file)

    videos.sort(key=lambda p: p.name.lower())
    return videos


def process_ugc_video(
    input_video: Path,
    output_path: Path,
    api_key: str,
    add1_overlay: Path,
    add2_overlay: Path,
    clip_end: Path,
    add1_position: tuple[int, int] = (190, 890),  # x, y position for add1.png
    add2_opacity: float = 0.5,  # opacity for add2.mov (0.0 to 1.0)
    crf: int = 18,
    enable_captions: bool = True,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None
) -> UGCProcessingResult:
    """
    Process a single UGC video with:
    1. Transcription and word-by-word captions (if enabled)
    2. add1.png overlay at specified position
    3. add2.mov overlay with reduced opacity
    4. ClipEnd.mov concatenated at the end

    Layer order (bottom to top):
    - Base video
    - Captions (burned in) - only if enable_captions=True
    - add1.png
    - add2.mov (with opacity)
    """
    if log_callback:
        log_callback(f"Processing: {input_video.name}")

    # Check for cancellation
    if cancel_check and cancel_check():
        return UGCProcessingResult(
            filename=input_video.name,
            success=False,
            error_message="Cancelled by user"
        )

    try:
        # Get video properties
        video_width, video_height = get_video_dimensions(input_video)
        video_duration_ms = get_video_duration_ms(input_video)
        video_duration_sec = video_duration_ms / 1000

        # Trim the last 2.8 seconds off the main video (ClipEnd replaces it)
        trim_amount_sec = 2.8
        trimmed_duration_sec = max(0, video_duration_sec - trim_amount_sec)

        if log_callback:
            log_callback(f"  Video: {video_width}x{video_height}, {video_duration_sec:.1f}s")
            log_callback(f"  Trimming last {trim_amount_sec}s -> {trimmed_duration_sec:.1f}s + ClipEnd")

        # Create temporary files
        temp_dir = tempfile.mkdtemp(prefix="ugc_")
        ass_file = Path(temp_dir) / "captions.ass"
        intermediate_video = Path(temp_dir) / "intermediate.mp4"

        words = []

        # Step 1: Transcribe with AssemblyAI (only if captions enabled)
        if enable_captions:
            if log_callback:
                log_callback(f"  Step 1: Transcribing audio...")

            words = transcribe_with_assemblyai(input_video, api_key, log_callback)

            if not words:
                if log_callback:
                    log_callback(f"  Warning: No words transcribed, continuing without captions")

            # Step 2: Generate ASS subtitles (only for trimmed duration)
            if words:
                # Filter words to only include those within trimmed duration
                trimmed_duration_ms = int(trimmed_duration_sec * 1000)
                words = [w for w in words if w.start_ms < trimmed_duration_ms]

                if log_callback:
                    log_callback(f"  Step 2: Generating captions ({len(words)} words within trimmed duration)...")
                generate_ass_subtitles(
                    words,
                    video_width,
                    video_height,
                    font_name="Futura",
                    font_size=int(video_height * 0.06),  # Scale font to video height
                    output_path=ass_file
                )
        else:
            if log_callback:
                log_callback(f"  Captions disabled, skipping transcription")

        # Step 3: Build FFmpeg filter complex
        # Overlays are applied ONLY to the trimmed main video (not ClipEnd)
        if log_callback:
            log_callback(f"  Step 3: Applying overlays to trimmed video...")

        # Build the filter complex for all overlays
        # Layer order (bottom to top): video -> captions -> add2.mov -> add1.png

        filter_parts = []
        current_label = "0:v"

        # Apply captions first (lowest overlay layer)
        if words and ass_file.exists():
            # Escape the path for FFmpeg (handle special characters)
            ass_path_escaped = str(ass_file).replace("\\", "/").replace(":", "\\:")
            filter_parts.append(f"[{current_label}]ass='{ass_path_escaped}'[captioned]")
            current_label = "captioned"

        # Overlay add2.mov with opacity (below add1)
        if add2_overlay.exists():
            opacity = add2_opacity
            # add2 is input 1 (first overlay input)
            filter_parts.append(
                f"[1:v]scale={video_width}:{video_height},format=rgba,colorchannelmixer=aa={opacity}[add2_alpha];"
                f"[{current_label}][add2_alpha]overlay=0:0:shortest=1[with_add2]"
            )
            current_label = "with_add2"

        # Overlay add1.png at specified position (on top of add2)
        # Scale add1 to 1.3x its original size
        if add1_overlay.exists():
            x_pos, y_pos = add1_position
            # add1 is input 2 if add2 exists, otherwise input 1
            add1_input_idx = 2 if add2_overlay.exists() else 1
            # Scale add1 by 1.3x (iw=input width, ih=input height)
            filter_parts.append(
                f"[{add1_input_idx}:v]scale=iw*1.3:ih*1.3[add1_scaled];"
                f"[{current_label}][add1_scaled]overlay={x_pos}:{y_pos}[with_add1]"
            )
            current_label = "with_add1"

        # Combine filter parts
        filter_complex = ";".join(filter_parts)
        if filter_parts:
            filter_complex += f";[{current_label}]copy[vout]"
        else:
            filter_complex = "[0:v]copy[vout]"

        # Build FFmpeg command for overlays
        # Use -t to trim to the desired duration (cutting off last 2.8s)
        cmd = ['ffmpeg', '-y', '-t', str(trimmed_duration_sec), '-i', str(input_video)]

        # Add overlay inputs (add2 first, then add1 - order matters for input indices)
        if add2_overlay.exists():
            # Use -stream_loop to loop the video if it's shorter than the main clip
            # -1 means loop indefinitely, overlay's shortest=1 will stop at main video end
            cmd.extend(['-stream_loop', '-1', '-i', str(add2_overlay)])
        if add1_overlay.exists():
            cmd.extend(['-i', str(add1_overlay)])

        cmd.extend([
            '-filter_complex', filter_complex,
            '-map', '[vout]',
            '-map', '0:a?',  # Map audio if exists
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', str(crf),
            '-c:a', 'aac',
            '-b:a', '192k',
            '-movflags', '+faststart',
            str(intermediate_video)
        ])

        if log_callback:
            log_callback(f"  Running FFmpeg overlay pass...")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if result.returncode != 0:
            if log_callback:
                log_callback(f"  FFmpeg overlay error: {result.stderr[-500:]}")
            return UGCProcessingResult(
                filename=input_video.name,
                success=False,
                error_message=f"FFmpeg overlay failed: {result.stderr[-200:]}"
            )

        # Step 4: Concatenate with ClipEnd.mov
        if log_callback:
            log_callback(f"  Step 4: Appending end sting...")

        if clip_end.exists():
            success = concatenate_with_end_sting(
                intermediate_video,
                clip_end,
                output_path,
                crf,
                log_callback,
                cancel_check
            )
        else:
            # No end sting, just copy intermediate to output
            import shutil
            shutil.copy2(intermediate_video, output_path)
            success = True

        # Cleanup temp files
        try:
            import shutil
            shutil.rmtree(temp_dir)
        except:
            pass

        if success:
            if log_callback:
                log_callback(f"  Success! Output: {output_path.name}")
            return UGCProcessingResult(
                filename=input_video.name,
                success=True
            )
        else:
            return UGCProcessingResult(
                filename=input_video.name,
                success=False,
                error_message="Failed to concatenate end sting"
            )

    except Exception as e:
        if log_callback:
            log_callback(f"  Error: {e}")
        return UGCProcessingResult(
            filename=input_video.name,
            success=False,
            error_message=str(e)
        )


def concatenate_with_end_sting(
    main_video: Path,
    end_sting: Path,
    output_path: Path,
    crf: int = 18,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None
) -> bool:
    """
    Concatenate main video with end sting.
    Re-encodes to ensure compatibility.
    """
    try:
        # Get dimensions of main video to scale end sting to match
        width, height = get_video_dimensions(main_video)

        filter_complex = (
            f"[0:v]fps=30,format=yuv420p[v0];"
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p[v1];"
            f"[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a0];"
            f"[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a1];"
            f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[outv][outa]"
        )

        cmd = [
            'ffmpeg', '-y',
            '-i', str(main_video),
            '-i', str(end_sting),
            '-filter_complex', filter_complex,
            '-map', '[outv]',
            '-map', '[outa]',
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', str(crf),
            '-c:a', 'aac',
            '-b:a', '192k',
            '-movflags', '+faststart',
            str(output_path)
        ]

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
                    if output_path.exists():
                        output_path.unlink()
                    return False

        if process.returncode == 0 and output_path.exists():
            return True
        else:
            # Try video-only fallback if audio fails
            if log_callback:
                log_callback(f"  Audio concat failed, trying video-only...")

            filter_video_only = (
                f"[0:v]fps=30,format=yuv420p[v0];"
                f"[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p[v1];"
                f"[v0][v1]concat=n=2:v=1:a=0[outv]"
            )

            cmd_video_only = [
                'ffmpeg', '-y',
                '-i', str(main_video),
                '-i', str(end_sting),
                '-filter_complex', filter_video_only,
                '-map', '[outv]',
                '-an',
                '-c:v', 'libx264',
                '-preset', 'medium',
                '-crf', str(crf),
                '-movflags', '+faststart',
                str(output_path)
            ]

            result = subprocess.run(cmd_video_only, capture_output=True, text=True, timeout=300)
            return result.returncode == 0 and output_path.exists()

    except Exception as e:
        if log_callback:
            log_callback(f"  Concat error: {e}")
        return False
