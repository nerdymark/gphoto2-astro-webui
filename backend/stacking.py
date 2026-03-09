"""
Image stacking module for astrophotography.

Supports three stacking modes:
  - mean   : arithmetic mean of all frames (good for light pollution reduction)
  - median : per-pixel median (best noise rejection)
  - sum    : simple accumulation (highlights faint stars)

Memory-efficient: mean and sum use running accumulators (O(1) frame memory).
Median processes images in horizontal strips to limit peak memory usage.
"""

import logging
from pathlib import Path
from typing import Literal

from PIL import Image

logger = logging.getLogger(__name__)

StackMode = Literal["mean", "median", "sum"]

# Number of pixel rows to process at a time for median stacking.
# Smaller = less memory, larger = fewer I/O passes.
_MEDIAN_STRIP_HEIGHT = 512


def stack_images(
    image_paths: list[Path],
    mode: StackMode = "mean",
) -> Image.Image:
    """
    Stack a list of images using the specified mode and return a PIL Image.

    Args:
        image_paths: Ordered list of image file paths to stack.
        mode: Stacking algorithm – "mean", "median", or "sum".

    Returns:
        A single PIL Image representing the stacked result.

    Raises:
        ImportError: When NumPy is not available (required for stacking).
        ValueError: When fewer than 2 images are provided or the mode is unknown.
        RuntimeError: When image sizes are inconsistent.
    """
    try:
        import numpy as np
    except ImportError as exc:
        raise ImportError(
            "NumPy is required for image stacking but could not be imported. "
            "Ensure it is installed (`pip install numpy`) and that its system "
            "dependencies (e.g. libopenblas0 on Debian/Ubuntu/Raspberry Pi OS) "
            "are present. See the project README for installation instructions."
        ) from exc

    if len(image_paths) < 2:
        raise ValueError("At least 2 images are required for stacking")
    if mode not in ("mean", "median", "sum"):
        raise ValueError(f"Unknown stacking mode: {mode!r}")

    logger.info("Stacking %d images with mode=%r", len(image_paths), mode)

    if mode in ("mean", "sum"):
        return _stack_accumulate(image_paths, mode, np)
    else:
        return _stack_median(image_paths, np)


def _open_and_prepare(
    path: Path, reference_size: tuple[int, int] | None
) -> Image.Image:
    """Open an image, convert to RGB, and resize if needed."""
    img = Image.open(path).convert("RGB")
    if reference_size is not None and img.size != reference_size:
        logger.warning(
            "Image %s has size %s; expected %s – resizing",
            path.name,
            img.size,
            reference_size,
        )
        img = img.resize(reference_size, Image.LANCZOS)
    return img


def _stack_accumulate(image_paths, mode, np):
    """Mean/sum stacking using a running accumulator (O(1) frame memory)."""
    accumulator = None
    reference_size = None
    n = len(image_paths)

    for path in image_paths:
        img = _open_and_prepare(path, reference_size)
        if reference_size is None:
            reference_size = img.size
        arr = np.array(img, dtype=np.float32)
        if accumulator is None:
            accumulator = arr
        else:
            accumulator += arr

    if mode == "mean":
        result = accumulator / n
    else:  # sum
        max_val = accumulator.max()
        if max_val > 0:
            result = accumulator / max_val * 255.0
        else:
            result = accumulator

    result = np.clip(result, 0, 255).astype(np.uint8)
    return Image.fromarray(result, mode="RGB")


def _stack_median(image_paths, np):
    """Median stacking using horizontal strips to limit peak memory.

    Instead of loading all N images into one (N, H, W, 3) array, we process
    _MEDIAN_STRIP_HEIGHT rows at a time, keeping only one strip per image in
    memory.  Peak memory ≈ N * strip_height * W * 3 * 4 bytes.
    """
    # Determine reference size from first image.
    first = Image.open(image_paths[0]).convert("RGB")
    reference_size = first.size
    width, height = reference_size
    first.close()

    result_rows = []

    for y_start in range(0, height, _MEDIAN_STRIP_HEIGHT):
        y_end = min(y_start + _MEDIAN_STRIP_HEIGHT, height)
        strip_h = y_end - y_start

        # Collect this strip from every image.
        strips = np.empty(
            (len(image_paths), strip_h, width, 3), dtype=np.float32
        )
        for i, path in enumerate(image_paths):
            img = _open_and_prepare(path, reference_size)
            # Crop to the strip: (left, upper, right, lower)
            strip_img = img.crop((0, y_start, width, y_end))
            strips[i] = np.array(strip_img, dtype=np.float32)

        median_strip = np.median(strips, axis=0)
        result_rows.append(np.clip(median_strip, 0, 255).astype(np.uint8))

    result = np.concatenate(result_rows, axis=0)
    return Image.fromarray(result, mode="RGB")
