"""
FastAPI backend for gphoto2-astro-webui.

Endpoints:
  GET  /api/camera/status        – camera connection & summary
  GET  /api/camera/config-keys   – list all config keys supported by the camera
  GET  /api/camera/exposure      – get current exposure settings + choices
  POST /api/camera/exposure      – set exposure settings
  POST /api/camera/capture       – capture one image into a gallery
  POST /api/camera/burst         – start burst capture (returns job)
  GET  /api/galleries            – list galleries
  POST /api/galleries            – create a new gallery
  GET  /api/galleries/{gallery}  – list images in a gallery
  POST /api/galleries/{gallery}/stack – start stacking (returns job)
  GET  /api/images/{gallery}/{filename} – serve a gallery image
  GET  /api/jobs                 – list all jobs
  GET  /api/jobs/{job_id}        – get job status
  POST /api/jobs/{job_id}/cancel – cancel a running job
"""

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import camera as cam
import stacking as stk
from jobs import jobs, JobStatus

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_level = getattr(logging, _LOG_LEVEL, logging.INFO)
# basicConfig configures the root logger when no handlers exist yet (standalone run).
# root.setLevel ensures the level is always applied even when uvicorn has already
# configured the root logger's handlers before this module is imported.
logging.basicConfig(level=_level)
logging.root.setLevel(_level)
logger = logging.getLogger(__name__)
if not hasattr(logging, _LOG_LEVEL):
    logger.warning("Unknown LOG_LEVEL %r; defaulting to INFO", _LOG_LEVEL)

# ---------------------------------------------------------------------------
# Gallery root – defaults to ./galleries, overrideable via env var
# ---------------------------------------------------------------------------
GALLERY_ROOT = Path(os.environ.get("GALLERY_ROOT", Path(__file__).parent / "galleries"))
GALLERY_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="gphoto2 Astro WebUI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ExposureSettings(BaseModel):
    aperture: Optional[str] = None
    shutter: Optional[str] = None
    iso: Optional[str] = None


class CaptureRequest(BaseModel):
    gallery: str
    bulb_seconds: Optional[int] = None


class BurstRequest(BaseModel):
    gallery: str
    count: int = 1
    interval: float = 0
    bulb_seconds: Optional[int] = None


class StackRequest(BaseModel):
    images: list[str]
    mode: str = "mean"
    output_name: Optional[str] = None


class CreateGalleryRequest(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# Camera endpoints
# ---------------------------------------------------------------------------


@app.get("/api/camera/status")
def camera_status():
    return cam.get_camera_summary()


@app.get("/api/camera/config-keys")
def list_config_keys():
    """Return the list of configuration key paths supported by the connected camera."""
    keys = cam.list_config_keys()
    return {"keys": keys}


@app.get("/api/camera/exposure")
def get_exposure():
    return cam.get_exposure_settings()


@app.post("/api/camera/exposure")
def set_exposure(settings: ExposureSettings):
    result = cam.set_exposure_settings(
        aperture=settings.aperture,
        shutter=settings.shutter,
        iso=settings.iso,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "Unknown error"))
    return result


@app.post("/api/camera/capture")
def capture_image(req: CaptureRequest):
    gallery_dir = _gallery_path(req.gallery)
    try:
        saved = cam.capture_image(gallery_dir, bulb_seconds=req.bulb_seconds)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {
        "ok": True,
        "gallery": req.gallery,
        "filename": saved.name,
        "url": f"/api/images/{req.gallery}/{saved.name}",
    }


@app.post("/api/camera/burst", status_code=202)
async def capture_burst(req: BurstRequest):
    """Start a burst capture as a background job.

    Returns immediately with a job ID.  Poll ``GET /api/jobs/{id}``
    for progress and results.
    """
    if req.count < 1:
        raise HTTPException(status_code=400, detail="count must be >= 1")
    gallery_dir = _gallery_path(req.gallery)
    gallery_name = req.gallery

    job = jobs.create(
        "burst",
        total=req.count,
        message=f"Burst {req.count} frames into {gallery_name}",
    )

    def _run_burst():
        jobs.start(job)
        try:
            def on_progress(frame_idx, total, saved_path):
                jobs.update_progress(
                    job,
                    frame_idx + 1,
                    f"Frame {frame_idx + 1}/{total}"
                    + (f" saved {saved_path.name}" if saved_path else " FAILED"),
                )

            saved = cam.capture_burst(
                gallery_dir,
                count=req.count,
                interval=req.interval,
                bulb_seconds=req.bulb_seconds,
                on_progress=on_progress,
                cancel_check=lambda: job.cancelled,
            )
            jobs.complete(job, {
                "ok": True,
                "gallery": gallery_name,
                "captured": len(saved),
                "requested": req.count,
                "files": [
                    {"filename": p.name, "url": f"/api/images/{gallery_name}/{p.name}"}
                    for p in saved
                ],
            })
        except Exception as exc:
            jobs.fail(job, str(exc))

    threading.Thread(target=_run_burst, daemon=True).start()
    return {"job_id": job.id}


