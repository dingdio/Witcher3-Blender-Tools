"""Run with Blender 4.5+:
  blender --background --factory-startup --python tests/blender_player_sword_native.py
"""

import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon  # noqa: E402

DT = 1.0 / 60.0
SETTINGS = SimpleNamespace(ground=True, camera_collision=False, camera_distance=3.5, camera_height=1.6, follow=False)


def run(sess, seconds, forward=0.0, right=0.0):
    for _ in range(int(round(seconds / DT))):
        sess.step(DT, sess.move_vector(forward, right), 1.0, 0.15)
    sess.apply()
    bpy.context.view_layer.update()


def session_for(arm):
    bpy.context.view_layer.objects.active = arm
    started = time.perf_counter()
    sess = Session(arm, SETTINGS)
    sess.start()
    print(f"SESSION {arm.name} {time.perf_counter() - started:.1f}s grab {sess.moves.grab if sess.moves else None}")
    return sess


def draw_mounts_at_the_grab(sess, category):
    """Check the sword mounts near the hand at the grip time."""
    weapon = sess.weapons[category]
    sword = weapon["objects"][0]
    _, _, inverse, basis = next(s for s in sess._weapon_saved if s[0] == sword)
    assert sess.toggle_weapon(category), category
    for _ in range(int(0.35 / DT)):
        sess.step(DT, (0.0, 0.0), 1.0, 0.15)
        if sword.parent == weapon["hand"]:
            break
    sess.apply()
    bpy.context.view_layer.update()
    hilt = (weapon["back"].matrix_world @ inverse @ basis).translation
    gap = (weapon["hand"].matrix_world.translation - hilt).length
    print(f"MOUNT {category} at {sess.controller.active.time:.2f}s, hand {gap * 100:.0f} cm from the hilt")
    assert sword.parent == weapon["hand"] and sess.drawn == category, f"{category} not in the hand 0.35 s into the draw"
    assert gap < 0.25, f"hand {gap:.2f} m from the hilt when the sword mounted"
    assert sess.moves.grab[category] < 0.3, sess.moves.grab


