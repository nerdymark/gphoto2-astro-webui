"""
gphoto2 camera interface.

Wraps the gphoto2 CLI tool for camera communication via MTP/PTP.
Falls back to a simulated camera when gphoto2 is not available (development mode).
"""

import io
import logging
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GPHOTO2_BIN = shutil.which("gphoto2")

# Substring present in gphoto2 stderr when the OS has claimed the USB device.
USB_CLAIM_ERROR = "Could not claim the USB device"

# Maximum number of attempts when a USB claim error is encountered.
_USB_MAX_ATTEMPTS = 3

# Substring present in gphoto2 stderr when the camera denies a PTP request.
# Cameras like the Nikon D780 use PTP/MTP as their only USB mode, so gvfs
# automatically opens a PTP session when the camera is connected.  If gvfs
# still holds that session when gphoto2 tries to capture, the camera firmware
# returns "PTP Access Denied".  Killing the gvfs daemons closes the competing
# session; retrying the capture after that usually succeeds.
PTP_ACCESS_ERROR = "PTP Access Denied"

# Substring present in gphoto2 stderr when the camera's PTP session is already
# open (error code 0x201e).  This happens when gvfs has opened a PTP session
# before gphoto2 starts – gphoto2 cannot open its own session on top of the
# existing one.  Like PTP_ACCESS_ERROR, the fix is to kill the gvfs daemons
# so that the competing session is closed before the next attempt.
PTP_SESSION_ERROR = "PTP Session Already Opened"

# Maximum number of capture attempts when a PTP access error is encountered.
_PTP_MAX_ATTEMPTS = 3

# Serialise all gphoto2 calls so that concurrent HTTP requests cannot race to
# claim the camera's USB interface.  RLock is used because some public
# functions call _run() more than once in the same thread (e.g.
# get_camera_summary calls is_camera_connected, which calls _run, and then
# calls _run again for --summary).
_camera_lock = threading.RLock()


def _kill_gvfs_monitor() -> None:
    """Stop GNOME VFS processes that hold the camera's USB interface.

    Four groups of cooperating GNOME VFS processes can claim a camera's USB
    interface, depending on how the camera presents itself to the OS:

    * gvfs-gphoto2-volume-monitor / gvfsd-gphoto2 – used when the camera is
      detected as a PTP device by libgphoto2's udev rules.
    * gvfs-mtp-volume-monitor / gvfsd-mtp – used when the camera enumerates as
      an MTP device.  Cameras like the Nikon D780 use PTP/MTP as their *only*
      USB mode, so the OS always sees them as MTP.  On Raspberry Pi OS Desktop
      gvfs-mtp-volume-monitor auto-starts at login and immediately claims the
      camera, making it the primary cause of "PTP Access Denied" errors.
    * gvfsd – the master GNOME VFS daemon.  It supervises all worker daemons
      and can restart them after they are killed.  Stopping gvfsd (or the
      gvfs-daemon user service) prevents automatic restarts that would
      re-claim the camera's USB interface before gphoto2 can acquire it.

    Killing only the volume monitor leaves the worker daemon running with the
    interface still claimed.  Both monitor and worker must be stopped for each
    group so gphoto2 can acquire the device and avoid error -53
    ('Could not claim the USB device') or 'PTP Access Denied'.

    sudo is not required: all daemons run as the same user that owns this
    process, so an unprivileged pkill is sufficient to terminate them.
    """
    logger.warning(
        "USB device is claimed by another process (or a PTP session is already open); "
        "attempting to stop gvfsd, gvfs-gphoto2-volume-monitor, gvfsd-gphoto2, "
        "gvfs-mtp-volume-monitor, and gvfsd-mtp…"
    )
    for cmd in (
        ["systemctl", "--user", "stop", "gvfs-gphoto2-volume-monitor"],
        ["pkill", "-f", "gvfs-gphoto2-volume-monitor"],
        ["pkill", "-f", "gvfsd-gphoto2"],
        ["systemctl", "--user", "stop", "gvfs-mtp-volume-monitor"],
        ["pkill", "-f", "gvfs-mtp-volume-monitor"],
        ["pkill", "-f", "gvfsd-mtp"],
        ["systemctl", "--user", "stop", "gvfs-daemon"],
        ["pkill", "-f", "gvfsd"],
    ):
        try:
            subprocess.run(cmd, capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
    # Give the kernel time to release the USB interface.
    time.sleep(3)


def _run(
    args: list[str], check: bool = True, cwd: Optional[str] = None
) -> subprocess.CompletedProcess:
    """Execute a gphoto2 command with automatic USB conflict resolution.

    *check* controls whether a non-zero exit code raises
    :class:`subprocess.CalledProcessError` (default ``True``).

    *cwd* is passed directly to :func:`subprocess.run` so callers can control
    the working directory (e.g. for ``--capture-image-and-download`` so that
    downloaded files land in a known temporary directory).
    """
    cmd = [GPHOTO2_BIN] + args
    logger.debug("_run: %s (cwd=%s)", " ".join(cmd), cwd)
    with _camera_lock:
        for attempt in range(_USB_MAX_ATTEMPTS):
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=30, cwd=cwd
            )
            logger.debug(
                "_run returncode=%d stdout=%r stderr=%r",
                result.returncode,
                (result.stdout or "").strip(),
                (result.stderr or "").strip(),
            )
            stderr_out = result.stderr or ""
            device_conflict = USB_CLAIM_ERROR in stderr_out or PTP_SESSION_ERROR in stderr_out
            if not device_conflict or attempt >= _USB_MAX_ATTEMPTS - 1:
                break
            _kill_gvfs_monitor()
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr
        )
    return result


