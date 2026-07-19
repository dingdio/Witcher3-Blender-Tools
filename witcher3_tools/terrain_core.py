"""Blender-independent terrain grid coordinate helpers."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence


TERRAIN_IMPORT_MODE_ITEMS = (
    (
        'VIEW_LOD',
        'Native View LOD',
        'Import native terrain detail around the view and a lightweight overview elsewhere',
        3,
    ),
    ('SELECTED_TILE', 'Selected Tile', 'Import only the chosen terrain tile', 0),
    ('FULL_MAP', 'Full Map', 'Import one combined map using Geometry Nodes + Multires', 1),
    ('TILES', 'All Tiles', 'Import every terrain tile (advanced and potentially expensive)', 2),
)

TERRAIN_FOLIAGE_MODE_ITEMS = (
    (
        'PROXY',
        'Viewer Ready',
        'Load the dominant real grass and trees quickly; hide unresolved technical fallbacks',
    ),
    ('FULL', 'All Sources', 'Import every foliage source during the tile load'),
)


@dataclass(frozen=True)
class Bounds2D:
    """Half-open world-space bounds: ``[min_x, max_x) x [min_y, max_y)``."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    def __post_init__(self) -> None:
        values = (self.min_x, self.min_y, self.max_x, self.max_y)
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("Terrain bounds must be finite")
        if self.max_x < self.min_x or self.max_y < self.min_y:
            raise ValueError("Terrain bounds maximum must not be below its minimum")


def coerce_bounds(bounds: Bounds2D | Sequence[float]) -> Bounds2D:
    if isinstance(bounds, Bounds2D):
        return bounds
    if len(bounds) != 4:
        raise ValueError("Bounds must contain min_x, min_y, max_x, max_y")
    return Bounds2D(*(float(value) for value in bounds))


def terrain_tile_bounds(
    tile_x: int,
    tile_y: int,
    tiles_x: int,
    tiles_y: int,
    terrain_size: float,
    invert_y: bool = False,
) -> Bounds2D:
    """Return deterministic half-open bounds for one centered terrain tile."""

    tiles_x = int(tiles_x)
    tiles_y = int(tiles_y)
    tile_x = int(tile_x)
    tile_y = int(tile_y)
    terrain_size = float(terrain_size)
    if tiles_x <= 0 or tiles_y <= 0:
        raise ValueError("Terrain tile counts must be positive")
    if not 0 <= tile_x < tiles_x or not 0 <= tile_y < tiles_y:
        raise ValueError(
            f"Terrain tile ({tile_x}, {tile_y}) is outside {tiles_x} x {tiles_y}"
        )
    if not math.isfinite(terrain_size) or terrain_size <= 0.0:
        raise ValueError("Terrain size must be a positive finite number")

    world_y = tiles_y - 1 - tile_y if invert_y else tile_y
    tile_size = terrain_size / max(tiles_x, tiles_y)
    origin = -terrain_size * 0.5
    return Bounds2D(
        origin + tile_x * tile_size,
        origin + world_y * tile_size,
        origin + (tile_x + 1) * tile_size,
        origin + (world_y + 1) * tile_size,
    )


def terrain_tile_from_world_position(
    position,
    tiles_x: int,
    tiles_y: int,
    terrain_size: float,
    *,
    clamp: bool = True,
) -> tuple[int, int]:
    """Return the source tile containing a world-space position."""

    tiles_x = int(tiles_x)
    tiles_y = int(tiles_y)
    terrain_size = float(terrain_size)
    if tiles_x <= 0 or tiles_y <= 0:
        raise ValueError("Terrain tile counts must be positive")
    if not math.isfinite(terrain_size) or terrain_size <= 0.0:
        raise ValueError("Terrain size must be a positive finite number")
    if position is None or len(position) < 2:
        raise ValueError("World position must contain X and Y")

    tile_size = terrain_size / max(tiles_x, tiles_y)
    half = terrain_size * 0.5
    tile_x = math.floor((float(position[0]) + half) / tile_size)
    tile_y = math.floor((float(position[1]) + half) / tile_size)
    if clamp:
        tile_x = max(0, min(tiles_x - 1, tile_x))
        tile_y = max(0, min(tiles_y - 1, tile_y))
    elif not 0 <= tile_x < tiles_x or not 0 <= tile_y < tiles_y:
        raise ValueError(f"World position lies outside the {tiles_x} x {tiles_y} terrain grid")
    return int(tile_x), int(tile_y)


def terrain_native_level(tile_res: int) -> int:
    tile_res = int(tile_res)
    if tile_res <= 0:
        raise ValueError("Terrain tile resolution must be positive")
    return max(0, tile_res.bit_length() - 1)


def terrain_view_lod_tiles(
    tiles_x: int,
    tiles_y: int,
    center_x: int,
    center_y: int,
    radius: int = 3,
) -> frozenset[tuple[int, int]]:
    tiles_x = int(tiles_x)
    tiles_y = int(tiles_y)
    if tiles_x <= 0 or tiles_y <= 0:
        raise ValueError("Terrain tile counts must be positive")
    center_x = max(0, min(tiles_x - 1, int(center_x)))
    center_y = max(0, min(tiles_y - 1, int(center_y)))
    radius = max(0, int(radius))
    return frozenset(
        (x, y)
        for y in range(max(0, center_y - radius), min(tiles_y, center_y + radius + 1))
        for x in range(max(0, center_x - radius), min(tiles_x, center_x + radius + 1))
    )


def point_in_bounds(
    x: float,
    y: float,
    bounds: Bounds2D | Sequence[float],
    *,
    include_max: bool = False,
) -> bool:
    bounds = coerce_bounds(bounds)
    x = float(x)
    y = float(y)
    if include_max:
        return bounds.min_x <= x <= bounds.max_x and bounds.min_y <= y <= bounds.max_y
    return bounds.min_x <= x < bounds.max_x and bounds.min_y <= y < bounds.max_y


__all__ = (
    "Bounds2D",
    "TERRAIN_FOLIAGE_MODE_ITEMS",
    "TERRAIN_IMPORT_MODE_ITEMS",
    "coerce_bounds",
    "point_in_bounds",
    "terrain_native_level",
    "terrain_tile_bounds",
    "terrain_tile_from_world_position",
    "terrain_view_lod_tiles",
)
