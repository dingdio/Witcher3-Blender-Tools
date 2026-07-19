from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from witcher3_tools.importers import import_w2w


def _tile_mesh(name: str, key: str, verts: int = import_w2w._TILE_MESH_CACHE_MIN_VERTS):
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(verts)
    mesh[import_w2w._TILE_MESH_KEY_PROP] = key
    return mesh


def main() -> None:
    lru = import_w2w._TILE_MESH_LRU
    doomed = import_w2w._TILE_MESH_DOOMED
    lru.clear()
    doomed.clear()

    mesh = _tile_mesh("w3_cache_a", "key_a")
    mesh.materials.append(bpy.data.materials.new("w3_cache_a_mat"))
    import_w2w._store_cached_tile_mesh(mesh)
    assert list(lru) == ["key_a"]
    assert len(mesh.materials) == 0

    assert import_w2w._take_cached_tile_mesh("key_a") == mesh
    assert import_w2w._take_cached_tile_mesh("key_a") is None

    import_w2w._store_cached_tile_mesh(mesh)
    bpy.data.meshes.remove(mesh)
    assert import_w2w._take_cached_tile_mesh("key_a") is None

    small = _tile_mesh("w3_cache_small", "key_small", verts=8)
    import_w2w._store_cached_tile_mesh(small)
    assert "key_small" not in lru
    assert small in doomed
    assert bpy.data.meshes.get("w3_cache_small") is not None
    import_w2w.flush_tile_mesh_garbage()
    assert not doomed
    assert bpy.data.meshes.get("w3_cache_small") is None

    one_mesh_bytes = (
        import_w2w._TILE_MESH_CACHE_MIN_VERTS * import_w2w._TILE_MESH_BYTES_PER_VERT
    )
    with mock.patch.object(
        import_w2w, "_tile_mesh_cache_budget_bytes", return_value=one_mesh_bytes
    ):
        first = _tile_mesh("w3_cache_b", "key_b")
        second = _tile_mesh("w3_cache_c", "key_c")
        import_w2w._store_cached_tile_mesh(first)
        import_w2w._store_cached_tile_mesh(second)
        assert list(lru) == ["key_c"]
        import_w2w.flush_tile_mesh_garbage()
        assert bpy.data.meshes.get("w3_cache_b") is None

    previous = _tile_mesh("w3_cache_old", "key_same")
    replacement = _tile_mesh("w3_cache_new", "key_same")
    import_w2w._store_cached_tile_mesh(previous)
    import_w2w._store_cached_tile_mesh(replacement)
    import_w2w.flush_tile_mesh_garbage()
    assert bpy.data.meshes.get("w3_cache_old") is None
    assert import_w2w._take_cached_tile_mesh("key_same") == replacement

    with mock.patch.object(
        import_w2w, "_tile_mesh_cache_budget_bytes", return_value=0
    ):
        gone = _tile_mesh("w3_cache_d", "key_d")
        import_w2w._store_cached_tile_mesh(gone)
        assert "key_d" not in lru
        import_w2w.flush_tile_mesh_garbage()
        assert bpy.data.meshes.get("w3_cache_d") is None

    zombie = _tile_mesh("w3_cache_e", "key_e", verts=8)
    import_w2w._store_cached_tile_mesh(zombie)
    bpy.data.meshes.remove(zombie)
    import_w2w.flush_tile_mesh_garbage()
    assert not doomed

    lru.clear()
    leftover = bpy.data.meshes.get("w3_cache_c")
    if leftover is not None:
        bpy.data.meshes.remove(leftover)
    print("blender_terrain_mesh_cache_native: OK")


if __name__ == "__main__":
    main()
