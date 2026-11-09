"""Run with Blender 4.5+:
  blender --background --factory-startup --python tests/blender_player_native.py
"""

import math
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


def run(sess, seconds, forward=0.0, right=0.0, speed=1.0):
    for _ in range(int(round(seconds / DT))):
        sess.step(DT, sess.move_vector(forward, right), speed, 0.15)
    sess.apply()


addon.register()
try:
    from witcher3_tools.controller import core
    from witcher3_tools.controller.session import Session, player_armature
    from witcher3_tools.controller.world import ensure_ground
    from witcher3_tools.extension_paths import get_dev_override

    prefs = addon._get_prefs(bpy.context)
    rel_player = os.path.join("gameplay", "templates", "characters", "player", "player.w2ent")
    if not (Path(prefs.uncook_path or "") / rel_player).is_file():
        prefs.uncook_path = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    assert (Path(prefs.uncook_path) / rel_player).is_file(), "Configure uncook_path before running this test"

    if "Cube" in bpy.data.objects:
        bpy.data.objects.remove(bpy.data.objects["Cube"])
    assert bpy.ops.witcher.import_geralt(inventory_preset_id="__none__") == {'FINISHED'}
    arm = player_armature(bpy.context)
    assert arm is not None, "player rig not found"
    assert arm.parent is None and arm.data.bones[0].name == "Root", arm.name

    bpy.ops.mesh.primitive_plane_add(size=200.0, location=(0.0, 0.0, 0.0))
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(-30.0, 0.0, -0.1))
    bpy.context.active_object.scale = (1.0, 10.0, 1.0)
    bpy.ops.mesh.primitive_cube_add(size=2.0, location=(30.0, 0.0, 1.0))
    bpy.context.active_object.scale = (0.5, 20.0, 1.0)
    bpy.context.view_layer.objects.active = arm
    arm.select_set(True)
    bpy.context.view_layer.update()

    settings = SimpleNamespace(ground=True, camera_collision=True, camera_distance=3.5, camera_height=1.6, follow=False)
    saved_matrix = arm.matrix_basis.copy()
    started = time.perf_counter()
    sess = Session(arm, settings)
    print(f"PROFILE {time.perf_counter() - started:.2f}s world {sess.world.triangles} tris")
    profile = sess.profile
    assert arm.animation_data is None or arm.animation_data.action is None, "profile load left an action assigned"
    assert not [a for a in bpy.data.actions if a.name.startswith("locomotion_")], "sampled actions were not removed"
    named = {"forward": 0, "forwardleft": 45, "left": 90, "backleft": 135, "bwd_lft": 135, "back_ccw": 180,
             "forwardright": -45, "right": -90, "backright": -135, "bwd_rgt": -135, "back_cw": -180}
    for gait in core.GAITS:
        assert len(profile.starts.get(gait, [])) >= 7, (gait, [c.name for c in profile.starts.get(gait, [])])
        assert gait in profile.loops and gait in profile.stops, gait
        turns = {c.name.rsplit("_idle_to_", 1)[1]: round(math.degrees(c.turn)) for c in profile.starts[gait]}
        print("TURNS", gait, turns)
        assert all(abs(t - named[d]) < 10 for d, t in turns.items()), turns
    assert len(profile.stops["walk"]) == 4 and len(profile.stops["sprint"]) == 2, profile.stops
    for foot, stop in profile.stops["run"].items():
        travel = stop.root_pos[-1] - stop.root_pos[0]
        assert 0.4 < travel[1] < 1.6 and abs(travel[0]) < 0.1, (stop.name, travel)

    from witcher3_tools.controller import clips
    for pb in arm.pose.bones:
        pb.rotation_mode = 'QUATERNION'
    for c in {id(c): c for c in profile.clips}.values():
        for t in (0.0, c.length * 0.5, c.length):
            clips.apply(arm, *clips.sample(c, t))
            bpy.context.view_layer.update()
            assert arm.pose.bones["pelvis"].head.xy.length < 0.4, f"{c.name} not in place at {t:.2f}s"
    loop = profile.loops["run"][0]
    assert {n for n, _, _ in loop.windows} >= set(core.FEET), loop.windows
    assert loop.speed > 3.0, loop.speed
    print("SPEEDS", {g: round(profile.loops[g][0].speed, 2) for g in core.GAITS})
    rot = loop.poses["rot"]
    assert rot.shape[1] == len(arm.pose.bones), rot.shape

    sess.start()
    pelvis = arm.pose.bones["pelvis"]
    run(sess, 0.5)
    ctl = sess.controller
    assert ctl.state == core.IDLE and abs(ctl.y) < 0.05 and abs(ctl.z) < 1e-4, (ctl.state, ctl.y, ctl.z)

    run(sess, 3.0, forward=1.0)
    assert ctl.state == core.LOOP, ctl.state
    assert ctl.y > 6.0 and abs(ctl.x) < 0.5 and abs(math.degrees(ctl.yaw)) < 3.0, (ctl.x, ctl.y, math.degrees(ctl.yaw))
    bpy.context.view_layer.update()
    head = arm.matrix_world @ pelvis.head
    assert (head.xy - arm.matrix_world.translation.xy).length < 0.4, (tuple(head), tuple(arm.matrix_world.translation))
    assert 0.8 < head.z < 1.2, head.z

    for _ in range(int(0.2 / DT)):
        sess.step(DT, (0.0, 0.0), 1.0, 0.15)
    assert ctl.state == core.STOP and "_to_idle" in ctl.active.dominant.name, (ctl.state, ctl.clip_name)
    print("STOP", ctl.active.dominant.name)
    run(sess, 3.0)
    assert ctl.state == core.IDLE, ctl.state

    run(sess, 2.0, right=-1.0)
    assert abs(math.degrees(ctl.yaw) - 90.0) < 8.0, math.degrees(ctl.yaw)
    run(sess, 3.0)

    ctl.x, ctl.y, ctl.yaw = 26.0, 0.0, 0.0
    run(sess, 3.0, right=1.0)
    assert ctl.x < 29.6, f"walked through the wall: x={ctl.x:.2f}"
    run(sess, 3.0)

    ctl.x, ctl.y, ctl.yaw = -27.0, 0.0, 0.0
    sess.world.build(ctl.x, ctl.y)
    on_box = []
    for _ in range(int(2.0 / DT)):
        sess.step(DT, sess.move_vector(0.0, -1.0), 1.0, 0.15)
        if -30.3 < ctl.x < -29.7:
            on_box.append(ctl.z)
    assert on_box and abs(max(on_box) - 0.4) < 0.02, f"did not step onto the box: {on_box}"
    assert ctl.x < -31.0 and abs(ctl.z) < 0.02, f"did not come back down: x={ctl.x:.2f} z={ctl.z:.2f}"
    print("STEP", round(max(on_box), 3), round(ctl.z, 3))

    ctl.x, ctl.y, ctl.yaw = 0.0, -20.0, 0.0
    sess.world.build(ctl.x, ctl.y)
    run(sess, 3.0)
    pelvis_path = []
    for seconds, fwd, right in ((2.0, 1, 0), (1.0, 0, 1), (1.0, 0, -1), (1.0, -1, 0)):
        for _ in range(int(seconds / DT)):
            sess.step(DT, sess.move_vector(fwd, right), 1.5, 0.15)
            sess.apply()
            bpy.context.view_layer.update()
            pelvis_path.append(tuple(pelvis.head))
    p = [Vector(v) for v in pelvis_path]
    worst = max((p[i + 1] - 2 * p[i] + p[i - 1]).length for i in range(1, len(p) - 1))
    print("SPRINT TURNS pose acc", round(worst * 1000, 1), "mm")
    assert worst < 0.05, f"pose jumps {worst * 1000:.0f} mm in one tick while sprint-turning"
    run(sess, 3.0)

    region = SimpleNamespace(view_perspective='PERSP', view_location=arm.location.copy(),
                             view_rotation=arm.rotation_quaternion.copy(), view_distance=10.0)
    sess.view(region)
    assert region.view_distance <= settings.camera_distance + 1e-6

    sess.restore()
    assert all(abs(a - b) < 1e-5 for ra, rb in zip(arm.matrix_basis, saved_matrix) for a, b in zip(ra, rb)), "transform not restored"

    keep = {arm.name} | {o.name for o in arm.children_recursive}
    for obj in [o for o in bpy.data.objects if o.type == 'MESH' and o.name not in keep]:
        bpy.data.objects.remove(obj)
    arm.location = (300.0, -40.0, 0.0)
    bpy.context.view_layer.update()
    started = time.perf_counter()
    assert ensure_ground(bpy.context, arm), "reference ground not imported on an empty scene"
    print(f"REFERENCE GROUND {time.perf_counter() - started:.1f}s at", tuple(round(v, 2) for v in arm.matrix_world.translation))
    assert abs(arm.matrix_world.translation.z - 5.0) < 0.5, arm.matrix_world.translation
    assert not ensure_ground(bpy.context, arm), "reference ground imported twice"
    sess = Session(arm, settings)
    sess.start()
    run(sess, 2.0, forward=1.0)
    assert abs(sess.controller.z - 5.0) < 0.5 and sess.controller.y > -36.0, (sess.controller.y, sess.controller.z)
    sess.restore()
    print("W3TB_PLAYER_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_PLAYER_FAIL")
    sys.exit(1)