def is_camera_connected() -> bool:
    """Return True if a camera is detected."""
    if not GPHOTO2_BIN:
        logger.warning("gphoto2 binary not found – running in simulation mode")
        return False
    try:
        result = _run(["--auto-detect"], check=False)
        lines = result.stdout.strip().splitlines()
        # Header is 2 lines; any additional lines mean a camera was found
        return len(lines) > 2
    except Exception as exc:
        logger.error("Camera detection failed: %s", exc)
        return False


def get_camera_summary() -> dict:
    """Return basic camera info."""
    if not GPHOTO2_BIN or not is_camera_connected():
        return {"connected": False, "model": "No camera", "summary": ""}
    try:
        result = _run(["--summary"])
        return {"connected": True, "model": "", "summary": result.stdout}
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        logger.error(
            "get_camera_summary failed (exit %d): stderr=%r stdout=%r",
            exc.returncode,
            stderr,
            stdout,
        )
        detail = stderr or stdout or f"exit code {exc.returncode}"
        return {"connected": False, "model": "", "summary": f"Error: {detail}"}
    except Exception as exc:
        logger.error("get_camera_summary unexpected error: %s", exc)
        return {"connected": False, "model": "", "summary": str(exc)}


def _get_config_value(key: str) -> Optional[str]:
    if not GPHOTO2_BIN:
        return None
    try:
        result = _run(["--get-config", key])
        for line in result.stdout.splitlines():
            if line.strip().startswith("Current:"):
                return line.split(":", 1)[1].strip()
    except subprocess.CalledProcessError as exc:
        logger.error(
            "_get_config_value(%r) failed (exit %d): %s",
            key,
            exc.returncode,
            (exc.stderr or "").strip() or (exc.stdout or "").strip(),
        )
    except Exception as exc:
        logger.error("_get_config_value(%r) unexpected error: %s", key, exc)
    return None


def _get_config_choices(key: str) -> list[str]:
    if not GPHOTO2_BIN:
        return []
    try:
        result = _run(["--get-config", key])
        choices = []
        for line in result.stdout.splitlines():
            if line.strip().startswith("Choice:"):
                # "Choice: 0 1/4000"
                parts = line.strip().split(None, 2)
                if len(parts) == 3:
                    choices.append(parts[2])
        return choices
    except subprocess.CalledProcessError as exc:
        logger.error(
            "_get_config_choices(%r) failed (exit %d): %s",
            key,
            exc.returncode,
            (exc.stderr or "").strip() or (exc.stdout or "").strip(),
        )
    except Exception as exc:
        logger.error("_get_config_choices(%r) unexpected error: %s", key, exc)
    return []


