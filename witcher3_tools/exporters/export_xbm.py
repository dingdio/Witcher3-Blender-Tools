"""Export a Blender image as a Witcher 3 .xbm texture file."""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime

import numpy as np

from ..CR2W.texture_dds import (
    DDSMetadata,
    EFormat,
    dds_format_name_from_header,
    eformat_from_format_name,
    mip_payload_size_for_eformat,
    row_pitch_for_format_name,
    swizzle_rgba8_bytes_to_bgra,
    write_dds_payload,
)
from .texture_groups import get_texture_group_info

log = logging.getLogger(__name__)


def export_xbm(image, filepath, *, texture_group="Default"):
    """Export a Blender image to an uncooked XBM file."""
    from ..CR2W import cr2w_writer
    from ..CR2W.xbm_builder import BuildXBM

    width, height = image.size
    if width == 0 or height == 0:
        raise ValueError("Image has zero dimensions")

    group_info = get_texture_group_info(texture_group)
    mip_payloads = _build_xbm_mip_payloads(image, group_info)
    import_file, import_timestamp = _get_import_metadata(image, filepath)
    cr2w = BuildXBM(
        mip_payloads,
        width,
        height,
        import_file=import_file,
        import_timestamp=import_timestamp,
        compression=group_info.compression,
        texture_group=group_info.name,
    )
    cr2w_writer.write_xbm(cr2w, filepath)
    log.info(
        "Exported XBM: %s (%dx%d, group=%s, compression=%s, %d mips)",
        filepath,
        width,
        height,
        group_info.name,
        group_info.compression,
        len(mip_payloads),
    )


def _build_xbm_mip_payloads(image, group_info):
    if not group_info.uses_texconv:
        return _blender_image_to_rgba8_mips(image, generate_mips=group_info.has_mips)
    return _blender_image_to_compressed_mips(image, group_info)


def _blender_image_to_compressed_mips(image, group_info):
    from ..CR2W import texconv_wrapper

    if not texconv_wrapper.is_available():
        raise RuntimeError(
            "texconv.dll not found in CR2W/third_party_libs/. "
            "Compressed XBM export is unavailable."
        )

    with tempfile.TemporaryDirectory(prefix="witcher_xbm_") as temp_dir:
        source_dds = os.path.join(temp_dir, "source_rgba.dds")
        output_dir = os.path.join(temp_dir, "compressed")
        _write_temp_rgba_dds(image, source_dds)

        output_dds = texconv_wrapper.convert_to_dds(
            source_dds,
            group_info.dxgi_format,
            output_dir=output_dir,
            no_mip=not group_info.has_mips,
            verbose=False,
        )
        return _extract_dds_mips(output_dds)


def _write_temp_rgba_dds(image, dds_path):
    width, height = image.size
    rgba_bytes = _blender_image_to_top_rgba8_bytes(image)
    bgra_bytes = swizzle_rgba8_bytes_to_bgra(rgba_bytes)

    metadata = DDSMetadata(width=width, height=height, mipscount=0, format=EFormat.R8G8B8A8_UNORM)
    write_dds_payload(dds_path, metadata, bgra_bytes)


def _extract_dds_mips(dds_path):
    with open(dds_path, "rb") as handle:
        data = handle.read()

    format_name, width, height, mip_count, data_offset = dds_format_name_from_header(data)
    mip_count = max(1, int(mip_count or 0))
    eformat = eformat_from_format_name(format_name)

    offset = data_offset
    mip_entries = []
    mip_width = int(width)
    mip_height = int(height)
    for _level in range(mip_count):
        mip_size = mip_payload_size_for_eformat(eformat, mip_width, mip_height)
        if offset + mip_size > len(data):
            raise ValueError(f"DDS mip payload overruns file: {dds_path}")

        mip_entries.append({
            "width": mip_width,
            "height": mip_height,
            "blocksize": row_pitch_for_format_name(format_name, mip_width),
            "bytes": data[offset:offset + mip_size],
        })
        offset += mip_size

        if mip_width == 1 and mip_height == 1:
            break
        mip_width = max(1, mip_width // 2)
        mip_height = max(1, mip_height // 2)

    return mip_entries


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


def _blender_image_to_rgba8_mips(image, *, generate_mips=True):
    """Convert a Blender image to top-to-bottom RGBA8 mip payloads."""
    current = _blender_image_to_rgba_pixels(image)
    mip_payloads = []

    while True:
        rgba8 = (current * 255.0).astype(np.uint8)
        mip_payloads.append(np.ascontiguousarray(rgba8).tobytes())
        if not generate_mips or (current.shape[0] == 1 and current.shape[1] == 1):
            break
        current = _downsample_rgba_image(current)

    return mip_payloads


def _blender_image_to_top_rgba8_bytes(image):
    current = _blender_image_to_rgba_pixels(image)
    rgba8 = (current * 255.0).astype(np.uint8)
    return np.ascontiguousarray(rgba8).tobytes()


def _blender_image_to_rgba_pixels(image):
    width, height = image.size
    num_pixels = width * height

    pixels_flat = np.empty(num_pixels * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels_flat)

    pixels = pixels_flat.reshape((height, width, 4))
    np.clip(pixels, 0.0, 1.0, out=pixels)

    # Blender stores image rows bottom-to-top; REDengine source textures use top-to-bottom rows.
    return np.flipud(pixels)


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
