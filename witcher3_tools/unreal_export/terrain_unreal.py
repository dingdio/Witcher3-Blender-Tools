"""Terrain -> Unreal Landscape conversion (Blender-free, unit-testable).

Turns the combined Witcher terrain heightmap (the 16-bit PNG produced by
``terrain_w2ter.combine_w2ter_tiles``) into the data an Unreal ``ALandscape``
needs: a UE-valid square resolution, a raw little-endian R16 heightmap, the
landscape component layout, and the actor transform that places the landscape
in Unreal world space.

Coordinate convention (THE single source of truth for W3 -> UE world space)
---------------------------------------------------------------------------
Witcher / Blender world is metres, Y-forward, Z-up, right-handed. Unreal world
is centimetres, X-forward, Y-right, Z-up, left-handed. The canonical mapping is

    UE = ( w3.x * 100,  w3.y * 100 * UNREAL_Y_SIGN,  w3.z * 100 )

with ``UNREAL_Y_SIGN = -1`` (the standard Blender->Unreal handedness flip that
matches the FBX export preset ``axis_forward=-Z, axis_up=Y``). Every actor we
later place from W3 world coordinates -- terrain AND imported layer geometry
(buildings) -- MUST go through :func:`w3_world_to_unreal` so they share one
frame and line up exactly. See :func:`compute_landscape_transform` for how the
landscape transform is derived from this mapping; the derivation cancels the
Y flip against the heightmap's row order, so the heightmap is fed to Unreal in
PNG row order with no extra flip (see ``HEIGHTMAP_FLIP_V``).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Optional

import numpy as np

# --- coordinate convention ---------------------------------------------------

UE_UNITS_PER_METER = 100.0
# Handedness flip between Blender/W3 (right-handed) and Unreal (left-handed).
UNREAL_Y_SIGN = -1.0
# Unreal landscape height encoding: world_z = (value - 32768) / 128 * Scale.Z.
LANDSCAPE_HEIGHT_MIDPOINT = 32768.0
LANDSCAPE_HEIGHT_DIVISOR = 128.0
# Span, in landscape height-units, covered by the full uint16 range.
LANDSCAPE_HEIGHT_SPAN_UNITS = 65535.0 / LANDSCAPE_HEIGHT_DIVISOR  # 511.9921875
# Verified-against-a-building knob. The transform derivation (see module
# docstring) already accounts for the Y handedness flip, so the heightmap is
# emitted in PNG row order. If a test building lands mirrored north/south,
# flip this and re-export -- it is the ONE place orientation is decided.
HEIGHTMAP_FLIP_V = False


def w3_world_to_unreal(x: float, y: float, z: float) -> tuple[float, float, float]:
    """W3/Blender world (metres) -> Unreal world (centimetres).

    The single canonical mapping; reuse for terrain and for any layer actor so
    everything shares one frame.
    """
    return (
        float(x) * UE_UNITS_PER_METER,
        float(y) * UE_UNITS_PER_METER * UNREAL_Y_SIGN,
        float(z) * UE_UNITS_PER_METER,
    )


# --- landscape resolution / component layout ---------------------------------

@dataclass(frozen=True)
class LandscapeLayout:
    """A UE-valid square landscape size and its component decomposition.

    ``resolution`` is verts per side (= quads + 1). Unreal's import wants
    ``MinX=MinY=0``, ``MaxX=MaxY=resolution-1``, the subsection size in quads,
    and the number of subsections per component (1 -> 1x1, 2 -> 2x2).
    """

    resolution: int
    subsection_size_quads: int
    num_subsections: int

    @property
    def quads(self) -> int:
        return self.resolution - 1

    @property
    def component_count_per_axis(self) -> int:
        return self.quads // (self.subsection_size_quads * self.num_subsections)

    @property
    def quads_per_component(self) -> int:
        return self.subsection_size_quads * self.num_subsections


# Curated set of recommended Unreal landscape sizes. All use 1x1 sections of
# 63 quads (the common "good" component), except the largest which uses 127 to
# keep the component count sane. Resolution = quads*components + 1.
_LANDSCAPE_LAYOUTS = (
    LandscapeLayout(253, 63, 1),    # 4x4 components
    LandscapeLayout(505, 63, 1),    # 8x8
    LandscapeLayout(1009, 63, 1),   # 16x16
    LandscapeLayout(2017, 63, 1),   # 32x32
    LandscapeLayout(4033, 63, 1),   # 64x64
    LandscapeLayout(8129, 127, 1),  # 64x64 (127-quad components)
)


def choose_landscape_layout(source_res: int) -> LandscapeLayout:
    """Pick the recommended landscape layout closest to ``source_res``.

    Ties and anything below the smallest size round up to the nearest valid
    resolution so detail is preserved rather than discarded.
    """
    src = max(1, int(source_res))
    best = _LANDSCAPE_LAYOUTS[0]
    best_dist = abs(best.resolution - src)
    for layout in _LANDSCAPE_LAYOUTS[1:]:
        dist = abs(layout.resolution - src)
        # On a tie prefer the larger resolution (preserve detail).
        if dist < best_dist or (dist == best_dist and layout.resolution > best.resolution):
            best = layout
            best_dist = dist
    return best


# --- heightmap IO + resampling -----------------------------------------------

def read_height_png_u16(path: str) -> np.ndarray:
    """Read a 16-bit grayscale PNG written by ``terrain_w2ter.write_png``.

    That writer always uses filter type 0 (None) on every scanline and stores
    big-endian samples, so decoding is just zlib-inflate + strip the per-row
    filter byte. Returns an ``(H, W)`` uint16 array (row 0 = top scanline).
    """
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a PNG file: {path}")

    width = height = bit_depth = color_type = 0
    idat = bytearray()
    offset = 8
    while offset + 8 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        tag = data[offset + 4:offset + 8]
        chunk = data[offset + 8:offset + 8 + length]
        offset += 12 + length  # length + tag + data + crc
        if tag == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack_from(">IIBB", chunk, 0)
        elif tag == b"IDAT":
            idat.extend(chunk)
        elif tag == b"IEND":
            break

    if color_type != 0 or bit_depth != 16:
        raise ValueError(f"Expected 16-bit grayscale PNG, got color_type={color_type} bit_depth={bit_depth}")

    raw = zlib.decompress(bytes(idat))
    row_bytes = width * 2
    stride = row_bytes + 1  # +1 filter byte per scanline
    if len(raw) < stride * height:
        raise ValueError("PNG data truncated")

    out = np.empty((height, width), dtype=">u2")
    for row in range(height):
        start = row * stride
        if raw[start] != 0:
            raise ValueError("Unsupported PNG row filter (expected None/0)")
        out[row] = np.frombuffer(raw, dtype=">u2", count=width, offset=start + 1)
    return out.astype("<u2")


def resample_height_u16(height: np.ndarray, target_res: int) -> np.ndarray:
    """Bilinearly resample an ``(H, W)`` uint16 heightmap to ``target_res`` square.

    Uses corner-aligned sampling (endpoints preserved) so the terrain edges map
    exactly to the world AABB corners -- essential for the landscape transform
    to be exact.
    """
    target = int(target_res)
    src_h, src_w = height.shape
    if src_h == target and src_w == target:
        return height.astype(np.uint16, copy=False)

    src = height.astype(np.float64)

    def axis_coords(src_n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if target == 1 or src_n == 1:
            pos = np.zeros(target, dtype=np.float64)
        else:
            pos = np.linspace(0.0, src_n - 1, target)
        lo = np.floor(pos).astype(np.int64)
        hi = np.minimum(lo + 1, src_n - 1)
        frac = pos - lo
        return lo, hi, frac

    ry_lo, ry_hi, ry_f = axis_coords(src_h)
    rx_lo, rx_hi, rx_f = axis_coords(src_w)

    # Interpolate along X first, then Y.
    top = src[ry_lo][:, rx_lo] * (1 - rx_f) + src[ry_lo][:, rx_hi] * rx_f
    bot = src[ry_hi][:, rx_lo] * (1 - rx_f) + src[ry_hi][:, rx_hi] * rx_f
    out = top * (1 - ry_f[:, None]) + bot * ry_f[:, None]

    return np.clip(np.rint(out), 0, 65535).astype(np.uint16)


def heightmap_to_r16_bytes(height: np.ndarray) -> bytes:
    """Row-major little-endian uint16 bytes for Unreal landscape import.

    Honours :data:`HEIGHTMAP_FLIP_V` (default False; see module docstring).
    """
    arr = np.flipud(height) if HEIGHTMAP_FLIP_V else height
    return np.ascontiguousarray(arr, dtype="<u2").tobytes()


def write_r16(path: str, height: np.ndarray) -> None:
    with open(path, "wb") as handle:
        handle.write(heightmap_to_r16_bytes(height))


# --- landscape transform -----------------------------------------------------

@dataclass(frozen=True)
class LandscapeTransform:
    """Unreal ``ALandscape`` actor transform, in centimetres."""

    location: tuple[float, float, float]
    scale: tuple[float, float, float]

    def as_dict(self) -> dict:
        return {"location": list(self.location), "scale": list(self.scale)}


def compute_landscape_transform(
    terrain_size_m: float,
    lowest_elevation_m: float,
    highest_elevation_m: float,
    resolution: int,
) -> LandscapeTransform:
    """Transform placing the landscape over the W3 world AABB in Unreal space.

    The W3 terrain clipmap is centred on the world origin and spans
    ``[-S/2, +S/2]`` in X and Y (S = ``terrain_size_m``). Heightmap value 0 maps
    to ``lowest_elevation_m`` and 65535 to ``highest_elevation_m``.

    Derivation (see module docstring): with the canonical W3->UE mapping, X is
    not flipped, and the Y handedness flip cancels the PNG's top-row=+Y order,
    so quad (col,row) of the un-flipped heightmap lands at
    ``UE = (-S/2 + col/M*S, -S/2 + row/M*S, ...) * 100`` (M = resolution-1).
    That yields identity rotation, positive XY scale, and origin-corner
    location below.
    """
    size = float(terrain_size_m)
    res = max(2, int(resolution))
    quads = res - 1

    scale_xy = size * UE_UNITS_PER_METER / quads

    elev_span = float(highest_elevation_m) - float(lowest_elevation_m)
    scale_z = elev_span * UE_UNITS_PER_METER / LANDSCAPE_HEIGHT_SPAN_UNITS
    # world_z(value=0) must equal lowest elevation:
    #   z0 = ActorZ + (0 - 32768)/128 * scale_z = lowest*100
    actor_z = float(lowest_elevation_m) * UE_UNITS_PER_METER + (
        LANDSCAPE_HEIGHT_MIDPOINT / LANDSCAPE_HEIGHT_DIVISOR
    ) * scale_z

    half = size * UE_UNITS_PER_METER / 2.0
    return LandscapeTransform(
        location=(-half, -half, actor_z),
        scale=(scale_xy, scale_xy, scale_z),
    )


def water_plane_z_cm(water_level_m: float = 0.0) -> float:
    """Unreal Z (cm) for the world water plane. W3 water sits at world Z=0."""
    return float(water_level_m) * UE_UNITS_PER_METER


# --- top-level convenience ---------------------------------------------------

@dataclass(frozen=True)
class TerrainExportResult:
    layout: LandscapeLayout
    transform: LandscapeTransform
    source_resolution: int
    r16_path: str


def build_terrain_r16(
    heightmap_png_path: str,
    out_r16_path: str,
    terrain_size_m: float,
    lowest_elevation_m: float,
    highest_elevation_m: float,
    source_resolution: Optional[int] = None,
) -> TerrainExportResult:
    """Read the combined heightmap PNG, resample to a UE-valid square, write the
    R16, and compute the landscape layout + transform."""
    height = read_height_png_u16(heightmap_png_path)
    src_res = int(source_resolution or max(height.shape))
    layout = choose_landscape_layout(src_res)
    resampled = resample_height_u16(height, layout.resolution)
    write_r16(out_r16_path, resampled)
    transform = compute_landscape_transform(
        terrain_size_m, lowest_elevation_m, highest_elevation_m, layout.resolution
    )
    return TerrainExportResult(
        layout=layout,
        transform=transform,
        source_resolution=src_res,
        r16_path=out_r16_path,
    )
