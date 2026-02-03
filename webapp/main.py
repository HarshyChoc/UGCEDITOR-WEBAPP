from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from rq import Queue, SimpleWorker

from . import tasks
from .config import QUEUE_NAME, REDIS_URL, STATIC_DIR, ensure_dirs
from .job_store import (
    create_job,
    create_outputs_zip,
    create_outputs_zip_for,
    list_output_files,
    read_job,
    resolve_output_path,
    tail_logs,
    update_job,
)
from .storage import save_upload


def create_redis_connection(url: str, max_retries: int = 5, retry_delay: float = 2.0) -> Redis:
    """Create Redis connection with retry logic."""
    last_error = None
    for attempt in range(max_retries):
        try:
            conn = Redis.from_url(url)
            conn.ping()  # Test connection
            return conn
        except RedisConnectionError as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))  # Exponential backoff
    raise RedisConnectionError(f"Failed to connect to Redis after {max_retries} attempts: {last_error}")


class OverlayConfig(BaseModel):
    text: str = ""
    x: int = 0
    y: int = 0
    duration: float = 0
    font_size: int = 48
    font_color: str = "white"
    font_family: str = ""
    font_style: str = "Normal"
    align: str = "top_center"
    max_width_ratio: float = 0.85
    stroke_width: int = 4
    stroke_color: str = "black"
    line_spacing: int = 6
    box_width: int = 0
    box_height: int = 0


class ConcatJobRequest(BaseModel):
    files_a: list[str] = Field(default_factory=list)
    files_b: list[str] = Field(default_factory=list)
    order: str = "A_THEN_B"
    crf: int = 18
    try_fast_copy: bool = True
    flat_folder: str = "flat"
    nested_folder: str = "nested"
    overlay_a: Optional[OverlayConfig] = None
    overlay_b: Optional[OverlayConfig] = None


class UGCJobRequest(BaseModel):
    files: list[str] = Field(default_factory=list)
    add1_file: Optional[str] = None
    add2_file: Optional[str] = None
    clip_end_file: Optional[str] = None
    add1_x: int = 190
    add1_y: int = 890
    add2_opacity: float = 0.5
    crf: int = 18
    enable_captions: bool = True
    api_key: Optional[str] = None


ensure_dirs()
redis_conn = create_redis_connection(REDIS_URL)
queue = Queue(QUEUE_NAME, connection=redis_conn)

# Flag to control embedded worker
RUN_EMBEDDED_WORKER = os.getenv("RUN_EMBEDDED_WORKER", "true").lower() == "true"


def run_worker_thread():
    """Run RQ worker in a background thread using SimpleWorker (no forking)."""
    worker = SimpleWorker([queue], connection=redis_conn)
    worker.work(burst=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: optionally start embedded worker
    worker_thread = None
    if RUN_EMBEDDED_WORKER:
        worker_thread = threading.Thread(target=run_worker_thread, daemon=True)
        worker_thread.start()
        print("Embedded RQ worker started")
    yield
    # Shutdown: worker thread is daemon, will stop automatically


app = FastAPI(title="Harsh's Twinky", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    index_path = Path(STATIC_DIR) / "index.html"
    return FileResponse(index_path)


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.post("/api/uploads")
def upload_file(
    file: UploadFile = File(...),
    role: Optional[str] = Form(None),
) -> JSONResponse:
    meta = save_upload(file, role)
    return JSONResponse(
        {
            "id": meta.file_id,
            "name": meta.original_name,
            "size": meta.size,
            "role": meta.role,
        }
    )


@app.post("/api/jobs/concat")
def create_concat_job(payload: ConcatJobRequest) -> JSONResponse:
    if not payload.files_a or not payload.files_b:
        raise HTTPException(status_code=400, detail="Both files_a and files_b are required.")

    if payload.order not in ("A_THEN_B", "B_THEN_A"):
        raise HTTPException(status_code=400, detail="Invalid order value.")

    job = create_job("concat", payload.model_dump())

    queue.enqueue(
        tasks.run_concat_job,
        job["id"],
        payload.files_a,
        payload.files_b,
        payload.order,
        payload.crf,
        False,
        payload.flat_folder,
        payload.nested_folder,
        payload.overlay_a.model_dump() if payload.overlay_a else None,
        payload.overlay_b.model_dump() if payload.overlay_b else None,
        job_timeout=60 * 60 * 6,
    )

    return JSONResponse({"job_id": job["id"]})


@app.post("/api/jobs/ugc")
def create_ugc_job(payload: UGCJobRequest) -> JSONResponse:
    if not payload.files:
        raise HTTPException(status_code=400, detail="files is required.")

    job = create_job("ugc", payload.model_dump())

    queue.enqueue(
        tasks.run_ugc_job,
        job["id"],
        payload.files,
        payload.add1_file,
        payload.add2_file,
        payload.clip_end_file,
        payload.add1_x,
        payload.add1_y,
        payload.add2_opacity,
        payload.crf,
        payload.enable_captions,
        payload.api_key,
        job_timeout=60 * 60 * 6,
    )

    return JSONResponse({"job_id": job["id"]})


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    job = read_job(job_id)
    if job.get("status") == "finished" and not job.get("outputs") and job.get("type") not in ("ugc", "concat"):
        outputs = list_output_files(job_id)
        job = update_job(job_id, outputs=outputs)
    return JSONResponse(job)


@app.get("/api/jobs/{job_id}/logs")
def get_job_logs(job_id: str, tail: int = 200) -> JSONResponse:
    logs = tail_logs(job_id, max_lines=tail)
    return JSONResponse({"logs": logs})


@app.get("/api/jobs/{job_id}/download/{file_path:path}")
def download_output(job_id: str, file_path: str) -> FileResponse:
    resolved = resolve_output_path(job_id, file_path)
    if not resolved:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(resolved, filename=resolved.name)


@app.get("/api/jobs/{job_id}/download-zip")
def download_outputs_zip(job_id: str) -> FileResponse:
    zip_path = create_outputs_zip(job_id)
    if not zip_path:
        raise HTTPException(status_code=404, detail="No outputs available")
    return FileResponse(zip_path, filename=f"{job_id}_outputs.zip")


@app.get("/api/jobs/{job_id}/download-zip/flat")
def download_flat_zip(job_id: str) -> FileResponse:
    job = read_job(job_id)
    flat_folder = (job.get("payload") or {}).get("flat_folder") or "flat"
    zip_path = create_outputs_zip_for(job_id, flat_folder, "flat_outputs.zip")
    if not zip_path:
        raise HTTPException(status_code=404, detail="No flat outputs available")
    return FileResponse(zip_path, filename=f"{job_id}_flat_outputs.zip")


@app.get("/api/jobs/{job_id}/download-zip/nested")
def download_nested_zip(job_id: str) -> FileResponse:
    job = read_job(job_id)
    nested_folder = (job.get("payload") or {}).get("nested_folder") or "nested"
    zip_path = create_outputs_zip_for(job_id, nested_folder, "nested_outputs.zip")
    if not zip_path:
        raise HTTPException(status_code=404, detail="No nested outputs available")
    return FileResponse(zip_path, filename=f"{job_id}_nested_outputs.zip")
