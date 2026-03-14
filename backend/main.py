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
  POST /api/galleries/{gallery}/timelapse – generate timelapse video (returns job)
  GET  /api/images/{gallery}/{filename} – serve a gallery image
  GET  /api/videos/{gallery}/{filename} – serve a gallery video
  GET  /api/jobs                 – list all jobs
  GET  /api/jobs/{job_id}        – get job status + log
  POST /api/jobs/{job_id}/cancel – cancel a running job
"""

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from PIL import Image

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import camera as cam
import remote
import stacking as stk
import timelapse as tl
from jobs import jobs, JobStatus, timelapse_semaphore, FFMPEG_THREADS

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_level = getattr(logging, _LOG_LEVEL, logging.INFO)
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


class BurstStackOptions(BaseModel):
    mode: str = "mean"
    output_name: Optional[str] = None


class BurstTimelapseOptions(BaseModel):
    fps: int = 30
    resolution: str = "1920x1080"
    output_name: Optional[str] = None


class BurstRequest(BaseModel):
    gallery: str
    count: int = 1
    interval: float = 0
    bulb_seconds: Optional[int] = None
    stack: Optional[BurstStackOptions] = None
    timelapse: Optional[BurstTimelapseOptions] = None
    remote: bool = False


class StackRequest(BaseModel):
    images: list[str]
    mode: str = "mean"
    output_name: Optional[str] = None
    remote: bool = False


class TimelapseRequest(BaseModel):
    images: list[str]
    fps: int = 30
    resolution: str = "1920x1080"
    output_name: Optional[str] = None
    remote: bool = False


class CreateGalleryRequest(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# Remote processing status
# ---------------------------------------------------------------------------


@app.get("/api/remote/status")
def remote_status():
    """Check if remote processing server is configured and reachable."""
    if not remote.is_configured():
        return {"configured": False, "server": None}
    health = remote.health_check()
    return {
        "configured": True,
        "server": remote.REMOTE_SERVER,
        "reachable": health is not None,
        "cuda": health.get("cuda", False) if health else False,
    }


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

    Optionally triggers stacking and/or timelapse generation after capture
    completes.  When remote processing is enabled, images are streamed to
    the remote server as they are captured to overlap transfer and capture.

    Returns 202 immediately with a job ID.
    """
    if req.count < 1:
        raise HTTPException(status_code=400, detail="count must be >= 1")
    if req.stack and req.stack.mode not in ("mean", "max", "align+mean"):
        raise HTTPException(status_code=400, detail=f"Unknown stacking mode: {req.stack.mode!r}")
    gallery_name = req.gallery
    want_stack = req.stack is not None
    want_timelapse = req.timelapse is not None
    use_remote = req.remote and remote.is_configured()

    label = f"Burst {req.count} frames into {gallery_name}"
    if want_stack:
        label += f" + stack ({req.stack.mode})"
    if want_timelapse:
        label += f" + timelapse ({req.timelapse.fps}fps)"
    if use_remote:
        label += f" [remote: {remote.REMOTE_SERVER}]"

    job = jobs.create(
        "burst",
        total=req.count,
        message=label,
    )

    def _run_burst():
        jobs.start(job)
        remote_job_ids = {}  # "stack" / "timelapse" -> remote job_id
        try:
            gallery_dir = _resolve_gallery(gallery_name)
            if gallery_dir is None:
                raise ValueError(f"Gallery '{gallery_name}' not found")

            # --- Remote streaming setup: create remote jobs before capture ---
            if use_remote and (want_stack or want_timelapse):
                if want_stack:
                    rid = remote.create_remote_job(
                        job_type="stack",
                        mode=req.stack.mode,
                        output_name=req.stack.output_name,
                    )
                    remote_job_ids["stack"] = rid
                    job.log(f"Created remote stack job {rid}")
                if want_timelapse:
                    rid = remote.create_remote_job(
                        job_type="timelapse",
                        fps=req.timelapse.fps,
                        resolution=req.timelapse.resolution,
                        output_name=req.timelapse.output_name,
                    )
                    remote_job_ids["timelapse"] = rid
                    job.log(f"Created remote timelapse job {rid}")

            def on_progress(frame_idx, total, saved_path):
                msg = (f"Frame {frame_idx + 1}/{total}"
                       + (f" saved {saved_path.name}" if saved_path else " FAILED"))
                jobs.update_progress(job, frame_idx + 1, msg)

                # Stream each captured frame to remote server(s)
                if saved_path and use_remote and remote_job_ids:
                    try:
                        for rid in remote_job_ids.values():
                            remote.upload_single_image(rid, saved_path)
                    except Exception as upload_exc:
                        job.log(f"Warning: remote upload failed for {saved_path.name}: {upload_exc}")

            saved = cam.capture_burst(
                gallery_dir,
                count=req.count,
                interval=req.interval,
                bulb_seconds=req.bulb_seconds,
                on_progress=on_progress,
                cancel_check=lambda: job.cancelled,
            )

            filenames = [p.name for p in saved]
            result = {
                "ok": True,
                "gallery": gallery_name,
                "captured": len(saved),
                "requested": req.count,
                "files": [
                    {"filename": p.name, "url": f"/api/images/{gallery_name}/{p.name}"}
                    for p in saved
                ],
            }

            # --- Post-processing ---
            post_job_ids = {}

            if want_stack and len(saved) >= 2:
                stack_opts = req.stack
                stack_output = stack_opts.output_name or f"stacked-{stack_opts.mode}-{int(time.time())}.jpg"

                if use_remote and "stack" in remote_job_ids:
                    # Finalize the remote job (images already uploaded)
                    job.log("Finalizing remote stack job…")
                    pp_job = _start_remote_finalize_job(
                        gallery_name, gallery_dir,
                        remote_job_ids["stack"], "stack", stack_output,
                    )
                    post_job_ids["stack_job_id"] = pp_job.id
                else:
                    pp_job = _start_local_stack_job(
                        gallery_name, gallery_dir, filenames,
                        stack_opts.mode, stack_output,
                    )
                    post_job_ids["stack_job_id"] = pp_job.id

            if want_timelapse and len(saved) >= 2:
                tl_opts = req.timelapse
                tl_output = tl_opts.output_name or f"timelapse-{tl_opts.fps}fps-{int(time.time())}.mp4"

                if use_remote and "timelapse" in remote_job_ids:
                    job.log("Finalizing remote timelapse job…")
                    pp_job = _start_remote_finalize_job(
                        gallery_name, gallery_dir,
                        remote_job_ids["timelapse"], "timelapse", tl_output,
                    )
                    post_job_ids["timelapse_job_id"] = pp_job.id
                else:
                    pp_job = _start_local_timelapse_job(
                        gallery_name, gallery_dir, filenames,
                        tl_opts.fps, tl_opts.resolution, tl_output,
                    )
                    post_job_ids["timelapse_job_id"] = pp_job.id

            result.update(post_job_ids)
            jobs.complete(job, result)

        except Exception as exc:
            # Clean up any remote jobs on failure
            for rid in remote_job_ids.values():
                try:
                    remote._cancel_remote(rid)
                except Exception:
                    pass
            jobs.fail(job, str(exc))

    threading.Thread(target=_run_burst, daemon=True).start()
    return {"job_id": job.id}


