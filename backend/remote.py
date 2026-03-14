"""
Client for the remote processing server.

When REMOTE_SERVER is configured, stacking and timelapse jobs are
offloaded to a powerful remote machine instead of running locally
on the Raspberry Pi.

Images are uploaded in batches to minimize HTTP round trips.
The server accumulates incrementally, so finalize is near-instant
for stacking.

All network operations retry with exponential backoff so that
transient WiFi drops don't abort a long-running transfer.
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

# Retry configuration
MAX_RETRIES = int(os.environ.get("REMOTE_MAX_RETRIES", "6"))
INITIAL_BACKOFF = float(os.environ.get("REMOTE_INITIAL_BACKOFF", "2"))
MAX_BACKOFF = float(os.environ.get("REMOTE_MAX_BACKOFF", "60"))


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


def _is_retryable(exc: Exception) -> bool:
    """Return True if the exception is a transient network error worth retrying."""
    if isinstance(exc, urllib.error.URLError):
        # Connection refused, no route to host, network unreachable, timeout
        return True
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        # Retry on server errors (5xx) but not client errors (4xx)
        return exc.code >= 500
    return False


def _request_with_retry(
    url: str,
    method: str = "GET",
    data: Optional[bytes] = None,
    headers: Optional[dict] = None,
    timeout: int = 30,
    max_retries: int = MAX_RETRIES,
    on_retry=None,
) -> bytes:
    """Make an HTTP request with exponential backoff retry on transient errors.

    Args:
        url: Full URL to request.
        method: HTTP method.
        data: Request body bytes.
        headers: Request headers.
        timeout: Per-request timeout in seconds.
        max_retries: Maximum retry attempts.
        on_retry: Optional callback(attempt, wait_secs, error) called before each retry.

    Returns:
        Response body bytes.

    Raises:
        The last exception if all retries are exhausted.
    """
    hdrs = headers or {}
    last_exc = None
    backoff = INITIAL_BACKOFF

    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            last_exc = exc
            if attempt >= max_retries or not _is_retryable(exc):
                raise

            wait = min(backoff, MAX_BACKOFF)
            logger.warning(
                "Request %s %s failed (attempt %d/%d): %s — retrying in %.0fs",
                method, url, attempt + 1, max_retries + 1, exc, wait,
            )
            if on_retry:
                on_retry(attempt + 1, wait, exc)
            time.sleep(wait)
            backoff *= 2

    raise last_exc  # type: ignore[misc]


def _get_remote_image_count(job_id: str) -> int:
    """Ask the server how many images it has received for this job.

    Used to resume uploads after a disconnect.
    """
    try:
        body = _request_with_retry(
            f"{REMOTE_SERVER}/api/jobs/{job_id}",
            method="GET",
            timeout=10,
            max_retries=3,
        )
        data = json.loads(body)
        return data.get("images_received", 0)
    except Exception:
        return 0


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

    All network operations are retried with exponential backoff so that
    transient WiFi disconnects do not lose a long-running transfer.

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

    def _log_retry(attempt, wait, exc):
        logger.info("Retry %d: waiting %.0fs after %s", attempt, wait, exc)

    # Step 1: Create job
    create_body = json.dumps({
        "type": job_type,
        "mode": mode,
        "fps": fps,
        "resolution": resolution,
        "output_name": output_name,
    }).encode()

    resp_body = _request_with_retry(
        f"{REMOTE_SERVER}/api/jobs",
        method="POST",
        data=create_body,
        headers={"Content-Type": "application/json"},
        timeout=30,
        on_retry=_log_retry,
    )
    data = json.loads(resp_body)
    job_id = data["job_id"]
    logger.info("Remote job %s created for %d images", job_id, total)

    try:
        # Step 2: Upload images in batches (with resume support)
        #
        # Before starting, check how many images the server already has.
        # This allows resuming after a disconnect — we skip batches the
        # server already received.
        already_received = _get_remote_image_count(job_id)
        if already_received > 0:
            logger.info(
                "Remote job %s: server already has %d/%d images, resuming",
                job_id, already_received, total,
            )

        # Skip batches that were fully uploaded
        start_from = already_received - (already_received % UPLOAD_BATCH_SIZE)
        # Be conservative: re-upload the batch that was in progress
        # (server handles duplicate filenames gracefully via sequential numbering)

        for batch_start in range(start_from, total, UPLOAD_BATCH_SIZE):
            if cancel_check and cancel_check():
                _cancel_remote(job_id)
                raise RuntimeError("Cancelled")

            batch_end = min(batch_start + UPLOAD_BATCH_SIZE, total)
            batch = image_paths[batch_start:batch_end]

            _upload_batch_with_retry(job_id, batch, on_retry=_log_retry)

            if on_progress:
                on_progress("upload", batch_end, total)

            logger.info("Remote job %s: uploaded %d/%d", job_id, batch_end, total)

        # Step 3: Finalize
        if cancel_check and cancel_check():
            _cancel_remote(job_id)
            raise RuntimeError("Cancelled")

        _request_with_retry(
            f"{REMOTE_SERVER}/api/jobs/{job_id}/finalize",
            method="POST",
            timeout=30,
            on_retry=_log_retry,
        )

        # Step 4: Poll for completion (with retry on transient errors)
        consecutive_failures = 0
        poll_backoff = INITIAL_BACKOFF

        while True:
            if cancel_check and cancel_check():
                _cancel_remote(job_id)
                raise RuntimeError("Cancelled")

            time.sleep(1)

            try:
                resp_body = _request_with_retry(
                    f"{REMOTE_SERVER}/api/jobs/{job_id}",
                    method="GET",
                    timeout=10,
                    max_retries=2,  # Light retry per poll cycle
                )
                status = json.loads(resp_body)
                consecutive_failures = 0
                poll_backoff = INITIAL_BACKOFF
            except Exception as exc:
                consecutive_failures += 1
                if consecutive_failures > MAX_RETRIES:
                    logger.error(
                        "Remote job %s: lost contact after %d consecutive poll failures",
                        job_id, consecutive_failures,
                    )
                    raise RuntimeError(
                        f"Lost connection to remote server after {consecutive_failures} "
                        f"poll failures: {exc}"
                    ) from exc

                wait = min(poll_backoff, MAX_BACKOFF)
                logger.warning(
                    "Remote job %s: poll failed (%d/%d): %s — retrying in %.0fs",
                    job_id, consecutive_failures, MAX_RETRIES, exc, wait,
                )
                if on_progress:
                    on_progress("reconnecting", consecutive_failures, MAX_RETRIES)
                time.sleep(wait)
                poll_backoff *= 2
                continue

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

        # Step 5: Download result (with retry)
        if on_progress:
            on_progress("download", 0, 1)

        result_url = f"{REMOTE_SERVER}/api/jobs/{job_id}/result"
        if not output_name:
            ext = ".mp4" if job_type == "timelapse" else ".jpg"
            output_name = f"remote-{job_type}-{int(time.time())}{ext}"
        output_path = (output_dir or Path(".")) / output_name

        resp_body = _request_with_retry(
            result_url,
            method="GET",
            timeout=600,
            max_retries=MAX_RETRIES,
            on_retry=_log_retry,
        )
        output_path.write_bytes(resp_body)

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


def create_remote_job(
    job_type: str,
    mode: str = "mean",
    fps: int = 30,
    resolution: str = "1920x1080",
    output_name: Optional[str] = None,
) -> str:
    """Create a remote processing job and return its job_id.

    Used by burst capture to pre-create the job so images can be streamed
    as they are captured.
    """
    if not REMOTE_SERVER:
        raise RuntimeError("Remote server not configured")

    create_body = json.dumps({
        "type": job_type,
        "mode": mode,
        "fps": fps,
        "resolution": resolution,
        "output_name": output_name,
    }).encode()

    resp_body = _request_with_retry(
        f"{REMOTE_SERVER}/api/jobs",
        method="POST",
        data=create_body,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    data = json.loads(resp_body)
    job_id = data["job_id"]
    logger.info("Remote job %s created (type=%s)", job_id, job_type)
    return job_id


def upload_single_image(job_id: str, image_path: Path, on_retry=None):
    """Upload a single image to an existing remote job.

    Used during burst capture to stream images as they are captured,
    overlapping network transfer with camera capture.

    Raises on failure after retries so the caller can track which
    images need re-uploading.
    """
    def _log_retry(attempt, wait, exc):
        logger.info(
            "Remote upload retry %d for %s: waiting %.0fs after %s",
            attempt, image_path.name, wait, exc,
        )
        if on_retry:
            on_retry(attempt, wait, exc)

    _upload_batch_with_retry(job_id, [image_path], on_retry=_log_retry)


def retry_failed_uploads(job_id: str, failed_paths: list[Path], on_retry=None) -> list[Path]:
    """Re-upload images that failed during burst streaming.

    Uploads in batches with full retry logic.  Returns the list of
    paths that still failed after all retries (empty on full success).
    """
    if not failed_paths:
        return []

    still_failed = []
    total = len(failed_paths)

    def _log_retry(attempt, wait, exc):
        logger.info("Retry %d: waiting %.0fs after %s", attempt, wait, exc)
        if on_retry:
            on_retry(attempt, wait, exc)

    for batch_start in range(0, total, UPLOAD_BATCH_SIZE):
        batch = failed_paths[batch_start:batch_start + UPLOAD_BATCH_SIZE]
        try:
            _upload_batch_with_retry(job_id, batch, on_retry=_log_retry)
            logger.info(
                "Remote job %s: re-uploaded %d/%d failed images",
                job_id, min(batch_start + UPLOAD_BATCH_SIZE, total), total,
            )
        except Exception as exc:
            logger.error(
                "Remote job %s: batch re-upload still failing: %s", job_id, exc
            )
            still_failed.extend(batch)

    return still_failed


def finalize_and_download(
    job_id: str,
    job_type: str = "stack",
    output_name: Optional[str] = None,
    output_dir: Optional[Path] = None,
    on_progress=None,
    cancel_check=None,
) -> Path:
    """Finalize a remote job that already has images uploaded, poll for
    completion, and download the result.

    This is the second half of process_remote(), used when images were
    streamed during burst capture rather than batch-uploaded.
    """
    if not REMOTE_SERVER:
        raise RuntimeError("Remote server not configured")

    def _log_retry(attempt, wait, exc):
        logger.info("Retry %d: waiting %.0fs after %s", attempt, wait, exc)

    # Finalize
    if cancel_check and cancel_check():
        _cancel_remote(job_id)
        raise RuntimeError("Cancelled")

    _request_with_retry(
        f"{REMOTE_SERVER}/api/jobs/{job_id}/finalize",
        method="POST",
        timeout=30,
        on_retry=_log_retry,
    )

    # Poll for completion
    consecutive_failures = 0
    poll_backoff = INITIAL_BACKOFF

    while True:
        if cancel_check and cancel_check():
            _cancel_remote(job_id)
            raise RuntimeError("Cancelled")

        time.sleep(1)

        try:
            resp_body = _request_with_retry(
                f"{REMOTE_SERVER}/api/jobs/{job_id}",
                method="GET",
                timeout=10,
                max_retries=2,
            )
            status = json.loads(resp_body)
            consecutive_failures = 0
            poll_backoff = INITIAL_BACKOFF
        except Exception as exc:
            consecutive_failures += 1
            if consecutive_failures > MAX_RETRIES:
                raise RuntimeError(
                    f"Lost connection to remote server after {consecutive_failures} "
                    f"poll failures: {exc}"
                ) from exc

            wait = min(poll_backoff, MAX_BACKOFF)
            logger.warning(
                "Remote job %s: poll failed (%d/%d): %s — retrying in %.0fs",
                job_id, consecutive_failures, MAX_RETRIES, exc, wait,
            )
            if on_progress:
                on_progress("reconnecting", consecutive_failures, MAX_RETRIES)
            time.sleep(wait)
            poll_backoff *= 2
            continue

        if on_progress and status.get("progress"):
            on_progress("processing", status["progress"], status.get("total", 1))

        if status["status"] == "completed":
            break
        elif status["status"] in ("failed", "cancelled"):
            raise RuntimeError(
                f"Remote job failed: {status.get('error', 'unknown')}"
            )

    # Download result
    if on_progress:
        on_progress("download", 0, 1)

    result_url = f"{REMOTE_SERVER}/api/jobs/{job_id}/result"
    if not output_name:
        ext = ".mp4" if job_type == "timelapse" else ".jpg"
        output_name = f"remote-{job_type}-{int(time.time())}{ext}"
    output_path = (output_dir or Path(".")) / output_name

    resp_body = _request_with_retry(
        result_url,
        method="GET",
        timeout=600,
        max_retries=MAX_RETRIES,
        on_retry=_log_retry,
    )
    output_path.write_bytes(resp_body)

    if on_progress:
        on_progress("download", 1, 1)

    logger.info("Remote job %s result saved to %s", job_id, output_path)

    try:
        _delete_remote(job_id)
    except Exception:
        pass

    return output_path


def _upload_batch_with_retry(job_id: str, paths: list[Path], on_retry=None):
    """Upload a batch of images with retry on transient network errors.

    On failure, the entire batch is re-sent. The server uses sequential
    filenames (000001_name.jpg) so partial re-uploads are safe — the server
    will either reject duplicates or overwrite harmlessly.
    """
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

    return _request_with_retry(
        f"{REMOTE_SERVER}/api/jobs/{job_id}/images",
        method="POST",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        timeout=300,
        max_retries=MAX_RETRIES,
        on_retry=on_retry,
    )


def _cancel_remote(job_id: str):
    """Cancel a remote job."""
    try:
        _request_with_retry(
            f"{REMOTE_SERVER}/api/jobs/{job_id}/cancel",
            method="POST",
            timeout=10,
            max_retries=3,
        )
    except Exception as exc:
        logger.warning("Failed to cancel remote job %s: %s", job_id, exc)


def _delete_remote(job_id: str):
    """Delete a remote job and its files."""
    _request_with_retry(
        f"{REMOTE_SERVER}/api/jobs/{job_id}",
        method="DELETE",
        timeout=10,
        max_retries=3,
    )
