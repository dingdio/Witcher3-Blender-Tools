"""Run with Blender 4.5+:
  blender --background --factory-startup --python tests/blender_entity_builder_native.py
"""

import math
import sys
import tempfile
import traceback
from pathlib import Path

import bpy
from mathutils import Matrix

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon


def new_mesh(name, depot="", via_settings=False):
    obj = bpy.data.objects.new(name, bpy.data.meshes.new(name + "Data"))
    bpy.context.scene.collection.objects.link(obj)
    if depot and via_settings:
        obj.witcherui_MeshSettings.item_repo_path = depot  # how standalone .w2mesh imports record it
    elif depot:
        obj["witcher_path"] = depot
    return obj


def select_only(*objs):
    for o in bpy.data.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]


def close(a, b, tol=1e-4):
    return all(abs(a[r][c] - b[r][c]) < tol for r in range(4) for c in range(4))


_ICONS = {item.identifier for item in bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"].enum_items}


class FakeLayout:
    def __init__(self, calls=None):
        self.calls = calls if calls is not None else []
        self.scale_y = 1.0
        self.enabled = True

    def _sub(self, *_a, **_k):
        return FakeLayout(self.calls)

    row = column = box = split = _sub

    def separator(self, *_a, **_k):
        pass

    def label(self, text="", icon='NONE', **_k):
        assert icon in _ICONS, f"unknown icon {icon!r}"
        self.calls.append(("label", text))

    def prop(self, data, path, **kw):
        data.path_resolve(path)
        assert kw.get("icon", 'NONE') in _ICONS
        self.calls.append(("prop", path))

    def operator(self, idname, text="", icon='NONE', **_k):
        module, name = idname.split(".")
        assert hasattr(getattr(bpy.ops, module), name), f"unknown operator {idname}"
        assert icon in _ICONS, f"unknown icon {icon!r}"
        self.calls.append(("op", idname))
        return FakeLayout(self.calls)


def draw_panels():
    calls = []

    class Host:
        layout = FakeLayout(calls)

    for cls in (eb.WITCHER_PT_animated_component, eb.WITCHER_PT_entity_builder_custom):
        if cls.poll(bpy.context) if hasattr(cls, "poll") else True:
            host = Host()
            for attr in ("draw", "draw_header", "_draw_create", "_draw_edit"):
                if attr in cls.__dict__:
                    setattr(host, attr, cls.__dict__[attr].__get__(host))
            host.draw(bpy.context)
            if hasattr(host, "draw_header"):
                host.draw_header(bpy.context)
    return calls


addon.register()
try:
    from witcher3_tools.ui import ui_animated_component as eb
    from witcher3_tools.CR2W import animated_component as ac
    from witcher3_tools.CR2W.CR2W_file import read_CR2W

    scene = bpy.context.scene
    prefs = addon._get_prefs(bpy.context)
    from witcher3_tools.extension_paths import get_dev_override
    real_uncook = str(getattr(prefs, "uncook_path", "") or "")
    if not (Path(real_uncook) / "characters" / "base_entities" / "man_base" / "man_base.w2rig").is_file():
        real_uncook = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    print(f"UNCOOK IN USE: {real_uncook or '<none>'}")
    tmp = Path(tempfile.mkdtemp(prefix="w3tb_entity_builder_"))
    prefs.uncook_path = str(tmp)
    assert Path(eb._resolve_export_dir(bpy.context)) == tmp, "export dir must be the temp folder (no REDkit project)"

    # Trajectory entity
    scene.witcher_ac_entity_path = r"animations\cutscenes\blender_tools\test_props.w2ent"
    assert bpy.ops.witcher.create_builder_entity(kind='TRAJECTORY') == {'FINISHED'}
    root = eb.active_builder_root(bpy.context)
    assert root is not None and root.get(eb.P_KIND) == eb.KIND_TRAJECTORY
    arm = eb.root_armature(root)
    assert arm is not None and arm.parent is root
    assert len(arm.data.bones) == ac.TRAJECTORY_BONE_COUNT + 1 and "Trajectory24" in arm.data.bones
    assert arm.get(eb.P_PATH) == ac.TRAJECTORY_RIG_PATH and arm.get(eb.P_BEHAVIOR) == ac.CUTSCENE_BEHAVIOR_PATH

    for bad in (r"..\outside.w2rig", r"E:\abs\man_base.w2rig", r"\\server\share\x.w2rig", r"a\.\b.w2rig"):
        assert eb._norm_repo_path(bad) == "", bad
    assert eb._norm_repo_path("/characters\\base_entities/man_base\\") == r"characters\base_entities\man_base"

    sword = new_mesh("sword", r"items\weapons\swords\test_sword.w2mesh")
    select_only(arm, sword)
    scene.witcher_ac_target_slot = "Trajectory03"
    assert bpy.ops.witcher.add_hard_attachment() == {'FINISHED'}
    attachments = eb.iter_hard_attachments(arm)
    assert len(attachments) == 1 and attachments[0][2] == "Trajectory03" and attachments[0][1] is sword

    select_only(sword)
    assert eb.active_builder_root(bpy.context) is root
    assert bpy.ops.witcher.export_builder_entity() == {'FINISHED'}
    out = tmp / "animations" / "cutscenes" / "blender_tools" / "test_props.w2ent"
    assert out.is_file()
    names = [e.name for e in read_CR2W(str(out)).CR2WExport]
    assert names == ["CEntityTemplate", "CEntity", "CAnimatedComponent",
                     "CHardAttachment", "CSkeletonBoneSlot", "CMeshComponent"], names

    # Static entity
    crate = new_mesh("crate", r"environment\decorations\test\crate.w2mesh", via_settings=True)
    crate.matrix_world = Matrix.Translation((1.0, 2.0, 3.0)) @ Matrix.Rotation(math.radians(90.0), 4, 'Z')
    nopath = new_mesh("nopath")
    select_only(crate, nopath)
    scene.witcher_ac_custom_entity_path = r"blender_tools\entities\test_static"
    assert bpy.ops.witcher.create_builder_entity(kind='STATIC') == {'FINISHED'}
    assert scene.witcher_ac_custom_entity_path.endswith(".w2ent")
    sroot = eb.active_builder_root(bpy.context)
    assert sroot is not None and sroot is not root and sroot.get(eb.P_KIND) == eb.KIND_STATIC
    assert eb.root_armature(sroot) is None
    assert crate.parent is sroot and nopath.parent is None
    statics, skipped = eb.collect_static_mesh_export_data(sroot)
    assert not skipped and len(statics) == 1
    t = statics[0]["transform"]
    assert abs(t["X"] - 1.0) < 1e-5 and abs(t["Y"] - 2.0) < 1e-5 and abs(t["Z"] - 3.0) < 1e-5, t
    assert abs(t["Yaw"] - 90.0) < 1e-4 and abs(t["Pitch"]) < 1e-4 and abs(t["Roll"]) < 1e-4, t

    # Engine mapping round-trip: Y(roll)·X(pitch)·Z(yaw) == Blender 'YXZ' with x=pitch, y=roll, z=yaw.
    from mathutils import Euler
    tilted = new_mesh("tilted", r"environment\decorations\test\tilted.w2mesh")
    tilted.matrix_world = Euler((math.radians(20.0), math.radians(10.0), math.radians(30.0)), 'YXZ').to_matrix().to_4x4()
    select_only(sroot, tilted)
    assert bpy.ops.witcher.add_static_mesh_component() == {'FINISHED'}
    t2 = next(s for s in eb.collect_static_mesh_export_data(sroot)[0] if s["mesh"].endswith("tilted.w2mesh"))["transform"]
    assert abs(t2["Pitch"] - 20.0) < 1e-3 and abs(t2["Roll"] - 10.0) < 1e-3 and abs(t2["Yaw"] - 30.0) < 1e-3, t2

    select_only(sroot)
    assert bpy.ops.witcher.export_builder_entity() == {'FINISHED'}
    sout = tmp / "blender_tools" / "entities" / "test_static.w2ent"
    parsed = read_CR2W(str(sout))
    names = [e.name for e in parsed.CR2WExport]
    assert names == ["CEntityTemplate", "CEntity", "CStaticMeshComponent", "CStaticMeshComponent"], names
    crate_chunk = parsed.CHUNKS.CHUNKS[2]
    transform_prop = next(p for p in crate_chunk.PROPS if p.theName == "transform")
    assert abs(transform_prop.EngineTransform.Yaw - 90.0) < 1e-4

    world_before = crate.matrix_world.copy()
    assert bpy.ops.witcher.remove_static_mesh_component(mesh_name=crate.name) == {'FINISHED'}
    assert crate.parent is None and close(crate.matrix_world, world_before)
    assert not eb.iter_static_meshes(sroot) or all(m is not crate for m in eb.iter_static_meshes(sroot))

    # Rigged entity
    rig_rel = r"characters\base_entities\man_base\man_base.w2rig"
    if real_uncook and (Path(real_uncook) / rig_rel).is_file():
        prefs.uncook_path = real_uncook
        scene.witcher_ac_skeleton_path = rig_rel
        scene.witcher_ac_custom_entity_path = r"blender_tools\entities\test_rigged.w2ent"
        select_only(sroot)
        assert bpy.ops.witcher.create_builder_entity(kind='ANIMATED') == {'FINISHED'}
        prefs.uncook_path = str(tmp)
        rroot = eb.active_builder_root(bpy.context)
        rarm = eb.root_armature(rroot)
        assert rroot.get(eb.P_KIND) == eb.KIND_ANIMATED and rarm is not None and rarm.parent is rroot
        assert rarm.get(eb.P_PATH) == rig_rel and len(rarm.data.bones) > 24
        assert "torso3" in rarm.data.bones, [b.name for b in rarm.data.bones][:8]
        hand = new_mesh("hand_prop", r"items\weapons\swords\test_sword.w2mesh", via_settings=True)
        select_only(rarm, hand)
        scene.witcher_ac_target_slot = "torso3"
        assert bpy.ops.witcher.add_hard_attachment() == {'FINISHED'}
        atts, skipped = eb.collect_hard_attachment_export_data(rarm)
        assert not skipped and atts[0]["slot"] == "torso3"
        # CSkeleton order, not Blender's tree order (they differ for most man_base bones).
        order = [b.name for b in rarm.data.witcherui_RigSettings.bone_order_list]
        assert atts[0]["bone_index"] == order.index("torso3")
        assert eb._bone_index(rarm, "r_hand") == order.index("r_hand")
        assert order.index("r_hand") != [b.name for b in rarm.data.bones].index("r_hand"), "expected orders to differ"
        select_only(rroot)
        assert bpy.ops.witcher.export_builder_entity() == {'FINISHED'}
        rout = tmp / "blender_tools" / "entities" / "test_rigged.w2ent"
        rparsed = read_CR2W(str(rout))
        assert [e.name for e in rparsed.CR2WExport][2] == "CAnimatedComponent"
        print(f"RIGGED OK: {len(rarm.data.bones)} bones from {rig_rel}")
    else:
        print("SKIP rigged: uncook has no man_base.w2rig")

    # Imported entities stay read-only
    imported = bpy.data.objects.new("imported_root", None)
    imported["witcher_entity_root"] = True
    scene.collection.objects.link(imported)
    select_only(imported)
    assert eb.active_builder_root(bpy.context) is None
    assert not bpy.ops.witcher.export_builder_entity.poll()

    # Panel draw smoke tests
    calls = draw_panels()
    ops = [c[1] for c in calls if c[0] == "op"]
    assert ops.count("witcher.create_builder_entity") == 2, ops
    assert any("Imported entities" in c[1] for c in calls if c[0] == "label"), calls
    scene.witcher_ac_kind = eb.KIND_STATIC
    for o in bpy.data.objects:
        o.select_set(False)
    bpy.context.view_layer.objects.active = None
    calls = draw_panels()
    assert any("Select an entity" in c[1] for c in calls if c[0] == "label"), calls
    assert ("prop", "witcher_ac_custom_entity_path") in calls
    for entity_root, expect_static_box in ((root, False), (sroot, True)):
        select_only(entity_root)
        calls = draw_panels()
        ops = [c[1] for c in calls if c[0] == "op"]
        assert "witcher.export_builder_entity" in ops, ops
        assert ("witcher.add_static_mesh_component" in ops) == expect_static_box, (entity_root.name, ops)
        assert ("witcher.add_hard_attachment" in ops) == (eb.root_armature(entity_root) is not None), ops
        assert "witcher.create_builder_entity" not in ops

    print("W3TB_ENTITY_BUILDER_NATIVE_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_ENTITY_BUILDER_NATIVE_FAIL")
    sys.exit(1)
finally:
    addon.unregister()
