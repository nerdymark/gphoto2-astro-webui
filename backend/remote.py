"""
Client for the remote processing server.

When REMOTE_SERVER is configured, stacking and timelapse jobs are
offloaded to a powerful remote machine instead of running locally
on the Raspberry Pi.

Images are uploaded in batches to minimize HTTP round trips.
The server accumulates incrementally, so finalize is near-instant
for stacking.
"""

import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import urllib.request
import urllib.error
import json

logger = logging.getLogger(__name__)

# Remote server URL — set via environment or default to the local network server.
REMOTE_SERVER = os.environ.get("REMOTE_SERVER", "").rstrip("/")

# Batch size for image uploads (images per HTTP request)
UPLOAD_BATCH_SIZE = int(os.environ.get("UPLOAD_BATCH_SIZE", "20"))


def is_configured() -> bool:
    """Return True if remote processing is configured."""
    return bool(REMOTE_SERVER)


def health_check() -> Optional[dict]:
    """Check if the remote server is reachable."""
    if not REMOTE_SERVER:
        return None
    try:
        req = urllib.request.Request(f"{REMOTE_SERVER}/api/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.warning("Remote server health check failed: %s", exc)
        return None


def process_remote(
    job_type: str,
    image_paths: list[Path],
    mode: str = "mean",
    fps: int = 30,
    resolution: str = "1920x1080",
    output_name: Optional[str] = None,
    output_dir: Optional[Path] = None,
    on_progress=None,
    cancel_check=None,
) -> Path:
    """Send images to the remote server for processing and download the result.

    Args:
        job_type: "stack" or "timelapse"
        image_paths: List of image file paths to process
        mode: Stacking mode (mean/max/align+mean)
        fps: Timelapse FPS
        resolution: Timelapse resolution
        output_name: Desired output filename
        output_dir: Directory to save the result
        on_progress: Callback(phase, current, total)
        cancel_check: Callable returning True to abort

    Returns:
        Path to the downloaded result file.
    """
    if not REMOTE_SERVER:
        raise RuntimeError("Remote server not configured")

    total = len(image_paths)

    # Step 1: Create job
    create_body = json.dumps({
        "type": job_type,
        "mode": mode,
        "fps": fps,
        "resolution": resolution,
        "output_name": output_name,
    }).encode()

    req = urllib.request.Request(
        f"{REMOTE_SERVER}/api/jobs",
        data=create_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    job_id = data["job_id"]
    logger.info("Remote job %s created for %d images", job_id, total)

    try:
        # Step 2: Upload images in batches
        for batch_start in range(0, total, UPLOAD_BATCH_SIZE):
            if cancel_check and cancel_check():
                _cancel_remote(job_id)
                raise RuntimeError("Cancelled")

            batch_end = min(batch_start + UPLOAD_BATCH_SIZE, total)
            batch = image_paths[batch_start:batch_end]

            _upload_batch(job_id, batch)

            if on_progress:
                on_progress("upload", batch_end, total)

            logger.info("Remote job %s: uploaded %d/%d", job_id, batch_end, total)

        # Step 3: Finalize
        if cancel_check and cancel_check():
            _cancel_remote(job_id)
            raise RuntimeError("Cancelled")

        req = urllib.request.Request(
            f"{REMOTE_SERVER}/api/jobs/{job_id}/finalize",
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            json.loads(resp.read())

        # Step 4: Poll for completion
        while True:
            if cancel_check and cancel_check():
                _cancel_remote(job_id)
                raise RuntimeError("Cancelled")

            time.sleep(1)
            req = urllib.request.Request(
                f"{REMOTE_SERVER}/api/jobs/{job_id}",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = json.loads(resp.read())

            if on_progress and status.get("progress"):
                on_progress(
                    "processing",
                    status["progress"],
                    status.get("total", total),
                )

            if status["status"] == "completed":
                break
            elif status["status"] in ("failed", "cancelled"):
                raise RuntimeError(
                    f"Remote job failed: {status.get('error', 'unknown')}"
                )

        # Step 5: Download result
        if on_progress:
            on_progress("download", 0, 1)

        result_url = f"{REMOTE_SERVER}/api/jobs/{job_id}/result"
        if not output_name:
            ext = ".mp4" if job_type == "timelapse" else ".jpg"
            output_name = f"remote-{job_type}-{int(time.time())}{ext}"
        output_path = (output_dir or Path(".")) / output_name

        req = urllib.request.Request(result_url, method="GET")
        with urllib.request.urlopen(req, timeout=600) as resp:
            output_path.write_bytes(resp.read())

        if on_progress:
            on_progress("download", 1, 1)

        logger.info("Remote job %s result saved to %s", job_id, output_path)

        # Cleanup remote job
        try:
            _delete_remote(job_id)
        except Exception:
            pass

        return output_path

    except Exception:
        # Try to cancel/cleanup on failure
        try:
            _cancel_remote(job_id)
        except Exception:
            pass
        raise


def _upload_batch(job_id: str, paths: list[Path]):
    """Upload a batch of images as multipart/form-data."""
    boundary = f"----AstroBatch{int(time.time() * 1000)}"
    body_parts = []

    for p in paths:
        body_parts.append(f"--{boundary}\r\n".encode())
        body_parts.append(
            f'Content-Disposition: form-data; name="files"; filename="{p.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
        )
        body_parts.append(p.read_bytes())
        body_parts.append(b"\r\n")

    body_parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(body_parts)

    req = urllib.request.Request(
        f"{REMOTE_SERVER}/api/jobs/{job_id}/images",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read())


def _cancel_remote(job_id: str):
    """Cancel a remote job."""
    try:
        req = urllib.request.Request(
            f"{REMOTE_SERVER}/api/jobs/{job_id}/cancel",
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        logger.warning("Failed to cancel remote job %s: %s", job_id, exc)


def _delete_remote(job_id: str):
    """Delete a remote job and its files."""
    req = urllib.request.Request(
        f"{REMOTE_SERVER}/api/jobs/{job_id}",
        method="DELETE",
    )
    urllib.request.urlopen(req, timeout=10)
