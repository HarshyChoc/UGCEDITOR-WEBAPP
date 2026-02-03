from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from redis import Redis

from .config import UPLOADS_DIR, REDIS_URL, ensure_dirs


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Redis client for upload metadata
_redis_client: Optional[Redis] = None


def _get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


def _upload_key(file_id: str) -> str:
    return f"upload:{file_id}"


@dataclass
class UploadMeta:
    file_id: str
    original_name: str
    stored_name: str
    path: str
    size: int
    role: Optional[str]
    created_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_filename(name: str) -> str:
    name = os.path.basename(name or "upload")
    name = _SAFE_NAME_RE.sub("_", name)
    return name or "upload"


def save_upload(upload_file, role: Optional[str] = None) -> UploadMeta:
    ensure_dirs()

    file_id = uuid4().hex
    original_name = upload_file.filename or "upload"
    safe_name = sanitize_filename(original_name)
    stored_name = f"{file_id}__{safe_name}"
    stored_path = UPLOADS_DIR / stored_name

    size = 0
    with open(stored_path, "wb") as f:
        shutil.copyfileobj(upload_file.file, f)
        size = stored_path.stat().st_size

    meta = UploadMeta(
        file_id=file_id,
        original_name=original_name,
        stored_name=stored_name,
        path=str(stored_path),
        size=size,
        role=role,
        created_at=_utc_now(),
    )

    # Store metadata in Redis
    redis = _get_redis()
    redis.set(_upload_key(file_id), json.dumps(asdict(meta)))

    return meta


def get_upload_meta(file_id: str) -> UploadMeta:
    redis = _get_redis()
    data = redis.get(_upload_key(file_id))

    if data is None:
        raise FileNotFoundError(f"Upload not found: {file_id}")

    return UploadMeta(**json.loads(data))


def stage_upload(file_id: str, dest_path: Path) -> UploadMeta:
    meta = get_upload_meta(file_id)
    source_path = Path(meta.path)

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists():
        return meta

    try:
        os.link(source_path, dest_path)
    except Exception:
        shutil.copy2(source_path, dest_path)

    return meta
