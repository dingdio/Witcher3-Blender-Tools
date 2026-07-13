"""Blender-native regression checks for .w2mesh tangent-basis export.

Run with Blender 4.5+:
  blender --background --factory-startup --python tests/blender_mesh_tangent_export_native.py
"""

from __future__ import annotations

import math
import io
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import bpy
from mathutils import Vector


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from witcher3_tools.CR2W.dc_mesh import (  # noqa: E402
    MeshData,
    _read_normals_packed10bit_numpy,
    _read_vertices_cooked_w2_numpy,
)
from witcher3_tools.exporters.export_mesh import split_mesh_by_material  # noqa: E402
from witcher3_tools.importers.import_mesh import (  # noqa: E402
    IMPORTED_BASIS_REPAIRED_ATTRIBUTE,
    IMPORTED_BASIS_REPAIRED_COUNT_PROPERTY,
    IMPORTED_BASIS_SOURCE_VERSION_PROPERTY,
    IMPORTED_TANGENT_ATTRIBUTE,
    TANGENT_HANDEDNESS_AS_CALCULATED,
    TANGENT_HANDEDNESS_AUTO,
    TANGENT_HANDEDNESS_FLIPPED,
    TANGENT_SPACE_MIKKTSPACE,
    TANGENT_SPACE_NO_MIRROR,
    TANGENT_SPACE_PRESERVE_IMPORTED,
    TANGENT_SPACE_REDENGINE,
    _bake_mikktspace_loop_basis,
    _mark_imported_tangent_basis,
    _solve_meshdata_tangent_basis,
    _store_imported_tangent_basis,
    get_mesh_info,
    imported_tangent_basis_status,
)
from witcher3_tools.ui.ui_mesh import (  # noqa: E402
    _collect_extra_stream_requirements,
    _preserve_join_transform_status,
)


def _cooker_sign(normal, tangent, binormal):
    value = Vector(normal).cross(Vector(tangent)).dot(Vector(binormal))
    return -1 if value < 0.0 else 1


def _assert_orthonormal(normal, tangent, binormal):
    n = Vector(normal)
    t = Vector(tangent)
    b = Vector(binormal)
    assert math.isclose(n.length, 1.0, abs_tol=2e-5)
    assert math.isclose(t.length, 1.0, abs_tol=2e-5)
    assert math.isclose(b.length, 1.0, abs_tol=2e-5)
    assert abs(n.dot(t)) <= 2e-5
    assert abs(n.dot(b)) <= 2e-5
    assert abs(t.dot(b)) <= 2e-5


def _check_ordinary_basis_stability():
    mesh_data = MeshData()
    mesh_data.vertex3DCoords = [(0, 0, 0), (1, 0, 0), (0, 1, 0)]
    mesh_data.UV_vertex3DCoords = [(0, 0), (1, 0), (0, 1)]
    diagonal = math.sqrt(0.5)
    mesh_data.normals = [(diagonal, 0, diagonal)] * 3
    mesh_data.faces = [(0, 1, 2)]
    tangents, binormals = _solve_meshdata_tangent_basis(mesh_data)

    # Preserve the legacy non-orthogonalized ordinary-chart basis.
    for tangent, binormal in zip(tangents, binormals):
        assert Vector(tangent) == Vector((1, 0, 0))
        assert Vector(binormal) == Vector((0, -1, 0))


def _face_signs(exported):
    result = []
    for face in exported.faces:
        signs = {
            _cooker_sign(
                exported.normals[index],
                exported.tangent_vector[index],
                exported.extra_vectors[index],
            )
            for index in face
        }
        assert len(signs) == 1
        result.append(signs.pop())
    return result


def _remove_object_and_mesh(obj):
    if obj is None:
        return
    mesh = getattr(obj, "data", None)
    if obj.name in bpy.data.objects:
        bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0 and mesh.name in bpy.data.meshes:
        bpy.data.meshes.remove(mesh)


