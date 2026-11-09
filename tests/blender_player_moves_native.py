"""Draw on the move, dodge / roll and the free camera on the real Geralt.

Run with Blender 4.5+:
  blender --background --factory-startup --python tests/blender_player_moves_native.py
"""

import math
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon  # noqa: E402

DT = 1.0 / 60.0
SETTINGS = SimpleNamespace(ground=True, camera_collision=False, camera_distance=3.5, camera_height=1.6, follow=False)


def run(sess, seconds, move=(0.0, 0.0), speed=1.0):
    for _ in range(int(round(seconds / DT))):
        sess.step(DT, move, speed, 0.15)
    sess.apply()
    bpy.context.view_layer.update()


def stick(degrees):
    """World stick direction; 0 is +Y, positive turns left."""
    a = math.radians(degrees)
    return (-math.sin(a), math.cos(a))


def heading(dx, dy):
    return math.degrees(math.atan2(-dx, dy))


addon.register()
try:
    from witcher3_tools.controller import core
    from witcher3_tools.controller.session import Session, player_armature
    from witcher3_tools.extension_paths import get_dev_override

    prefs = addon._get_prefs(bpy.context)
    if not (Path(prefs.uncook_path or "") / "gameplay/templates/characters/player/player.w2ent").is_file():
        prefs.uncook_path = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    if "Cube" in bpy.data.objects:
        bpy.data.objects.remove(bpy.data.objects["Cube"])
    bpy.ops.mesh.primitive_plane_add(size=400.0)
    assert bpy.ops.witcher.import_geralt(inventory_preset_id="shipped_witcher_wolf_enhanced") == {'FINISHED'}
    geralt = player_armature(bpy.context)
    bpy.context.view_layer.objects.active = geralt
    sess = Session(geralt, SETTINGS)
    sess.start()
    ctl, moves = sess.controller, sess.moves
    print("EVADES", {k: sorted((e.clip.name, e.direction, round(math.degrees(e.travel))) for e in v)
                     for k, v in moves.evades.items()})
    for kind in ("dodge", "roll"):
        found = {e.direction for e in moves.evades.get(kind, [])}
        assert {"forward", "back"} <= found, (kind, found)
    assert {"left", "right"} <= {e.direction for e in moves.evades["dodge"]}
    assert sess.upper_mask is not None and 0 < sess.upper_mask.sum() < len(geralt.data.bones)
    steel = sess.weapons["steelsword"]

    # Draw while running: locomotion carries on, the sword reaches the hand, the run turns into the combat run.
    assert not sess.evade("dodge"), "dodged sheathed (exploration has no dodge)"
    run(sess, 2.0, stick(0.0))
    assert ctl.state == core.LOOP, ctl.state
    y0, states = ctl.y, set()
    assert sess.sword_key("steelsword") and ctl.upper is not None, "draw while running was not an upper-body draw"
    for _ in range(int(1.2 / DT)):
        sess.step(DT, stick(0.0), 1.0, 0.15)
        states.add(ctl.state)
    sess.apply()
    bpy.context.view_layer.update()
    print("RUN DRAW", states, f"{ctl.y - y0:.2f} m", ctl.clip_name, steel["objects"][0].parent.name)
    assert states == {core.LOOP}, states
    assert ctl.y - y0 > 2.5, ctl.y - y0
    assert sess.drawn == "steelsword" and steel["objects"][0].parent == steel["hand"]
    run(sess, 0.5, stick(0.0))
    assert ctl.clip_name.startswith("combat_"), ctl.clip_name
    assert not sess.sword_key("steelsword"), "pressing the drawn sword's key drew again"
    run(sess, 3.0)
    assert ctl.state == core.IDLE, ctl.state

    # Dodges travel along the stick, whatever way Geralt faces.
    for rel in (0.0, 90.0, -90.0, 180.0, None):
        run(sess, 1.0)
        sess.step(DT, stick(math.degrees(ctl.yaw) + rel) if rel is not None else (0.0, 0.0), 1.0, 0.15)
        x0, y0, yaw0 = ctl.x, ctl.y, ctl.yaw
        assert sess.evade("dodge"), rel
        clip = ctl.active.dominant.name
        run(sess, 0.7, stick(math.degrees(yaw0) + rel) if rel is not None else (0.0, 0.0))
        dx, dy = ctl.x - x0, ctl.y - y0
        want = math.degrees(yaw0) + (rel if rel is not None else 180.0)
        err = abs((heading(dx, dy) - want + 180.0) % 360.0 - 180.0)
        print(f"DODGE {rel} {clip} {math.hypot(dx, dy):.2f} m, {err:.0f} deg off the stick")
        assert math.hypot(dx, dy) > 2.0 and err < 30.0, (rel, clip, dx, dy, err)
        run(sess, 1.5)

    # Rolls go further; a second roll waits for ActionBlend.
    run(sess, 1.0)
    sess.step(DT, stick(45.0), 1.0, 0.15)
    x0, y0 = ctl.x, ctl.y
    assert sess.evade("roll")
    first = ctl.active.dominant.name
    run(sess, 0.1, stick(45.0))
    assert sess.evade("roll") and ctl.queued is not None and ctl.active.dominant.name == first
    run(sess, 2.5, stick(45.0))
    print(f"ROLLS {first} {math.hypot(ctl.x - x0, ctl.y - y0):.2f} m")
    assert math.hypot(ctl.x - x0, ctl.y - y0) > 6.0
    run(sess, 2.0)

    # Holding 1 sheathes; a dodge mid-draw completes the grip.
    assert sess.sheathe("steelsword") and ctl.action == "sheathe:steelsword"
    run(sess, 2.5)
    assert not sess.drawn and steel["objects"][0].parent == steel["back"]
    assert sess.sword_key("steelsword")
    run(sess, 0.05)
    assert sess.evade("dodge") and sess.drawn == "steelsword" and steel["objects"][0].parent == steel["hand"]
    run(sess, 2.0)

    # Free camera: 2 m above Geralt, flies 25 m/s, teleports him back down to the ground.
    assert sess.toggle_freecam()
    cam = sess.freecam
    assert abs(cam.pos[2] - (ctl.z + 2.0)) < 1e-6
    cam.keys(1.0, 0.0, 0.0, False, 0.0)
    for _ in range(60):
        cam.tick(DT)
    cam.keys(0.0, 0.0, 0.0, False, 0.0)
    assert sess.teleport()
    run(sess, 2.0)
    print(f"TELEPORT {ctl.x:.2f} {ctl.y:.2f} {ctl.z:.2f}")
    assert abs(ctl.z) < 0.05 and math.hypot(ctl.x - cam.pos[0], ctl.y - cam.pos[1]) < 1.0
    assert not sess.toggle_freecam()
    sess.restore()
    assert steel["objects"][0].parent == steel["back"]
    print("W3TB_PLAYER_MOVES_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_PLAYER_MOVES_FAIL")
    sys.exit(1)
