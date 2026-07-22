from __future__ import annotations

import sys
from pathlib import Path

import bpy


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from witcher3_tools.importers import import_blender_fun as deferred


def mesh_object(name):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0.0, 0.0, 0.0)], [], [])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


deferred._DEFERRED_MATERIAL_QUEUE.clear()
deferred._DEFERRED_MATERIALS_PAUSED = True
first = mesh_object("DeferredFirst")
second = mesh_object("DeferredSecond")
merged = mesh_object("DeferredMerged")
expected_material = mesh_object("ExpectedMaterial")
expected_material["witcher_expected_material_count"] = 1
assert not deferred.import_mesh.witcher_mesh_materials_ready(expected_material)

legacy = mesh_object("LegacyComplete")
legacy_material = bpy.data.materials.new("LegacyCompleteMaterial")
legacy_material.use_nodes = True
legacy_material.node_tree.nodes.new("ShaderNodeValue")
legacy_material["witcher3_material_setup_version"] = (
    deferred.import_mesh.MATERIAL_SETUP_VERSION - 1
)
legacy.data.materials.append(legacy_material)
legacy["witcher_expected_material_count"] = 1
custom_material = bpy.data.materials.new("UserTrailingMaterial")
legacy.data.materials.append(custom_material)
assert deferred.import_mesh.witcher_mesh_materials_ready(legacy)
legacy["witcher_materials_pending"] = True
assert not deferred.import_mesh.witcher_mesh_materials_ready(legacy)
legacy_material["witcher3_material_setup_version"] = deferred.import_mesh.MATERIAL_SETUP_VERSION
assert deferred.import_mesh.witcher_mesh_materials_ready(legacy)
legacy.pop("witcher_materials_pending", None)

slot_repair = mesh_object("MissingImportedSlot")
slot_repair.data.materials.append(legacy_material)
deferred.import_mesh._ensure_imported_material_slots(
    [slot_repair], ["FirstImported", "SecondImported"], r"C:\Depot\slot_repair.w2mesh"
)
assert slot_repair["witcher_expected_material_count"] == 2
assert len(slot_repair.data.materials) == 2
assert slot_repair.data.materials[1].get("w3_source_material_name") == "SecondImported"

empty_a = mesh_object("EmptyResolvedA")
empty_b = mesh_object("EmptyResolvedB")
deferred.queue_deferred_mesh_materials("", None, [empty_a], r"environment\a.w2mesh")
deferred.queue_deferred_mesh_materials("", None, [empty_b], r"environment\b.w2mesh")
assert len(deferred._DEFERRED_MATERIAL_QUEUE) == 2
deferred._DEFERRED_MATERIAL_QUEUE.clear()

deferred.queue_deferred_mesh_materials(r"C:\Depot\shared.w2mesh", None, [first], "shared.w2mesh")
deferred.queue_deferred_mesh_materials(r"c:\depot\shared.w2mesh", None, [second], "shared.w2mesh")
assert len(deferred._DEFERRED_MATERIAL_QUEUE) == 1
assert deferred._DEFERRED_MATERIAL_QUEUE[0][2] == [first.name, second.name]

deferred.replace_deferred_queue_objects([first.name], merged.name)
assert deferred._DEFERRED_MATERIAL_QUEUE[0][2] == [merged.name, second.name]

