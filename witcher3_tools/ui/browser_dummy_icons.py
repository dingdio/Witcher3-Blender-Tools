import hashlib
import logging
import os
import re
import struct
import tempfile
import zlib

import numpy as np

from ..CR2W.common_blender import win_path_exists, win_safe_path

log = logging.getLogger(__name__)

_BROWSER_DUMMY_ICON_VERSION = "v4"
_browser_dummy_icon_path_cache = {}
_EXTERNAL_CACHE_EFFECTIVE_TYPES = {
    "External Bundle": "Bundle",
    "External Collision": "Collision",
    "External Texture": "Texture",
    "External Sound": "Sound",
}


def clear_browser_dummy_icon_cache():
    _browser_dummy_icon_path_cache.clear()


def _cache_bounded_store(cache: dict, key, value, max_entries: int = 2048):
    if len(cache) >= max_entries and key not in cache:
        cache.clear()
    cache[key] = value


def _normalize_virtual_path(path: str) -> str:
    return str(path or "").replace("/", "\\").strip("\\")


def _get_effective_cache_type(cache_type: str) -> str:
    return _EXTERNAL_CACHE_EFFECTIVE_TYPES.get(cache_type, cache_type)


def get_browser_item_type_label(item_path: str, cache_type: str = "") -> str:
    ext = os.path.splitext(_normalize_virtual_path(item_path))[1].lower()
    type_map = {
        ".w2ent": "W2ENT",
        ".w2mesh": "W2MESH",
        ".w2rig": "W2RIG",
        ".w2anims": "ANIMS",
        ".w2cutscene": "CUT",
        ".w2scene": "SCENE",
        ".w2mg": "MESH",
        ".xbm": "XBM",
        ".w2cube": "CUBE",
        ".dds": "DDS",
        ".png": "PNG",
        ".tga": "TGA",
        ".jpg": "JPG",
        ".jpeg": "JPG",
        ".bmp": "BMP",
        ".wem": "WEM",
        ".ogg": "OGG",
        ".wav": "WAV",
        ".csv": "CSV",
        ".xml": "XML",
        ".json": "JSON",
    }
    if ext in type_map:
        return type_map[ext]
    if ext:
        cleaned = re.sub(r"[^A-Z0-9]+", "", ext[1:].upper())
        if cleaned:
            return cleaned[:6]
    fallback = re.sub(r"[^A-Z0-9]+", "", _get_effective_cache_type(cache_type).upper())
    return fallback[:6] or "FILE"


def _get_browser_dummy_icon_color(item_path: str, cache_type: str = ""):
    ext = os.path.splitext(_normalize_virtual_path(item_path))[1].lower()
    palette = {
        ".w2ent": (30, 132, 116),
        ".w2mesh": (176, 108, 32),
        ".w2rig": (62, 122, 184),
        ".w2anims": (66, 92, 178),
        ".w2cutscene": (166, 74, 54),
        ".w2scene": (108, 140, 60),
        ".xbm": (74, 116, 90),
        ".wem": (120, 78, 150),
        ".ogg": (120, 78, 150),
    }
    if ext in palette:
        return palette[ext]

    accent_palette = [
        (42, 122, 146),
        (156, 98, 36),
        (92, 128, 54),
        (146, 76, 58),
        (66, 96, 166),
        (126, 86, 146),
    ]
    key = ext or cache_type or "file"
    idx = int(hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:2], 16) % len(accent_palette)
    return accent_palette[idx]


