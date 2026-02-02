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

from .config import UPLOADS_DIR, ensure_dirs


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


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


def _meta_path(file_id: str) -> Path:
    return UPLOADS_DIR / f"{file_id}.json"


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

    with open(_meta_path(file_id), "w", encoding="utf-8") as f:
        json.dump(asdict(meta), f, indent=2, ensure_ascii=True)

    return meta


def get_upload_meta(file_id: str) -> UploadMeta:
    meta_file = _meta_path(file_id)
    if not meta_file.exists():
        raise FileNotFoundError(f"Upload not found: {file_id}")

    with open(meta_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    return UploadMeta(**data)


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