def _create_mirrored_test_mesh():
    mesh = bpy.data.meshes.new("W2TangentRegressionData")
    obj = bpy.data.objects.new("W2TangentRegression", mesh)
    bpy.context.scene.collection.objects.link(obj)

    mesh.from_pydata(
        [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
        [],
        [(0, 1, 2), (0, 2, 3)],
    )
    uv = mesh.uv_layers.new(name="DiffuseUV")
    triangle_uvs = (
        ((0, 0), (1, 0), (1, 1)),
        ((0, 0), (1, 1), (1, 0)),
    )
    for polygon, polygon_uvs in zip(mesh.polygons, triangle_uvs):
        for loop_index, value in zip(polygon.loop_indices, polygon_uvs):
            uv.data[loop_index].uv = value
    mesh.update()
    return mesh, obj


def _create_smooth_material_boundary_mesh(materials):
    mesh = bpy.data.meshes.new("W2MikkMaterialBoundaryData")
    obj = bpy.data.objects.new("W2MikkMaterialBoundary", mesh)
    bpy.context.scene.collection.objects.link(obj)
    mesh.from_pydata(
        [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)],
        [],
        [(0, 1, 2), (0, 3, 1)],
    )
    for material in materials:
        mesh.materials.append(material)
    for material_index, polygon in enumerate(mesh.polygons):
        polygon.material_index = material_index
        polygon.use_smooth = True
    uv = mesh.uv_layers.new(name="DiffuseUV")
    for polygon in mesh.polygons:
        for loop_index, value in zip(
            polygon.loop_indices,
            ((0, 0), (1, 0), (0, 1)),
        ):
            uv.data[loop_index].uv = value
    mesh.update()
    return mesh, obj


def _create_uv_test_mesh(name, vertices, faces, polygon_uvs):
    mesh = bpy.data.meshes.new(f"{name}Data")
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    mesh.from_pydata(vertices, [], faces)
    uv = mesh.uv_layers.new(name="DiffuseUV")
    for polygon, values in zip(mesh.polygons, polygon_uvs):
        for loop_index, value in zip(polygon.loop_indices, values):
            uv.data[loop_index].uv = value
    mesh.update()
    return mesh, obj


