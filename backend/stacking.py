"""
Image stacking module for astrophotography.

Supports stacking modes:
  - mean      : arithmetic mean of all frames (noise reduction)
  - max       : per-pixel maximum (star trails, brightest features)
  - align+mean: ORB-based alignment then mean (noise reduction with
                registration to correct for drift / field rotation)

Memory-efficient design for Raspberry Pi:
  Single pass through images with strip-based accumulators.
  Each image is opened once and sliced into all strip accumulators.
"""

import logging
from pathlib import Path
from typing import Literal

from PIL import Image

logger = logging.getLogger(__name__)

StackMode = Literal["mean", "max", "align+mean"]

# Default strip height for mean/sum accumulation.
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
    if mode not in ("mean", "max", "align+mean"):
        raise ValueError(f"Unknown stacking mode: {mode!r}")

    logger.info("Stacking %d images with mode=%r", len(image_paths), mode)

    # Determine reference size from first image.
    first = Image.open(image_paths[0]).convert("RGB")
    reference_size = first.size
    width, height = reference_size
    first.close()

    if mode == "max":
        return _stack_max(image_paths, reference_size, width, height, np, on_progress)
    elif mode == "align+mean":
        return _stack_aligned_mean(image_paths, reference_size, np, on_progress)
    else:
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


# ---- Max (brightest pixel) stacking for star trails ----

def _stack_max(image_paths, reference_size, width, height, np, on_progress=None):
    """Per-pixel maximum across all frames.  Creates star trails."""
    n = len(image_paths)
    logger.info("Max-stacking %d images for star trails", n)

    # Start with a writable copy of the first image as the baseline
    result = np.array(_open_image(image_paths[0], reference_size), dtype=np.uint8)

    if on_progress:
        on_progress(1, n)

    for idx, path in enumerate(image_paths[1:], start=2):
        img = _open_image(path, reference_size)
        arr = np.asarray(img, dtype=np.uint8)
        np.maximum(result, arr, out=result)
        img.close()
        del arr
        if on_progress:
            on_progress(idx, n)

    return Image.fromarray(result, mode="RGB")


# ---- Aligned mean stacking (ORB registration) ----

def _align_image(img_arr, ref_gray, np):
    """Align img_arr to reference using ORB feature matching.

    Returns the warped image array, or the original if alignment fails.
    """
    try:
        import cv2
    except ImportError:
        logger.warning("OpenCV not available – skipping alignment")
        return img_arr

    img_gray = cv2.cvtColor(img_arr, cv2.COLOR_RGB2GRAY)

    orb = cv2.ORB_create(5000)
    kp1, des1 = orb.detectAndCompute(img_gray, None)
    kp2, des2 = orb.detectAndCompute(ref_gray, None)

    if des1 is None or des2 is None or len(kp1) < 4 or len(kp2) < 4:
        logger.warning("Not enough keypoints for alignment – using unaligned frame")
        return img_arr

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    matches = bf.match(des1, des2)
    matches = sorted(matches, key=lambda x: x.distance)

    # Need at least 4 good matches for homography
    if len(matches) < 4:
        logger.warning("Too few matches (%d) – using unaligned frame", len(matches))
        return img_arr

    # Use the best matches (top 70% or at least 10)
    n_good = max(10, int(len(matches) * 0.7))
    matches = matches[:n_good]

    points1 = np.zeros((len(matches), 2), dtype=np.float32)
    points2 = np.zeros((len(matches), 2), dtype=np.float32)
    for i, m in enumerate(matches):
        points1[i, :] = kp1[m.queryIdx].pt
        points2[i, :] = kp2[m.trainIdx].pt

    h, mask = cv2.findHomography(points1, points2, cv2.RANSAC, 5.0)
    if h is None:
        logger.warning("Homography estimation failed – using unaligned frame")
        return img_arr

    height, width = img_arr.shape[:2]
    aligned = cv2.warpPerspective(img_arr, h, (width, height))
    return aligned


def _stack_aligned_mean(image_paths, reference_size, np, on_progress=None):
    """Align each frame to the first using ORB, then mean-stack."""
    try:
        import cv2
    except ImportError:
        raise ImportError(
            "OpenCV is required for aligned stacking but could not be imported. "
            "Install it with: pip install opencv-python-headless"
        )

    n = len(image_paths)
    logger.info("Aligned mean-stacking %d images", n)

    # Load reference
    ref_img = _open_image(image_paths[0], reference_size)
    ref_arr = np.asarray(ref_img, dtype=np.uint8)
    ref_gray = cv2.cvtColor(ref_arr, cv2.COLOR_RGB2GRAY)

    # Use float64 accumulator for precision
    accumulator = ref_arr.astype(np.float64)
    if on_progress:
        on_progress(1, n)

    for idx, path in enumerate(image_paths[1:], start=2):
        img = _open_image(path, reference_size)
        arr = np.asarray(img, dtype=np.uint8)

        aligned = _align_image(arr, ref_gray, np)
        accumulator += aligned.astype(np.float64)

        img.close()
        del arr
        if on_progress:
            on_progress(idx, n)

    result = (accumulator / n).astype(np.uint8)
    return Image.fromarray(result, mode="RGB")


# ---- Mean accumulation stacking (original strip-based approach) ----

def _stack_accumulate(image_paths, mode, reference_size, width, height, np, on_progress=None):
    """Mean stacking – single pass through all images.

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
            image_paths, reference_size, width, strip_ranges, n, np, on_progress
        )
    else:
        return _accumulate_multi_pass(
            image_paths, reference_size, width, height, strip_ranges, n, np, on_progress
        )


def _accumulate_single_pass(image_paths, reference_size, width, strip_ranges, n, np, on_progress=None):
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

    return _finalize_accumulator(accumulators, n, np)


def _accumulate_multi_pass(image_paths, reference_size, width, height, strip_ranges, n, np, on_progress=None):
    """Process strips in batches, trading extra image opens for lower memory."""
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

        for acc in accumulators:
            all_results.append((acc / n).astype(np.uint8))

    result = np.concatenate(all_results, axis=0)
    return Image.fromarray(result, mode="RGB")


def _finalize_accumulator(accumulators, n, np):
    """Finalize and concatenate all accumulator strips into a PIL Image."""
    results = []
    for acc in accumulators:
        results.append((acc / n).astype(np.uint8))

    result = np.concatenate(results, axis=0)
    return Image.fromarray(result, mode="RGB")
