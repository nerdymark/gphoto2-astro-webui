"""
Image stacking module for astrophotography.

Supports three stacking modes:
  - mean   : arithmetic mean of all frames (good for light pollution reduction)
  - median : per-pixel median (best noise rejection)
  - sum    : simple accumulation (highlights faint stars)

Memory-efficient design for Raspberry Pi:
  - Mean/sum: single pass through images, strip-based uint32 accumulators.
    Each image is opened once and sliced into all strip accumulators.
  - Median: strip height auto-scales based on image count to stay within
    a configurable memory budget (~200 MiB default).
"""

import logging
from pathlib import Path
from typing import Literal

from PIL import Image

logger = logging.getLogger(__name__)

StackMode = Literal["mean", "median", "sum"]

# Default strip height for mean/sum accumulation.  Increasing this has
# negligible memory impact (all strip accumulators are live anyway)
# but controls granularity of progress logging.
_ACC_STRIP_HEIGHT = 512

# Peak memory budget (bytes) for the median strip array.
# strip_array = N * strip_h * W * 3 bytes (uint8).
_MEDIAN_MEMORY_BUDGET = 200 * 1024 * 1024  # 200 MiB


def stack_images(
    image_paths: list[Path],
    mode: StackMode = "mean",
) -> Image.Image:
    """
    Stack a list of images using the specified mode and return a PIL Image.
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

    if mode in ("mean", "sum"):
        return _stack_accumulate(image_paths, mode, reference_size, width, height, np)
    else:
        return _stack_median(image_paths, reference_size, width, height, np)


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


def _stack_accumulate(image_paths, mode, reference_size, width, height, np):
    """Mean/sum stacking – single pass through all images.

    Opens each image exactly once, converts to a numpy uint8 array, and
    slices it into per-strip uint32 accumulators.  Total memory:
      accumulators ≈ H * W * 3 * 4 bytes  (one full-frame uint32)
      + one uint8 frame ≈ H * W * 3 bytes
    For 6048x4024 that's ~279 MiB + ~70 MiB ≈ 349 MiB peak.

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
            image_paths, mode, reference_size, width, strip_ranges, n, np
        )
    else:
        return _accumulate_multi_pass(
            image_paths, mode, reference_size, width, height, strip_ranges, n, np
        )


def _accumulate_single_pass(image_paths, mode, reference_size, width, strip_ranges, n, np):
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
        # Free the PIL image and numpy view promptly.
        img.close()
        del arr

    return _finalize_accumulator(accumulators, mode, n, np)


def _accumulate_multi_pass(image_paths, mode, reference_size, width, height, strip_ranges, n, np):
    """Process strips in batches, trading extra image opens for lower memory.

    Each pass processes enough strips to stay under ~200 MiB of accumulator
    memory, plus one full image decode (~70 MiB).
    """
    # How many strips can we fit in 200 MiB of accumulators?
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
        # Find global max across all strips for normalisation.
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
        # Need global max across all strips for normalisation.
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


def _stack_median(image_paths, reference_size, width, height, np):
    """Median stacking with auto-scaled strip height.

    The strip height is chosen so that the per-strip array
    (N * strip_h * W * 3 bytes) stays within _MEDIAN_MEMORY_BUDGET.
    """
    n = len(image_paths)
    bytes_per_row = n * width * 3  # one row of all images, uint8

    # Compute strip height that fits in memory budget.
    strip_height = max(1, _MEDIAN_MEMORY_BUDGET // bytes_per_row)
    # Clamp to something reasonable.
    strip_height = min(strip_height, 512)

    logger.info(
        "  median: strip_height=%d (%.1f MiB per strip for %d images)",
        strip_height,
        n * strip_height * width * 3 / (1024 * 1024),
        n,
    )

    result_strips = []

    for y_start in range(0, height, strip_height):
        y_end = min(y_start + strip_height, height)
        strip_h = y_end - y_start

        logger.info("  median: rows %d–%d of %d", y_start, y_end, height)

        strips = np.empty((n, strip_h, width, 3), dtype=np.uint8)

        for i, path in enumerate(image_paths):
            img = _open_image(path, reference_size)
            strip_img = img.crop((0, y_start, width, y_end))
            strips[i] = np.asarray(strip_img, dtype=np.uint8)
            img.close()

        median_strip = np.median(strips, axis=0)
        result_strips.append(np.clip(median_strip, 0, 255).astype(np.uint8))
        del strips

    result = np.concatenate(result_strips, axis=0)
    return Image.fromarray(result, mode="RGB")
