"""
gphoto2 camera interface.

Wraps the gphoto2 CLI tool for camera communication via MTP/PTP.
Falls back to a simulated camera when gphoto2 is not available (development mode).
"""

import io
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GPHOTO2_BIN = shutil.which("gphoto2")


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    cmd = [GPHOTO2_BIN] + args
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=30)


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
    except Exception:
        pass
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
    except Exception:
        return []


def get_exposure_settings() -> dict:
    """Return current aperture, shutter speed, and ISO."""
    aperture = _get_config_value("aperture") or _get_config_value("f-number")
    shutter = _get_config_value("shutterspeed") or _get_config_value("shutterspeed2")
    iso = _get_config_value("iso")
    return {
        "aperture": aperture,
        "shutter": shutter,
        "iso": iso,
        "aperture_choices": _get_config_choices("aperture") or _get_config_choices("f-number"),
        "shutter_choices": _get_config_choices("shutterspeed") or _get_config_choices("shutterspeed2"),
        "iso_choices": _get_config_choices("iso"),
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
            _run(
                [
                    "--capture-image-and-download",
                    "--filename",
                    os.path.join(tmpdir, "%Y%m%d-%H%M%S-%05n.%C"),
                    "--force-overwrite",
                ],
            )
            captured = list(Path(tmpdir).iterdir())
            if not captured:
                raise RuntimeError("gphoto2 captured nothing")
            src = captured[0]
            dst = gallery_path / src.name
            shutil.move(str(src), str(dst))
            return dst
        except subprocess.CalledProcessError as exc:
            logger.error("capture_image failed: %s", exc.stderr)
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
