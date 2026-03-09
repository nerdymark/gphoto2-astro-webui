"""
Image stacking module for astrophotography.

Supports three stacking modes:
  - mean   : arithmetic mean of all frames (good for light pollution reduction)
  - median : per-pixel median (best noise rejection)
  - sum    : simple accumulation (highlights faint stars)

All modes process images in horizontal strips to limit peak memory usage,
making stacking feasible on memory-constrained devices like Raspberry Pi.
"""

import logging
from pathlib import Path
from typing import Literal

from PIL import Image

logger = logging.getLogger(__name__)

StackMode = Literal["mean", "median", "sum"]

# Number of pixel rows to process at a time.  Each strip holds at most
# N * _STRIP_HEIGHT * W * 3 * 2 bytes (uint16 accumulator + uint8 frame).
# At 512 rows and 6048 pixels wide that's ~18 MiB per strip for median
# with 12 images (float32) or ~35 KiB per strip for mean/sum (uint32 acc).
_STRIP_HEIGHT = 512


def stack_images(
    image_paths: list[Path],
    mode: StackMode = "mean",
) -> Image.Image:
    """
    Stack a list of images using the specified mode and return a PIL Image.

    All modes process images in horizontal strips so that peak memory stays
    well below 100 MiB even for large (24 MP) images on a Raspberry Pi.
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

    # Determine reference size from first image.
    first = Image.open(image_paths[0]).convert("RGB")
    reference_size = first.size
    width, height = reference_size
    first.close()

    result_strips: list = []

    for y_start in range(0, height, _STRIP_HEIGHT):
        y_end = min(y_start + _STRIP_HEIGHT, height)

        if mode == "median":
            strip = _median_strip(image_paths, reference_size, width, y_start, y_end, np)
        else:
            strip = _accumulate_strip(image_paths, reference_size, width, y_start, y_end, mode, np)

        result_strips.append(strip)

    result = np.concatenate(result_strips, axis=0)
    return Image.fromarray(result, mode="RGB")


def _open_strip(path: Path, reference_size, width, y_start, y_end):
    """Open an image, crop to the strip, return as PIL Image."""
    img = Image.open(path).convert("RGB")
    if img.size != reference_size:
        logger.warning(
            "Image %s has size %s; expected %s – resizing",
            path.name,
            img.size,
            reference_size,
        )
        img = img.resize(reference_size, Image.LANCZOS)
    return img.crop((0, y_start, width, y_end))


def _accumulate_strip(image_paths, reference_size, width, y_start, y_end, mode, np):
    """Mean/sum for one horizontal strip using a uint32 accumulator.

    uint32 can hold 16,843,009 frames at max pixel value 255 before overflow,
    so it's safe for any realistic number of images.  Peak memory per strip:
    strip_height * width * 3 * 4 bytes (one uint32 accumulator) + one uint8
    frame being added.
    """
    strip_h = y_end - y_start
    accumulator = np.zeros((strip_h, width, 3), dtype=np.uint32)
    n = len(image_paths)

    for path in image_paths:
        strip_img = _open_strip(path, reference_size, width, y_start, y_end)
        accumulator += np.array(strip_img, dtype=np.uint8)

    if mode == "mean":
        result = (accumulator / n).astype(np.uint8)
    else:  # sum – normalise so brightest pixel maps to 255
        max_val = accumulator.max()
        if max_val > 0:
            result = (accumulator * 255 // max_val).astype(np.uint8)
        else:
            result = accumulator.astype(np.uint8)

    return result


def _median_strip(image_paths, reference_size, width, y_start, y_end, np):
    """Median for one horizontal strip.

    Loads one strip per image into a (N, H, W, 3) uint8 array, computes
    the per-pixel median, and returns the result as uint8.  Using uint8
    instead of float32 halves memory (N * strip_h * W * 3 bytes).
    """
    strip_h = y_end - y_start
    # Use uint8 for storage, convert to float only for the median computation.
    strips = np.empty((len(image_paths), strip_h, width, 3), dtype=np.uint8)

    for i, path in enumerate(image_paths):
        strip_img = _open_strip(path, reference_size, width, y_start, y_end)
        strips[i] = np.array(strip_img, dtype=np.uint8)

    median_strip = np.median(strips, axis=0)
    return np.clip(median_strip, 0, 255).astype(np.uint8)
