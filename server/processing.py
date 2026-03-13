"""
GPU-accelerated image processing for the remote server.

Uses cupy (CUDA) when available, falls back to numpy.
Uses nvenc for ffmpeg encoding when available, falls back to libx264.
"""

import logging
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CUDA / cupy detection
# ---------------------------------------------------------------------------

CUDA_AVAILABLE = False
_xp = None  # numpy or cupy

try:
    import cupy as cp
    # Verify CUDA is actually working
    cp.array([1.0])
    _xp = cp
    CUDA_AVAILABLE = True
    logger.info("CUDA available via cupy (device: %s)", cp.cuda.Device().name)
except Exception:
    import numpy as np
    _xp = np
    logger.info("cupy not available, using numpy (CPU-only)")

import numpy as np  # always need numpy for final conversions


# ---------------------------------------------------------------------------
# nvenc detection for ffmpeg
# ---------------------------------------------------------------------------

def _has_nvenc() -> bool:
    """Check if ffmpeg supports h264_nvenc."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5,
        )
        return "h264_nvenc" in result.stdout
    except Exception:
        return False


NVENC_AVAILABLE = _has_nvenc()
logger.info("nvenc available: %s", NVENC_AVAILABLE)


# ---------------------------------------------------------------------------
# Incremental stacker
# ---------------------------------------------------------------------------

class IncrementalStacker:
    """Accumulates images incrementally as they arrive.

    For mean/max modes, the accumulator is updated with each image so
    finalize() is nearly instant. For align+mean, images are aligned
    against the first frame before accumulation.

    Uses cupy for GPU acceleration when available.
    """

    def __init__(self, mode: str):
        self.mode = mode
        self._count = 0
        self._accumulator = None  # xp array (GPU or CPU)
        self._reference_size = None
        self._ref_gray = None     # for align+mean: grayscale reference
        self._lock = threading.Lock()

    def add_image(self, path: Path):
        """Add one image to the accumulator."""
        with self._lock:
            img = Image.open(path).convert("RGB")

            if self._reference_size is None:
                self._reference_size = img.size
            elif img.size != self._reference_size:
                img = img.resize(self._reference_size, Image.LANCZOS)

            arr = np.array(img, dtype=np.uint8)
            img.close()

            if self.mode == "align+mean" and self._count > 0:
                arr = self._align(arr)

            if self.mode == "align+mean" and self._count == 0:
                self._setup_reference(arr)

            self._accumulate(arr)
            self._count += 1

    def _setup_reference(self, arr: np.ndarray):
        """Set up the alignment reference from the first frame."""
        try:
            import cv2
            self._ref_gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        except ImportError:
            logger.warning("OpenCV not available, align+mean will skip alignment")

    def _align(self, arr: np.ndarray) -> np.ndarray:
        """Align arr to reference using ORB feature matching."""
        if self._ref_gray is None:
            return arr
        try:
            import cv2
        except ImportError:
            return arr

        img_gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        orb = cv2.ORB_create(5000)
        kp1, des1 = orb.detectAndCompute(img_gray, None)
        kp2, des2 = orb.detectAndCompute(self._ref_gray, None)

        if des1 is None or des2 is None or len(kp1) < 4 or len(kp2) < 4:
            return arr

        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = bf.match(des1, des2)
        matches = sorted(matches, key=lambda x: x.distance)

        if len(matches) < 4:
            return arr

        n_good = max(10, int(len(matches) * 0.7))
        matches = matches[:n_good]

        pts1 = np.zeros((len(matches), 2), dtype=np.float32)
        pts2 = np.zeros((len(matches), 2), dtype=np.float32)
        for i, m in enumerate(matches):
            pts1[i] = kp1[m.queryIdx].pt
            pts2[i] = kp2[m.trainIdx].pt

        h, _ = cv2.findHomography(pts1, pts2, cv2.RANSAC, 5.0)
        if h is None:
            return arr

        height, width = arr.shape[:2]
        return cv2.warpPerspective(arr, h, (width, height))

    def _accumulate(self, arr: np.ndarray):
        """Add array to the running accumulator."""
        if CUDA_AVAILABLE:
            xp_arr = _xp.asarray(arr)
        else:
            xp_arr = arr

        if self._accumulator is None:
            if self.mode == "max":
                self._accumulator = xp_arr.astype(_xp.uint8).copy()
            else:
                # mean or align+mean: use float64 for precision
                self._accumulator = xp_arr.astype(_xp.float64)
        else:
            if self.mode == "max":
                _xp.maximum(self._accumulator, xp_arr.astype(_xp.uint8), out=self._accumulator)
            else:
                self._accumulator += xp_arr.astype(_xp.float64)

    def finalize(self) -> Image.Image:
        """Produce the final stacked image."""
        with self._lock:
            if self._accumulator is None or self._count == 0:
                raise RuntimeError("No images accumulated")

            if self.mode == "max":
                result = self._accumulator
            else:
                # mean or align+mean
                result = (self._accumulator / self._count)

            # Move back to CPU if on GPU
            if CUDA_AVAILABLE:
                result = _xp.asnumpy(result.astype(_xp.uint8))
            else:
                result = result.astype(np.uint8)

            return Image.fromarray(result, mode="RGB")


# ---------------------------------------------------------------------------
# Timelapse generation (with nvenc support)
# ---------------------------------------------------------------------------

def run_timelapse(
    image_paths: list[Path],
    output_path: Path,
    fps: int = 30,
    resolution: str = "1920x1080",
    on_progress=None,
    cancel_check=None,
) -> Path:
    """Generate a timelapse video, using nvenc when available."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not installed")
    if len(image_paths) < 2:
        raise ValueError("Need at least 2 images")

    match = re.match(r"(\d+)x(\d+)", resolution)
    if not match:
        raise ValueError(f"Invalid resolution: {resolution}")
    width, height = int(match.group(1)), int(match.group(2))

    total = len(image_paths)
    logger.info("Timelapse: %d images -> %s @ %dfps %dx%d (nvenc=%s)",
                total, output_path.name, fps, width, height, NVENC_AVAILABLE)

    # Phase 1: resize
    tmp_dir = tempfile.mkdtemp(prefix="tl_frames_")
    concat_file = None
    try:
        resized = []
        for i, p in enumerate(image_paths):
            if cancel_check and cancel_check():
                raise RuntimeError("Cancelled")
            dst = Path(tmp_dir) / f"frame_{i:06d}.jpg"
            _resize_image(p, dst, width, height)
            resized.append(dst)
            if on_progress:
                on_progress("resize", i + 1, total)

        # Phase 2: concat file
        frame_dur = 1.0 / fps
        concat_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="tl_"
        )
        for rp in resized:
            safe = str(rp).replace("'", "'\\''")
            concat_file.write(f"file '{safe}'\nduration {frame_dur}\n")
        safe_last = str(resized[-1]).replace("'", "'\\''")
        concat_file.write(f"file '{safe_last}'\n")
        concat_file.flush()
        concat_path = concat_file.name
        concat_file.close()

        # Phase 3: encode
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_path,
        ]

        if NVENC_AVAILABLE:
            cmd.extend([
                "-c:v", "h264_nvenc",
                "-preset", "p4",       # balanced speed/quality
                "-rc", "vbr",
                "-cq", "20",
                "-b:v", "0",
            ])
        else:
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "fast",      # server has CPU headroom
                "-crf", "18",
            ])

        cmd.extend([
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            str(output_path),
        ])

        logger.info("Running: %s", " ".join(cmd))

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        stderr_lines = []
        def _drain():
            for line in proc.stderr:
                stderr_lines.append(line)
        t = threading.Thread(target=_drain, daemon=True)
        t.start()

        while True:
            if cancel_check and cancel_check():
                proc.kill()
                proc.wait()
                t.join(timeout=5)
                if output_path.exists():
                    output_path.unlink()
                raise RuntimeError("Cancelled")

            line = proc.stdout.readline()
            if not line:
                break
            if line.startswith("frame="):
                try:
                    frames = int(line.split("=", 1)[1].strip())
                    if on_progress:
                        on_progress("encode", min(frames, total), total)
                except ValueError:
                    pass

        proc.wait()
        t.join(timeout=10)

        if proc.returncode != 0:
            err = "".join(stderr_lines)
            logger.error("ffmpeg failed (rc=%d): %s", proc.returncode, err)
            if output_path.exists():
                output_path.unlink()
            raise RuntimeError(f"ffmpeg failed: {err[-500:]}")

        if on_progress:
            on_progress("encode", total, total)

        return output_path

    finally:
        if concat_file:
            Path(concat_file.name).unlink(missing_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _resize_image(src: Path, dst: Path, width: int, height: int):
    """Resize image to fit within WxH with black padding."""
    with Image.open(src) as img:
        img.thumbnail((width, height), Image.LANCZOS)
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        x = (width - img.width) // 2
        y = (height - img.height) // 2
        if img.mode != "RGB":
            img = img.convert("RGB")
        canvas.paste(img, (x, y))
        canvas.save(dst, "JPEG", quality=92)