def _check_export_modes():
    mesh, obj = _create_mirrored_test_mesh()
    transient_meshes = []
    transient_objects = []
    materials = []

    try:
        work_mesh = mesh.copy()
        transient_meshes.append(work_mesh)
        exported = get_mesh_info(work_mesh, obj)
        assert len(exported.faces) == 2
        assert len(exported.vertex3DCoords) == 6
        assert len(exported.tangent_vector) == 6
        assert len(exported.extra_vectors) == 6

        # Flipped UV V yields -1 for ordinary charts and +1 for mirrored charts.
        assert _face_signs(exported) == [-1, 1]

        for normal, tangent, binormal in zip(
            exported.normals,
            exported.tangent_vector,
            exported.extra_vectors,
        ):
            _assert_orthonormal(normal, tangent, binormal)

        no_mirror_mesh = mesh.copy()
        transient_meshes.append(no_mirror_mesh)
        no_mirror = get_mesh_info(
            no_mirror_mesh,
            obj,
            tangent_space_mode=TANGENT_SPACE_NO_MIRROR,
            tangent_handedness_mode=TANGENT_HANDEDNESS_FLIPPED,
        )
        assert len(no_mirror.vertex3DCoords) == 4
        # No Mirror ignores handedness controls to preserve legacy behavior.
        assert _face_signs(no_mirror) == [-1, -1]

        mikk_mesh = mesh.copy()
        transient_meshes.append(mikk_mesh)
        _bake_mikktspace_loop_basis(mikk_mesh)
        mikk = get_mesh_info(
            mikk_mesh,
            obj,
            tangent_space_mode=TANGENT_SPACE_MIKKTSPACE,
            tangent_handedness_mode=TANGENT_HANDEDNESS_AS_CALCULATED,
        )
        assert len(mikk.vertex3DCoords) == 6
        assert _face_signs(mikk) == [1, -1]
        for normal, tangent, binormal in zip(
            mikk.normals, mikk.tangent_vector, mikk.extra_vectors
        ):
            _assert_orthonormal(normal, tangent, binormal)

        def export_handedness_signs(
            tangent_space_mode,
            tangent_handedness_mode,
            source_version=None,
        ):
            work_mesh = mesh.copy()
            transient_meshes.append(work_mesh)
            if tangent_space_mode == TANGENT_SPACE_MIKKTSPACE:
                _bake_mikktspace_loop_basis(work_mesh)

            if source_version is None:
                mesh.pop(IMPORTED_BASIS_SOURCE_VERSION_PROPERTY, None)
            else:
                mesh[IMPORTED_BASIS_SOURCE_VERSION_PROPERTY] = source_version
            result = get_mesh_info(
                work_mesh,
                obj,
                tangent_space_mode=tangent_space_mode,
                tangent_handedness_mode=tangent_handedness_mode,
                basis_source_mesh=mesh,
            )
            return _face_signs(result)

        # Flipping handedness changes the binormal, not the basis solver.
        assert export_handedness_signs(
            TANGENT_SPACE_REDENGINE,
            TANGENT_HANDEDNESS_AS_CALCULATED,
        ) == [-1, 1]
        assert export_handedness_signs(
            TANGENT_SPACE_REDENGINE,
            TANGENT_HANDEDNESS_FLIPPED,
        ) == [1, -1]
        assert export_handedness_signs(
            TANGENT_SPACE_MIKKTSPACE,
            TANGENT_HANDEDNESS_AS_CALCULATED,
        ) == [1, -1]
        assert export_handedness_signs(
            TANGENT_SPACE_MIKKTSPACE,
            TANGENT_HANDEDNESS_FLIPPED,
        ) == [-1, 1]

        # Auto matches the imported game's convention for each solver.
        assert export_handedness_signs(
            TANGENT_SPACE_REDENGINE,
            TANGENT_HANDEDNESS_AUTO,
            source_version=115,
        ) == [1, -1]
        assert export_handedness_signs(
            TANGENT_SPACE_MIKKTSPACE,
            TANGENT_HANDEDNESS_AUTO,
            source_version=115,
        ) == [1, -1]
        assert export_handedness_signs(
            TANGENT_SPACE_REDENGINE,
            TANGENT_HANDEDNESS_AUTO,
            source_version=163,
        ) == [-1, 1]
        assert export_handedness_signs(
            TANGENT_SPACE_MIKKTSPACE,
            TANGENT_HANDEDNESS_AUTO,
            source_version=163,
        ) == [-1, 1]

        # Without a source version, keep each solver's default.
        assert export_handedness_signs(
            TANGENT_SPACE_REDENGINE,
            TANGENT_HANDEDNESS_AUTO,
        ) == [-1, 1]
        assert export_handedness_signs(
            TANGENT_SPACE_MIKKTSPACE,
            TANGENT_HANDEDNESS_AUTO,
        ) == [1, -1]
        mesh.pop(IMPORTED_BASIS_SOURCE_VERSION_PROPERTY, None)

        tiny_uv_mesh, tiny_uv_obj = _create_uv_test_mesh(
            "W2MikkTinyUV",
            [(0, 0, 0), (1, 0, 0), (0, 1, 0)],
            [(0, 1, 2)],
            [[(0, 0), (1 / 4096, 0), (0, 1 / 4096)]],
        )
        transient_objects.append(tiny_uv_obj)
        _bake_mikktspace_loop_basis(tiny_uv_mesh)
        tiny_uv_export = get_mesh_info(
            tiny_uv_mesh,
            tiny_uv_obj,
            tangent_space_mode=TANGENT_SPACE_MIKKTSPACE,
        )
        assert all(
            Vector(tangent).dot(Vector((1, 0, 0))) > 0.999
            for tangent in tiny_uv_export.tangent_vector
        )

        mixed_ngon_mesh, mixed_ngon_obj = _create_uv_test_mesh(
            "W2MikkMixedNgon",
            [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)],
            [(0, 1, 2, 3)],
            [[(0, 0), (1, 0), (2, 0), (0, -1)]],
        )
        transient_objects.append(mixed_ngon_obj)
        _bake_mikktspace_loop_basis(mixed_ngon_mesh)
        mixed_ngon_export = get_mesh_info(
            mixed_ngon_mesh,
            mixed_ngon_obj,
            tangent_space_mode=TANGENT_SPACE_MIKKTSPACE,
        )
        assert _face_signs(mixed_ngon_export) == [-1, -1]

        # Mikk corner signs must survive material splitting.
        for name in ("MikkMat0", "MikkMat1"):
            material = bpy.data.materials.new(name)
            materials.append(material)
            mesh.materials.append(material)
        mesh.polygons[0].material_index = 0
        mesh.polygons[1].material_index = 1
        prepared_obj = obj.copy()
        prepared_obj.data = mesh.copy()
        bpy.context.collection.objects.link(prepared_obj)
        transient_objects.append(prepared_obj)
        _bake_mikktspace_loop_basis(prepared_obj.data)
        mikk_splits = split_mesh_by_material(prepared_obj)
        transient_objects.extend(entry[0] for entry in mikk_splits)
        split_signs = []
        for split_obj, _material_indices in mikk_splits:
            split_export = get_mesh_info(
                split_obj.data,
                split_obj,
                tangent_space_mode=TANGENT_SPACE_MIKKTSPACE,
            )
            split_signs.extend(_face_signs(split_export))
        assert split_signs == [1, -1]

        # Material splits must retain the full-LOD Mikk basis.
        boundary_mesh, boundary_obj = _create_smooth_material_boundary_mesh(materials)
        transient_objects.append(boundary_obj)
        _bake_mikktspace_loop_basis(boundary_mesh)
        boundary_splits = split_mesh_by_material(boundary_obj)
        transient_objects.extend(entry[0] for entry in boundary_splits)
        shared_normals = []
        for split_obj, _material_indices in boundary_splits:
            split_export = get_mesh_info(
                split_obj.data,
                split_obj,
                tangent_space_mode=TANGENT_SPACE_MIKKTSPACE,
            )
            for position, normal, tangent, binormal in zip(
                split_export.vertex3DCoords,
                split_export.normals,
                split_export.tangent_vector,
                split_export.extra_vectors,
            ):
                _assert_orthonormal(normal, tangent, binormal)
                if tuple(position) in ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)):
                    shared_normals.append(Vector(normal))
        assert len(shared_normals) == 4
        assert all(normal.y > 0.6 and normal.z > 0.6 for normal in shared_normals)

        join_peer = obj.copy()
        join_peer.data = mesh.copy()
        bpy.context.collection.objects.link(join_peer)
        transient_objects.append(join_peer)
        assert _preserve_join_transform_status([obj, join_peer])[0]
        join_peer.location.x = 2.0
        bpy.context.view_layer.update()
        assert _preserve_join_transform_status([obj, join_peer])[0]
        join_peer.rotation_euler.z = math.radians(90.0)
        bpy.context.view_layer.update()
        assert not _preserve_join_transform_status([obj, join_peer])[0]

        repair_mesh, repair_obj = _create_uv_test_mesh(
            "W2ImportedBasisRepair",
            [(0, 0, 0), (1, 0, 0), (0, 1, 0)],
            [(0, 1, 2)],
            [[(0, 0), (1, 0), (0, 1)]],
        )
        transient_objects.append(repair_obj)
        repair_source = MeshData()
        repair_source.vertex3DCoords = [
            [0, 0, 0], [1, 0, 0], [0, 1, 0]]
        repair_source.UV_vertex3DCoords = [
            [0, 0], [1, 0], [0, 1]]
        repair_source.faces = [[0, 1, 2]]
        repair_source.normals = [[0, 0, 0.75], [0, 0, 1], [0, 0, 1]]
        repair_source.normalsAll = [
            component
            for normal in repair_source.normals
            for component in normal
        ]
        repair_source.tangent_vector = [
            [math.nan, math.nan, math.nan],
            [1, 0, 0],
            [1, 0, 0],
        ]
        repair_source.extra_vectors = [
            [math.nan, math.nan, math.nan],
            [0, 1, 0],
            [0, 1, 0],
        ]
        assert _store_imported_tangent_basis(
            repair_mesh, repair_source, source_version=112)
        repaired_values = [
            int(item.value)
            for item in repair_mesh.attributes[IMPORTED_BASIS_REPAIRED_ATTRIBUTE].data
        ]
        assert repaired_values == [1, 0, 0]
        assert _mark_imported_tangent_basis(repair_mesh)
        assert repair_mesh[IMPORTED_BASIS_REPAIRED_COUNT_PROPERTY] == 1
        assert imported_tangent_basis_status(repair_mesh)[0]
        repair_work = repair_mesh.copy()
        transient_meshes.append(repair_work)
        repaired_export = get_mesh_info(
            repair_work,
            repair_obj,
            tangent_space_mode=TANGENT_SPACE_PRESERVE_IMPORTED,
            basis_source_mesh=repair_mesh,
        )
        assert all(
            math.isfinite(component)
            for values in (
                repaired_export.normals,
                repaired_export.tangent_vector,
                repaired_export.extra_vectors,
            )
            for vector in values
            for component in vector
        )
        assert _cooker_sign(
            repaired_export.normals[0],
            repaired_export.tangent_vector[0],
            repaired_export.extra_vectors[0],
        ) == 1
        assert Vector(repaired_export.normals[0]) == Vector((0, 0, 0.75))
        assert Vector(repaired_export.tangent_vector[1]) == Vector((1, 0, 0))
        assert Vector(repaired_export.extra_vectors[1]) == Vector((0, 1, 0))

        # Preserve mode reuses imported N/T/B and rejects basis edits.
        imported_mesh = bpy.data.meshes.new("ImportedBasisData")
        imported_obj = bpy.data.objects.new("ImportedBasis", imported_mesh)
        bpy.context.scene.collection.objects.link(imported_obj)
        transient_objects.append(imported_obj)
        imported_mesh.from_pydata(
            exported.vertex3DCoords, [], exported.faces)
        imported_uv = imported_mesh.uv_layers.new(name="DiffuseUV")
        for loop in imported_mesh.loops:
            imported_uv.data[loop.index].uv = exported.UV_vertex3DCoords[loop.vertex_index]
        imported_mesh.polygons.foreach_set(
            "use_smooth", [True] * len(imported_mesh.polygons))
        imported_mesh.normals_split_custom_set_from_vertices(exported.normals)
        imported_mesh.update()
        literal_w2_basis = MeshData()
        literal_w2_basis.normals = [list(value) for value in exported.normals]
        literal_w2_basis.tangent_vector = [
            list(value) for value in exported.tangent_vector]
        literal_w2_basis.extra_vectors = [
            [-value[0], -value[1], -value[2]]
            for value in exported.extra_vectors
        ]
        imported_colors = [
            (float(position[0]), float(position[1]), 1.0 - float(position[0]), 1.0)
            for position in exported.vertex3DCoords
        ]
        imported_color_attribute = imported_mesh.color_attributes.new(
            name="Color", domain='POINT', type='BYTE_COLOR')
        imported_color_attribute.data.foreach_set(
            "color", [component for color in imported_colors for component in color])
        assert _collect_extra_stream_requirements([imported_obj])[1]
        assert _store_imported_tangent_basis(
            imported_mesh, literal_w2_basis, source_version=115)
        assert _mark_imported_tangent_basis(imported_mesh)
        assert imported_tangent_basis_status(imported_mesh)[0]

        uv_edited_mesh = imported_mesh.copy()
        transient_meshes.append(uv_edited_mesh)
        assert imported_tangent_basis_status(uv_edited_mesh)[0]
        uv_edited_mesh.uv_layers["DiffuseUV"].data[0].uv.x += 0.125
        uv_valid, uv_reason = imported_tangent_basis_status(uv_edited_mesh)
        assert not uv_valid and "changed after import" in uv_reason

        basis_edited_mesh = imported_mesh.copy()
        transient_meshes.append(basis_edited_mesh)
        assert imported_tangent_basis_status(basis_edited_mesh)[0]
        basis_edited_mesh.attributes[IMPORTED_TANGENT_ATTRIBUTE].data[0].vector.x += 0.125
        basis_valid, basis_reason = imported_tangent_basis_status(basis_edited_mesh)
        assert not basis_valid and "changed after import" in basis_reason

        preserved_work = imported_mesh.copy()
        transient_meshes.append(preserved_work)
        copied_color_attribute = preserved_work.color_attributes.get("Color")
        assert copied_color_attribute is not None
        assert Vector(copied_color_attribute.data[0].color[:3]) == Vector(
            imported_colors[0][:3])
        preserved = get_mesh_info(
            preserved_work,
            imported_obj,
            tangent_space_mode=TANGENT_SPACE_PRESERVE_IMPORTED,
            basis_source_mesh=imported_mesh,
        )
        assert len(preserved.vertex3DCoords) == len(exported.vertex3DCoords)
        expected_colors_by_position = {
            tuple(position): color
            for position, color in zip(exported.vertex3DCoords, imported_colors)
        }
        for position, color in zip(preserved.vertex3DCoords, preserved.vertexColor):
            expected_color = expected_colors_by_position[tuple(position)]
            assert (
                Vector(color[:3]) - Vector(expected_color[:3])
            ).length <= 0.01, (position, color, expected_color)
            assert abs(float(color[3]) - float(expected_color[3])) <= 0.01
        for actual_values, expected_values in (
            (preserved.normals, literal_w2_basis.normals),
            (preserved.tangent_vector, literal_w2_basis.tangent_vector),
            (preserved.extra_vectors, literal_w2_basis.extra_vectors),
        ):
            for actual, expected in zip(actual_values, expected_values):
                assert (Vector(actual) - Vector(expected)).length <= 1.0e-6

        # Preserve mode ignores handedness overrides.
        flipped_preserve_work = imported_mesh.copy()
        transient_meshes.append(flipped_preserve_work)
        flipped_preserved = get_mesh_info(
            flipped_preserve_work,
            imported_obj,
            tangent_space_mode=TANGENT_SPACE_PRESERVE_IMPORTED,
            tangent_handedness_mode=TANGENT_HANDEDNESS_FLIPPED,
            basis_source_mesh=imported_mesh,
        )
        for actual_values, expected_values in (
            (flipped_preserved.normals, literal_w2_basis.normals),
            (flipped_preserved.tangent_vector, literal_w2_basis.tangent_vector),
            (flipped_preserved.extra_vectors, literal_w2_basis.extra_vectors),
        ):
            for actual, expected in zip(actual_values, expected_values):
                assert (Vector(actual) - Vector(expected)).length <= 1.0e-6

        # Preserve the imported W3 basis verbatim.
        imported_mesh[IMPORTED_BASIS_SOURCE_VERSION_PROPERTY] = 163
        preserved_w3_work = imported_mesh.copy()
        transient_meshes.append(preserved_w3_work)
        preserved_w3 = get_mesh_info(
            preserved_w3_work,
            imported_obj,
            tangent_space_mode=TANGENT_SPACE_PRESERVE_IMPORTED,
            basis_source_mesh=imported_mesh,
        )
        for actual_values, expected_values in (
            (preserved_w3.normals, literal_w2_basis.normals),
            (preserved_w3.tangent_vector, literal_w2_basis.tangent_vector),
            (preserved_w3.extra_vectors, literal_w2_basis.extra_vectors),
        ):
            for actual, expected in zip(actual_values, expected_values):
                assert (Vector(actual) - Vector(expected)).length <= 1.0e-6
        imported_mesh[IMPORTED_BASIS_SOURCE_VERSION_PROPERTY] = 115

        for material in materials:
            imported_mesh.materials.append(material)
        imported_mesh.polygons[0].material_index = 0
        imported_mesh.polygons[1].material_index = 1
        preserved_splits = split_mesh_by_material(imported_obj)
        transient_objects.extend(entry[0] for entry in preserved_splits)
        for split_obj, _material_indices in preserved_splits:
            split_preserved = get_mesh_info(
                split_obj.data,
                split_obj,
                tangent_space_mode=TANGENT_SPACE_PRESERVE_IMPORTED,
                basis_source_mesh=imported_mesh,
            )
            assert len(split_preserved.tangent_vector) == 3
            assert len(split_preserved.extra_vectors) == 3

        imported_mesh.vertices[0].co.x += 0.125
        assert not imported_tangent_basis_status(imported_mesh)[0]
        edited_work = imported_mesh.copy()
        transient_meshes.append(edited_work)
        try:
            get_mesh_info(
                edited_work,
                imported_obj,
                tangent_space_mode=TANGENT_SPACE_PRESERVE_IMPORTED,
                basis_source_mesh=imported_mesh,
            )
        except ValueError as exc:
            assert "changed after import" in str(exc)
        else:
            raise AssertionError("Edited mesh unexpectedly accepted Preserve Imported Basis")

        missing_work = mesh.copy()
        transient_meshes.append(missing_work)
        try:
            get_mesh_info(
                missing_work,
                obj,
                tangent_space_mode=TANGENT_SPACE_PRESERVE_IMPORTED,
                basis_source_mesh=mesh,
            )
        except ValueError as exc:
            assert "unavailable" in str(exc)
        else:
            raise AssertionError("Non-imported mesh unexpectedly accepted Preserve Imported Basis")
    finally:
        for transient_obj in reversed(transient_objects):
            _remove_object_and_mesh(transient_obj)
        _remove_object_and_mesh(obj)
        for candidate in transient_meshes:
            if candidate and candidate.users == 0 and candidate.name in bpy.data.meshes:
                bpy.data.meshes.remove(candidate)
        for material in materials:
            if material.users == 0 and material.name in bpy.data.materials:
                bpy.data.materials.remove(material)


