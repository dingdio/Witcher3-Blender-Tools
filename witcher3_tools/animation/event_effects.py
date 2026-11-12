"""Bake animation-triggered trails from the item actually held by the character."""
import json
import math
import os
from contextlib import nullcontext

import bpy
from mathutils import Euler, Matrix

from . import events
from ..CR2W import dc_entity
from ..CR2W.common_blender import _get_redkit_depot_roots, redkit_repo_context, repo_file
from ..importers import import_particle, import_trail, import_blood

ITEM_EFFECT_TYPES = {'CExtAnimItemEffectEvent', 'CExtAnimItemEffectDurationEvent'}


def _matrix(transform):
    if transform is None:
        return Matrix.Identity(4)
    def get(name, default=0):
        return float(transform.get(name, default) if isinstance(transform, dict) else getattr(transform, name, default))
    rotation = Euler(tuple(math.radians(get(k)) for k in ('Yaw', 'Pitch', 'Roll')), 'YXZ')
    return Matrix.LocRotScale(tuple(get(k) for k in ('X', 'Y', 'Z')), rotation.to_quaternion(),
                              tuple(get(k, 1) for k in ('Scale_x', 'Scale_y', 'Scale_z')))


def _remove(objects):
    for obj in objects:
        try:
            mesh = obj.data
        except ReferenceError:  # already deleted by the user or an undo
            continue
        materials = list(mesh.materials)
        action = obj.animation_data.action if obj.animation_data else None
        keys = mesh.shape_keys
        key_action = keys.animation_data.action if keys and keys.animation_data else None
        action_names = {a.name for a in (action, key_action) if a is not None}
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        for material in materials:
            if material.users == 0:
                bpy.data.materials.remove(material)
        for name in action_names:
            action = bpy.data.actions.get(name)
            if action is not None and action.users == 0:
                bpy.data.actions.remove(action)


def previews(scene, owner, action, kind=None):
    return [o for o in scene.objects if o.get('witcher_trail_version')
            and o.get('witcher_trail_owner') == owner and o.get('witcher_trail_action') == action
            and (kind is None or o.get('witcher_trail_kind', 'LIGHT') == kind)]


def clear(scene, owner, action):
    objects = previews(scene, owner, action)
    _remove(objects)
    return len(objects)


DEFAULT_WEAPON = ('steelsword', 'Long Steel Sword')  # Geralt's starting sword


def _held(owner, slot_name):
    return [s for s in owner.data.witcherui_RigSettings.equipment_slots
            if s.is_loaded and s.is_in_hold_slot and s.equip_guid and (s.hold_slot or 'r_weapon') == slot_name]


def sword_slot(owner, category=DEFAULT_WEAPON[0]):
    """(index, slot) of the Equipment panel's sword entry, or (None, None)."""
    return next(((i, s) for i, s in enumerate(owner.data.witcherui_RigSettings.equipment_slots) if s.category == category),
                (None, None))


def has_item(slot):
    return slot is not None and slot.equip_template not in ('', 'None')


def assign_default_weapon(context, owner):
    """Give an empty steel-sword entry the default sword; returns (slot index, item name)."""
    from ..ui import ui_equipment as equipment
    category, item = DEFAULT_WEAPON
    slots = owner.data.witcherui_RigSettings.equipment_slots
    index, slot = sword_slot(owner)
    if slot is None:
        slot = slots.add()
        slot.category = category
        index = len(slots) - 1
    slot.source_game = slot.source_game or 'w3'
    equipment.ensure_equipment_catalog_ready(slot.source_game, context=context)
    by_category, attributes = equipment.equipment_catalog.get_equipment_catalog(slot.source_game)
    names = [item] + [row[0] for row in by_category.get(category, ())]
    item = next((n for n in names if attributes.get(n, {}).get('equip_template')), None)
    if item is None:
        raise ValueError('No weapon is equipped and the item catalog has no steel sword to equip')
    slot.item_name = item
    slot.equip_template = slot.base_equip_template = attributes[item]['equip_template']
    equipment._apply_catalog_attributes_to_slot(slot, slot.source_game)
    return index, item


def equip_sword(context, owner):
    """Equip the configured steel sword, falling back to the default."""
    from ..ui import ui_equipment as equipment
    settings = owner.data.witcherui_RigSettings
    index, slot = sword_slot(owner)
    note = ''
    if not has_item(slot):
        index, item = assign_default_weapon(context, owner)
        note = f'Equipped a default {item}'
    slot = settings.equipment_slots[index]
    if not slot.is_loaded and not equipment.load_equipment_item(context, owner, index, settings, mount_mode='equip'):
        raise ValueError(equipment._get_last_equipment_load_failure(owner, index) or f"Could not load '{slot.item_name}'")
    try:
        equipment.sync_equipment_slots_to_temp(context, settings)
    except Exception:
        pass
    return note or f"Equipped '{slot.item_name}'"


