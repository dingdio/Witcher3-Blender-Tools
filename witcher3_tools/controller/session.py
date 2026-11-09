"""Connect a player controller to the armature, sound and viewport."""
import logging
import math
import os
import time

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import clips, core, freecam
from .world import World

log = logging.getLogger(__name__)
CAMERA_MARGIN = 0.25           # clearance, metres
CAMERA_NEAREST = 0.4           # closest to pivot, metres
CAMERA_EASE = 3.0              # return rate per second
FOLLOW_CONE = math.radians(45.0)
_PROFILE_CACHE = {}


def _is_player_rig(obj) -> bool:
    rs = getattr(obj.data, "witcherui_RigSettings", None) if obj is not None and obj.type == 'ARMATURE' else None
    return (rs is not None and "Trajectory" in obj.data.bones
            and any((i.path or "").lower().endswith(".w2anims") for i in rs.animset_list))


def player_armature(context):
    """Find the active character rig, then fall back to a scene rig."""
    found, obj = None, context.active_object
    while obj is not None:
        if _is_player_rig(obj):
            found = obj
        obj = obj.parent
    if found is not None:
        return found
    rigs = [o for o in context.scene.objects if o.parent is None and _is_player_rig(o)]
    player = [o for o in rigs if (o.data.witcherui_RigSettings.repo_path or "").replace("/", "\\").lower().endswith("player.w2ent")]
    return (player or rigs or [None])[0]


def load_profile(arm) -> core.Profile:
    from . import profiles
    sets = profiles.entity_animsets(arm, profiles._SKIP_SETS)
    key = (arm.name, len(arm.data.bones), tuple((p, os.path.getmtime(p)) for p in sets))
    if key not in _PROFILE_CACHE:
        _PROFILE_CACHE.clear()
        _PROFILE_CACHE[key] = profiles.build(arm)
    return _PROFILE_CACHE[key]


_MOVES_CACHE = {}
WEAPONS = ("steelsword", "silversword")


def load_moves(arm):
    from . import profiles
    sets = profiles.entity_animsets(arm, profiles._SKIP_COMBAT_SETS)
    key = (arm.name, len(arm.data.bones), tuple((p, os.path.getmtime(p)) for p in sets))
    if key not in _MOVES_CACHE:
        _MOVES_CACHE.clear()
        _MOVES_CACHE[key] = profiles.build_moves(arm)
    return _MOVES_CACHE[key]


def find_weapons(arm) -> dict:
    """Find equipped swords and their back and hand slots."""
    rs = arm.data.witcherui_RigSettings
    empties = {o.get("witcher_slot_name"): o for o in bpy.data.objects
               if o.type == 'EMPTY' and o.get("witcher_owner_armature") == arm.name and o.get("witcher_slot_name")}
    out = {}
    for category in WEAPONS:
        slot = next((s for s in rs.equipment_slots if s.category == category and s.is_loaded and s.weapon), None)
        if slot is None:
            continue
        back, hand = empties.get(slot.equip_slot), empties.get(slot.hold_slot or "r_weapon")
        if back is None or hand is None:
            continue
        objects = ([o for o in back.children if o.get("witcher_equip_guid") == slot.equip_guid]
                   or [o for o in back.children if o.type in {'MESH', 'ARMATURE'}])
        if objects:
            out[category] = {"back": back, "hand": hand, "objects": objects}
    return out


def rig_extent(arm) -> float:
    """Largest rest extent of the skeleton."""
    points = np.array([p for b in arm.data.bones for p in (b.head_local, b.tail_local)] or [(0.0, 0.0, 1.8)])
    return float(max(points[:, 2].max(), *(points[:, :2].max(axis=0) - points[:, :2].min(axis=0))))


UPPER_BODY = "torso"


def upper_mask(arm):
    """Per-bone weights of the upper body (the `torso` chain and everything below it), or None."""
    top = arm.data.bones.get(UPPER_BODY)
    if top is None:
        return None
    return np.array([1.0 if b == top or top in b.parent_recursive else 0.0 for b in arm.data.bones])


def _yaw_of(obj) -> float:
    forward = obj.matrix_world.to_3x3() @ Vector((0.0, 1.0, 0.0))
    return math.atan2(-forward.x, forward.y)