def _pack_dec4(vector, alpha):
    quantized = [
        max(0, min(1023, int((float(value) * 0.5 + 0.5) * 1023.0)))
        for value in vector
    ]
    packed = quantized[0] | (quantized[1] << 10) | (quantized[2] << 20) | (alpha << 30)
    return struct.pack('<I', packed)


def _check_packed_basis_decoders():
    empty_type_11 = MeshData()
    empty_cache = SimpleNamespace(w3_DataCache=SimpleNamespace(vertices=[]))
    _read_vertices_cooked_w2_numpy(
        io.BytesIO(b""), 0, 44, True, 4, False, False,
        [], [], empty_cache, empty_type_11, 11,
    )
    assert empty_type_11.tangent_vector == []
    assert empty_type_11.extra_vectors == []

    w2_data = (
        struct.pack('<3f', 1.0, 2.0, 3.0)
        + bytes((127, 127, 254, 0))
        + bytes((255, 255, 255, 255))
        + struct.pack('<2f', 0.25, 0.75)
        + bytes((254, 127, 127, 0))
        + bytes((127, 254, 127, 0))
    )
    w2_mesh = MeshData()
    cache = SimpleNamespace(w3_DataCache=SimpleNamespace(vertices=[]))
    _read_vertices_cooked_w2_numpy(
        io.BytesIO(w2_data),
        1,
        36,
        False,
        4,
        False,
        False,
        [],
        [],
        cache,
        w2_mesh,
        0,
    )
    assert len(w2_mesh.tangent_vector) == 1
    assert Vector(w2_mesh.tangent_vector[0]).dot(Vector((1, 0, 0))) > 0.999
    assert Vector(w2_mesh.extra_vectors[0]).dot(Vector((0, 1, 0))) > 0.999

    skinned_data = (
        struct.pack('<3f', 1.0, 2.0, 3.0)
        + bytes((0, 0, 0, 0, 255, 0, 0, 0))
        + bytes((127, 127, 254, 0))
        + bytes((255, 255, 255, 255))
        + struct.pack('<2f', 0.25, 0.75)
        + bytes((254, 127, 127, 0))
        + bytes((127, 254, 127, 0))
    )
    skinned_mesh = MeshData()
    skinned_cache = SimpleNamespace(w3_DataCache=SimpleNamespace(vertices=[]))
    _read_vertices_cooked_w2_numpy(
        io.BytesIO(skinned_data), 1, 44, True, 4, False, False,
        [0], ["root"], skinned_cache, skinned_mesh, 1,
    )
    assert Vector(skinned_mesh.tangent_vector[0]).dot(Vector((1, 0, 0))) > 0.999
    assert Vector(skinned_mesh.extra_vectors[0]).dot(Vector((0, 1, 0))) > 0.999

    unsupported_mesh = MeshData()
    _read_vertices_cooked_w2_numpy(
        io.BytesIO(w2_data), 1, 36, False, 4, False, False,
        [], [], cache, unsupported_mesh, 4,
    )
    assert unsupported_mesh.tangent_vector == []
    assert unsupported_mesh.extra_vectors == []

    malformed_type_11 = MeshData()
    _read_vertices_cooked_w2_numpy(
        io.BytesIO(skinned_data[:-8] + bytes((127,)) * 8),
        1, 44, True, 4, False, False,
        [0], ["root"], skinned_cache, malformed_type_11, 11,
    )
    assert malformed_type_11.tangent_vector == []
    assert malformed_type_11.extra_vectors == []

    vegetation_data = (
        w2_data[:28]
        + bytes(8)
        + bytes((254, 127, 127, 0))
        + bytes((127, 254, 127, 0))
        + bytes((17,)) * 16
    )
    vegetation_mesh = MeshData()
    _read_vertices_cooked_w2_numpy(
        io.BytesIO(vegetation_data),
        1,
        60,
        False,
        4,
        False,
        False,
        [],
        [],
        cache,
        vegetation_mesh,
        5,
    )
    assert Vector(vegetation_mesh.tangent_vector[0]).dot(Vector((1, 0, 0))) > 0.999
    assert Vector(vegetation_mesh.extra_vectors[0]).dot(Vector((0, 1, 0))) > 0.999

    normal = _pack_dec4((0, 0, 1), 0)
    tangent_negative = _pack_dec4((1, 0, 0), 0)
    tangent_positive = _pack_dec4((1, 0, 0), 3)
    normals, tangents, binormals = _read_normals_packed10bit_numpy(
        io.BytesIO(normal + tangent_negative + normal + tangent_positive), 2)
    assert abs(float(normals[0][0]) + (1.0 / 1023.0)) <= 1.0e-7
    assert abs(float(normals[0][2]) - 1.0) <= 1.0e-7
    assert abs(float(tangents[0][0]) - 1.0) <= 1.0e-7
    assert _cooker_sign(normals[0], tangents[0], binormals[0]) == -1
    assert _cooker_sign(normals[1], tangents[1], binormals[1]) == 1


def main():
    _check_ordinary_basis_stability()
    _check_export_modes()
    _check_packed_basis_decoders()

    print("W2MESH_TANGENT_EXPORT_BLENDER_OK")


if __name__ == "__main__":
    main()
