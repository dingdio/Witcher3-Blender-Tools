"""Run with Blender 4.5+:
  blender --background --factory-startup --python tests/blender_player_sound_native.py
"""

import gzip
import json
import math
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


def run(sess, seconds, forward=0.0, speed=1.0):
    for _ in range(int(round(seconds / DT))):
        sess.step(DT, sess.move_vector(forward, 0.0), speed, 0.15)


addon.register()
try:
    from witcher3_tools.controller.session import Session, _yaw_of, player_armature
    from witcher3_tools.controller.sfx_assets import GROUND, H
    from witcher3_tools.extension_paths import get_dev_override

    prefs = addon._get_prefs(bpy.context)
    if not (Path(prefs.uncook_path or "") / "gameplay/templates/characters/player/player.w2ent").is_file():
        prefs.uncook_path = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    if "Cube" in bpy.data.objects:
        bpy.data.objects.remove(bpy.data.objects["Cube"])
    before = set(bpy.data.objects)
    assert bpy.ops.witcher.import_ciri() == {'FINISHED'}
    bpy.context.view_layer.objects.active = next(o for o in bpy.data.objects if o not in before and o.type == 'ARMATURE')
    arm = player_armature(bpy.context)
    yaw = _yaw_of(arm)
    fwd = Vector((-math.sin(yaw), math.cos(yaw), 0.0))
    origin = arm.matrix_world.translation.copy()
    bpy.ops.mesh.primitive_plane_add(size=400.0, location=origin - fwd * 198.5, rotation=(0.0, 0.0, yaw))
    bpy.context.active_object["witcher_physical_material"] = "stone_solid"
    bpy.ops.mesh.primitive_plane_add(size=400.0, location=origin + fwd * 201.5, rotation=(0.0, 0.0, yaw))
    bpy.context.active_object.data.materials.append(bpy.data.materials.new("grass_meadow_01"))
    bpy.context.view_layer.objects.active = arm

    settings = SimpleNamespace(ground=True, camera_collision=True, camera_distance=3.5, camera_height=1.6, follow=False,
                               sound=True, sound_volume=0.8)
    started = time.perf_counter()
    sess = Session(arm, settings)
    print(f"session with sound {time.perf_counter() - started:.1f}s: {sess.sound_info}")
    assert sess.sound is not None, "sound did not start"
    sess.start()
    ctl = sess.controller
    run(sess, 0.3)
    steps = []
    for _ in range(int(3.0 / DT)):
        count = len(sess.sound.played)
        sess.step(DT, sess.move_vector(1.0, 0.0), 0.6, 0.15)
        along = (Vector((ctl.x, ctl.y, 0.0)) - origin.to_3d()).dot(fwd)
        steps += [(along, sess.surfaces.last, e, w) for e, k, w in sess.sound.played[count:] if k == "footsteps"]
    run(sess, 1.5)
    for step in steps:
        print("STEP %.2f %s %s %s" % step)
    stone = [s for s in steps if s[0] < 1.3]
    grass = [s for s in steps if s[0] > 1.7]
    assert stone and grass, steps
    assert all(s[1] == "stone_solid" for s in stone) and all(s[1] == "grass_short" for s in grass), steps
    for along, name, event, wems in stone + grass:
        pool = sess.sound.graph.sources(H(event), {**sess.sound.switches, GROUND: H(name)})
        assert wems and set(wems) <= set(pool), (along, name, event, wems, pool)
    print("SURFACE", sess._sound_status())
    assert sess.toggle_weapon("steelsword")
    run(sess, 1.5)
    assert sess.attack()
    run(sess, 0.4)
    assert sess.attack()
    run(sess, 2.0)
    player = sess.sound
    played = player.played
    for event, kind, wems in played:
        print("PLAYED", event, kind, wems)

    names = {int(k): v["short_name"] for k, v in json.load(gzip.open(
        REPO_ROOT / "witcher3_tools/CR2W/data/soundbanksinfo.json.gz"))["streamed_files_by_id"].items()}
    walks = [w for e, _, ws in played if e == "w_footstep_walk" for w in ws]
    assert len(walks) >= 5 and len(set(walks)) >= 3, f"walk footsteps {walks}"
    grunt_plays = [ws for e, _, ws in played if e.startswith("grunt_vo_attack")]
    assert len(grunt_plays) >= 2, played                     # probabilistic grunts
    pool = player.graph.sources(H("grunt_vo_attack_short"), player.switches)
    ciri = [[names.get(w, "") for w in ws] for ws in grunt_plays + [pool]]
    assert pool and all(n.startswith("grunt\\ciri\\") for ns in ciri for n in ns), ciri
    swings = [[names.get(w, "") for w in ws] for e, _, ws in played if e == "cmb_weapon_swoosh_light_fast"]
    assert len(swings) == 2 and all(ns and all("sword_whoosh_light_fast" in n for n in ns) for ns in swings), swings
    assert all(wems for e, _, wems in played if not e.startswith("grunt_vo")), [e for e, _, w in played if not w]
    sess.restore()
    assert all(not live for live in player.handles.values()), "handles left after restore"
    print("W3TB_PLAYER_SOUND_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_PLAYER_SOUND_FAIL")
    sys.exit(1)
