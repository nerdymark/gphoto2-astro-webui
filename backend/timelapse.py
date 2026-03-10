"""
Timelapse video generation from gallery images.

Uses ffmpeg to encode a sequence of images into an MP4 video.
Images are pre-resized to the target resolution using Pillow before
encoding, keeping RAM usage low on constrained devices like Raspberry Pi.
"""

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)


def check_ffmpeg() -> bool:
    """Return True if ffmpeg is available on the system."""
    return shutil.which("ffmpeg") is not None


def _resize_image(src: Path, dst: Path, width: int, height: int) -> None:
    """Resize a single image to fit within width x height, with black padding.

    Processes one image at a time to keep memory usage minimal on low-RAM
    devices like Raspberry Pi 2.
    """
    with Image.open(src) as img:
        img.thumbnail((width, height), Image.LANCZOS)
        # Create black canvas at target size and paste centered
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        x = (width - img.width) // 2
        y = (height - img.height) // 2
        # Convert to RGB if needed (handles RGBA, palette, etc.)
        if img.mode != "RGB":
            img = img.convert("RGB")
        canvas.paste(img, (x, y))
        canvas.save(dst, "JPEG", quality=92)


def generate_timelapse(
    image_paths: list[Path],
    output_path: Path,
    fps: int = 60,
    resolution: str = "1920x1080",
    threads: int = 0,
    on_progress=None,
    cancel_check=None,
) -> Path:
    """
    Generate a timelapse video from a list of image files.

    Images are pre-resized to the target resolution using Pillow before
    being passed to ffmpeg.  This avoids ffmpeg needing to decode large
    camera RAW/TIFF files in memory, which can OOM on devices like the
    Raspberry Pi 2.

    Args:
        image_paths: Ordered list of image file paths.
        output_path: Destination path for the output MP4 file.
        fps: Frames per second (default 60).
        resolution: Output resolution as "WxH" (default "1920x1080").
        on_progress: Optional callback(phase, frames_processed, total_frames).
                     phase is "resize" or "encode".
        cancel_check: Optional callable returning True if job should abort.

    Returns:
        The output_path on success.

    Raises:
        RuntimeError: If ffmpeg is not installed or encoding fails.
        ValueError: If fewer than 2 images are provided.
    """
    if not check_ffmpeg():
        raise RuntimeError(
            "ffmpeg is not installed. Install it with: sudo apt-get install ffmpeg"
        )

    if len(image_paths) < 2:
        raise ValueError("At least 2 images are required to create a timelapse")

    # Parse resolution
    match = re.match(r"(\d+)x(\d+)", resolution)
    if not match:
        raise ValueError(f"Invalid resolution format: {resolution!r} (expected WxH)")
    width, height = int(match.group(1)), int(match.group(2))

    total = len(image_paths)
    logger.info(
        "Generating timelapse: %d images -> %s @ %dfps %dx%d",
        total, output_path.name, fps, width, height,
    )

    # Phase 1: Pre-resize images to temp directory as JPEGs.
    # This keeps ffmpeg memory usage minimal — it only decodes small JPEGs
    # instead of full-resolution camera images (24MP+).
    tmp_dir = tempfile.mkdtemp(prefix="timelapse_frames_")
    concat_file = None
    try:
        resized_paths = []
        logger.info("Pre-resizing %d images to %dx%d ...", total, width, height)
        for i, p in enumerate(image_paths):
            if cancel_check and cancel_check():
                raise RuntimeError("Timelapse generation cancelled")

            dst = Path(tmp_dir) / f"frame_{i:06d}.jpg"
            _resize_image(p, dst, width, height)
            resized_paths.append(dst)

            if on_progress:
                on_progress("resize", i + 1, total)

        logger.info("Pre-resize complete, building ffmpeg concat list")

        # Phase 2: Build concat demuxer file from pre-resized frames.
        frame_duration = 1.0 / fps
        concat_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="timelapse_"
        )
        for rp in resized_paths:
            safe_path = str(rp).replace("'", "'\\''")
            concat_file.write(f"file '{safe_path}'\n")
            concat_file.write(f"duration {frame_duration}\n")
        # Repeat last frame to avoid it being skipped
        safe_last = str(resized_paths[-1]).replace("'", "'\\''")
        concat_file.write(f"file '{safe_last}'\n")
        concat_file.flush()
        concat_path = concat_file.name
        concat_file.close()

        # Phase 3: Encode with ffmpeg — no scaling filter needed since
        # frames are already at the target resolution.
        cmd = [
            "ffmpeg",
            "-y",  # overwrite output
            "-f", "concat",
            "-safe", "0",
            "-i", concat_path,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
        ]
        if threads > 0:
            cmd.extend(["-threads", str(threads)])
        cmd.append(str(output_path))

        logger.info("Running: %s", " ".join(cmd))

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Parse ffmpeg progress output
        frames_done = 0
        while True:
            if cancel_check and cancel_check():
                proc.kill()
                proc.wait()
                if output_path.exists():
                    output_path.unlink()
                raise RuntimeError("Timelapse generation cancelled")

            line = proc.stdout.readline()
            if not line:
                break

            # ffmpeg progress lines look like: frame=123
            if line.startswith("frame="):
                try:
                    frames_done = int(line.split("=", 1)[1].strip())
                    if on_progress:
                        on_progress("encode", min(frames_done, total), total)
                except ValueError:
                    pass

        proc.wait()

        if proc.returncode != 0:
            stderr = proc.stderr.read()
            logger.error("ffmpeg failed (rc=%d): %s", proc.returncode, stderr)
            if output_path.exists():
                output_path.unlink()
            raise RuntimeError(f"ffmpeg failed: {stderr[-500:]}")

        # Final progress
        if on_progress:
            on_progress("encode", total, total)

        file_size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info("Timelapse saved: %s (%.1f MiB)", output_path.name, file_size_mb)
        return output_path

    finally:
        # Clean up temp files
        if concat_file:
            Path(concat_file.name).unlink(missing_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)