_BROWSER_DUMMY_FONT_4X5 = {
    "0": (0b0110, 0b1001, 0b1001, 0b1001, 0b0110),
    "1": (0b0010, 0b0110, 0b0010, 0b0010, 0b0111),
    "2": (0b0110, 0b1001, 0b0010, 0b0100, 0b1111),
    "3": (0b1110, 0b0001, 0b0110, 0b0001, 0b1110),
    "4": (0b0011, 0b0101, 0b1001, 0b1111, 0b0001),
    "5": (0b1111, 0b1000, 0b1110, 0b0001, 0b1110),
    "6": (0b0111, 0b1000, 0b1110, 0b1001, 0b0110),
    "7": (0b1111, 0b0001, 0b0010, 0b0100, 0b0100),
    "8": (0b0110, 0b1001, 0b0110, 0b1001, 0b0110),
    "9": (0b0110, 0b1001, 0b0111, 0b0001, 0b1110),
    "A": (0b0110, 0b1001, 0b1111, 0b1001, 0b1001),
    "B": (0b1110, 0b1001, 0b1110, 0b1001, 0b1110),
    "C": (0b0111, 0b1000, 0b1000, 0b1000, 0b0111),
    "D": (0b1110, 0b1001, 0b1001, 0b1001, 0b1110),
    "E": (0b1111, 0b1000, 0b1110, 0b1000, 0b1111),
    "F": (0b1111, 0b1000, 0b1110, 0b1000, 0b1000),
    "G": (0b0111, 0b1000, 0b1011, 0b1001, 0b0111),
    "H": (0b1001, 0b1001, 0b1111, 0b1001, 0b1001),
    "I": (0b1111, 0b0010, 0b0010, 0b0010, 0b1111),
    "J": (0b0011, 0b0001, 0b0001, 0b1001, 0b0110),
    "K": (0b1001, 0b1010, 0b1100, 0b1010, 0b1001),
    "L": (0b1000, 0b1000, 0b1000, 0b1000, 0b1111),
    "M": (0b1001, 0b1111, 0b1111, 0b1001, 0b1001),
    "N": (0b1001, 0b1101, 0b1011, 0b1001, 0b1001),
    "O": (0b0110, 0b1001, 0b1001, 0b1001, 0b0110),
    "P": (0b1110, 0b1001, 0b1110, 0b1000, 0b1000),
    "Q": (0b0110, 0b1001, 0b1001, 0b1011, 0b0111),
    "R": (0b1110, 0b1001, 0b1110, 0b1010, 0b1001),
    "S": (0b0111, 0b1000, 0b0110, 0b0001, 0b1110),
    "T": (0b1111, 0b0010, 0b0010, 0b0010, 0b0010),
    "U": (0b1001, 0b1001, 0b1001, 0b1001, 0b0110),
    "V": (0b1001, 0b1001, 0b1001, 0b0110, 0b0010),
    "W": (0b1001, 0b1001, 0b1111, 0b1111, 0b0110),
    "X": (0b1001, 0b0110, 0b0110, 0b0110, 0b1001),
    "Y": (0b1001, 0b1001, 0b0110, 0b0010, 0b0010),
    "Z": (0b1111, 0b0001, 0b0010, 0b0100, 0b1111),
    "?": (0b1110, 0b0001, 0b0110, 0b0000, 0b0100),
}


def _fill_browser_dummy_rect(rgba: np.ndarray, left: int, top: int, right: int, bottom: int, color):
    height, width, _ = rgba.shape
    left = max(0, min(width, int(left)))
    right = max(0, min(width, int(right)))
    top = max(0, min(height, int(top)))
    bottom = max(0, min(height, int(bottom)))
    if left >= right or top >= bottom:
        return
    rgba[top:bottom, left:right, 0] = int(color[0])
    rgba[top:bottom, left:right, 1] = int(color[1])
    rgba[top:bottom, left:right, 2] = int(color[2])
    rgba[top:bottom, left:right, 3] = 255