def hold_weapon(context, owner, slot_name='r_weapon'):
    """Hold an equipped weapon or the default sword; return a change note or ''."""
    if _held(owner, slot_name):
        return ''
    from ..ui import ui_equipment as equipment
    settings = owner.data.witcherui_RigSettings
    slots = settings.equipment_slots
    usable = [i for i, s in enumerate(slots) if s.weapon and (s.hold_slot or 'r_weapon') == slot_name
              and s.equip_template not in ('', 'None')]
    note = ''
    if not usable:
        index, item = assign_default_weapon(context, owner)
        usable, note = [index], f'Equipped a default {item}'
    index = min(usable, key=lambda i: (not slots[i].is_loaded, slots[i].category != DEFAULT_WEAPON[0]))
    layer = context.view_layer
    active = layer.objects.active
    layer.objects.active = owner
    try:
        bpy.ops.witcher.equipment_toggle_item(slot_index=index)
    finally:
        layer.objects.active = active
    if not _held(owner, slot_name):
        raise ValueError(f"Could not put '{slots[index].item_name}' into {slot_name}; equip a weapon in the Equipment panel")
    try:
        equipment.sync_equipment_slots_to_temp(context, settings)
    except Exception:
        pass
    return note or f"Moved '{slots[index].item_name}' to the hand"


EFFECTS_COLLECTION = 'W3 Weapon Effects'


def set_visible(scene, visible):
    """Use collection visibility to override ribbon visibility keys."""
    collection = bpy.data.collections.get(EFFECTS_COLLECTION) or bpy.data.collections.new(EFFECTS_COLLECTION)
    if collection.name not in scene.collection.children:
        scene.collection.children.link(collection)
    for obj in [o for o in scene.objects if o.get('witcher_trail_version') and collection not in o.users_collection]:
        for other in list(obj.users_collection):
            other.objects.unlink(obj)
        collection.objects.link(obj)
    collection.hide_viewport = collection.hide_render = not visible


def refresh(context, owner, action, strip=None):
    """Rebake enabled effects and remove disabled kinds; return (count, messages)."""
    scene = context.scene
    set_visible(scene, True)
    # Effects show the current clip only; ribbons baked for the character's other clips would stay on screen.
    _remove([o for o in scene.objects if o.get('witcher_trail_version') and o.get('witcher_trail_owner') == owner
             and o.get('witcher_trail_action') != action])
    count, messages = 0, []
    for kind, enabled, blood in (('LIGHT', scene.witcher_effects_trails, False), ('BLOOD', scene.witcher_effects_blood, True)):
        if not enabled:
            _remove(previews(scene, owner, action, kind))
            continue
        try:
            objects, skipped, notes = bake(context, owner, action, strip, blood=blood)
            count += len(objects)
            messages += notes + skipped
        except (ValueError, OSError) as exc:
            messages.append(str(exc))
    set_visible(scene, True)  # gather the new ribbons
    return count, list(dict.fromkeys(messages))


def refresh_after_load(context, owner):
    """Refresh the loaded clip when Effects is enabled."""
    if owner is None or not getattr(context.scene, 'witcher_event_effects', False):
        return
    action, strip = events.clip(owner, context.scene.frame_current_final, selected=True)
    if action is not None:
        refresh(context, owner, action, strip)


def _held_item(scene, owner, slot_name):
    slots = _held(owner, slot_name)
    if not slots:
        raise ValueError(f'No weapon is held in {slot_name}')
    return _item_from_slot(scene, slots[0])


def _item_from_slot(scene, slot):
    """Resolve the equipped item independently of its temporary controller mount."""
    objects = [o for o in scene.objects if o.get('witcher_equip_guid') == slot.equip_guid
               and not o.get('witcher_bound_parent_guid') and o.type == 'MESH']
    if not objects:
        raise ValueError('The held weapon has no imported mesh')
    roots = _get_redkit_depot_roots()
    with redkit_repo_context(roots=roots) if roots else nullcontext():
        path = repo_file(slot.resolved_repo_path)
        if not path or not os.path.isfile(path):
            raise ValueError(f'Weapon template is missing: {slot.resolved_repo_path}')
        entity = dc_entity.load_bin_entity(path)
    # Undo the mesh component's local transform to obtain the item entity's basis.
    for obj in objects:
        chunk = next((c for c in entity.staticMeshes.chunks if getattr(c, 'name', '') == obj.get('witcher_name')), None)
        if chunk is not None and not getattr(chunk, 'transformParent', None):
            return slot, obj, _matrix(getattr(chunk, 'transform', None)).inverted(), entity, path
    raise ValueError('Trail preview currently requires a rigid weapon component without an attachment parent')


def _effect_matrix(entity, name):
    slot = next((s for s in getattr(entity, 'slots', []) if s.name == name), None)
    if slot is not None and slot.boneName:
        raise ValueError(f'Bone-attached weapon effect slots are not supported: {name}')
    component = getattr(entity, 'effectComponents', {}).get(slot.componentName if slot else name)
    if component is None or component.get('parent'):
        raise ValueError(f'Cannot resolve weapon effect component: {name}')
    return _matrix(component['transform']) @ (_matrix(slot.transform) if slot else Matrix.Identity(4))


