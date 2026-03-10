"""
Image stacking module for astrophotography.

Supports two stacking modes:
  - mean : arithmetic mean of all frames (noise reduction, good for large sets)
  - sum  : simple accumulation (highlights faint stars)

Memory-efficient design for Raspberry Pi:
  Single pass through images with strip-based uint32 accumulators.
  Each image is opened once and sliced into all strip accumulators.
"""

import logging
from pathlib import Path
from typing import Literal

from PIL import Image

logger = logging.getLogger(__name__)

StackMode = Literal["mean", "sum"]

# Default strip height for mean/sum accumulation.  Increasing this has
# negligible memory impact (all strip accumulators are live anyway)
# but controls granularity of progress logging.
_ACC_STRIP_HEIGHT = 512


def stack_images(
    image_paths: list[Path],
    mode: StackMode = "mean",
    on_progress=None,
) -> Image.Image:
    """
    Stack a list of images using the specified mode and return a PIL Image.

    *on_progress* is an optional ``(images_processed, total_images)``
    callback invoked after each image is accumulated.
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
    if mode not in ("mean", "sum"):
        raise ValueError(f"Unknown stacking mode: {mode!r}")

    logger.info("Stacking %d images with mode=%r", len(image_paths), mode)

    # Determine reference size from first image.
    first = Image.open(image_paths[0]).convert("RGB")
    reference_size = first.size
    width, height = reference_size
    first.close()

    return _stack_accumulate(image_paths, mode, reference_size, width, height, np, on_progress)


def _open_image(path: Path, reference_size):
    """Open an image and convert to RGB, resizing if needed."""
    img = Image.open(path).convert("RGB")
    if img.size != reference_size:
        logger.warning(
            "Image %s has size %s; expected %s – resizing",
            path.name,
            img.size,
            reference_size,
        )
        img = img.resize(reference_size, Image.LANCZOS)
    return img


def _stack_accumulate(image_paths, mode, reference_size, width, height, np, on_progress=None):
    """Mean/sum stacking – single pass through all images.

    Opens each image exactly once, converts to a numpy uint8 array, and
    slices it into per-strip uint32 accumulators.  Total memory:
      accumulators ~ H * W * 3 * 4 bytes  (one full-frame uint32)
      + one uint8 frame ~ H * W * 3 bytes
    For 6048x4024 that's ~279 MiB + ~70 MiB = 349 MiB peak.

    If that's too large, we fall back to a multi-pass approach that
    processes batches of strips per pass to trade speed for memory.
    """
    n = len(image_paths)
    strip_height = _ACC_STRIP_HEIGHT

    # Build strip boundaries.
    strip_ranges = []
    for y in range(0, height, strip_height):
        strip_ranges.append((y, min(y + strip_height, height)))

    # Estimate peak memory: all accumulators + one uint8 frame.
    acc_bytes = height * width * 3 * 4  # uint32
    frame_bytes = height * width * 3    # uint8
    total_bytes = acc_bytes + frame_bytes

    # If total exceeds 400 MiB, use multi-pass to limit resident memory.
    max_single_pass = 400 * 1024 * 1024
    if total_bytes <= max_single_pass:
        return _accumulate_single_pass(
            image_paths, mode, reference_size, width, strip_ranges, n, np, on_progress
        )
    else:
        return _accumulate_multi_pass(
            image_paths, mode, reference_size, width, height, strip_ranges, n, np, on_progress
        )


def _accumulate_single_pass(image_paths, mode, reference_size, width, strip_ranges, n, np, on_progress=None):
    """Open each image once, accumulate all strips in memory."""
    # Pre-allocate all strip accumulators.
    accumulators = []
    for y_start, y_end in strip_ranges:
        accumulators.append(np.zeros((y_end - y_start, width, 3), dtype=np.uint32))

    for idx, path in enumerate(image_paths):
        if idx % 10 == 0:
            logger.info("  accumulate: image %d/%d", idx + 1, n)
        img = _open_image(path, reference_size)
        arr = np.asarray(img, dtype=np.uint8)
        for i, (y_start, y_end) in enumerate(strip_ranges):
            accumulators[i] += arr[y_start:y_end]
        img.close()
        del arr
        if on_progress:
            on_progress(idx + 1, n)

    return _finalize_accumulator(accumulators, mode, n, np)


def _accumulate_multi_pass(image_paths, mode, reference_size, width, height, strip_ranges, n, np, on_progress=None):
    """Process strips in batches, trading extra image opens for lower memory.

    Each pass processes enough strips to stay under ~200 MiB of accumulator
    memory, plus one full image decode (~70 MiB).
    """
    bytes_per_strip_row = width * 3 * 4  # uint32
    max_acc_bytes = 200 * 1024 * 1024
    rows_budget = max_acc_bytes // bytes_per_strip_row
    strips_per_pass = max(1, rows_budget // _ACC_STRIP_HEIGHT)

    logger.info(
        "  multi-pass: %d strips total, %d per pass",
        len(strip_ranges),
        strips_per_pass,
    )

    all_results = []
    for batch_start in range(0, len(strip_ranges), strips_per_pass):
        batch = strip_ranges[batch_start : batch_start + strips_per_pass]
        accumulators = []
        for y_start, y_end in batch:
            accumulators.append(np.zeros((y_end - y_start, width, 3), dtype=np.uint32))

        for idx, path in enumerate(image_paths):
            if idx % 20 == 0:
                logger.info(
                    "  multi-pass batch %d: image %d/%d",
                    batch_start // strips_per_pass + 1,
                    idx + 1,
                    n,
                )
            img = _open_image(path, reference_size)
            arr = np.asarray(img, dtype=np.uint8)
            for i, (y_start, y_end) in enumerate(batch):
                accumulators[i] += arr[y_start:y_end]
            img.close()
            del arr
            if on_progress:
                on_progress(idx + 1, n)

        all_results.extend(
            _finalize_strips(accumulators, mode, n, np)
        )

    result = np.concatenate(all_results, axis=0)
    return Image.fromarray(result, mode="RGB")


def _finalize_strips(accumulators, mode, n, np):
    """Convert a list of uint32 accumulator strips to uint8 results."""
    results = []
    if mode == "mean":
        for acc in accumulators:
            results.append((acc / n).astype(np.uint8))
    else:  # sum
        global_max = max(acc.max() for acc in accumulators)
        for acc in accumulators:
            if global_max > 0:
                results.append((acc * 255 // global_max).astype(np.uint8))
            else:
                results.append(acc.astype(np.uint8))
    return results


def _finalize_accumulator(accumulators, mode, n, np):
    """Finalize and concatenate all accumulator strips into a PIL Image."""
    if mode == "sum":
        global_max = max(acc.max() for acc in accumulators)

    results = []
    for acc in accumulators:
        if mode == "mean":
            results.append((acc / n).astype(np.uint8))
        else:  # sum
            if global_max > 0:
                results.append((acc * 255 // global_max).astype(np.uint8))
            else:
                results.append(acc.astype(np.uint8))

    result = np.concatenate(results, axis=0)
    return Image.fromarray(result, mode="RGB")
