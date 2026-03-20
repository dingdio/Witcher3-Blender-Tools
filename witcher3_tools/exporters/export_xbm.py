"""Export a Blender image as a Witcher 3 .xbm texture file."""

import logging
import os
from datetime import datetime

import numpy as np

log = logging.getLogger(__name__)


def export_xbm(image, filepath):
    """Export a Blender image to an uncooked XBM file with a full RGBA8 mip chain."""
    from ..CR2W.xbm_builder import BuildXBM
    from ..CR2W import cr2w_writer

    width, height = image.size
    if width == 0 or height == 0:
        raise ValueError("Image has zero dimensions")

    mip_payloads = _blender_image_to_rgba8_mips(image)
    import_file, import_timestamp = _get_import_metadata(image, filepath)
    cr2w = BuildXBM(
        mip_payloads,
        width,
        height,
        import_file=import_file,
        import_timestamp=import_timestamp,
    )
    cr2w_writer.write_xbm(cr2w, filepath)
    log.info("Exported XBM: %s (%dx%d, %d mips)", filepath, width, height, len(mip_payloads))


def _get_import_metadata(image, export_filepath):
    """Best-effort import file metadata for the exported texture."""
    source_path = ""

    filepath_from_user = getattr(image, "filepath_from_user", None)
    if callable(filepath_from_user):
        try:
            source_path = filepath_from_user() or ""
        except Exception:
            source_path = ""

    if not source_path:
        source_path = getattr(image, "filepath_raw", "") or getattr(image, "filepath", "") or ""

    source_path = os.path.abspath(source_path) if source_path else ""
    import_file = source_path or os.path.abspath(export_filepath)

    if source_path and os.path.isfile(source_path):
        import_timestamp = datetime.fromtimestamp(os.path.getmtime(source_path))
    else:
        import_timestamp = datetime.now()

    return import_file, import_timestamp


def _blender_image_to_rgba8_mips(image):
    """Convert a Blender image to top-to-bottom RGBA8 mip payloads."""
    width, height = image.size
    num_pixels = width * height

    pixels_flat = np.empty(num_pixels * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels_flat)

    pixels = pixels_flat.reshape((height, width, 4))
    np.clip(pixels, 0.0, 1.0, out=pixels)

    # Blender stores image rows bottom-to-top; REDengine source textures use top-to-bottom rows.
    current = np.flipud(pixels)
    mip_payloads = []

    while True:
        rgba8 = (current * 255.0).astype(np.uint8)
        mip_payloads.append(np.ascontiguousarray(rgba8).tobytes())
        if current.shape[0] == 1 and current.shape[1] == 1:
            break
        current = _downsample_rgba_image(current)

    return mip_payloads


def _downsample_rgba_image(pixels: np.ndarray) -> np.ndarray:
    """Downsample an RGBA float image by averaging 2x2 texel blocks."""
    height, width, _channels = pixels.shape
    if height == 1 and width == 1:
        return pixels

    src = pixels
    if height % 2:
        src = np.concatenate((src, src[-1:, :, :]), axis=0)
    if width % 2:
        src = np.concatenate((src, src[:, -1:, :]), axis=1)

    new_height = max(1, (height + 1) // 2)
    new_width = max(1, (width + 1) // 2)
    reshaped = src.reshape((new_height, 2, new_width, 2, 4))
    return reshaped.mean(axis=(1, 3), dtype=np.float32)
