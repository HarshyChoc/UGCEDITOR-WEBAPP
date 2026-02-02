from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from processor import (
    ConcatOrder,
    TextOverlayConfig,
    check_ffmpeg_available,
    find_matches,
    get_match_counts,
    process_video_pair,
)
from ugc_processor import ASSETS_DIR, process_ugc_video, scan_ugc_videos

from .config import ASSEMBLYAI_API_KEY
from .job_store import (
    append_log,
    create_outputs_zip,
    create_outputs_zip_for,
    get_job_paths,
    set_job_status,
    update_job,
)
from .storage import get_upload_meta, sanitize_filename, stage_upload


def _log(job_id: str, message: str) -> None:
    append_log(job_id, message)


def _build_overlay(config: Optional[dict[str, Any]]) -> Optional[TextOverlayConfig]:
    if not config:
        return None

    text = str(config.get("text", "")).strip()
    if not text:
        return None

    return TextOverlayConfig(
        text=text,
        x=int(config.get("x", 0)),
        y=int(config.get("y", 0)),
        duration=float(config.get("duration", 0)),
        font_size=int(config.get("font_size", 48)),
        font_color=str(config.get("font_color", "white")),
        font_family=str(config.get("font_family", "")) or None,
        font_style=str(config.get("font_style", "Normal")),
        align=str(config.get("align", "top_center")),
        max_width_ratio=float(config.get("max_width_ratio", 0.85)),
        stroke_width=int(config.get("stroke_width", 4)),
        stroke_color=str(config.get("stroke_color", "black")),
        line_spacing=int(config.get("line_spacing", 6)),
        box_width=int(config.get("box_width", 0)),
        box_height=int(config.get("box_height", 0)),
    )


