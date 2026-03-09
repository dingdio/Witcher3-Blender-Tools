import logging

import bpy

log = logging.getLogger(__name__)


def _has_legacy_action_fcurves(action):
    return hasattr(action, "fcurves")


def _slot_in_action(action, slot):
    if slot is None:
        return False
    slots = getattr(action, "slots", None)
    if slots is None:
        return False
    return any(candidate == slot for candidate in slots)


def _slot_target_id_type(slot):
    return getattr(slot, "target_id_type", getattr(slot, "id_root", None))


def _create_action_slot(action, target, slot_name=None):
    slots = getattr(action, "slots", None)
    if slots is None or target is None:
        return None

    target_id_type = getattr(target, "id_type", None)
    name = slot_name or getattr(target, "name", None) or action.name

    create_attempts = (
        lambda: slots.new(target_id_type, name),
        lambda: slots.new(target_id_type),
        lambda: slots.new(for_id=target),
        lambda: slots.new(target),
    )
    for attempt in create_attempts:
        try:
            slot = attempt()
            if getattr(slot, "name", None) in (None, "") and name:
                try:
                    slot.name = name
                except Exception:
                    pass
            return slot
        except TypeError:
            continue
        except Exception as exc:
            log.debug("Action slot creation attempt failed for %s: %s", action.name, exc)
    return None


def resolve_action_slot(action, target=None, slot=None, slot_name=None, ensure=False):
    if slot is not None:
        return slot

    slots = getattr(action, "slots", None)
    if slots is None:
        return None

    if target is not None:
        anim_data = getattr(target, "animation_data", None)
        if anim_data is not None and getattr(anim_data, "action", None) == action:
            current_slot = getattr(anim_data, "action_slot", None)
            if _slot_in_action(action, current_slot):
                return current_slot

    if slot_name:
        for candidate in slots:
            if getattr(candidate, "name", None) == slot_name:
                return candidate

    target_id_type = getattr(target, "id_type", None) if target is not None else None
    if target_id_type is not None:
        for candidate in slots:
            if _slot_target_id_type(candidate) == target_id_type:
                return candidate

    if len(slots) > 0:
        return slots[0]

    if not ensure:
        return None

    return _create_action_slot(action, target, slot_name=slot_name)


def _ensure_action_layer(action):
    layers = getattr(action, "layers", None)
    if layers is None:
        return None
    if len(layers) > 0:
        return layers[0]
    try:
        return layers.new("Layer")
    except Exception as exc:
        log.debug("Unable to create action layer for %s: %s", action.name, exc)
        return None


def _ensure_action_strip(layer):
    if layer is None:
        return None
    if len(layer.strips) > 0:
        return layer.strips[0]
    try:
        return layer.strips.new(type='KEYFRAME')
    except Exception as exc:
        log.debug("Unable to create action strip on %s: %s", layer.name, exc)
        return None


def get_action_channelbag(action, target=None, slot=None, slot_name=None, ensure=False):
    if _has_legacy_action_fcurves(action):
        return None

    slot = resolve_action_slot(action, target=target, slot=slot, slot_name=slot_name, ensure=ensure)
    if slot is None:
        return None

    layers = getattr(action, "layers", None)
    if layers is None:
        return None

    layer = _ensure_action_layer(action) if ensure else (layers[0] if len(layers) > 0 else None)
    strip = _ensure_action_strip(layer) if ensure else (layer.strips[0] if layer and len(layer.strips) > 0 else None)
    if strip is None:
        return None

    slot_refs = [slot]
    slot_handle = getattr(slot, "handle", None)
    if slot_handle is not None:
        slot_refs.append(slot_handle)

    for slot_ref in slot_refs:
        try:
            channelbag = strip.channelbag(slot_ref, ensure=ensure)
            if channelbag is not None:
                return channelbag
        except TypeError:
            try:
                channelbag = strip.channelbag(slot_ref)
                if channelbag is not None:
                    return channelbag
            except Exception:
                pass
        except Exception:
            pass

    if ensure:
        channelbags = getattr(strip, "channelbags", None)
        if channelbags is not None:
            for slot_ref in slot_refs:
                try:
                    return channelbags.new(slot_ref)
                except Exception:
                    continue
    return None


def assign_action(target, action):
    if target.animation_data is None:
        target.animation_data_create()

    target.animation_data.action = action
    slot = resolve_action_slot(action, target=target, ensure=True)
    if slot is not None and hasattr(target.animation_data, "action_slot"):
        try:
            target.animation_data.action_slot = slot
        except Exception as exc:
            log.debug("Unable to assign action slot %s to %s: %s", getattr(slot, "name", "<slot>"), target.name, exc)
    return slot


def bind_strip_action_slot(strip, slot):
    if strip is None or slot is None:
        return

    if hasattr(strip, "action_slot"):
        try:
            strip.action_slot = slot
            return
        except Exception:
            pass

    slot_handle = getattr(slot, "handle", None)
    if slot_handle is not None and hasattr(strip, "action_slot_handle"):
        try:
            strip.action_slot_handle = slot_handle
        except Exception:
            pass


def iter_action_fcurves(action, target=None, slot=None):
    if _has_legacy_action_fcurves(action):
        return tuple(action.fcurves)

    channelbag = get_action_channelbag(action, target=target, slot=slot, ensure=False)
    if channelbag is None:
        return ()
    return tuple(channelbag.fcurves)


def new_action_fcurve(action, target, data_path, index=None, group_name=None, slot=None):
    kwargs = {"data_path": data_path}
    if index is not None:
        kwargs["index"] = index

    if _has_legacy_action_fcurves(action):
        if group_name:
            kwargs["action_group"] = group_name
        return action.fcurves.new(**kwargs)

    channelbag = get_action_channelbag(action, target=target, slot=slot, ensure=True)
    if channelbag is None:
        raise AttributeError(f"Unable to access FCurves for action {action.name}")

    if group_name:
        kwargs["group_name"] = group_name

    try:
        return channelbag.fcurves.new(**kwargs)
    except TypeError:
        kwargs.pop("group_name", None)
        fcurve = channelbag.fcurves.new(**kwargs)
        if group_name:
            groups = getattr(channelbag, "groups", None)
            if groups is not None:
                try:
                    group = groups.get(group_name)
                except Exception:
                    group = None
                if group is None:
                    try:
                        group = groups.new(group_name)
                    except Exception:
                        group = None
                if group is not None:
                    try:
                        fcurve.group = group
                    except Exception:
                        pass
        return fcurve


def remove_action_fcurve(action, fcurve, target=None, slot=None):
    if _has_legacy_action_fcurves(action):
        action.fcurves.remove(fcurve)
        return

    channelbag = get_action_channelbag(action, target=target, slot=slot, ensure=False)
    if channelbag is not None:
        channelbag.fcurves.remove(fcurve)