original_resolve = deferred._resolve_deferred_mesh_path
original_import = deferred.import_mesh.import_mesh_materials
original_ready = deferred.import_mesh.witcher_mesh_materials_ready
outcomes = [False, True]
calls = []
try:
    deferred._resolve_deferred_mesh_path = lambda path, repo: path

    def fake_import(path, objects, embedded_cmesh_chunk_index=None):
        calls.append([obj.name for obj in objects])
        return outcomes.pop(0)

    deferred.import_mesh.import_mesh_materials = fake_import
    deferred.import_mesh.witcher_mesh_materials_ready = lambda obj, repair_vertex_color=False: True
    deferred._DEFERRED_MATERIALS_PAUSED = False

    assert deferred._deferred_material_tick() == 0.5
    assert len(deferred._DEFERRED_MATERIAL_QUEUE) == 1
    assert deferred._DEFERRED_MATERIAL_QUEUE[0][4] == 1
    assert deferred._deferred_material_tick() is None
    assert not deferred._DEFERRED_MATERIAL_QUEUE
    assert calls == [[merged.name, second.name], [merged.name, second.name]]
    assert not merged.get("witcher_materials_pending", False)
    assert not second.get("witcher_materials_pending", False)

    trigger = mesh_object("RecoveryTrigger")
    orphan = mesh_object("RecoveryOrphan")
    orphan_material = bpy.data.materials.new("RecoveryOrphanMaterial")
    orphan_material["w3_source_material_name"] = "RecoveryOrphanMaterial"
    orphan_material["w3_source_mesh_path"] = r"C:\Depot\orphan.w2mesh"
    orphan.data.materials.append(orphan_material)
    orphan["witcher_expected_material_count"] = 1
    orphan["witcher_resolved_mesh_path"] = r"C:\Depot\orphan.w2mesh"
    orphan["witcher_materials_pending"] = True
    recovery_state = {orphan.name: False}

    def fake_recovery_import(path, objects, embedded_cmesh_chunk_index=None):
        calls.append([obj.name for obj in objects])
        for obj in objects:
            recovery_state[obj.name] = True
        return True

    deferred.import_mesh.import_mesh_materials = fake_recovery_import
    deferred.import_mesh.witcher_mesh_materials_ready = (
        lambda obj, repair_vertex_color=False: recovery_state.get(obj.name, True)
    )
    deferred.queue_deferred_mesh_materials(
        r"C:\Depot\trigger.w2mesh", None, [trigger], "trigger.w2mesh"
    )
    assert deferred._deferred_material_tick() == 0.05
    assert len(deferred._DEFERRED_MATERIAL_QUEUE) == 1
    assert deferred._DEFERRED_MATERIAL_QUEUE[0][2] == [orphan.name]
    assert deferred._deferred_material_tick() is None
    assert recovery_state[orphan.name]
    assert not orphan.get("witcher_materials_pending", False)

    recovery_material = bpy.data.materials.new("SharedRecoveryMaterial")
    recovery_material["w3_source_mesh_path"] = r"C:\Depot\right.w2mesh"
    renamed = mesh_object("RecoveryOriginalName")
    decoy = mesh_object("RecoveryOriginalName.001")
    renamed.data.materials.append(recovery_material)
    decoy.data.materials.append(recovery_material)
    renamed["witcher_resolved_mesh_path"] = r"C:\Depot\right.w2mesh"
    decoy["witcher_resolved_mesh_path"] = r"C:\Depot\wrong.w2mesh"
    deferred.queue_deferred_mesh_materials(
        r"C:\Depot\right.w2mesh", None, [renamed], "right.w2mesh"
    )
    renamed.name = "RecoveryRenamed"
    assert deferred._deferred_material_tick() is None
    assert calls[-1] == [renamed.name]
    assert not renamed.get("witcher_materials_pending", False)

    terminal = mesh_object("TerminalFailure")
    deferred.import_mesh.import_mesh_materials = lambda *args, **kwargs: False
    deferred.queue_deferred_mesh_materials(
        r"C:\Depot\terminal.w2mesh", None, [terminal], "terminal.w2mesh"
    )
    for _ in range(deferred._DEFERRED_MATERIAL_MAX_ATTEMPTS - 1):
        assert deferred._deferred_material_tick() == 0.5
    assert deferred._deferred_material_tick() is None
    assert not deferred._DEFERRED_MATERIAL_QUEUE
    assert terminal.get(deferred._DEFERRED_MATERIAL_EXHAUSTED_PROP, False)
    assert terminal.get(deferred._DEFERRED_MATERIAL_ERROR_PROP) == "terminal.w2mesh"
    deferred.queue_deferred_mesh_materials(
        r"C:\Depot\terminal.w2mesh", None, [terminal], "terminal.w2mesh"
    )
    assert not terminal.get(deferred._DEFERRED_MATERIAL_EXHAUSTED_PROP, False)
    assert not terminal.get(deferred._DEFERRED_MATERIAL_ERROR_PROP)
    deferred._DEFERRED_MATERIAL_QUEUE.clear()
    terminal.pop("witcher_materials_pending", None)
finally:
    deferred._resolve_deferred_mesh_path = original_resolve
    deferred.import_mesh.import_mesh_materials = original_import
    deferred.import_mesh.witcher_mesh_materials_ready = original_ready
    deferred._DEFERRED_MATERIAL_QUEUE.clear()
    deferred._DEFERRED_MATERIALS_PAUSED = False

deferred.unregister_deferred_material_load_handler()
assert deferred._resume_deferred_materials_on_load not in bpy.app.handlers.load_post
deferred.register_deferred_material_load_handler()
deferred.register_deferred_material_load_handler()
assert bpy.app.handlers.load_post.count(deferred._resume_deferred_materials_on_load) == 1
deferred.unregister_deferred_material_load_handler()
assert deferred._resume_deferred_materials_on_load not in bpy.app.handlers.load_post

print("DEFERRED_MATERIAL_QUEUE_BLENDER_SMOKE_OK")