def bake(context, owner, action, strip=None, *, blood=False):
    if owner is None or action is None:
        raise ValueError('Choose a character animation first')
    if getattr(getattr(context.window_manager, 'witcher_player', None), 'running', False):
        raise ValueError('Stop the player controller before baking trails')
    notes = []
    if any(w.screen.is_animation_playing for w in context.window_manager.windows):
        bpy.ops.screen.animation_cancel(restore_frame=False)
        notes.append('Paused playback')
    scene = context.scene
    records = list(events.strip_events(strip, strip.frame_start, strip.frame_end)) if strip else [events.values(e) for e in action.witcher_events]
    records = sorted((e for e in records if (e['event_name'] in ('BloodTrail', 'BloodTrailForced')
                     if blood else e['event_type'] in ITEM_EFFECT_TYPES)), key=lambda e: e['frame'])
    if not records:
        raise ValueError('This clip has no BloodTrail markers' if blood else 'This clip has no item-effect events')
    rate = events.fps(scene)
    clip_end = strip.frame_end if strip else action.frame_range[1]
    previous_frame = scene.frame_current_final
    created, skipped, items = [], [], {}
    try:
        for index, event in enumerate(records):
            raw = json.loads(event['payload']).get('raw_fields', {})
            if raw.get('action', 'IEA_Start') == 'IEA_Stop':
                continue
            effect_name = 'default_blood_trail' if blood else raw.get('effectName', 'item_effect')
            slot_name = raw.get('itemSlot', 'r_weapon')
            if slot_name not in items:
                note = hold_weapon(context, owner, slot_name)
                if note:
                    notes.append(note)
                items[slot_name] = _held_item(scene, owner, slot_name)
            slot, weapon, inverse, entity, entity_path = items[slot_name]
            effect = next((e for e in entity.cookedEffects if e['name'] == effect_name), None)
            if effect is None:
                skipped.append(f'{effect_name}: not defined on the held item')
                continue
            if effect['is_looped']:
                skipped.append(f'{effect_name}: looping effects are not supported')
                continue
            stop = min(clip_end, event['frame'] + effect['length'] * rate)
            if event['event_type'] == 'CExtAnimItemEffectDurationEvent':
                stop = min(stop, event['frame'] + event['duration'])
            for later in records[index+1:]:
                fields = json.loads(later['payload']).get('raw_fields', {})
                if (fields.get('itemSlot', 'r_weapon') == slot_name and fields.get('effectName', 'item_effect') == effect_name
                        and fields.get('action', 'IEA_Start') == 'IEA_Stop'):
                    stop = min(stop, later['frame'])
                    break
            for track in effect['particle_systems']:
                if track['duration'] <= 0:
                    continue
                with redkit_repo_context(entity_path):
                    system, source, source_path = import_particle._resolve_source(track['path'])
                emitters = [e for e in system.emitters if e.drawer_type == 'CParticleDrawerTrail']
                if not emitters:
                    skipped.append(f'{effect_name}: no supported trail emitter')
                    continue
                local = inverse @ _effect_matrix(entity, track['slot'])

                def matrix_at(frame):
                    whole = math.floor(frame)
                    scene.frame_set(whole, subframe=frame-whole)
                    return weapon.evaluated_get(context.evaluated_depsgraph_get()).matrix_world @ local

                start = event['frame'] + track['time_begin'] * rate
                end = min(stop, start + track['duration'] * rate)
                for emitter in emitters:
                    with redkit_repo_context(source_path):
                        # Simulated hits start at the marker, ignoring the CFX track offset.
                        obj = (import_blood.bake_blood(scene, scene.collection, emitter, matrix_at, event['frame'], track)
                               if blood else import_trail.bake_trail(scene, scene.collection, emitter, matrix_at, start, end, track))
                    if obj is None:
                        continue
                    created.append(obj)
                    obj['witcher_trail_owner'] = owner
                    obj['witcher_trail_action'] = action
                    obj['witcher_trail_effect'] = effect_name
                    obj['witcher_trail_particle'] = source
                    obj['witcher_trail_item'] = slot.resolved_repo_path
                    obj['witcher_trail_component'] = track['slot']
                    if blood:
                        obj['witcher_blood_trigger'] = event['event_name']
                        obj['witcher_blood_source_track_offset'] = track['time_begin']
        if not created:
            raise ValueError('; '.join(dict.fromkeys(skipped)) or 'No active weapon trails in this clip')
        # Keep the old bake until every new ribbon has been built successfully.
        _remove([o for o in previews(scene, owner, action, 'BLOOD' if blood else 'LIGHT') if o not in created])
        return created, list(dict.fromkeys(skipped)), notes
    except Exception:
        _remove(created)
        raise
    finally:
        whole = math.floor(previous_frame)
        scene.frame_set(whole, subframe=previous_frame-whole)
