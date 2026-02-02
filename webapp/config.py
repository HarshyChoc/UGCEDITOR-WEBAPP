from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("RECLIP_DATA_DIR", REPO_ROOT / "data"))
UPLOADS_DIR = DATA_DIR / "uploads"
JOBS_DIR = DATA_DIR / "jobs"
STATIC_DIR = Path(__file__).parent / "static"

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
QUEUE_NAME = os.getenv("RECLIP_QUEUE", "reclip")
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY", "")


def ensure_dirs() -> None:
    for path in (DATA_DIR, UPLOADS_DIR, JOBS_DIR):
        path.mkdir(parents=True, exist_ok=True)
