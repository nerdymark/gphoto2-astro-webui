"""
Image stacking module for astrophotography.

Supports three stacking modes:
  - mean   : arithmetic mean of all frames (good for light pollution reduction)
  - median : per-pixel median (best noise rejection)
  - sum    : simple accumulation (highlights faint stars)
"""

import logging
from pathlib import Path
from typing import Literal

from PIL import Image

logger = logging.getLogger(__name__)

StackMode = Literal["mean", "median", "sum"]


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

    frames: list[np.ndarray] = []
    reference_size: tuple[int, int] | None = None

    for path in image_paths:
        img = Image.open(path).convert("RGB")
        if reference_size is None:
            reference_size = img.size
        elif img.size != reference_size:
            # Resize to match the first frame rather than failing hard
            logger.warning(
                "Image %s has size %s; expected %s – resizing",
                path.name,
                img.size,
                reference_size,
            )
            img = img.resize(reference_size, Image.LANCZOS)
        frames.append(np.array(img, dtype=np.float32))

    stack = np.stack(frames, axis=0)  # shape: (N, H, W, 3)

    if mode == "mean":
        result = np.mean(stack, axis=0)
    elif mode == "median":
        result = np.median(stack, axis=0)
    elif mode == "sum":
        result = np.sum(stack, axis=0)
        # Normalise so the brightest pixel maps to 255
        max_val = result.max()
        if max_val > 0:
            result = result / max_val * 255.0

    result = np.clip(result, 0, 255).astype(np.uint8)
    return Image.fromarray(result, mode="RGB")
