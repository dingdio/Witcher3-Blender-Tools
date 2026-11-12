"""Retarget a Mixamo walk, replace a workspace clip, and check the round-trip.

  blender --background --factory-startup --python tests/blender_mixamo_walk_replace_native.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import traceback
import types
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
import witcher3_tools as addon  # noqa: E402

FBX = REPO_ROOT / "DEV_STUFF" / "mixamo" / "Strut Walking.fbx"
DEPOT = Path(os.environ.get("W3_REDKIT_DEPOT", r"C:\w3.modding\The Witcher 3 REDkit\r4data"))
SET = Path("animations") / "man" / "movement" / "man_geralt_movement.w2anims"
CLIP = "locomotion_walk_cycle_fast_forward"

if not FBX.is_file() or not (DEPOT / SET).is_file():
    print("Strut Walking.fbx or the REDkit depot missing, skipped W3TB_MIXAMO_WALK_REPLACE_OK")
    sys.exit(0)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Layout:
    """Validate drawn properties and operators; collect labels."""

    def __init__(self, texts):
        self.texts = texts
        self.alert = self.enabled = True
        self.scale_y = 1.0

    def _child(self, *_, **__):
        return Layout(self.texts)

    row = column = box = split = _child

    def label(self, text="", **_):
        self.texts.append(text)

    def prop(self, data, name, **_):
        assert name in data.bl_rna.properties, f"{type(data).__name__}.{name} missing"

    def prop_search(self, data, name, search_data, search_name, **_):
        assert name in data.bl_rna.properties and search_name in search_data.bl_rna.properties, name

    def operator(self, idname, **kwargs):
        module, name = idname.split(".")
        getattr(getattr(bpy.ops, module), name).get_rna_type()
        self.texts.append(kwargs.get("text", idname))
        return types.SimpleNamespace()

    def template_list(self, list_type, _id, data, prop, active_data, active_prop, **_):
        assert hasattr(bpy.types, list_type) and prop in data.bl_rna.properties and active_prop in active_data.bl_rna.properties


def _under_depot(path):
    return os.path.normcase(os.path.abspath(path)).startswith(os.path.normcase(str(DEPOT)))


def draw_all(context):
    texts = []
    ui_motion.draw(Layout(texts), context)
    ui_inject.draw(Layout(texts), context)
    return texts


addon.register()
tmp = Path(tempfile.mkdtemp())
try:
    from witcher3_tools.animation import motion_extraction as mx, trajectory_tools as tt
    from witcher3_tools.controller.session import player_armature
    from witcher3_tools.CR2W import w2anims_inject as wi
    from witcher3_tools.extension_paths import get_dev_override
    from witcher3_tools.ui import ui_anims_list, ui_inject, ui_motion

    context = bpy.context
    scene = context.scene
    prefs = addon._get_prefs(context)
    rel_player = os.path.join("gameplay", "templates", "characters", "player", "player.w2ent")
    if not (Path(prefs.uncook_path or "") / rel_player).is_file():
        prefs.uncook_path = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    prefs.redkit_depot_path = str(DEPOT)
    project = tmp / "walk_project"
    (project / "workspace").mkdir(parents=True)
    if isinstance(getattr(prefs, "redkit_projects", ""), str):  # headless prefs stand-in
        prefs.redkit_projects = [types.SimpleNamespace(path=str(project))]
    else:
        prefs.redkit_projects.clear()
        prefs.redkit_projects.add().path = str(project)
    prefs.redkit_projects_index = 0
    depot_hash = sha(DEPOT / SET)

    if "Cube" in bpy.data.objects:
        bpy.data.objects.remove(bpy.data.objects["Cube"])
    assert bpy.ops.witcher.import_geralt(inventory_preset_id="__none__") == {'FINISHED'}
    arm = player_armature(context)
    bpy.ops.import_scene.fbx(filepath=str(FBX))
    source = next(o for o in scene.objects if o.type == 'ARMATURE' and any(b.name.startswith("mixamorig:") for b in o.data.bones))

    draw_all(context)
    r = scene.witcher_mixamo_retarget
    r.source, r.target = source, arm
    assert bpy.ops.witcher.retarget_auto_map() == {'FINISHED'}
    r.name, r.root_motion = "strut_walk", True
    assert bpy.ops.witcher.retarget_bake() == {'FINISHED'}
    action = arm.animation_data.action
    frames = tt.action_frames(action)
    for o in context.view_layer.objects:
        o.select_set(o is arm)
    context.view_layer.objects.active = arm

    with tt.using_action(arm, action):
        hips = tt.sample_bone(context, arm, "pelvis", frames)
    body_travel = (hips[-1][0] - hips[0][0]).to_2d()
    print(f"pelvis travels {body_travel.length:.3f} m over {len(frames)} frames")
    assert body_travel.length > 0.5, "the retargeted body keeps its walk travel"

    m = scene.witcher_motion
    m.path, m.rotation, m.source_bone = 'LINEAR', 'NONE', "pelvis"
    assert bpy.ops.witcher.motion_generate_trajectory('EXEC_DEFAULT') == {'FINISHED'}
    assert bpy.ops.witcher.motion_analyse() == {'FINISHED'}
    report = json.loads(action[ui_motion.REPORT_PROP])
    print("motion extraction:", {k: round(v, 3) if isinstance(v, float) else v for k, v in report.items()})
    assert report["keys"] == 2, "a straight constant-speed Trajectory compresses to two keys, like vanilla walks"
    assert report["flags"] & mx.FLAG_Y and not report["flags"] & mx.FLAG_YAW
    assert abs(report["distance"] - body_travel.length) < 0.02
    assert bpy.data.objects.get(ui_motion.PATH_OBJECT) is not None
    texts = draw_all(context)
    assert any(t.startswith("Travel 1.40 m") for t in texts), texts

    with tt.using_action(arm, action):
        traj = tt.sample_bone(context, arm, tt.TRAJECTORY, frames)
    offsets = [(h[0] - t[0]).to_2d() - (hips[0][0] - traj[0][0]).to_2d() for h, t in zip(hips, traj)]
    side, ahead = max(abs(o.x) for o in offsets), max(abs(o.y) for o in offsets)
    print(f"hips vs Trajectory: sideways {side:.3f} m, ahead/behind {ahead:.3f} m (vanilla walk: 0.04 / 0.05)")
    # Allow the strut's 20 cm hip sway.
    assert side < 0.3 and ahead < 0.3, "Trajectory follows the hips along the ground"
    assert max(abs(t[0].x - traj[0][0].x) for t in traj) < 0.01 and all(abs(t[0].z) < 1e-6 for t in traj)
    assert abs(offsets[-1].length) < 1e-3, "Trajectory ends under the hips"

    entry = scene.witcher_anim_export_set.add()
    # Moving replacements require motion extraction even when unchecked.
    entry.action_name, entry.enabled, entry.include_motion_extraction = action.name, True, False
    scene.witcher_anim_export_set_index = 0

    inj = scene.witcher_inject
    inj.query = "walk cycle fast forward"
    assert bpy.ops.witcher.inject_find_clip() == {'FINISHED'}
    assert inj.searched == "Searched the REDkit depot" and all(_under_depot(h.path) for h in inj.hits)
    inj.hit_index = next(i for i, h in enumerate(inj.hits) if h.name == CLIP and Path(h.path).name == SET.name)
    assert bpy.ops.witcher.inject_use_hit() == {'FINISHED'}
    assert entry.replace_clip == CLIP and len(inj.clips) > 200
    assert ui_inject.classify(context, inj.target)[0] == "DEPOT"
    texts = draw_all(context)
    assert "REDkit depot (read-only)" in texts and "Save Modified Copy" not in str(texts), texts
    assert not bpy.ops.witcher.inject_apply.poll() and not bpy.ops.witcher.inject_save_copy.poll(), \
        "nothing may be written into the REDkit depot"

    work = project / "workspace" / SET
    game = Path(prefs.uncook_path) / SET
    if game.is_file():
        # Reproduce a workspace containing cooked data.
        work.parent.mkdir(parents=True, exist_ok=True)
        for p in game.parent.glob(SET.name + "*"):
            shutil.copyfile(p, work.parent / p.name)
        inj.target = str(work)
        assert inj.external_buffers > 0 and not bpy.ops.witcher.inject_apply.poll()
        assert any(t.startswith("Cooked game set") for t in draw_all(context))
        assert bpy.ops.witcher.inject_rebuild_from_depot('EXEC_DEFAULT') == {'FINISHED'}
        assert work.read_bytes() == (DEPOT / SET).read_bytes() and not list(work.parent.glob("*.buffer"))
        rebuilt_backup = next((project / "blender_backups").glob("*"))
        assert len(list(rebuilt_backup.glob("*.buffer"))) == len(list(game.parent.glob(SET.name + ".*.buffer")))
        assert (rebuilt_backup / SET.name).read_bytes() == game.read_bytes()

        # Workspace copies must use the uncooked depot twin.
        shutil.rmtree(project / "workspace" / "animations")
        inj.target = str(game)
        assert ui_inject.classify(context, inj.target)[0] == "GAME"
        try:
            bpy.ops.witcher.inject_save_copy('EXEC_DEFAULT', filepath=str(work))
            raise AssertionError("a cooked copy was written into the workspace")
        except RuntimeError as exc:
            assert "inside REDkit" in str(exc) and not work.exists(), exc
        assert bpy.ops.witcher.inject_copy_to_workspace() == {'FINISHED'}
        assert work.read_bytes() == (DEPOT / SET).read_bytes()
        inj.target = str(DEPOT / SET)

    assert bpy.ops.witcher.inject_copy_to_workspace() == {'FINISHED'}
    assert Path(bpy.path.abspath(inj.target)) == work and work.read_bytes() == (DEPOT / SET).read_bytes()
    assert ui_inject.classify(context, inj.target)[0] == "WORKSPACE"
    texts = draw_all(context)
    assert "Edited action events replace these" in texts and "Workspace; backup before apply" in texts, texts
    before = {c["name"]: c for c in wi.describe_set(wi.RawCR2W(work.read_bytes()))}

    assert bpy.ops.witcher.inject_apply('EXEC_DEFAULT') == {'FINISHED'}
    assert sha(DEPOT / SET) == depot_hash, "REDkit depot untouched"
    backups = sorted((project / "blender_backups").glob("*/man_geralt_movement.w2anims"), key=lambda p: p.stat().st_mtime)
    assert backups and backups[-1].read_bytes() == (DEPOT / SET).read_bytes()
    raw = wi.RawCR2W(work.read_bytes())
    after = {c["name"]: c for c in wi.describe_set(raw)}
    assert list(after) == list(before), "clip order and names unchanged"
    new, old = after[CLIP], before[CLIP]
    assert (new["entry"], new["anim"], new["buffer"], new["events"]) == (old["entry"], old["anim"], old["buffer"], old["events"])
    assert abs(new["duration"] - (len(frames) - 1) / 30) < 1e-4
    assert new["me"] is not None and new["ume"] is not None
    assert list(work.parent.glob("*.buffer")) == [], "workspace sets keep buffers embedded"

    expected = []
    for f in frames[::10]:
        scene.frame_set(f)
        expected.append({b.name: b.matrix.copy() for b in arm.pose.bones})
    ui_anims_list.load_anim_into_scene(context, CLIP, str(work), arm, use_NLA=False)
    reloaded = arm.animation_data.action
    assert reloaded is not action and reloaded.name.startswith(CLIP)
    back = tt.action_frames(reloaded)
    assert len(back) == len(frames), (len(back), len(frames))
    worst, where = 0.0, None
    for i, pose in zip(range(0, len(frames), 10), expected):
        scene.frame_set(back[i])
        for name, mat in pose.items():
            delta = max(abs(v) for row in (arm.pose.bones[name].matrix - mat) for v in row)
            if delta > worst:
                worst, where = delta, (back[i], name)
    print(f"re-imported clip vs baked action: worst bone matrix delta {worst:.2e} at {where}")
    assert worst < 5e-3
    me_back = json.loads(reloaded.get("w3_motion_extraction_data", "{}") or "{}")
    print("re-imported motion extraction:", {k: v for k, v in me_back.items() if k != "frames"})
    print("W3TB_MIXAMO_WALK_REPLACE_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_MIXAMO_WALK_REPLACE_FAIL")
    sys.exit(1)
finally:
    shutil.rmtree(tmp, ignore_errors=True)