def _get_config(key: str) -> tuple[Optional[str], list[str]]:
    """Return ``(current_value, choices)`` for a camera config key.

    Both the current value and the list of choices are parsed from a **single**
    ``gphoto2 --get-config`` invocation, halving the number of subprocess calls
    compared with calling :func:`_get_config_value` and
    :func:`_get_config_choices` separately.

    Returns ``(None, [])`` when the key is not in the camera's config tree or
    gphoto2 is unavailable.
    """
    if not GPHOTO2_BIN:
        return None, []
    try:
        result = _run(["--get-config", key])
        value: Optional[str] = None
        choices: list[str] = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Current:"):
                value = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Choice:"):
                # "Choice: 0 1/4000"
                parts = stripped.split(None, 2)
                if len(parts) == 3:
                    choices.append(parts[2])
        logger.debug("_get_config(%r): value=%r choices=%r", key, value, choices)
        return value, choices
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or "").strip() or (exc.stdout or "").strip()
        if "not found in configuration tree" in msg:
            logger.warning("_get_config(%r): key not supported by this camera", key)
        else:
            logger.error(
                "_get_config(%r) failed (exit %d): %s",
                key,
                exc.returncode,
                msg,
            )
    except Exception as exc:
        logger.error("_get_config(%r) unexpected error: %s", key, exc)
    return None, []


def list_config_keys() -> list[str]:
    """Return all configuration key paths supported by the connected camera.

    Runs ``gphoto2 --list-config`` and returns the list of key paths (e.g.
    ``/main/imgsettings/iso``).  This is useful for diagnosing which settings
    are available on a specific camera model.

    Returns an empty list when gphoto2 is unavailable, the camera is not
    connected, or any other error occurs (the error is logged).
    """
    if not GPHOTO2_BIN:
        return []
    try:
        result = _run(["--list-config"])
        keys = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        logger.debug("list_config_keys: found %d keys", len(keys))
        return keys
    except subprocess.CalledProcessError as exc:
        logger.error(
            "list_config_keys failed (exit %d): %s",
            exc.returncode,
            (exc.stderr or "").strip() or (exc.stdout or "").strip(),
        )
    except Exception as exc:
        logger.error("list_config_keys unexpected error: %s", exc)
    return []


def get_exposure_settings() -> dict:
    """Return current aperture, shutter speed, and ISO.

    Each setting is fetched with a single ``gphoto2 --get-config`` call that
    returns both the current value and the list of available choices.  When the
    primary key is not found in the camera's config tree the secondary key is
    tried (e.g. ``f-number`` when ``aperture`` is absent).  This avoids making
    separate calls for value and choices when the first key fails, reducing the
    number of subprocess invocations and the time the camera lock is held.
    """
    aperture, aperture_choices = _get_config("aperture")
    if aperture is None and not aperture_choices:
        aperture, aperture_choices = _get_config("f-number")

    shutter, shutter_choices = _get_config("shutterspeed")
    if shutter is None and not shutter_choices:
        shutter, shutter_choices = _get_config("shutterspeed2")

    iso, iso_choices = _get_config("iso")

    return {
        "aperture": aperture,
        "shutter": shutter,
        "iso": iso,
        "aperture_choices": aperture_choices,
        "shutter_choices": shutter_choices,
        "iso_choices": iso_choices,
    }


def set_exposure_settings(
    aperture: Optional[str] = None,
    shutter: Optional[str] = None,
    iso: Optional[str] = None,
) -> dict:
    """Apply one or more exposure settings to the camera."""
    if not GPHOTO2_BIN:
        logger.warning("gphoto2 not available – skipping set_exposure_settings")
        return {"ok": True, "simulated": True}
    args = []
    if aperture:
        args += ["--set-config", f"aperture={aperture}"]
    if shutter:
        args += ["--set-config", f"shutterspeed={shutter}"]
    if iso:
        args += ["--set-config", f"iso={iso}"]
    if not args:
        return {"ok": True}
    try:
        _run(args)
        return {"ok": True}
    except subprocess.CalledProcessError as exc:
        logger.error("set_exposure_settings failed: %s", exc.stderr)
        return {"ok": False, "error": exc.stderr}