# ---------------------------------------------------------------------------
# Burst post-processing helpers
# ---------------------------------------------------------------------------


def _start_local_stack_job(gallery_name, gallery_dir, filenames, mode, output_name):
    """Kick off a local stacking job for burst post-processing."""
    pp_job = jobs.create(
        "stack",
        total=len(filenames),
        message=f"Burst post-stack {len(filenames)} images ({mode})",
    )

    def _run():
        jobs.start(pp_job)
        try:
            paths = [gallery_dir / fn for fn in filenames]
            missing = [fn for fn, p in zip(filenames, paths) if not p.exists()]
            if missing:
                raise FileNotFoundError(f"Images not found: {', '.join(missing)}")

            pp_job.log(f"Stacking {len(paths)} images, mode={mode}")

            def on_progress(processed, total):
                jobs.update_progress(pp_job, processed, f"Processing image {processed}/{total}")

            result_image = stk.stack_images(paths, mode=mode, on_progress=on_progress)
            output_path = gallery_dir / output_name
            result_image.save(str(output_path), format="JPEG", quality=95)
            jobs.complete(pp_job, {
                "ok": True,
                "gallery": gallery_name,
                "filename": output_name,
                "url": f"/api/images/{gallery_name}/{output_name}",
            })
        except Exception as exc:
            jobs.fail(pp_job, str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return pp_job


def _start_local_timelapse_job(gallery_name, gallery_dir, filenames, fps, resolution, output_name):
    """Kick off a local timelapse job for burst post-processing."""
    pp_job = jobs.create(
        "timelapse",
        total=len(filenames),
        message=f"Burst post-timelapse {len(filenames)} images @ {fps}fps {resolution}",
    )

    def _run():
        pp_job.log("Waiting for timelapse slot (limit: 1 concurrent)…")
        acquired = timelapse_semaphore.acquire(timeout=0)
        if not acquired:
            pp_job.log("Another timelapse is running, queued…")
            timelapse_semaphore.acquire()
        jobs.start(pp_job)
        try:
            paths = [gallery_dir / fn for fn in filenames]
            missing = [fn for fn, p in zip(filenames, paths) if not p.exists()]
            if missing:
                raise FileNotFoundError(f"Images not found: {', '.join(missing)}")

            pp_job.log(f"Generating {fps}fps {resolution} timelapse from {len(paths)} images")
            pp_job.log(f"ffmpeg threads limited to {FFMPEG_THREADS}")

            def on_progress(phase, processed, total):
                if phase == "resize":
                    jobs.update_progress(pp_job, processed, f"Resizing image {processed}/{total}")
                else:
                    jobs.update_progress(pp_job, processed, f"Encoding frame {processed}/{total}")

            output_path = gallery_dir / output_name
            tl.generate_timelapse(
                paths, output_path,
                fps=fps, resolution=resolution,
                threads=FFMPEG_THREADS,
                on_progress=on_progress,
                cancel_check=lambda: pp_job.cancelled,
            )
            file_size_mb = output_path.stat().st_size / (1024 * 1024)
            jobs.complete(pp_job, {
                "ok": True,
                "gallery": gallery_name,
                "filename": output_name,
                "url": f"/api/videos/{gallery_name}/{output_name}",
                "size_mb": round(file_size_mb, 1),
            })
        except Exception as exc:
            jobs.fail(pp_job, str(exc))
        finally:
            timelapse_semaphore.release()

    threading.Thread(target=_run, daemon=True).start()
    return pp_job


def _start_remote_finalize_job(gallery_name, gallery_dir, remote_job_id, job_type, output_name):
    """Finalize a pre-populated remote job, poll for result, and download."""
    pp_job = jobs.create(
        job_type,
        total=1,
        message=f"Remote {job_type}: finalizing & downloading",
    )

    def _run():
        jobs.start(pp_job)
        try:
            def on_progress(phase, processed, total):
                jobs.update_progress(pp_job, processed, f"{phase.capitalize()}: {processed}/{total}")

            result_path = remote.finalize_and_download(
                remote_job_id,
                job_type=job_type,
                output_name=output_name,
                output_dir=gallery_dir,
                on_progress=on_progress,
                cancel_check=lambda: pp_job.cancelled,
            )

            result_data = {
                "ok": True,
                "gallery": gallery_name,
                "filename": result_path.name,
                "remote": True,
            }
            if job_type == "timelapse":
                result_data["url"] = f"/api/videos/{gallery_name}/{result_path.name}"
                result_data["size_mb"] = round(result_path.stat().st_size / (1024 * 1024), 1)
            else:
                result_data["url"] = f"/api/images/{gallery_name}/{result_path.name}"

            jobs.complete(pp_job, result_data)
        except Exception as exc:
            jobs.fail(pp_job, str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return pp_job


# ---------------------------------------------------------------------------
# Gallery endpoints
# ---------------------------------------------------------------------------


@app.get("/api/galleries")
def list_galleries():
    galleries = []
    for d in sorted(GALLERY_ROOT.iterdir()):
        if not d.is_dir():
            continue
        # Skip hidden dirs, .thumbs caches, and system dirs like lost+found
        if d.name.startswith(".") or d.name == "lost+found":
            continue
        # Only show dirs whose names pass gallery-name validation
        safe = "".join(c for c in d.name if c.isalnum() or c in "._- ").strip()
        if safe != d.name:
            continue
        images = _list_images(d)
        galleries.append(
            {
                "name": d.name,
                "image_count": len(images),
                "thumbnail": f"/api/thumbnails/{d.name}/{images[0]}" if images else None,
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

    Returns 202 immediately with a job ID.  All validation and processing
    happens in the background thread so this never blocks.
    """
    gallery_name = gallery
    image_filenames = list(req.images)
    mode = req.mode
    use_remote = req.remote and remote.is_configured()
    if mode not in ("mean", "max", "align+mean"):
        raise HTTPException(status_code=400, detail=f"Unknown stacking mode: {mode!r}")
    output_name = req.output_name or f"stacked-{mode}-{int(time.time())}.jpg"

    label = f"Stacking {len(image_filenames)} images ({mode})"
    if use_remote:
        label += f" [remote: {remote.REMOTE_SERVER}]"

    job = jobs.create(
        "stack",
        total=len(image_filenames),
        message=label,
    )

    def _run_stack():
        jobs.start(job)
        try:
            gallery_dir = _resolve_gallery(gallery_name)
            if gallery_dir is None:
                raise ValueError(f"Gallery '{gallery_name}' not found")

            paths = []
            for fn in image_filenames:
                p = gallery_dir / fn
                if not p.exists():
                    raise FileNotFoundError(f"Image not found: {fn}")
                paths.append(p)

            if len(paths) < 2:
                raise ValueError("At least 2 images required for stacking")

            if use_remote:
                job.log(f"Sending {len(paths)} images to remote server")

                def on_progress(phase, processed, total):
                    jobs.update_progress(
                        job, processed,
                        f"{phase.capitalize()}: {processed}/{total}"
                    )

                result_path = remote.process_remote(
                    job_type="stack",
                    image_paths=paths,
                    mode=mode,
                    output_name=output_name,
                    output_dir=gallery_dir,
                    on_progress=on_progress,
                    cancel_check=lambda: job.cancelled,
                )
                jobs.complete(job, {
                    "ok": True,
                    "gallery": gallery_name,
                    "filename": result_path.name,
                    "url": f"/api/images/{gallery_name}/{result_path.name}",
                    "remote": True,
                })
            else:
                job.log(f"Validated {len(paths)} images, starting {mode} stack")

                def on_progress(processed, total):
                    jobs.update_progress(
                        job, processed, f"Processing image {processed}/{total}"
                    )

                result_image = stk.stack_images(paths, mode=mode, on_progress=on_progress)
                output_path = gallery_dir / output_name
                job.log(f"Saving result to {output_name}")
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
# Timelapse endpoint
# ---------------------------------------------------------------------------


@app.post("/api/galleries/{gallery}/timelapse", status_code=202)
async def create_timelapse(gallery: str, req: TimelapseRequest):
    """Generate a timelapse video from gallery images as a background job.

    Returns 202 immediately with a job ID.
    """
    gallery_name = gallery
    image_filenames = list(req.images)
    fps = req.fps
    resolution = req.resolution
    use_remote = req.remote and remote.is_configured()
    output_name = req.output_name or f"timelapse-{fps}fps-{int(time.time())}.mp4"

    label = f"Timelapse {len(image_filenames)} images @ {fps}fps {resolution}"
    if use_remote:
        label += f" [remote: {remote.REMOTE_SERVER}]"

    job = jobs.create(
        "timelapse",
        total=len(image_filenames),
        message=label,
    )

    def _run_timelapse():
        if not use_remote:
            job.log("Waiting for timelapse slot (limit: 1 concurrent)…")
            acquired = timelapse_semaphore.acquire(timeout=0)
            if not acquired:
                job.log("Another timelapse is running, queued…")
                timelapse_semaphore.acquire()
        jobs.start(job)
        try:
            gallery_dir = _resolve_gallery(gallery_name)
            if gallery_dir is None:
                raise ValueError(f"Gallery '{gallery_name}' not found")

            paths = []
            for fn in image_filenames:
                p = gallery_dir / fn
                if not p.exists():
                    raise FileNotFoundError(f"Image not found: {fn}")
                paths.append(p)

            if len(paths) < 2:
                raise ValueError("At least 2 images required for a timelapse")

            if use_remote:
                job.log(f"Sending {len(paths)} images to remote server")

                def on_progress(phase, processed, total):
                    jobs.update_progress(
                        job, processed,
                        f"{phase.capitalize()}: {processed}/{total}"
                    )

                result_path = remote.process_remote(
                    job_type="timelapse",
                    image_paths=paths,
                    fps=fps,
                    resolution=resolution,
                    output_name=output_name,
                    output_dir=gallery_dir,
                    on_progress=on_progress,
                    cancel_check=lambda: job.cancelled,
                )
                file_size_mb = result_path.stat().st_size / (1024 * 1024)
                jobs.complete(job, {
                    "ok": True,
                    "gallery": gallery_name,
                    "filename": result_path.name,
                    "url": f"/api/videos/{gallery_name}/{result_path.name}",
                    "size_mb": round(file_size_mb, 1),
                    "remote": True,
                })
            else:
                job.log(f"Validated {len(paths)} images, generating {fps}fps {resolution} video")
                job.log(f"ffmpeg threads limited to {FFMPEG_THREADS}")

                def on_progress(phase, processed, total):
                    if phase == "resize":
                        jobs.update_progress(
                            job, processed, f"Resizing image {processed}/{total}"
                        )
                    else:
                        jobs.update_progress(
                            job, processed, f"Encoding frame {processed}/{total}"
                        )

                output_path = gallery_dir / output_name
                tl.generate_timelapse(
                    paths,
                    output_path,
                    fps=fps,
                    resolution=resolution,
                    threads=FFMPEG_THREADS,
                    on_progress=on_progress,
                    cancel_check=lambda: job.cancelled,
                )

                file_size_mb = output_path.stat().st_size / (1024 * 1024)
                jobs.complete(job, {
                    "ok": True,
                    "gallery": gallery_name,
                    "filename": output_name,
                    "url": f"/api/videos/{gallery_name}/{output_name}",
                    "size_mb": round(file_size_mb, 1),
                })
        except Exception as exc:
            jobs.fail(job, str(exc))
        finally:
            if not use_remote:
                timelapse_semaphore.release()

    threading.Thread(target=_run_timelapse, daemon=True).start()
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
    """Get the status of a specific job including its log."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict(include_log=True)


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


@app.get("/api/thumbnails/{gallery}/{filename}")
def serve_thumbnail(gallery: str, filename: str):
    """Serve a cached 300px thumbnail for a gallery image."""
    gallery_dir = _gallery_path(gallery)
    file_path = gallery_dir / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    thumb_path = _get_or_create_thumbnail(gallery_dir, file_path)
    return FileResponse(str(thumb_path), media_type="image/jpeg")


@app.get("/api/images/{gallery}/{filename}")
def serve_image(gallery: str, filename: str):
    gallery_dir = _gallery_path(gallery)
    file_path = gallery_dir / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(str(file_path))


@app.get("/api/videos/{gallery}/{filename}")
def serve_video(gallery: str, filename: str):
    gallery_dir = _gallery_path(gallery)
    file_path = gallery_dir / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(str(file_path), media_type="video/mp4")


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
    """Resolve and validate a gallery name.  Raises HTTPException on failure."""
    safe = "".join(c for c in name if c.isalnum() or c in "._- ").strip()
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid gallery name")
    p = GALLERY_ROOT / safe
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Gallery '{safe}' not found")
    return p


def _resolve_gallery(name: str) -> Optional[Path]:
    """Resolve a gallery name to a path, returning None if it doesn't exist.

    Unlike _gallery_path this does NOT raise HTTPException – it's safe
    to call from background threads where there's no request context.
    """
    safe = "".join(c for c in name if c.isalnum() or c in "._- ").strip()
    if not safe:
        return None
    p = GALLERY_ROOT / safe
    return p if p.exists() else None


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".cr2", ".cr3", ".nef", ".arw"}
_VIDEO_EXTS = {".mp4", ".webm"}
_MEDIA_EXTS = _IMAGE_EXTS | _VIDEO_EXTS


def _list_images(directory: Path) -> list[str]:
    return sorted(f.name for f in directory.iterdir() if f.suffix.lower() in _MEDIA_EXTS)


_THUMB_SIZE = 300
_THUMB_DIR = ".thumbs"


def _get_or_create_thumbnail(gallery_dir: Path, file_path: Path) -> Path:
    """Return the path to a cached JPEG thumbnail, creating it if needed."""
    thumb_dir = gallery_dir / _THUMB_DIR
    thumb_dir.mkdir(exist_ok=True)
    thumb_name = file_path.stem + ".thumb.jpg"
    thumb_path = thumb_dir / thumb_name

    # Regenerate if source is newer than cached thumbnail
    if thumb_path.exists() and thumb_path.stat().st_mtime >= file_path.stat().st_mtime:
        return thumb_path

    try:
        img = Image.open(file_path)
        img.thumbnail((_THUMB_SIZE, _THUMB_SIZE))
        img = img.convert("RGB")
        img.save(str(thumb_path), "JPEG", quality=80)
    except Exception:
        logger.warning("Failed to create thumbnail for %s, serving original", file_path.name)
        return file_path

    return thumb_path