def _split_browser_dummy_type_lines(type_label: str):
    cleaned = re.sub(r"[^A-Z0-9]+", "", str(type_label or "").upper())[:8] or "FILE"
    if len(cleaned) <= 6:
        return [cleaned]
    midpoint = min(4, (len(cleaned) + 1) // 2)
    return [cleaned[:midpoint], cleaned[midpoint:]]


def _draw_browser_dummy_text_line(
    rgba: np.ndarray,
    text: str,
    top: int,
    color,
    shadow_color=(0, 0, 0),
    pixel_scale: int = 3,
    spacing: int = 2,
):
    glyph_width = 4 * pixel_scale
    text = str(text or "").upper()
    if not text:
        return

    total_width = len(text) * glyph_width + max(0, len(text) - 1) * spacing
    left = max(0, (rgba.shape[1] - total_width) // 2)

    def _paint(offset_x: int, offset_y: int, fill_color):
        cursor_x = left + offset_x
        for char in text:
            pattern = _BROWSER_DUMMY_FONT_4X5.get(char, _BROWSER_DUMMY_FONT_4X5["?"])
            for row_index, row_mask in enumerate(pattern):
                for col_index in range(4):
                    if row_mask & (1 << (3 - col_index)):
                        px0 = cursor_x + col_index * pixel_scale
                        py0 = top + offset_y + row_index * pixel_scale
                        _fill_browser_dummy_rect(
                            rgba,
                            px0,
                            py0,
                            px0 + pixel_scale,
                            py0 + pixel_scale,
                            fill_color,
                        )
            cursor_x += glyph_width + spacing

    _paint(1, 1, shadow_color)
    _paint(0, 0, color)


def _make_browser_dummy_icon_path(cache_type: str, item_path: str) -> str:
    type_label = get_browser_item_type_label(item_path, cache_type)
    ext = os.path.splitext(_normalize_virtual_path(item_path))[1].lower() or ".file"
    digest = hashlib.sha1(
        f"{_BROWSER_DUMMY_ICON_VERSION}|{cache_type}|{type_label}|{ext}".encode("utf-8", errors="ignore")
    ).hexdigest()[:12]
    preview_dir = os.path.join(tempfile.gettempdir(), "witcher_preview", "browser", "dummy")
    os.makedirs(preview_dir, exist_ok=True)
    safe_label = re.sub(r"[^a-z0-9]+", "_", type_label.lower()).strip("_") or "file"
    return win_safe_path(os.path.join(preview_dir, f"{safe_label}.{digest}.png"))


def _save_preview_png(path: str, rgba_u8: np.ndarray) -> bool:
    try:
        rgba = np.asarray(rgba_u8, dtype=np.uint8)
        if rgba.ndim != 3 or rgba.shape[2] != 4:
            raise ValueError(f"Expected RGBA uint8 array, got shape {getattr(rgba, 'shape', None)}")

        height, width, _channels = rgba.shape
        parent_dir = os.path.dirname(path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        def _png_chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
            crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
            return (
                struct.pack(">I", len(chunk_data))
                + chunk_type
                + chunk_data
                + struct.pack(">I", crc)
            )

        raw_rows = bytearray()
        for row_index in range(height):
            raw_rows.append(0)
            raw_rows.extend(rgba[row_index].tobytes())

        png_bytes = bytearray(b"\x89PNG\r\n\x1a\n")
        png_bytes.extend(
            _png_chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0),
            )
        )
        png_bytes.extend(_png_chunk(b"IDAT", zlib.compress(bytes(raw_rows), level=6)))
        png_bytes.extend(_png_chunk(b"IEND", b""))

        with open(path, "wb") as handle:
            handle.write(png_bytes)
        return True
    except Exception:
        log.debug("Failed to write preview PNG: %s", path, exc_info=True)
        return False


def ensure_browser_dummy_icon_path(cache_type: str, item_path: str) -> str:
    type_label = get_browser_item_type_label(item_path, cache_type)
    cache_key = f"{_BROWSER_DUMMY_ICON_VERSION}|{cache_type}|{type_label}"
    cached = _browser_dummy_icon_path_cache.get(cache_key, "")
    if cached and win_path_exists(cached):
        return cached

    color_r, color_g, color_b = _get_browser_dummy_icon_color(item_path, cache_type)
    rgba = np.zeros((96, 96, 4), dtype=np.uint8)
    rgba[:, :, 0] = max(10, color_r // 6)
    rgba[:, :, 1] = max(10, color_g // 6)
    rgba[:, :, 2] = max(10, color_b // 6)
    rgba[:, :, 3] = 255

    frame_color = (max(18, color_r // 3), max(18, color_g // 3), max(18, color_b // 3))
    panel_color = (color_r, color_g, color_b)
    header_color = (
        min(255, color_r + 28),
        min(255, color_g + 28),
        min(255, color_b + 28),
    )
    body_color = (
        max(24, color_r // 2),
        max(24, color_g // 2),
        max(24, color_b // 2),
    )
    label_plate_color = (18, 20, 24)
    label_text_color = (245, 245, 245)

    _fill_browser_dummy_rect(rgba, 6, 6, 90, 90, frame_color)
    _fill_browser_dummy_rect(rgba, 10, 10, 86, 86, panel_color)
    _fill_browser_dummy_rect(rgba, 16, 16, 80, 30, header_color)
    _fill_browser_dummy_rect(rgba, 16, 34, 80, 54, body_color)
    _fill_browser_dummy_rect(rgba, 16, 58, 80, 84, label_plate_color)
    _fill_browser_dummy_rect(rgba, 20, 20, 32, 26, (255, 255, 255))
    _fill_browser_dummy_rect(rgba, 36, 20, 64, 26, (255, 255, 255))
    _fill_browser_dummy_rect(rgba, 20, 40, 76, 44, header_color)
    _fill_browser_dummy_rect(rgba, 20, 48, 68, 52, header_color)
    _fill_browser_dummy_rect(rgba, 20, 60, 60, 64, header_color)

    text_lines = _split_browser_dummy_type_lines(type_label)
    max_line_length = max((len(line) for line in text_lines), default=0)
    if len(text_lines) == 1 and max_line_length <= 4:
        pixel_scale = 3
        spacing = 2
    else:
        pixel_scale = 2
        spacing = 1 if max_line_length >= 5 else 2
    line_height = 5 * pixel_scale
    line_gap = 3 if len(text_lines) > 1 else 0
    total_text_height = len(text_lines) * line_height + max(0, len(text_lines) - 1) * line_gap
    top = 71 - (total_text_height // 2)
    for line in text_lines:
        _draw_browser_dummy_text_line(
            rgba,
            line,
            top,
            label_text_color,
            shadow_color=(0, 0, 0),
            pixel_scale=pixel_scale,
            spacing=spacing,
        )
        top += line_height + line_gap

    dummy_path = _make_browser_dummy_icon_path(cache_type, item_path)
    if (not win_path_exists(dummy_path)) and (not _save_preview_png(dummy_path, rgba)):
        return ""
    _cache_bounded_store(_browser_dummy_icon_path_cache, cache_key, dummy_path, max_entries=256)
    return dummy_path
