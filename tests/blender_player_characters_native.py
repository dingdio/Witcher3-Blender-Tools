"""Run with Blender 4.5+:
  blender --background --factory-startup --python tests/blender_player_characters_native.py
"""

import math
import re
import os
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

import bpy
from mathutils import Vector

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon  # noqa: E402

DT = 1.0 / 60.0
ENTITIES = {
    "ciri": None,
    "wild_hunt": r"characters\npc_entities\crowd_npc\wild_hunt_lvl3\wild_hunt_lvl3.w2ent",
    "ghoul": r"characters\npc_entities\monsters\ghoul_lvl1.w2ent",
    "nekker": r"characters\npc_entities\monsters\nekker_lvl1.w2ent",
}


def run(sess, seconds, forward=0.0, right=0.0, speed=1.0):
    for _ in range(int(round(seconds / DT))):
        sess.step(DT, sess.move_vector(forward, right), speed, 0.15)
    sess.apply()


def body_bone(arm):
    root = arm.data.bones["Root"]
    return max((c for c in root.children if c.name not in {"Trajectory", "Reference"} and not c.name.startswith("IK_")),
               key=lambda b: len(b.children_recursive)).name


addon.register()
try:
    from witcher3_tools.controller import clips, core
    from witcher3_tools.controller.session import Session, player_armature
    from witcher3_tools.extension_paths import get_dev_override
    from witcher3_tools.importers import import_entity
    from witcher3_tools.repo_paths import repo_file_for_source

    prefs = addon._get_prefs(bpy.context)
    if not (Path(prefs.uncook_path or "") / "gameplay/templates/characters/player/player.w2ent").is_file():
        prefs.uncook_path = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    if "Cube" in bpy.data.objects:
        bpy.data.objects.remove(bpy.data.objects["Cube"])

    rigs = {}
    for i, (key, rel) in enumerate(ENTITIES.items()):
        before = set(bpy.data.objects)
        if rel is None:
            assert bpy.ops.witcher.import_ciri() == {'FINISHED'}
        else:
            import_entity.import_entity_file(repo_file_for_source(rel, "w3"), import_apperance=1)
        new = [o for o in bpy.data.objects if o not in before]
        bpy.context.view_layer.objects.active = next(o for o in new if o.type == 'ARMATURE')
        arm = player_armature(bpy.context)
        assert arm is not None and arm.parent is None, key
        arm.location = (i * 30.0, 0.0, 0.0)
        rigs[key] = arm
    bpy.ops.mesh.primitive_plane_add(size=400.0)
    bpy.context.view_layer.update()

    settings = SimpleNamespace(ground=True, camera_collision=True, camera_distance=3.5, camera_height=1.6, follow=False)
    for key, arm in rigs.items():
        bpy.context.view_layer.objects.active = arm
        started = time.perf_counter()
        sess = Session(arm, settings)
        p = sess.profile
        print(f"== {key} {p.name} {time.perf_counter() - started:.1f}s size {sess.size:.2f} idle {p.idle.name}")
        print("   LOOPS", {g: [c.name if c else None for c in l] + [round(l[0].speed, 2)] for g, l in p.loops.items()})
        print("   STARTS", {g: sorted(round(math.degrees(c.turn)) for c in l) for g, l in p.starts.items()})
        print("   STOPS", {g: sorted(c.name for c in s.values()) for g, s in p.stops.items()})
        expected = "monster" if key in ("ghoul", "nekker") else "humanoid"
        assert p.name == expected, (key, p.name)
        if key == "ciri":
            assert abs(p.loops["walk"][0].length - 0.867) < 0.01, f"Ciri walks with {p.loops['walk'][0].length:.3f}s: Geralt's clips"
        assert {"walk", "run"} <= set(p.loops), (key, list(p.loops))
        assert p.starts and any(len(s) >= 3 for s in p.starts.values()), (key, p.starts)
        if expected == "monster":
            assert any(l[1] or l[2] for l in p.loops.values()), f"{key}: no turning side cycles"
            names = [c.name for cs in p.starts.values() for c in cs]
            assert all(re.search(r"walk|run", n) for n in names), f"{key}: non-locomotion starts {names}"

        body = body_bone(arm)
        rest = arm.data.bones[body].head_local.xy
        for pb in arm.pose.bones:
            pb.rotation_mode = 'QUATERNION'
        drift = []
        for c in {id(c): c for c in p.clips}.values():
            for t in (0.0, c.length * 0.5, c.length):
                clips.apply(arm, *clips.sample(c, t))
                bpy.context.view_layer.update()
                drift.append((arm.pose.bones[body].head.xy.length - rest.length, c.name, t))
        worst = max(drift)
        print(f"   IN PLACE worst {worst[0]:.2f} m ({worst[1]} at {worst[2]:.2f}s)")
        assert worst[0] < 0.5, f"{key}: {worst[1]} not in place"
        for g, cs in p.starts.items():
            assert all(math.hypot(*(c.root_pos[-1, :2] - c.root_pos[0, :2])) > 0.2 for c in cs), f"{key}: {g} start without travel"

        sess.start()
        ctl = sess.controller
        x0, y0 = ctl.x, ctl.y
        run(sess, 0.5)
        assert ctl.state == core.IDLE, (key, ctl.state)
        run(sess, 3.0, forward=1.0)
        assert ctl.state == core.LOOP and ctl.y - y0 > 2.0 and abs(ctl.x - x0) < 1.0, (key, ctl.state, ctl.x - x0, ctl.y - y0)
        run(sess, 4.0)
        assert ctl.state == core.IDLE, (key, ctl.state)
        run(sess, 2.5, right=-1.0)
        assert abs(math.degrees(ctl.yaw) - 90.0) < 15.0, (key, math.degrees(ctl.yaw))
        pts = []
        for fwd, right in ((1, 0), (0, 1), (-1, 0)):
            for _ in range(int(1.0 / DT)):
                sess.step(DT, sess.move_vector(fwd, right), 1.0, 0.15)
                sess.apply()
                bpy.context.view_layer.update()
                pts.append(Vector(arm.pose.bones[body].head))
        worst = max((pts[i + 1] - 2 * pts[i] + pts[i - 1]).length for i in range(1, len(pts) - 1))
        print(f"   TURNS pose acc {worst * 1000:.1f} mm")
        assert worst < 0.06 * sess.size, f"{key}: pose jumps {worst * 1000:.0f} mm in a tick"
        sess.restore()
    print("W3TB_PLAYER_CHARACTERS_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_PLAYER_CHARACTERS_FAIL")
    sys.exit(1)
