import math
import sys
import traceback
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

OUT_DIR = REPO_ROOT / "WORKING_TEMP" / "e2e_dlc" / "dlc" / "modw3tools_e2e" / "data"
CS_REPO = "dlc\\modw3tools_e2e\\data\\cutscenes\\cs_e2e_dryrun.w2cutscene"
CS_PATH = OUT_DIR / "cutscenes" / "cs_e2e_dryrun.w2cutscene"
SCENE_PATH = OUT_DIR / "scenes" / "cs_e2e_dryrun.w2scene"

FPS = 30
NUM_FRAMES = 150  # 5 s

import witcher3_tools as addon

addon.register()
try:
    from witcher3_tools.extension_paths import get_dev_override

    prefs = addon._get_prefs(bpy.context)
    uncook_path = str(getattr(prefs, "uncook_path", "") or "")
    if not (Path(uncook_path) / "quests" / "main_npcs" / "cirilla.w2ent").is_file():
        uncook_path = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    prefs.uncook_path = uncook_path
    ciri_path = Path(prefs.uncook_path) / "quests" / "main_npcs" / "cirilla.w2ent"
    assert ciri_path.is_file(), "Configure uncook_path before running this test"

    from witcher3_tools.w3_casting import cast_actor
    from witcher3_tools.exporters import export_cutscene
    from witcher3_tools.CR2W import scene_builder
    from witcher3_tools.CR2W.CR2W_file import read_CR2W

    scene = bpy.context.scene
    scene.render.fps = FPS
    scene.frame_start = 0
    scene.frame_end = NUM_FRAMES - 1

    ciri, ciri_info = cast_actor("ciri", at=(0.0, 0.0, 0.0))
    drowner, drw_info = cast_actor("drowner", at=(0.0, 2.5, 0.0))
    print(f"E2E cast: {ciri_info['label']} + {drw_info['label']}")

    def find_armature(obj):
        if getattr(obj, "type", None) == 'ARMATURE':
            return obj
        stack = list(getattr(obj, "children", []) or [])
        while stack:
            child = stack.pop()
            if getattr(child, "type", None) == 'ARMATURE':
                return child
            stack.extend(getattr(child, "children", []) or [])
        return None

    def animate(armature, action_name, sway_bone_names):
        armature.animation_data_create()
        action = bpy.data.actions.new(action_name)
        armature.animation_data.action = action
        posed = []
        for name in sway_bone_names:
            bone = armature.pose.bones.get(name)
            if bone is not None:
                posed.append(bone)
        assert posed, f"none of {sway_bone_names} found on {armature.name}"
        for frame in range(0, NUM_FRAMES, 10):
            angle = 0.12 * math.sin(2.0 * math.pi * frame / float(NUM_FRAMES))
            for bone in posed:
                bone.rotation_mode = 'QUATERNION'
                bone.rotation_quaternion = (math.cos(angle / 2), 0.0, 0.0, math.sin(angle / 2))
                bone.keyframe_insert("rotation_quaternion", frame=frame)
        return action

    ciri_arm = find_armature(ciri)
    drw_arm = find_armature(drowner)
    assert ciri_arm is not None and drw_arm is not None, (ciri, drowner)
    animate(ciri_arm, "ciri_sway", ["torso", "torso2", "pelvis"])
    animate(drw_arm, "drowner_sway", ["torso", "torso2", "pelvis", "spine1"])

    CS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCENE_PATH.parent.mkdir(parents=True, exist_ok=True)

    result = export_cutscene.export_w3_cutscene(bpy.context, str(CS_PATH))
    assert CS_PATH.is_file(), f"cutscene export produced no file ({result})"
    print(f"E2E cutscene written: {CS_PATH} ({CS_PATH.stat().st_size} bytes)")

    duration = (NUM_FRAMES - 1) / float(FPS)
    scene_builder.save_cutscene_wrapper_scene(
        str(SCENE_PATH), CS_REPO, duration=duration,
        section_name="cs_e2e_dryrun",
    )
    assert SCENE_PATH.is_file()
    print(f"E2E wrapper written: {SCENE_PATH} ({SCENE_PATH.stat().st_size} bytes)")

    cs = read_CR2W(str(CS_PATH))
    cs_types = [exp.name for exp in cs.CR2WExport]
    assert "CCutsceneTemplate" in cs_types, cs_types
    anim_entries = [t for t in cs_types if t == "CSkeletalAnimationSetEntry"]
    assert len(anim_entries) >= 2, f"expected >=2 animation entries, got {cs_types}"

    wrapper = read_CR2W(str(SCENE_PATH))
    w_types = [exp.name for exp in wrapper.CR2WExport]
    assert "CStorySceneCutsceneSection" in w_types, w_types
    imports = [
        (getattr(imp, "path", None) or getattr(imp, "depotPath", None))
        for imp in (getattr(wrapper, "CR2WImport", []) or [])
    ]
    assert any(str(p or "").lower().endswith("cs_e2e_dryrun.w2cutscene") for p in imports), imports

    print(f"E2E parse-back OK: cutscene chunks={len(cs_types)} "
          f"(anim entries={len(anim_entries)}), wrapper chunks={len(w_types)}")
    print("W3TB_CUTSCENE_E2E_DRYRUN_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_CUTSCENE_E2E_DRYRUN_FAIL")
    sys.exit(1)
finally:
    addon.unregister()