# ---------------------------------------------------------------------------
# Gallery endpoints
# ---------------------------------------------------------------------------


@app.get("/api/galleries")
def list_galleries():
    galleries = []
    for d in sorted(GALLERY_ROOT.iterdir()):
        if d.is_dir():
            images = _list_images(d)
            galleries.append(
                {
                    "name": d.name,
                    "image_count": len(images),
                    "thumbnail": f"/api/images/{d.name}/{images[0]}" if images else None,
                }
            )
    return {"galleries": galleries}


@app.post("/api/galleries", status_code=201)
def create_gallery(req: CreateGalleryRequest):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Gallery name cannot be empty")
    safe = "".join(c for c in name if c.isalnum() or c in "._- ").strip()
    if not safe:
        raise HTTPException(status_code=400, detail="Gallery name contains no valid characters")
    gallery_dir = GALLERY_ROOT / safe
    gallery_dir.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "name": safe}


@app.get("/api/galleries/{gallery}")
def list_gallery_images(gallery: str):
    gallery_dir = _gallery_path(gallery)
    images = _list_images(gallery_dir)
    return {
        "gallery": gallery,
        "images": [
            {"filename": fn, "url": f"/api/images/{gallery}/{fn}"}
            for fn in images
        ],
    }


@app.delete("/api/galleries/{gallery}/{filename}")
def delete_image(gallery: str, filename: str):
    gallery_dir = _gallery_path(gallery)
    file_path = gallery_dir / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    file_path.unlink()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Stacking endpoint
# ---------------------------------------------------------------------------


@app.post("/api/galleries/{gallery}/stack", status_code=202)
async def stack_gallery_images(gallery: str, req: StackRequest):
    """Start image stacking as a background job.

    Returns immediately with a job ID.  Poll ``GET /api/jobs/{id}``
    for progress and results.
    """
    gallery_dir = _gallery_path(gallery)
    gallery_name = gallery

    # Resolve filenames to paths and validate
    paths = []
    for fn in req.images:
        p = gallery_dir / fn
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"Image not found: {fn}")
        paths.append(p)

    if len(paths) < 2:
        raise HTTPException(status_code=400, detail="At least 2 images required for stacking")

    output_name = req.output_name or f"stacked-{req.mode}-{int(time.time())}.jpg"
    mode = req.mode

    job = jobs.create(
        "stack",
        total=len(paths),
        message=f"Stacking {len(paths)} images ({mode})",
    )

    def _run_stack():
        jobs.start(job)
        try:
            def on_progress(processed, total):
                jobs.update_progress(
                    job, processed, f"Processing image {processed}/{total}"
                )

            result_image = stk.stack_images(paths, mode=mode, on_progress=on_progress)
            output_path = gallery_dir / output_name
            result_image.save(str(output_path), format="JPEG", quality=95)
            jobs.complete(job, {
                "ok": True,
                "gallery": gallery_name,
                "filename": output_name,
                "url": f"/api/images/{gallery_name}/{output_name}",
            })
        except Exception as exc:
            jobs.fail(job, str(exc))

    threading.Thread(target=_run_stack, daemon=True).start()
    return {"job_id": job.id}


# ---------------------------------------------------------------------------
# Job tracking endpoints
# ---------------------------------------------------------------------------


@app.get("/api/jobs")
async def list_jobs():
    """List all jobs (active and recent history)."""
    return {"jobs": jobs.list_all()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    """Get the status of a specific job."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    """Request cancellation of a running job."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in (JobStatus.queued, JobStatus.running):
        raise HTTPException(status_code=400, detail=f"Cannot cancel job in state: {job.status.value}")
    jobs.cancel(job)
    return {"ok": True, "job_id": job_id}


# ---------------------------------------------------------------------------
# Static image serving
# ---------------------------------------------------------------------------


@app.get("/api/images/{gallery}/{filename}")
def serve_image(gallery: str, filename: str):
    gallery_dir = _gallery_path(gallery)
    file_path = gallery_dir / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(str(file_path))


# ---------------------------------------------------------------------------
# Serve the Vite/React build (SPA catch-all)
# ---------------------------------------------------------------------------
_DIST = Path(__file__).parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="static")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gallery_path(name: str) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in "._- ").strip()
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid gallery name")
    p = GALLERY_ROOT / safe
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Gallery '{safe}' not found")
    return p


def _list_images(directory: Path) -> list[str]:
    exts = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".cr2", ".cr3", ".nef", ".arw"}
    return sorted(f.name for f in directory.iterdir() if f.suffix.lower() in exts)
