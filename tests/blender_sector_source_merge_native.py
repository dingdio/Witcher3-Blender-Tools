from __future__ import annotations

import ast
import re
from types import SimpleNamespace
from pathlib import Path

import bpy
from mathutils import Matrix


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "witcher3_tools" / "importers" / "import_blender_fun.py"


def load_helpers():
    tree = ast.parse(TARGET.read_text(encoding="utf-8"), filename=str(TARGET))
    names = {
        "_sector_source_lod_level",
        "_primary_sector_source_meshes",
        "_object_matrix_relative_to_ancestor",
        "_merge_sector_source_meshes",
        "_pick_best_sector_source_mesh",
    }
    nodes = [
        item for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name in names
    ]
    def ensure_raw_vertex_color(obj):
        attributes = obj.data.color_attributes
        source = attributes.get("Color")
        if source is None:
            return
        raw = attributes.get("W3RawColor") or attributes.new(
            name="W3RawColor", domain=source.domain, type="FLOAT_COLOR"
        )
        raw.data.foreach_set(
            "color",
            [component for item in source.data for component in item.color_srgb],
        )

    namespace = {
        "bpy": bpy,
        "Matrix": Matrix,
        "Path": Path,
        "re": re,
        "import_mesh": SimpleNamespace(_ensure_raw_vertex_color=ensure_raw_vertex_color),
        "_object_identity": lambda obj: int(obj.as_pointer()),
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(TARGET), "exec"), namespace)
    return namespace


def make_chunk(name, material, offset):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(
        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        [],
        [(0, 1, 2)],
    )
    mesh.materials.append(material)
    mesh.polygons[0].material_index = 0
    color = mesh.color_attributes.new(name="Color", domain="POINT", type="BYTE_COLOR")
    for item in color.data:
        item.color_srgb = (0.0, 0.25, 0.0, 1.0)
    uv_layer = mesh.uv_layers.new(name="UVMap")
    for loop_index, uv in enumerate(((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))):
        uv_layer.data[loop_index].uv = uv
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = offset
    return obj


helpers = load_helpers()
merge = helpers["_merge_sector_source_meshes"]
red = bpy.data.materials.new("MergeRed")
blue = bpy.data.materials.new("MergeBlue")
left = make_chunk("left_chunk", red, (0.0, 0.0, 0.0))
right = make_chunk("right_chunk", blue, (3.0, 0.0, 0.0))

merged = merge([left, right], "merged_source")

assert len(merged.data.polygons) == 2, len(merged.data.polygons)
assert list(merged.data.materials) == [red, blue], list(merged.data.materials)
assert merged["witcher_expected_material_count"] == 2
assert {polygon.material_index for polygon in merged.data.polygons} == {0, 1}
assert merged.data.uv_layers.get("UVMap") is not None
raw_color = merged.data.color_attributes.get("W3RawColor")
assert raw_color is not None
raw_greens = [item.color[1] for item in raw_color.data]
assert all(abs(value - (64.0 / 255.0)) < 1e-6 for value in raw_greens), raw_greens
xs = [vertex.co.x for vertex in merged.data.vertices]
assert min(xs) == 0.0 and max(xs) == 4.0, (min(xs), max(xs))

wrapper = bpy.data.objects.new("nested_wrapper", None)
nested = bpy.data.objects.new("nested_group", None)
bpy.context.scene.collection.objects.link(wrapper)
bpy.context.scene.collection.objects.link(nested)
wrapper.location = (10.0, 0.0, 0.0)
nested.parent = wrapper
nested.matrix_parent_inverse = Matrix.Identity(4)
nested.location = (3.0, 0.0, 0.0)

lod0_red = make_chunk("asset_lod0_red", red, (2.0, 0.0, 0.0))
lod0_blue = make_chunk("asset_lod0_blue", blue, (0.0, 0.0, 0.0))
lod1 = make_chunk("asset_lod1", red, (0.0, 0.0, 5.0))
for obj in (lod0_red, lod0_blue, lod1):
    obj.parent = nested
    obj.matrix_parent_inverse = Matrix.Identity(4)

temporary_names = {obj.name for obj in (wrapper, nested, lod0_red, lod0_blue, lod1)}
production_source = helpers["_pick_best_sector_source_mesh"](
    [wrapper, nested, lod0_red, lod0_blue, lod1],
    wrapper,
)

assert len(production_source.data.polygons) == 2, len(production_source.data.polygons)
assert set(production_source.data.materials) == {red, blue}
production_xs = [vertex.co.x for vertex in production_source.data.vertices]
production_zs = [vertex.co.z for vertex in production_source.data.vertices]
assert min(production_xs) == 3.0 and max(production_xs) == 6.0, (min(production_xs), max(production_xs))
assert min(production_zs) == 0.0 and max(production_zs) == 0.0, (min(production_zs), max(production_zs))
assert not temporary_names.intersection(bpy.data.objects.keys()), temporary_names.intersection(bpy.data.objects.keys())

duplicate_left = make_chunk("duplicate_left", red, (0.0, 0.0, 0.0))
duplicate_right = make_chunk("duplicate_right", red, (0.0, 0.0, 0.0))
for obj, material_index in ((duplicate_left, 1), (duplicate_right, 2)):
    obj.data.materials.clear()
    for material in (red, red, blue):
        obj.data.materials.append(material)
    obj.data.polygons[0].material_index = material_index
duplicate_merged = merge([duplicate_left, duplicate_right], "duplicate_slots")
assert list(duplicate_merged.data.materials) == [red, red, blue]
assert duplicate_merged["witcher_expected_material_count"] == 3
assert {polygon.material_index for polygon in duplicate_merged.data.polygons} == {1, 2}

print("sector source merge native smoke test: OK")