def _stage_inputs(file_ids: list[str], dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for file_id in file_ids:
        meta = get_upload_meta(file_id)
        dest_path = dest_dir / sanitize_filename(meta.original_name)
        stage_upload(file_id, dest_path)


def run_concat_job(
    job_id: str,
    file_ids_a: list[str],
    file_ids_b: list[str],
    order: str,
    crf: int,
    try_fast_copy: bool,
    flat_folder: str,
    nested_folder: str,
    overlay_a: Optional[dict[str, Any]] = None,
    overlay_b: Optional[dict[str, Any]] = None,
) -> None:
    try:
        ok, ffmpeg_info = check_ffmpeg_available()
        if not ok:
            _log(job_id, f"FFmpeg not available: {ffmpeg_info}")
            update_job(job_id, status="failed", summary={"error": "ffmpeg_not_found"})
            return

        set_job_status(job_id, "running")
        paths = get_job_paths(job_id)
        input_a = paths["input"] / "a"
        input_b = paths["input"] / "b"
        output_dir = paths["output"]
        flat_dir = output_dir / (flat_folder or "flat")
        nested_dir = output_dir / (nested_folder or "nested")

        _log(job_id, f"FFmpeg: {ffmpeg_info}")
        _log(job_id, "Staging inputs...")
        _stage_inputs(file_ids_a, input_a)
        _stage_inputs(file_ids_b, input_b)

        matches = find_matches(input_a, input_b)
        matched_count, only_a_count, only_b_count = get_match_counts(matches)

        update_job(
            job_id,
            summary={
                "matched": matched_count,
                "only_a": only_a_count,
                "only_b": only_b_count,
            },
        )

        matched = [m for m in matches if m.is_matched]
        total = len(matched)

        if total == 0:
            _log(job_id, "No matched pairs to process.")
            update_job(job_id, status="finished", progress={"current": 0, "total": 0})
            return

        overlay_a_cfg = _build_overlay(overlay_a)
        overlay_b_cfg = _build_overlay(overlay_b)

        order_enum = ConcatOrder.A_THEN_B if order == "A_THEN_B" else ConcatOrder.B_THEN_A

        success_count = 0
        fail_count = 0

        _log(job_id, f"Starting concat for {total} matched pairs...")
        _log(job_id, f"Order: {order_enum.value}, CRF: {crf}, Fast copy: {try_fast_copy}")

        for idx, match in enumerate(matched, 1):
            update_job(job_id, progress={"current": idx, "total": total})

            output_name = f"{match.basename}.mp4"
            output_flat = flat_dir / output_name
            nested_subdir = nested_dir / str(idx)
            nested_subdir.mkdir(parents=True, exist_ok=True)
            output_nested = nested_subdir / output_name

            result = process_video_pair(
                match=match,
                output_flat=output_flat,
                output_nested=output_nested,
                order=order_enum,
                crf=crf,
                try_fast_copy=try_fast_copy,
                overlay_a=overlay_a_cfg,
                overlay_b=overlay_b_cfg,
                log_callback=lambda msg: _log(job_id, msg),
            )

            if result.success:
                success_count += 1
            else:
                fail_count += 1

        flat_zip = create_outputs_zip_for(job_id, flat_dir.relative_to(output_dir).as_posix(), "flat_outputs.zip")
        nested_zip = create_outputs_zip_for(job_id, nested_dir.relative_to(output_dir).as_posix(), "nested_outputs.zip")

        summary = {
            "matched": matched_count,
            "only_a": only_a_count,
            "only_b": only_b_count,
            "success": success_count,
            "failed": fail_count,
            "flat_zip_ready": bool(flat_zip and flat_zip.exists()),
            "nested_zip_ready": bool(nested_zip and nested_zip.exists()),
        }

        update_job(
            job_id,
            status="finished",
            progress={"current": total, "total": total},
            summary=summary,
            outputs=[],
        )

    except Exception as exc:
        _log(job_id, f"Error: {exc}")
        update_job(job_id, status="failed", summary={"error": str(exc)})


def _resolve_asset(default_path: Path, upload_id: Optional[str], dest_dir: Path) -> Path:
    if not upload_id:
        return default_path
    meta = get_upload_meta(upload_id)
    dest_path = dest_dir / sanitize_filename(meta.original_name)
    stage_upload(upload_id, dest_path)
    return dest_path


def run_ugc_job(
    job_id: str,
    file_ids: list[str],
    add1_file: Optional[str],
    add2_file: Optional[str],
    clip_end_file: Optional[str],
    add1_x: int,
    add1_y: int,
    add2_opacity: float,
    crf: int,
    enable_captions: bool,
    api_key: Optional[str] = None,
) -> None:
    try:
        ok, ffmpeg_info = check_ffmpeg_available()
        if not ok:
            _log(job_id, f"FFmpeg not available: {ffmpeg_info}")
            update_job(job_id, status="failed", summary={"error": "ffmpeg_not_found"})
            return

        set_job_status(job_id, "running")
        paths = get_job_paths(job_id)
        input_dir = paths["input"] / "ugc"
        output_dir = paths["output"] / "ugc"
        output_dir.mkdir(parents=True, exist_ok=True)

        _log(job_id, f"FFmpeg: {ffmpeg_info}")
        _log(job_id, "Staging inputs...")
        _stage_inputs(file_ids, input_dir)

        videos = scan_ugc_videos(input_dir)
        total = len(videos)
        if total == 0:
            _log(job_id, "No UGC videos to process.")
            update_job(job_id, status="finished", progress={"current": 0, "total": 0})
            return

        if enable_captions:
            key = api_key or ASSEMBLYAI_API_KEY
            if not key:
                _log(job_id, "AssemblyAI API key missing. Set ASSEMBLYAI_API_KEY or provide api_key.")
                update_job(job_id, status="failed", summary={"error": "missing_api_key"})
                return
        else:
            key = api_key or ""

        assets_dir = paths["input"] / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        add1_path = _resolve_asset(ASSETS_DIR / "add1.png", add1_file, assets_dir)
        add2_path = _resolve_asset(ASSETS_DIR / "add2.mov", add2_file, assets_dir)
        clip_end_path = _resolve_asset(ASSETS_DIR / "ClipEnd.mov", clip_end_file, assets_dir)

        _log(job_id, f"Starting UGC processing of {total} videos...")

        success_count = 0
        fail_count = 0

        for idx, video in enumerate(videos, 1):
            update_job(job_id, progress={"current": idx, "total": total})

            output_path = output_dir / f"{video.stem}_processed.mp4"

            result = process_ugc_video(
                input_video=video,
                output_path=output_path,
                api_key=key,
                add1_overlay=add1_path,
                add2_overlay=add2_path,
                clip_end=clip_end_path,
                add1_position=(add1_x, add1_y),
                add2_opacity=add2_opacity,
                crf=crf,
                enable_captions=enable_captions,
                log_callback=lambda msg: _log(job_id, msg),
            )

            if result.success:
                success_count += 1
            else:
                fail_count += 1

        zip_path = create_outputs_zip(job_id)
        zip_ready = bool(zip_path and zip_path.exists())

        summary = {"success": success_count, "failed": fail_count, "zip_ready": zip_ready}
        update_job(
            job_id,
            status="finished",
            progress={"current": total, "total": total},
            summary=summary,
            outputs=[],
        )

    except Exception as exc:
        _log(job_id, f"Error: {exc}")
        update_job(job_id, status="failed", summary={"error": str(exc)})
