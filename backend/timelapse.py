"""
Timelapse video generation from gallery images.

Uses ffmpeg to encode a sequence of images into a 4K 60fps MP4 video.
Images are sorted alphabetically and scaled/padded to fit 3840x2160.

Memory-efficient: ffmpeg handles all image decoding and encoding in a
streaming fashion, so RAM usage stays low regardless of image count.
"""

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def check_ffmpeg() -> bool:
    """Return True if ffmpeg is available on the system."""
    return shutil.which("ffmpeg") is not None


def generate_timelapse(
    image_paths: list[Path],
    output_path: Path,
    fps: int = 60,
    resolution: str = "3840x2160",
    on_progress=None,
    cancel_check=None,
) -> Path:
    """
    Generate a timelapse video from a list of image files.

    Args:
        image_paths: Ordered list of image file paths.
        output_path: Destination path for the output MP4 file.
        fps: Frames per second (default 60).
        resolution: Output resolution as "WxH" (default "3840x2160").
        on_progress: Optional callback(frames_processed, total_frames).
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

    # Build a concat demuxer file listing all images with their duration.
    # This avoids requiring sequential filenames and handles arbitrary names.
    frame_duration = 1.0 / fps
    concat_file = None
    try:
        concat_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="timelapse_"
        )
        for p in image_paths:
            # ffmpeg concat demuxer requires paths to use forward slashes
            # and single quotes around paths with special chars.
            safe_path = str(p.resolve()).replace("'", "'\\''")
            concat_file.write(f"file '{safe_path}'\n")
            concat_file.write(f"duration {frame_duration}\n")
        # Repeat last image to avoid it being skipped
        safe_last = str(image_paths[-1].resolve()).replace("'", "'\\''")
        concat_file.write(f"file '{safe_last}'\n")
        concat_file.flush()
        concat_path = concat_file.name
        concat_file.close()

        # Build ffmpeg command
        cmd = [
            "ffmpeg",
            "-y",  # overwrite output
            "-f", "concat",
            "-safe", "0",
            "-i", concat_path,
            "-vf", (
                f"scale={width}:{height}"
                ":force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
            ),
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            str(output_path),
        ]

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
                # Clean up partial output
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
                        on_progress(min(frames_done, total), total)
                except ValueError:
                    pass

        proc.wait()

        if proc.returncode != 0:
            stderr = proc.stderr.read()
            logger.error("ffmpeg failed (rc=%d): %s", proc.returncode, stderr)
            # Clean up partial output
            if output_path.exists():
                output_path.unlink()
            raise RuntimeError(f"ffmpeg failed: {stderr[-500:]}")

        # Final progress
        if on_progress:
            on_progress(total, total)

        file_size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info("Timelapse saved: %s (%.1f MiB)", output_path.name, file_size_mb)
        return output_path

    finally:
        # Clean up concat file
        if concat_file:
            Path(concat_file.name).unlink(missing_ok=True)