class Session:
    def __init__(self, armature, settings):
        self.armature = armature
        self.settings = settings
        self._saved = None
        self._view = self._lens = None
        self.freecam = None
        self._save()
        try:
            self.profile = load_profile(armature)
        except Exception:
            self.restore()
            raise
        self.world = World(armature) if (getattr(settings, "ground", True) or getattr(settings, "camera_collision", True)) else None
        pos = armature.matrix_world.translation
        if self.world is not None:
            self.world.ensure(pos.x, pos.y)
        self._scale = armature.matrix_world.to_scale()
        self.controller = core.Controller(self.profile, self.world if getattr(settings, "ground", True) else None,
                                          pos, _yaw_of(armature))
        self.cam_yaw = self.controller.yaw
        self.cam_pitch = math.radians(12.0)
        self.size = min(max(rig_extent(armature) / 1.8, 0.5), 4.0)       # scale camera for creature size
        self.cam_distance = getattr(settings, "camera_distance", 3.5) * self.size
        self.tick_ms = 0.0
        self.weapons = find_weapons(armature)
        self.upper_mask = upper_mask(armature)
        self.moves = None
        self.drawn = ""                # held sword category
        self.stance, self.combo = "rp", 0
        self._evade_turn = {}
        if self.weapons:
            try:
                self.moves = load_moves(armature)
            except Exception:
                log.exception("player: sword moves not loaded for %s", armature.name)
        self._weapon_saved = [(o, o.parent, o.matrix_parent_inverse.copy(), o.matrix_basis.copy())
                              for w in self.weapons.values() for o in w["objects"]]
        self.sound_info = {}
        self.sound = self._open_sound() if getattr(settings, "sound", False) else None

    def _all_clips(self) -> list:
        out = list(self.profile.clips)
        if self.moves is not None:
            if self.moves.combat is not None:
                out += self.moves.combat.clips
            out += [*self.moves.draw.values(), *self.moves.sheathe.values(), *(c for cs in self.moves.attacks.values() for c in cs)]
        return out

    def _armour(self) -> str:
        """Use the torso's sound type, or another armour piece's."""
        meshes = [o.witcherui_MeshSettings for o in self.armature.children_recursive
                  if o.type == 'MESH' and getattr(o, "witcherui_MeshSettings", None) is not None]
        typed = [(m.soundInfo_soundBoneMappingInfo, m.soundInfo_soundTypeIdentification) for m in meshes
                 if m.soundInfo_soundTypeIdentification]
        return next((t for b, t in typed if b == 'TorsoArmor'), typed[0][1] if typed else "")

    def _open_sound(self):
        from .. import get_vgmstream_path
        from ..repo_paths import repo_file_for_source, source_game_for_rig_settings
        from . import sfx, sfx_assets, surface
        started = time.perf_counter()
        try:
            library = sfx_assets.game_library()
            rs = self.armature.data.witcherui_RigSettings
            template = repo_file_for_source(rs.repo_path, source_game_for_rig_settings(rs)) if rs.repo_path else None
            spawn = sfx_assets.template_emitter_events(template) if template and os.path.isfile(template) else []
            switches = library.switches(spawn, self._armour())
            events = {s[1] for c in self._all_clips() for s in c.sounds}
            wavs = sfx_assets.decode(library, library.pool(events, switches), sfx_assets.wav_cache_dir(),
                                     get_vgmstream_path(bpy.context))
            player = sfx.Player(library, wavs, switches, sfx.open_device())
            self.surfaces = surface.Probe(self.world, getattr(self.settings, "default_surface", "default"))
        except Exception:
            log.exception("player: sound disabled for %s", self.armature.name)
            return None
        self.sound_info = {"template": template, "spawn": spawn, "armour": self._armour(), "events": len(events),
                           "sounds": len(wavs), "seconds": time.perf_counter() - started, "device": player.device is not None}
        log.info("player: sound %s", self.sound_info)
        return player

    def can_draw(self, category: str) -> bool:
        return bool(self.moves and category in self.weapons and category in self.moves.draw and category in self.moves.sheathe)

    @property
    def armed(self) -> bool:
        return any(self.can_draw(c) for c in WEAPONS)

    def _busy(self) -> bool:
        return self.controller.action.startswith(("draw", "sheathe"))

    def _play(self, kind: str, category: str, queue: bool = False):
        from .profiles import stance_after
        ctl = self.controller
        clip = (self.moves.draw if kind == "draw" else self.moves.sheathe)[category]
        if queue:
            ctl.queue(clip, f"{kind}:{category}")
        else:            # locomotion carries on under a draw (playerWeaponHolster.ws Lock keeps Movement)
            ctl.play(clip, f"{kind}:{category}", upper=ctl.locomoting and self.upper_mask is not None)
        self.stance = stance_after(clip) or self.stance

    def sword_key(self, category: str) -> bool:
        """1 / 2 pressed: draw or switch to that sword; the held sword sheathes on release or hold instead."""
        return self.drawn != category and self.toggle_weapon(category)

    def sheathe(self, category: str = "") -> bool:
        """C sheathes any sword; a held 1 / 2 only the sword it names."""
        if not self.drawn or (category and self.drawn != category):
            return False
        return self.toggle_weapon(self.drawn)

    def toggle_weapon(self, category: str = "") -> bool:
        """Draw or sheathe a sword, switching swords when needed."""
        category = category or self.drawn or next((c for c in WEAPONS if self.can_draw(c)), "")
        if not self.can_draw(category) or self._busy() or self.controller.action == "attack":
            return False
        if self.drawn == category:
            self._play("sheathe", category)
        elif self.drawn:
            self._play("sheathe", self.drawn)
            self._play("draw", category, queue=True)
        else:
            self._play("draw", category)
        return True

    def attack(self) -> bool:
        """Draw a sword or start/queue a light attack."""
        from .profiles import stance_after
        ctl = self.controller
        if not self.armed or self._busy():
            return False
        if not self.drawn:
            return self.toggle_weapon()
        attacks = self.moves.attacks.get(self.stance) or next(iter(self.moves.attacks.values()), [])
        if not attacks:
            return False
        clip = attacks[self.combo % len(attacks)]
        if ctl.action == "attack":
            if not ctl.window_open("AllowInput") or ctl.queued is not None:
                return False
            ctl.queue(clip, "attack")
        elif ctl.action in ("dodge", "roll"):
            ctl.queue(clip, "attack")
        else:
            ctl.play(clip, "attack")
        self.combo += 1
        self.stance = stance_after(clip) or ("lp" if self.stance == "rp" else "rp")
        return True

    def _finish_draw(self):
        """An evade cuts a draw or sheathe short; the sword ends where it was going."""
        kind, _, category = self.controller.action.partition(":")
        if kind == "draw":
            self._hold(category)
        elif kind == "sheathe":
            self._hold("")

    def evade(self, kind: str) -> bool:
        """Dodge (Alt / pad B) or roll (Space / pad A) with a sword drawn, after combat.ws PerformEvade."""
        from .profiles import stance_after
        ctl = self.controller
        armed = self.drawn or ctl.action.startswith("draw")
        evades = self.moves.evades.get(kind) if self.moves is not None and armed else None
        if not evades:
            return False
        held = ctl.released == 0.0
        if ctl.state == core.LOOP and ctl.gait_name == "sprint":
            direction = "forward"
        elif not held:
            direction = "back"
        else:
            rel = core.wrap(ctl.wanted_yaw - ctl.yaw)
            direction = ("forward" if abs(rel) <= math.radians(45.0) else "back" if abs(rel) > math.radians(135.0)
                         else "left" if rel > 0.0 else "right")
        pool = ([e for e in evades if e.direction == direction]
                or [e for e in evades if e.direction == ("back" if direction == "back" else "forward")])
        pool = [e for e in pool if e.stance == self.stance] or [e for e in pool if not e.stance] or pool
        if not pool:
            return False
        turn = self._evade_turn.get((kind, direction), 0)       # the game shuffles playlists of these
        self._evade_turn[(kind, direction)] = turn + 1
        evade = pool[turn % len(pool)]
        face = (core.wrap(ctl.wanted_yaw - evade.travel), evade.travel) if held else None
        drawing = self._busy()                                  # the draw Lock() lets Dodge and Roll through
        self._finish_draw()
        if ctl.state == core.ACTION and ctl.action and not drawing:   # buffered until ActionBlend (PushCombatActionOnBuffer)
            ctl.queue(evade.clip, kind, face)
        else:
            ctl.play(evade.clip, kind, face=face)
        self.stance = stance_after(evade.clip) or self.stance
        return True

    def _measure_grabs(self):
        """Find each grip time from hand-to-hilt distance; item events vary by clip."""
        if self.moves is None or getattr(self.moves, "grab", None) is not None:
            return
        self.moves.grab = {}
        arm = self.armature
        for category in WEAPONS:
            if not self.can_draw(category):
                continue
            clip, weapon = self.moves.draw[category], self.weapons[category]
            grip = weapon["objects"][0]
            ready = next((t for t, n in clip.events if n == "OnWeaponDrawReady"), clip.length)
            best = (math.inf, 0.0)
            count = max(int(ready * 30), 1)
            for i in range(count + 1):
                t = ready * i / count
                clips.apply(arm, *clips.sample(clip, t))
                bpy.context.view_layer.update()
                best = min(best, ((weapon["hand"].matrix_world.translation - grip.matrix_world.translation).length, t))
            self.moves.grab[category] = max(best[1], core.ACTION_FADE)

    def _hold(self, category: str):
        """Reset swords to their slots, then hold `category` if given."""
        for obj, parent, inverse, basis in self._weapon_saved:
            obj.parent, obj.matrix_parent_inverse, obj.matrix_basis = parent, inverse, basis
        if category:
            saved = {o: (i, b) for o, _, i, b in self._weapon_saved}
            for obj in self.weapons[category]["objects"]:
                obj.parent = self.weapons[category]["hand"]
                obj.matrix_parent_inverse, obj.matrix_basis = saved[obj]
        self.drawn = category
        self.controller.set_profile(self.moves.combat if category and self.moves.combat else self.profile)

    def _save(self):
        obj = self.armature
        data = obj.animation_data
        self._saved = {
            "matrix": obj.matrix_basis.copy(), "mode": obj.rotation_mode,
            "action": data.action if data else None,
            "slot": getattr(data, "action_slot", None) if data and data.action else None,
            "use_nla": data.use_nla if data else True,
            "pose": [(pb.rotation_mode, pb.rotation_quaternion.copy(), pb.rotation_euler.copy(), pb.location.copy(), pb.scale.copy())
                     for pb in obj.pose.bones],
            "active": bpy.context.view_layer.objects.active,
            "selected": [o for o in bpy.context.view_layer.objects if o.select_get()],
        }

    def start(self):
        try:
            obj = self.armature
            data = obj.animation_data
            if data is not None:
                data.action = None
                data.use_nla = False
            for pb in obj.pose.bones:
                pb.rotation_mode = 'QUATERNION'
                pb.scale = (1.0, 1.0, 1.0)
            self._restore_selection()
            self._measure_grabs()
            self.apply()
        except Exception:
            self.restore()
            raise

    def _restore_selection(self):
        s = self._saved
        layer = bpy.context.view_layer
        for o in layer.objects:
            o.select_set(o in s["selected"])
        if s["active"] is not None and s["active"].name in layer.objects:
            layer.objects.active = s["active"]

    def restore(self):
        if getattr(self, "sound", None) is not None:
            self.sound.stop()
            self.sound = None
        if self._saved is None:
            return
        obj, s = self.armature, self._saved
        for item, parent, inverse, basis in getattr(self, "_weapon_saved", ()):
            item.parent, item.matrix_parent_inverse, item.matrix_basis = parent, inverse, basis
        for pb, (mode, q, e, l, sc) in zip(obj.pose.bones, s["pose"]):
            pb.rotation_mode = mode
            pb.rotation_quaternion, pb.rotation_euler, pb.location, pb.scale = q, e, l, sc
        obj.rotation_mode = s["mode"]
        obj.matrix_basis = s["matrix"]
        data = obj.animation_data
        if data is not None:
            data.use_nla = s["use_nla"]
            data.action = s["action"]
            if s["action"] is not None and s["slot"] is not None and hasattr(data, "action_slot"):
                data.action_slot = s["slot"]
        self._restore_selection()
        if self._view is not None:
            region_3d, saved = self._view
            region_3d.view_perspective, region_3d.view_location, region_3d.view_rotation, region_3d.view_distance = saved
            if self._lens is not None:
                self._lens[0].lens = self._lens[1]
            self._view = self._lens = None
        self._saved = None

    def orbit(self, dx: float, dy: float):
        """Orbit by degrees, with positive dx right and dy up."""
        self.cam_yaw = core.wrap(self.cam_yaw - math.radians(dx))
        self.cam_pitch = min(max(self.cam_pitch + math.radians(dy), math.radians(-60.0)), math.radians(80.0))

    def move_vector(self, forward: float, right: float):
        """Rotate camera-relative input into world XY."""
        s, c = math.sin(self.cam_yaw), math.cos(self.cam_yaw)
        mx, my = -s * forward + c * right, c * forward + s * right
        mag = math.hypot(mx, my)
        return (mx / mag, my / mag) if mag > 1.0 else (mx, my)

    def step(self, dt: float, move, speed: float, stop_delay: float):
        ctl = self.controller
        if self.world is not None:
            self.world.ensure(ctl.x, ctl.y)
        ctl.step(dt, move, speed, stop_delay)
        kind, _, category = ctl.action.partition(":")
        if kind == "draw" and self.drawn != category and ctl.action_time >= self.moves.grab.get(category, 0.0):
            self._hold(category)
        for action, name in ctl.fired:
            kind, _, category = action.partition(":")
            if kind == "sheathe" and self.drawn == category and (name == "item" or name in core.EXIT_EVENTS):
                self._hold("")
        if self.sound is not None:
            from .sfx_assets import H
            master = getattr(self.settings, "sound_volume", 1.0)
            for kind in self.sound.gains:
                self.sound.gains[kind] = master * getattr(self.settings, f"volume_{kind}", 1.0)
            surface = None
            for event, _bone, footstep in ctl.sounds:
                if footstep and surface is None:
                    surface = H(self.surfaces.at(ctl.x, ctl.y, ctl.z))
                wems = self.sound.play(event, footstep, surface if footstep else None)
                if getattr(self.settings, "sound_debug", False):
                    log.info("player sound %s%s -> %s", event, f" on {self.surfaces.last}" if footstep else "", wems)
        if not ctl.action:
            self.combo = 0
        if getattr(self.settings, "follow", False) and ctl.state in (core.LOOP, core.START):
            diff = core.wrap(ctl.yaw - self.cam_yaw)
            if abs(diff) < FOLLOW_CONE:
                self.cam_yaw = core.wrap(self.cam_yaw + diff * min(1.0, 1.5 * dt))

    def apply(self):
        ctl, obj = self.controller, self.armature
        rot, loc = clips.blend(ctl.pose_mix())
        mix, weight = ctl.upper_mix()
        if mix:
            rot, loc = clips.overlay(rot, loc, *clips.blend(mix), self.upper_mask * weight)
        clips.apply(obj, rot, loc)
        obj.matrix_world = (Matrix.Translation((ctl.x, ctl.y, ctl.z)) @ Matrix.Rotation(ctl.yaw, 4, 'Z')
                            @ Matrix.Diagonal(self._scale.to_4d()))

    def step_dangles(self, dt: float):
        from ..physics import breast_blender, dyng_blender
        scene = bpy.context.scene
        runtime = [(m, o) for m, objs in ((dyng_blender, dyng_blender._runtime_dyng_objects(scene)),
                                          (breast_blender, breast_blender._runtime_breast_objects(scene))) for o in objs]
        if not runtime:
            return
        bpy.context.view_layer.update()
        for module, obj in runtime:
            try:
                module.step_object(obj, dt, update_status=False)
            except Exception:
                log.debug("player: dangle step failed for %s", obj.name, exc_info=True)

    def _collide(self, pivot: Vector, distance: float, height: float, dt: float) -> float:
        """Keep the camera clear of geometry and ease it back out."""
        world = self.world
        if world is None or world.bvh is None:
            self.cam_distance = distance
            return distance
        up = world.ray(pivot, Vector((0.0, 0.0, 1.0)), distance + CAMERA_MARGIN)
        down = world.ray(pivot, Vector((0.0, 0.0, -1.0)), height + CAMERA_MARGIN)
        top = math.asin(min(max((up - CAMERA_MARGIN) / distance, 0.0), 1.0)) if up is not None else math.radians(80.0)
        bottom = -math.asin(min(max((down - CAMERA_MARGIN) / distance, 0.0), 1.0)) if down is not None else math.radians(-60.0)
        self.cam_pitch = min(max(self.cam_pitch, bottom), top)
        cp, sp = math.cos(self.cam_pitch), math.sin(self.cam_pitch)
        back = Vector((math.sin(self.cam_yaw) * cp, -math.cos(self.cam_yaw) * cp, sp))
        hit = world.ray(pivot, back, distance + CAMERA_MARGIN)
        wanted = distance if hit is None else min(max(hit - CAMERA_MARGIN, CAMERA_NEAREST), distance)
        if wanted < self.cam_distance:
            self.cam_distance = wanted
        else:
            self.cam_distance += (wanted - self.cam_distance) * min(1.0, CAMERA_EASE * dt)
        return self.cam_distance

    def toggle_freecam(self) -> bool:
        ctl = self.controller
        self.freecam = None if self.freecam else freecam.FreeCamera.at_player(ctl.x, ctl.y, ctl.z, ctl.yaw)
        return self.freecam is not None

    def teleport(self, centre: bool = False) -> bool:
        """Put the player at the free camera (pad X) or where its centre ray lands (right mouse), facing its yaw."""
        cam, ctl = self.freecam, self.controller
        if cam is None:
            return False
        pos = Vector(cam.pos)
        if centre:
            if self.world is None:
                return False
            self.world.ensure(pos.x, pos.y)
            hit = self.world.ray(pos, Vector(cam.axes()[0]), 1000.0)
            if hit is None:
                return False
            pos += Vector(cam.axes()[0]) * hit
        ctl.x, ctl.y, ctl.z, ctl.vz = pos.x, pos.y, pos.z, 0.0
        ctl.yaw = ctl.wanted_yaw = core.wrap(math.radians(cam.rot[0]))
        if self.world is not None:
            self.world.ensure(ctl.x, ctl.y)
        return True

    def _freecam_view(self, region_3d, space, size):
        cam = self.freecam
        yaw, pitch, roll = (math.radians(a) for a in cam.visible_rot)
        rotation = (Matrix.Rotation(yaw, 4, 'Z') @ Matrix.Rotation(math.pi / 2 + pitch, 4, 'X')
                    @ Matrix.Rotation(roll, 4, 'Z')).to_quaternion()
        region_3d.view_perspective = 'PERSP'
        region_3d.view_rotation = rotation
        region_3d.view_distance = 1.0
        region_3d.view_location = Vector(cam.visible_pos) - rotation @ Vector((0.0, 0.0, 1.0))
        if space is not None:       # horizontal FOV; the free viewport spans 72 mm across its longer side
            width, height = size
            space.lens = 36.0 / math.tan(math.radians(cam.fov) / 2.0) * width / max(width, height, 1)

    def view(self, region_3d, dt: float = 1.0 / 60.0, space=None, size=(16, 9)):
        """Orbit the viewport around the character, or fly the free camera."""
        if self._view is None:
            self._view = (region_3d, (region_3d.view_perspective, region_3d.view_location.copy(),
                                      region_3d.view_rotation.copy(), region_3d.view_distance))
            self._lens = (space, space.lens) if space is not None else None
        if self.freecam is not None:
            return self._freecam_view(region_3d, space, size)
        if self._lens is not None and self._lens[0].lens != self._lens[1]:
            self._lens[0].lens = self._lens[1]
        ctl = self.controller
        distance = getattr(self.settings, "camera_distance", 3.5) * self.size
        height = getattr(self.settings, "camera_height", 1.6) * self.size
        pivot = Vector((ctl.x, ctl.y, ctl.z + height))
        if getattr(self.settings, "camera_collision", True):
            distance = self._collide(pivot, distance, height, dt)
        else:
            self.cam_distance = distance
        cp, sp = math.cos(self.cam_pitch), math.sin(self.cam_pitch)
        direction = Vector((-math.sin(self.cam_yaw) * cp, math.cos(self.cam_yaw) * cp, -sp))
        region_3d.view_perspective = 'PERSP'
        region_3d.view_location = pivot
        region_3d.view_rotation = direction.to_track_quat('-Z', 'Y')
        region_3d.view_distance = distance

    def status(self) -> dict:
        ctl = self.controller
        return {"profile": ctl.profile.name, "weapon": (f"{self.drawn[:-5]} drawn" if self.drawn else "sheathed") if self.armed else "", "state": ctl.state, "clip": ctl.clip_name, "gait": ctl.gait_name, "speed": ctl.speed,
                "position": f"{ctl.x:.2f} {ctl.y:.2f} {ctl.z:.2f}  yaw {math.degrees(ctl.yaw):.0f}", "tick_ms": self.tick_ms,
                "on_ground": self.world is not None and self.world.bvh is not None, "sound_status": self._sound_status()}

    def _sound_status(self) -> str:
        if self.sound is None:
            return "sound failed, see the log" if getattr(self.settings, "sound", False) else ""
        info = self.sound_info
        device = "" if info.get("device") else ", no audio device"
        return f"{info.get('sounds', 0)} sounds{device}, on {self.surfaces.last or '-'}"
