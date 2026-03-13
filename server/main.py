"""
Remote processing server for gphoto2-astro-webui.

Runs on a beefy machine (GPU, lots of RAM) and accepts images from
the Raspberry Pi for stacking and timelapse generation.

Port: 8069 (default)

Job lifecycle:
  POST   /api/jobs              → create job (mode, type, params)
  POST   /api/jobs/{id}/images  → batch upload images (multipart)
  POST   /api/jobs/{id}/finalize → trigger final processing, return result
  GET    /api/jobs/{id}         → status + progress
  GET    /api/jobs/{id}/result  → download processed image/video
  DELETE /api/jobs/{id}         → cleanup temp files
"""

import logging
import os
import shutil
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from processing import (
    IncrementalStacker,
    run_timelapse,
    CUDA_AVAILABLE,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp/astro-server-jobs"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

MAX_HISTORY = 100
MAX_LOG_LINES = 500

app = FastAPI(title="Astro Processing Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Job model
# ---------------------------------------------------------------------------

class JobStatus:
    UPLOADING = "uploading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job:
    def __init__(self, job_id: str, job_type: str, mode: str, params: dict):
        self.id = job_id
        self.type = job_type        # "stack" or "timelapse"
        self.mode = mode            # "mean", "max", "align+mean" for stack; ignored for timelapse
        self.params = params        # fps, resolution, etc.
        self.status = JobStatus.UPLOADING
        self.progress = 0
        self.total = 0
        self.message = "Waiting for images"
        self.images_received = 0
        self.result_path: Optional[Path] = None
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self._cancel = threading.Event()
        self._log: deque = deque(maxlen=MAX_LOG_LINES)
        self._lock = threading.Lock()

        # Directory for this job's uploaded images
        self.work_dir = WORK_DIR / job_id
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir = self.work_dir / "images"
        self.images_dir.mkdir(exist_ok=True)

        # Incremental stacker (initialized on first image for stack jobs)
        self.stacker: Optional[IncrementalStacker] = None

        self.log(f"Job created: {job_type} mode={mode}")

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def log(self, line: str):
        ts = time.strftime("%H:%M:%S")
        self._log.append(f"[{ts}] {line}")

    def to_dict(self, include_log: bool = False) -> dict:
        d = {
            "id": self.id,
            "type": self.type,
            "mode": self.mode,
            "status": self.status,
            "progress": self.progress,
            "total": self.total,
            "message": self.message,
            "images_received": self.images_received,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cuda": CUDA_AVAILABLE,
        }
        if self.error:
            d["error"] = self.error
        if self.result_path and self.result_path.exists():
            d["has_result"] = True
        if include_log:
            d["log"] = list(self._log)
        return d


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------

_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _get_job(job_id: str) -> Job:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


def _trim_history():
    finished = [
        j for j in _jobs.values()
        if j.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
    ]
    if len(finished) > MAX_HISTORY:
        finished.sort(key=lambda j: j.finished_at or 0)
        for j in finished[: len(finished) - MAX_HISTORY]:
            del _jobs[j.id]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateJobRequest(BaseModel):
    type: str                   # "stack" or "timelapse"
    mode: str = "mean"          # stacking mode (ignored for timelapse)
    fps: int = 30               # timelapse fps
    resolution: str = "1920x1080"  # timelapse resolution
    output_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "cuda": CUDA_AVAILABLE}


@app.post("/api/jobs", status_code=201)
def create_job(req: CreateJobRequest):
    """Create a new processing job. Returns job_id for subsequent uploads."""
    if req.type not in ("stack", "timelapse"):
        raise HTTPException(status_code=400, detail="type must be 'stack' or 'timelapse'")
    if req.type == "stack" and req.mode not in ("mean", "max", "align+mean"):
        raise HTTPException(status_code=400, detail=f"Unknown stacking mode: {req.mode}")

    job_id = uuid.uuid4().hex[:12]
    params = {
        "fps": req.fps,
        "resolution": req.resolution,
        "output_name": req.output_name,
    }
    job = Job(job_id, req.type, req.mode, params)

    # For stack jobs with incremental modes, create stacker now
    if req.type == "stack" and req.mode in ("mean", "max", "align+mean"):
        job.stacker = IncrementalStacker(req.mode)
        job.log(f"Incremental stacker ready (mode={req.mode}, cuda={CUDA_AVAILABLE})")

    with _jobs_lock:
        _jobs[job_id] = job
        _trim_history()

    logger.info("Job %s created: type=%s mode=%s", job_id, req.type, req.mode)
    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/images")
async def upload_images(job_id: str, files: list[UploadFile] = File(...)):
    """Upload one or more images to a job. Multipart batch upload.

    For stack jobs (mean/max/align+mean), images are incrementally
    accumulated as they arrive — no need to wait for finalize.
    """
    job = _get_job(job_id)
    if job.status not in (JobStatus.UPLOADING,):
        raise HTTPException(status_code=409, detail=f"Job is {job.status}, cannot upload")
    if job.cancelled:
        raise HTTPException(status_code=409, detail="Job was cancelled")

    saved = []
    for f in files:
        # Save to disk (needed for timelapse; stack also keeps for reference)
        filename = f"{job.images_received:06d}_{f.filename}"
        dest = job.images_dir / filename
        content = await f.read()
        dest.write_bytes(content)
        saved.append(dest)
        job.images_received += 1

        # Incremental accumulation for stack jobs
        if job.stacker is not None:
            try:
                job.stacker.add_image(dest)
                job.log(f"Accumulated image {job.images_received}: {f.filename}")
            except Exception as exc:
                job.log(f"Warning: failed to accumulate {f.filename}: {exc}")
                logger.warning("Accumulate failed for %s: %s", f.filename, exc)

    job.message = f"Received {job.images_received} images"
    logger.info("Job %s: received %d images (total: %d)", job_id, len(saved), job.images_received)
    return {
        "received": len(saved),
        "total_images": job.images_received,
    }


@app.post("/api/jobs/{job_id}/finalize")
def finalize_job(job_id: str):
    """Signal that all images have been uploaded. Starts final processing."""
    job = _get_job(job_id)
    if job.status != JobStatus.UPLOADING:
        raise HTTPException(status_code=409, detail=f"Job is {job.status}, cannot finalize")
    if job.images_received < 2:
        raise HTTPException(status_code=400, detail="Need at least 2 images")

    job.status = JobStatus.PROCESSING
    job.started_at = time.time()
    job.total = job.images_received
    job.log(f"Finalizing with {job.images_received} images")

    # Run finalization in background thread
    threading.Thread(target=_run_finalize, args=(job,), daemon=True).start()
    return {"status": "processing", "images": job.images_received}


def _run_finalize(job: Job):
    """Background thread: finalize processing and produce output."""
    try:
        if job.type == "stack":
            _finalize_stack(job)
        elif job.type == "timelapse":
            _finalize_timelapse(job)
    except Exception as exc:
        job.status = JobStatus.FAILED
        job.error = str(exc)
        job.finished_at = time.time()
        job.log(f"FAILED: {exc}")
        logger.error("Job %s failed: %s", job.id, exc)


def _finalize_stack(job: Job):
    """Finalize stacking — the accumulator already has all images."""
    job.log("Finalizing stack...")
    job.progress = job.images_received

    if job.stacker is None:
        raise RuntimeError("No stacker initialized")

    result_image = job.stacker.finalize()
    ext = ".jpg"
    output_name = job.params.get("output_name") or f"stacked-{job.mode}-{int(time.time())}{ext}"
    output_path = job.work_dir / output_name
    result_image.save(str(output_path), "JPEG", quality=95)
    job.result_path = output_path

    job.status = JobStatus.COMPLETED
    job.finished_at = time.time()
    elapsed = job.finished_at - (job.started_at or job.created_at)
    job.message = f"Stack complete ({elapsed:.1f}s)"
    job.log(f"Saved result: {output_name} ({output_path.stat().st_size / 1024 / 1024:.1f} MiB)")
    job.log(f"Completed in {elapsed:.1f}s")
    logger.info("Job %s stack complete in %.1fs", job.id, elapsed)


def _finalize_timelapse(job: Job):
    """Run ffmpeg timelapse encoding on all uploaded images."""
    job.log("Starting timelapse encoding...")

    # Collect all image paths in order
    image_paths = sorted(job.images_dir.iterdir())
    if len(image_paths) < 2:
        raise RuntimeError("Need at least 2 images for timelapse")

    fps = job.params.get("fps", 30)
    resolution = job.params.get("resolution", "1920x1080")
    output_name = job.params.get("output_name") or f"timelapse-{fps}fps-{int(time.time())}.mp4"
    output_path = job.work_dir / output_name

    def on_progress(phase, done, total):
        job.progress = done
        job.total = total
        job.message = f"{phase.capitalize()}: {done}/{total}"
        if done % 10 == 0 or done == total:
            job.log(f"{phase}: {done}/{total}")

    def cancel_check():
        return job.cancelled

    run_timelapse(
        image_paths=image_paths,
        output_path=output_path,
        fps=fps,
        resolution=resolution,
        on_progress=on_progress,
        cancel_check=cancel_check,
    )

    job.result_path = output_path
    job.status = JobStatus.COMPLETED
    job.finished_at = time.time()
    elapsed = job.finished_at - (job.started_at or job.created_at)
    file_size_mb = output_path.stat().st_size / 1024 / 1024
    job.message = f"Timelapse complete ({elapsed:.1f}s, {file_size_mb:.1f} MiB)"
    job.log(f"Completed in {elapsed:.1f}s")
    logger.info("Job %s timelapse complete in %.1fs", job.id, elapsed)


@app.get("/api/jobs")
def list_jobs():
    with _jobs_lock:
        return [j.to_dict() for j in reversed(list(_jobs.values()))]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _get_job(job_id)
    return job.to_dict(include_log=True)


@app.get("/api/jobs/{job_id}/result")
def get_result(job_id: str):
    job = _get_job(job_id)
    if job.status != JobStatus.COMPLETED or not job.result_path:
        raise HTTPException(status_code=404, detail="Result not available")
    media_type = "video/mp4" if job.result_path.suffix == ".mp4" else "image/jpeg"
    return FileResponse(
        str(job.result_path),
        media_type=media_type,
        filename=job.result_path.name,
    )


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = _get_job(job_id)
    if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        raise HTTPException(status_code=409, detail=f"Job already {job.status}")
    job._cancel.set()
    job.status = JobStatus.CANCELLED
    job.finished_at = time.time()
    job.log("Cancelled by user")
    return {"status": "cancelled"}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job = _get_job(job_id)
    # Clean up files
    if job.work_dir.exists():
        shutil.rmtree(job.work_dir, ignore_errors=True)
    with _jobs_lock:
        _jobs.pop(job_id, None)
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Startup info
# ---------------------------------------------------------------------------

@app.on_event("startup")
def startup():
    logger.info("Astro Processing Server starting on port 8069")
    logger.info("CUDA available: %s", CUDA_AVAILABLE)
    logger.info("Work directory: %s", WORK_DIR)
