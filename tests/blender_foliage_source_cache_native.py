from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import bpy  # noqa: E402

from witcher3_tools.importers import import_foliage  # noqa: E402


def main():
    depot_path = r"environment\vegetation\unit\test_tree.srt"
    cache_path = os.path.join(
        tempfile.mkdtemp(prefix="w3_foliage_cache_"), "test_tree_cache.blend"
    )
    import_foliage._source_cache_file = lambda _depot, _ctx="": cache_path

    mesh = bpy.data.meshes.new("src_mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], (), [(0, 1, 2)])
    mesh.update()
    material = bpy.data.materials.new("src_mat")
    material.use_nodes = True
    image = bpy.data.images.new("src_img", 4, 4)
    texture_node = material.node_tree.nodes.new('ShaderNodeTexImage')
    texture_node.image = image
    mesh.materials.append(material)
    source = bpy.data.objects.new("src_obj", mesh)
    source["_depot_path"] = "_src_" + depot_path
    source[import_foliage._SOURCE_KIND_PROP] = import_foliage.FOLIAGE_SOURCE_MODE_FULL
    source[import_foliage._SOURCE_READY_PROP] = True
    bpy.context.scene.collection.objects.link(source)

    import_foliage._write_source_cache(depot_path, source)
    assert os.path.isfile(cache_path), "cache .blend was not written"
    assert not os.path.isfile(cache_path + ".tmp.blend"), "tmp file left behind"

    bpy.data.objects.remove(source, do_unlink=True)
    bpy.data.meshes.remove(mesh)

    foliage_root = bpy.data.collections.new("Foliage")
    bpy.context.scene.collection.children.link(foliage_root)

    appended = import_foliage._append_source_from_cache(depot_path, foliage_root)
    assert appended is not None, "append from cache failed"
    assert appended.type == 'MESH' and len(appended.data.vertices) == 3
    assert appended.material_slots and appended.material_slots[0].material is not None
    assert import_foliage._is_real_foliage_source(appended)
    assert appended.hide_viewport and appended.hide_render
    assert foliage_root in tuple(appended.users_collection)

    again = import_foliage._get_or_import_source_mesh(
        depot_path, foliage_root, cached_only=True
    )
    assert again is appended

    other_root = bpy.data.collections.new("Foliage2")
    bpy.context.scene.collection.children.link(other_root)
    second = import_foliage._get_or_import_source_mesh(
        depot_path, other_root, cached_only=True
    )
    assert second is not None and second is not appended
    assert len(second.data.vertices) == 3

    print("FOLIAGE SOURCE CACHE NATIVE: OK")


main()
