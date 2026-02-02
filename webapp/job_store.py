from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
import zipfile

from .config import JOBS_DIR, ensure_dirs


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def _job_file(job_id: str) -> Path:
    return _job_dir(job_id) / "job.json"


def _log_file(job_id: str) -> Path:
    return _job_dir(job_id) / "logs.txt"


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=True)
    os.replace(temp_path, path)


def create_job(job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_dirs()
    job_id = uuid4().hex
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    job = {
        "id": job_id,
        "type": job_type,
        "status": "queued",
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "progress": {"current": 0, "total": 0},
        "payload": payload,
        "summary": {},
        "outputs": [],
    }

    _atomic_write(_job_file(job_id), job)
    return job


def read_job(job_id: str) -> dict[str, Any]:
    job_file = _job_file(job_id)
    if not job_file.exists():
        raise FileNotFoundError(f"Job not found: {job_id}")
    with open(job_file, "r", encoding="utf-8") as f:
        return json.load(f)


def update_job(job_id: str, **updates: Any) -> dict[str, Any]:
    job = read_job(job_id)
    job.update(updates)
    job["updated_at"] = _utc_now()
    _atomic_write(_job_file(job_id), job)
    return job


def set_job_status(job_id: str, status: str) -> dict[str, Any]:
    return update_job(job_id, status=status)


def append_log(job_id: str, message: str) -> None:
    log_path = _log_file(job_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")


def get_job_paths(job_id: str) -> dict[str, Path]:
    base = _job_dir(job_id)
    return {
        "base": base,
        "input": base / "input",
        "output": base / "output",
    }


def tail_logs(job_id: str, max_lines: int = 200) -> str:
    log_path = _log_file(job_id)
    if not log_path.exists():
        return ""
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return "".join(lines[-max_lines:])


def list_output_files(job_id: str) -> list[str]:
    output_dir = get_job_paths(job_id)["output"]
    if not output_dir.exists():
        return []
    files = []
    for path in output_dir.rglob("*"):
        if path.is_file():
            files.append(path.relative_to(output_dir).as_posix())
    return files


def resolve_output_path(job_id: str, rel_path: str) -> Optional[Path]:
    output_dir = get_job_paths(job_id)["output"].resolve()
    target = (output_dir / rel_path).resolve()
    if not str(target).startswith(str(output_dir)):
        return None
    if not target.exists() or not target.is_file():
        return None
    return target


def create_outputs_zip(job_id: str) -> Optional[Path]:
    return create_outputs_zip_for(job_id, "output", "outputs.zip")


def create_outputs_zip_for(job_id: str, rel_dir: str, zip_name: str) -> Optional[Path]:
    output_dir = get_job_paths(job_id)["output"]
    target_dir = (output_dir / rel_dir).resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        return None

    files = [p for p in target_dir.rglob("*") if p.is_file()]
    if not files:
        return None

    zip_path = _job_dir(job_id) / zip_name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            arcname = path.relative_to(target_dir).as_posix()
            zf.write(path, arcname)

    return zip_path
