import math
import sys
from pathlib import Path

import bpy
from mathutils import Matrix


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from witcher3_tools.importers import import_blender_fun


def assert_matrix_close(actual, expected, rel_tol=2e-5, abs_tol=2e-5):
    for row in range(4):
        for col in range(4):
            a = float(actual[row][col])
            b = float(expected[row][col])
            assert math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol), (
                row, col, a, b, abs(a - b)
            )


def new_empty(name, collection):
    obj = bpy.data.objects.new(name, None)
    collection.objects.link(obj)
    return obj


def new_mesh_object(name, collection):
    mesh = bpy.data.meshes.new(name + "Data")
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


scene_collection = bpy.context.scene.collection
source_collection = bpy.data.collections.new("Source")
clone_collection = bpy.data.collections.new("Clone")
scene_collection.children.link(source_collection)
scene_collection.children.link(clone_collection)

source_root = new_empty("cached_collision", source_collection)
source_root["repo_path"] = r"environment\test\cached_collision.w2mesh#collision"
source_child = new_mesh_object("cached_collision_col", source_collection)
source_child.parent = source_root
source_child_parent_inverse = (
    Matrix.Translation((-0.4, 0.2, 0.1))
    @ Matrix.Rotation(math.radians(-9.0), 4, "Y")
)
source_child.matrix_parent_inverse = source_child_parent_inverse
source_child_local = (
    Matrix.Translation((0.25, -0.5, 0.75))
    @ Matrix.Rotation(math.radians(17.0), 4, "X")
)
source_child.matrix_basis = source_child_local
bpy.context.view_layer.update()

# Avoid a dependency-graph update here; stale transforms trigger the regression.
source_root.matrix_world = (
    Matrix.Translation((12.0, -7.5, 3.0))
    @ Matrix.Rotation(math.radians(123.0), 4, "Z")
)
assert_matrix_close(source_child.matrix_basis, source_child_local)
assert not all(
    math.isclose(
        float(source_child.matrix_local[row][col]),
        float(source_child_local[row][col]),
        rel_tol=2e-5,
        abs_tol=2e-5,
    )
    for row in range(4)
    for col in range(4)
)

clone_root = import_blender_fun._clone_duplicate_hierarchy(
    source_root,
    clone_collection,
)
assert clone_root is not None
assert len(clone_root.children) == 1
clone_child = clone_root.children[0]
assert_matrix_close(clone_child.matrix_basis, source_child_local)
assert_matrix_close(clone_child.matrix_parent_inverse, source_child_parent_inverse)
assert_matrix_close(
    clone_child.matrix_local,
    source_child_parent_inverse @ source_child_local,
)

placement_parent = new_empty("Collision", clone_collection)
placement_parent.matrix_basis = Matrix.Translation((-4.0, 8.0, 1.0))

root_local = clone_root.matrix_local.copy()
clone_root.parent = placement_parent
import_blender_fun._set_object_local_matrix_direct(clone_root, root_local)

clone_root.matrix_world @= (
    Matrix.Rotation(math.radians(-71.0), 4, "Z")
    @ Matrix.Rotation(math.radians(11.0), 4, "Y")
)
clone_root.location = (24.0, 13.0, -2.0)
clone_root.scale = (1.25, 0.8, 1.1)
bpy.context.view_layer.update()

assert_matrix_close(source_child.matrix_basis, source_child_local)
assert_matrix_close(clone_child.matrix_basis, source_child_local)
assert_matrix_close(clone_child.matrix_parent_inverse, source_child_parent_inverse)
assert_matrix_close(
    clone_child.matrix_local,
    source_child_parent_inverse @ source_child_local,
)
assert_matrix_close(
    clone_child.matrix_world,
    clone_root.matrix_world @ source_child_parent_inverse @ source_child_local,
)

print("cached hierarchy clone transform checks passed")