def capture_image(gallery_path: Path) -> Path:
    """
    Trigger the camera shutter, download the image, and save it into gallery_path.
    Returns the path of the saved image file.
    """
    gallery_path.mkdir(parents=True, exist_ok=True)

    if not GPHOTO2_BIN or not is_camera_connected():
        # Simulation: create a small blank JPEG
        return _simulate_capture(gallery_path)

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            # Ensure the image is downloaded to the host rather than saved only
            # to the camera's memory card.  capturetarget=0 means "Internal RAM"
            # on most cameras; if the key is not supported the warning is logged
            # and capture proceeds unchanged.
            ct = _run(["--set-config", "capturetarget=0"], check=False)
            if ct.returncode != 0:
                logger.warning(
                    "capture_image: could not set capturetarget=0: %s",
                    (ct.stderr or "").strip(),
                )
            logger.debug("capture_image: starting capture into tmpdir=%s", tmpdir)
            # Retry on PTP access errors.  On cameras like the Nikon D780 that
            # use PTP/MTP as their only USB mode, gvfs-mtp-volume-monitor
            # automatically opens an MTP session when the camera is connected.
            # If that session is still active when gphoto2 tries to capture,
            # the camera firmware returns "PTP Access Denied" (exit 0) instead
            # of downloading the image.  Killing the gvfs daemons between
            # attempts closes the competing session so the next attempt can
            # succeed.
            for attempt in range(_PTP_MAX_ATTEMPTS):
                result = _run(
                    [
                        "--capture-image-and-download",
                        "--filename",
                        # Basename only – gphoto2 expands format codes and writes
                        # the file relative to cwd (tmpdir), which is more
                        # reliable than embedding an absolute path in the
                        # --filename template.
                        "%Y%m%d-%H%M%S-%05n.%C",
                        "--force-overwrite",
                    ],
                    cwd=tmpdir,
                )
                stderr_stripped = (result.stderr or "").strip()
                logger.debug(
                    "capture_image: gphoto2 stdout=%r stderr=%r",
                    (result.stdout or "").strip(),
                    stderr_stripped,
                )
                # gphoto2 sometimes exits with code 0 while printing a capture
                # error to stderr.  Detect these patterns early so callers
                # receive the real error rather than the generic "no file was
                # downloaded" fallback.
                ptp_error = stderr_stripped and (
                    PTP_ACCESS_ERROR in stderr_stripped
                    or PTP_SESSION_ERROR in stderr_stripped
                )
                if ptp_error:
                    if attempt < _PTP_MAX_ATTEMPTS - 1:
                        logger.warning(
                            "capture_image: PTP access or session conflict (attempt %d/%d)"
                            " – killing gvfs camera daemons and retrying…",
                            attempt + 1,
                            _PTP_MAX_ATTEMPTS,
                        )
                        _kill_gvfs_monitor()
                        continue
                    logger.error(
                        "capture_image: gphoto2 exited 0 but reported a capture"
                        " error – stderr=%r",
                        stderr_stripped,
                    )
                    raise RuntimeError(f"Capture failed: {stderr_stripped}")
                if stderr_stripped and "ERROR: Could not capture" in stderr_stripped:
                    logger.error(
                        "capture_image: gphoto2 exited 0 but reported a capture"
                        " error – stderr=%r",
                        stderr_stripped,
                    )
                    raise RuntimeError(f"Capture failed: {stderr_stripped}")
                break
            captured = list(Path(tmpdir).iterdir())
            logger.debug("capture_image: files in tmpdir after capture: %s", captured)
            if not captured:
                logger.error(
                    "capture_image: gphoto2 exited successfully but no file was downloaded"
                    " – stdout=%r stderr=%r",
                    (result.stdout or "").strip(),
                    stderr_stripped,
                )
                raise RuntimeError(
                    f"Capture failed: {stderr_stripped}"
                    if stderr_stripped
                    else "gphoto2 captured nothing"
                )
            src = captured[0]
            dst = gallery_path / src.name
            shutil.move(str(src), str(dst))
            logger.debug("capture_image: saved %s -> %s", src, dst)
            return dst
        except subprocess.CalledProcessError as exc:
            logger.error(
                "capture_image failed (exit %d): stderr=%r stdout=%r",
                exc.returncode,
                (exc.stderr or "").strip(),
                (exc.stdout or "").strip(),
            )
            raise RuntimeError(f"Capture failed: {exc.stderr}") from exc


def _simulate_capture(gallery_path: Path) -> Path:
    """Generate a placeholder JPEG for development/testing."""
    from PIL import Image, ImageDraw  # type: ignore
    import datetime

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = gallery_path / f"sim-{ts}.jpg"
    img = Image.new("RGB", (800, 600), color=(20, 20, 40))
    draw = ImageDraw.Draw(img)
    draw.text((20, 20), f"Simulated capture – {ts}", fill=(200, 200, 255))
    # Draw a few stars
    import random

    rng = random.Random(ts)
    for _ in range(200):
        x = rng.randint(0, 799)
        y = rng.randint(0, 599)
        r = rng.choice([1, 1, 1, 2])
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 220))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    dst.write_bytes(buf.getvalue())
    logger.info("Simulated capture saved to %s", dst)
    return dst
