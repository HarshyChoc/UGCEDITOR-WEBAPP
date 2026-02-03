from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from redis import Redis

from .config import JOBS_DIR, REDIS_URL, ensure_dirs

# Redis client for job metadata
_redis_client: Optional[Redis] = None


def _get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


def _job_key(job_id: str) -> str:
    return f"job:{job_id}"


def _log_key(job_id: str) -> str:
    return f"job:{job_id}:logs"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def create_job(job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_dirs()
    job_id = uuid4().hex

    # Create local directory for files
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

    # Store in Redis
    redis = _get_redis()
    redis.set(_job_key(job_id), json.dumps(job))

    return job


def read_job(job_id: str) -> dict[str, Any]:
    redis = _get_redis()
    data = redis.get(_job_key(job_id))
    if data is None:
        raise FileNotFoundError(f"Job not found: {job_id}")
    return json.loads(data)


def update_job(job_id: str, **updates: Any) -> dict[str, Any]:
    job = read_job(job_id)

    # Handle nested updates for progress
    if "progress" in updates and isinstance(updates["progress"], dict):
        job["progress"].update(updates.pop("progress"))

    job.update(updates)
    job["updated_at"] = _utc_now()

    redis = _get_redis()
    redis.set(_job_key(job_id), json.dumps(job))

    return job


def set_job_status(job_id: str, status: str) -> dict[str, Any]:
    return update_job(job_id, status=status)


def append_log(job_id: str, message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    log_line = f"[{timestamp}] {message}\n"

    redis = _get_redis()
    redis.append(_log_key(job_id), log_line)


def get_job_paths(job_id: str) -> dict[str, Path]:
    ensure_dirs()
    base = _job_dir(job_id)
    base.mkdir(parents=True, exist_ok=True)
    return {
        "base": base,
        "input": base / "input",
        "output": base / "output",
    }


def tail_logs(job_id: str, max_lines: int = 200) -> str:
    redis = _get_redis()
    logs = redis.get(_log_key(job_id))
    if not logs:
        return ""
    lines = logs.split("\n")
    return "\n".join(lines[-max_lines:])


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
    # Zip everything under the job's output directory.
    return create_outputs_zip_for(job_id, ".", "outputs.zip")


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