addon.register()
try:
    from witcher3_tools.controller import clips, core
    from witcher3_tools.controller.session import Session, player_armature
    from witcher3_tools.extension_paths import get_dev_override

    prefs = addon._get_prefs(bpy.context)
    if not (Path(prefs.uncook_path or "") / "gameplay/templates/characters/player/player.w2ent").is_file():
        prefs.uncook_path = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    if "Cube" in bpy.data.objects:
        bpy.data.objects.remove(bpy.data.objects["Cube"])
    bpy.ops.mesh.primitive_plane_add(size=400.0)

    # Ciri: one sword
    assert bpy.ops.witcher.import_ciri() == {'FINISHED'}
    ciri = player_armature(bpy.context)
    ciri.location = (-20.0, 0.0, 0.0)
    saved_pose = [(pb.rotation_mode, tuple(pb.scale)) for pb in ciri.pose.bones]
    saved_action = ciri.animation_data.action if ciri.animation_data else None
    original_measure = Session._measure_grabs

    def fail_measure(_):
        raise RuntimeError("grab measurement failed")

    try:
        Session._measure_grabs = fail_measure
        failed = Session(ciri, SETTINGS)
        try:
            failed.start()
        except RuntimeError as exc:
            assert str(exc) == "grab measurement failed"
        else:
            raise AssertionError("failed startup did not raise")
        assert failed._saved is None, "failed startup did not restore"
        assert (ciri.animation_data.action if ciri.animation_data else None) == saved_action
        assert [(pb.rotation_mode, tuple(pb.scale)) for pb in ciri.pose.bones] == saved_pose
    finally:
        Session._measure_grabs = original_measure

    sess = session_for(ciri)
    moves = sess.moves
    assert list(sess.weapons) == ["steelsword"] and sess.can_draw("steelsword") and not sess.can_draw("silversword")
    assert moves.combat is not None and moves.combat.loops.get("run"), "no combat locomotion"
    assert moves.attacks.get("rp") and moves.attacks.get("lp"), moves.attacks
    assert all("woman_ciri" in c.name for v in moves.attacks.values() for c in v), "Ciri should use her own attacks"
    ctl = sess.controller
    sword = sess.weapons["steelsword"]["objects"][0]
    back = sess.weapons["steelsword"]["back"]
    assert not sess.toggle_weapon("silversword"), "drew a silver sword Ciri does not have"
    draw_mounts_at_the_grab(sess, "steelsword")
    grip = (sword.matrix_world.translation - ciri.matrix_world @ ciri.pose.bones["r_weapon"].head).length
    assert grip < 0.15, grip
    run(sess, 2.0)
    assert ctl.state == core.IDLE and ctl.profile.name == "combat" and ctl.clip_name.startswith("combat_"), ctl.clip_name

    y0 = ctl.y
    assert sess.attack() and ctl.action == "attack"
    first = ctl.active.dominant.name
    run(sess, 0.4)
    assert sess.attack(), "second click in AllowInput not queued"
    run(sess, 0.3)
    second = ctl.active.dominant.name
    print("COMBO", first, "->", second, f"travel {ctl.y - y0:.2f} m")
    assert first.endswith("_rp_40ms") and second.endswith("_lp_40ms"), (first, second)
    run(sess, 2.0)
    assert ctl.state == core.IDLE and ctl.action == "", (ctl.state, ctl.action)
    run(sess, 2.0, forward=1.0)
    assert ctl.state == core.LOOP and ctl.clip_name.startswith("combat_locomotion_run"), ctl.clip_name
    run(sess, 3.0)
    for c in (moves.draw["steelsword"], moves.sheathe["steelsword"], *moves.attacks["rp"]):
        for t in (0.0, c.length * 0.5, c.length):
            clips.apply(ciri, *clips.sample(c, t))
            bpy.context.view_layer.update()
            assert ciri.pose.bones["pelvis"].head.xy.length < 0.6, f"{c.name} not in place at {t:.2f}s"
    sess.apply()
    assert sess.toggle_weapon("steelsword") and ctl.action == "sheathe:steelsword"
    run(sess, 2.5)
    assert sword.parent == back and not sess.drawn and ctl.profile.name == "humanoid", (sword.parent, sess.drawn)
    sess.toggle_weapon()
    run(sess, 1.5)
    assert sess.drawn == "steelsword"
    sess.restore()
    assert sword.parent == back, "restore left the sword in the hand"

    # Geralt: steel and silver
    assert bpy.ops.witcher.import_geralt(inventory_preset_id="shipped_witcher_wolf_enhanced") == {'FINISHED'}
    geralt = player_armature(bpy.context)
    assert geralt != ciri
    geralt.location = (20.0, 0.0, 0.0)
    sess = session_for(geralt)
    assert set(sess.weapons) == {"steelsword", "silversword"} and sess.can_draw("silversword"), (list(sess.weapons), sess.moves.draw)
    assert all("man_geralt" in c.name for v in sess.moves.attacks.values() for c in v), "Geralt should use his own attacks"
    steel, silver = (sess.weapons[c]["objects"][0] for c in ("steelsword", "silversword"))
    homes = {o: o.parent for o in (steel, silver)}
    ctl = sess.controller
    draw_mounts_at_the_grab(sess, "silversword")
    assert steel.parent == homes[steel], "steel left its slot when silver was drawn"
    run(sess, 1.5)
    assert ctl.state == core.IDLE and sess.drawn == "silversword", (ctl.state, sess.drawn)
    assert sess.toggle_weapon("steelsword"), "swap to steel refused"
    run(sess, 1.2)                                    # between silver sheathe and steel draw
    assert silver.parent == homes[silver] and steel.parent == homes[steel], "silver not sheathed before steel is drawn"
    run(sess, 2.0)
    print("SWAP", ctl.state, sess.drawn, steel.parent.name, silver.parent.name)
    assert sess.drawn == "steelsword" and steel.parent == sess.weapons["steelsword"]["hand"], (sess.drawn, steel.parent)
    assert sess.attack() and ctl.action == "attack"
    run(sess, 2.0)
    sess.restore()
    assert steel.parent == homes[steel] and silver.parent == homes[silver], "restore left a sword in the hand"
    print("W3TB_PLAYER_SWORD_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_PLAYER_SWORD_FAIL")
    sys.exit(1)
