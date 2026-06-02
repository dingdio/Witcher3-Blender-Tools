"""Appearance resolution for cutscene animation events."""

from ..CR2W.cutscene_event_schema import GAME_W2, normalize_game


BODY_PART_EVENT_TYPE = "CExtAnimCutsceneBodyPartEvent"


def _event_value(event, attr_name, default=None):
    if event is None:
        return default
    if isinstance(event, dict):
        return event.get(attr_name, default)
    if hasattr(event, attr_name):
        return getattr(event, attr_name, default)
    raw_fields = getattr(event, "raw_fields", None)
    if isinstance(raw_fields, dict) and attr_name in raw_fields:
        return raw_fields.get(attr_name, default)
    return default


def _key(value):
    return str(value or "").strip().lower().lstrip("_")


def _appearance_name(appearance):
    return str(getattr(appearance, "name", "") or "").strip()


def _appearance_part_keys(appearance):
    return {
        _key(part)
        for part in (getattr(appearance, "w2_parts", None) or [])
        if _key(part)
    }


def _appearance_by_w2_parts(entity, wanted_keys, require_all=True):
    wanted_keys = {
        _key(part)
        for part in (wanted_keys or [])
        if _key(part)
    }
    if not wanted_keys:
        return None

    for idx, appearance in enumerate(getattr(entity, "appearances", None) or []):
        part_keys = _appearance_part_keys(appearance)
        if not part_keys:
            continue
        matched = wanted_keys.issubset(part_keys) if require_all else bool(wanted_keys & part_keys)
        if matched:
            return appearance, idx, _appearance_name(appearance)
    return None


def is_body_part_event(event):
    return str(_event_value(event, "type_name", "") or "") == BODY_PART_EVENT_TYPE


def body_part_event_has_body_state(event):
    if not is_body_part_event(event):
        return False
    return (
        _event_value(event, "bodyPart", None) is not None
        or _event_value(event, "body_part", None) is not None
        or _event_value(event, "state", None) is not None
    )


def body_part_event_request_name(event):
    """Return the event's user-facing requested appearance/body-state token."""
    if not is_body_part_event(event):
        return ""

    appearance = str(_event_value(event, "appearance", "") or "").strip()
    if appearance:
        return appearance

    body_part = str(
        _event_value(event, "bodyPart", "")
        or _event_value(event, "body_part", "")
        or ""
    ).strip()
    state = str(_event_value(event, "state", "") or "").strip()
    if body_part and state:
        return f"{body_part}:{state}"
    return body_part or state


def w2_body_part_state_components(entity, body_part, state):
    body_part_key = _key(body_part)
    state_key = _key(state)
    if not body_part_key or not state_key:
        return []

    body_part_states = getattr(entity, "w2_body_part_states", None) or {}
    if not isinstance(body_part_states, dict):
        return []

    state_map = body_part_states.get(body_part_key) or body_part_states.get(body_part_key.lstrip("_")) or {}
    if not isinstance(state_map, dict):
        return []

    components = state_map.get(state_key)
    if components is None:
        for candidate_state, candidate_components in state_map.items():
            if _key(candidate_state) == state_key:
                components = candidate_components
                break

    return [
        str(component or "").strip()
        for component in (components or [])
        if str(component or "").strip()
    ]


def resolve_w2_body_part_event_appearance(entity, event):
    """Resolve W2 bodyPart/state to the closest authored whole appearance.

    W2 runtime events switch one body-part state. The Blender importer can only
    switch a whole imported appearance, so this adapter uses the entity's W2
    body-part table deterministically:
    1. an appearance containing every component in the requested state,
    2. otherwise an appearance containing the event's own body-part component,
       but only when that component is explicitly part of the requested state.
    """
    if entity is None or not is_body_part_event(event):
        return None

    body_part = str(
        _event_value(event, "bodyPart", "")
        or _event_value(event, "body_part", "")
        or ""
    ).strip()
    state = str(_event_value(event, "state", "") or "").strip()
    if not body_part or not state:
        return None

    components = w2_body_part_state_components(entity, body_part, state)
    component_keys = [_key(component) for component in components if _key(component)]
    if not component_keys:
        return None

    resolved = _appearance_by_w2_parts(entity, component_keys, require_all=True)
    if resolved is not None:
        return resolved

    body_part_key = _key(body_part)
    if body_part_key and body_part_key in set(component_keys):
        return _appearance_by_w2_parts(entity, (body_part_key,), require_all=True)

    return None


def resolve_body_part_event_appearance(entity, event):
    if not is_body_part_event(event):
        return None

    source_game = normalize_game(_event_value(event, "source_game", ""))
    if source_game == GAME_W2:
        return resolve_w2_body_part_event_appearance(entity, event)

    if not source_game and (
        _event_value(event, "bodyPart", None) is not None
        or _event_value(event, "body_part", None) is not None
    ):
        return resolve_w2_body_part_event_appearance(entity, event)

    return None
