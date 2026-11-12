"""Typed animation event fields shared by import and native export."""
import copy
from .CR2W_helpers import Enums
from .CR2W_types import PROPERTY, HANDLE, CEnum
from .prop_utils import iter_prop_elements


# Engine RTTI, so authored events need no imported type hints:
# extAnimScriptEvent.h, preAttackEvent.h, cameraComponent.h, cameraDirector.h, lightComponent.h.
STRUCTS = {
    'SEnumVariant': {'enumType': 'CName', 'enumValue': 'Int32'},
    'SSlideToTargetEventProps': {'minSlideDist': 'Float', 'maxSlideDist': 'Float',
                                 'slideToMaxDistIfTargetSeen': 'Bool', 'slideToMaxDistIfNoTarget': 'Bool'},
    'SMultiValue': {'floats': 'array:2,0,Float', 'bools': 'array:2,0,Bool',
                    'enums': 'array:2,0,SEnumVariant', 'names': 'array:2,0,CName'},
    'CPreAttackEventData': {
        'attackName': 'CName', 'weaponSlot': 'CName', 'hitReactionType': 'Int32', 'rangeName': 'CName',
        'Damage_Friendly': 'Bool', 'Damage_Neutral': 'Bool', 'Damage_Hostile': 'Bool', 'Can_Parry_Attack': 'Bool',
        'hitFX': 'CName', 'hitBackFX': 'CName', 'hitParriedFX': 'CName', 'hitBackParriedFX': 'CName',
        'swingType': 'Int32', 'swingDir': 'Int32', 'soundAttackType': 'CName', 'canBeDodged': 'Bool',
        'cameraAnimOnMissedHit': 'CName'},
    'SCustomClippingPlanes': {'nearPlaneDistance': 'Float', 'farPlaneDistance': 'Float'},
    'SBokehDofParams': {'enabled': 'Bool', 'hexToCircleScale': 'Float', 'usePhysicalSetup': 'Bool',
                        'planeInFocus': 'Float', 'fStops': 'EApertureValue', 'bokehSizeMuliplier': 'Float'},
    'SLightFlickering': {'positionOffset': 'Float', 'flickerStrength': 'Float', 'flickerPeriod': 'Float'},
    # curveData.h: these names are serialized as written here (the engine strips two characters of
    # the un-prefixed members). 147 is the next-gen memory class; older files use other numbers.
    'SCurveData': {'Curve Values': 'array:147,0,SCurveDataEntry', 'value type': 'ECurveValueType',
                   'type': 'ECurveBaseType', 'is looped': 'Bool'},
    'SCurveDataEntry': {'me': 'Float', 'ntrolPoint': 'Vector', 'lue': 'Float', 'rveTypeL': 'Uint16', 'rveTypeR': 'Uint16'},
}
# Members only older files use; typed so those events rebuild, never offered for new ones.
LEGACY_FIELDS = {
    'SCurveData': {'times': 'array:138,0,Float', 'values': 'array:138,0,Float', 'control points': 'array:138,0,Vector',
                   'left curve type': 'array:138,0,Uint32', 'right curve type': 'array:138,0,Uint32'},
}
# Constructor values that differ from zero/empty.
STRUCT_DEFAULTS = {
    'CPreAttackEventData': {'attackName': 'attack_light', 'weaponSlot': 'r_weapon', 'hitReactionType': 1,
                            'Damage_Neutral': True, 'Damage_Hostile': True, 'Can_Parry_Attack': True, 'canBeDodged': True},
    'SCustomClippingPlanes': {'nearPlaneDistance': 0.4, 'farPlaneDistance': 8000.0},
    'SBokehDofParams': {'planeInFocus': 3.0, 'fStops': 'APERTURE_4_0', 'bokehSizeMuliplier': 1.0, 'hexToCircleScale': 1.0},
    'SLightFlickering': {'flickerPeriod': 0.2},
    'SCurveData': {'value type': 'CVT_Float', 'type': 'CT_Segmented', 'Curve Values': [
        {'me': t, 'lue': t, 'ntrolPoint': {'X': -0.1, 'Y': 0.0, 'Z': 0.1, 'W': 0.0}, 'rveTypeL': 1, 'rveTypeR': 1}
        for t in (0.0, 1.0)]},  # a linear 0-1 ramp to edit; the engine default has no points
}


def struct_default(kind):
    """Engine default value of a struct, for a newly authored event."""
    zero = {'Bool': False, 'Float': 0.0, 'Int32': 0}
    return copy.deepcopy({name: STRUCT_DEFAULTS.get(kind, {}).get(name, [] if t.startswith('array:') else zero.get(t, ''))
                          for name, t in STRUCTS[kind].items()})


def children(prop):
    for attr in ('More', 'MoreProps', 'PROPS'):
        values = getattr(prop, attr, None)
        if values:
            return values
    return []


def field_type(prop):
    result = {'type': str(getattr(prop, 'theType', '') or '')}
    nested = children(prop)
    if nested and result['type'].startswith('array:'):
        # Array of structs: unnamed ELEMENTs, or the bare members of a single one.
        single = hasattr(nested[0], 'theName')
        result['elements'] = [{'type': '', 'fields': {p.theName: field_type(p) for p in nested}}] if single else [field_type(p) for p in nested]
        return result
    if nested:
        result['fields'] = {p.theName: field_type(p) for p in nested}
    handles = getattr(prop, 'Handles', None) or ([prop] if isinstance(prop, HANDLE) else [])
    elements = iter_prop_elements(prop) or (handles if result['type'].startswith('array:') else [])
    if elements:
        result['elements'] = [field_type(p) for p in elements]
    if any(getattr(h, 'ChunkHandle', False) and getattr(h, 'Reference', None) is not None for h in handles):
        result['internal_reference'] = True
    return result


def make_field(name, spec, value):
    from .anims_builder import _make_cname_prop, _make_string_prop, _make_taglist_prop
    kind = spec['type'] if isinstance(spec, dict) else spec
    spec = spec if isinstance(spec, dict) else {'type': kind}
    if spec.get('internal_reference'):
        raise ValueError(f"Event field '{name}' references another source chunk; use an unchanged replacement")
    if kind == 'CName':
        return _make_cname_prop(name, value)
    if kind in {'String', 'StringAnsi'}:
        return _make_string_prop(name, value, kind)
    if kind == 'TagList':
        return _make_taglist_prop(name, value)
    if kind in Enums.Enum_Types or kind in Enums.Enum_Flags_Types:
        enum = CEnum(None)
        enum.String = value if isinstance(value, str) else ''
        enum.strings = [value] if isinstance(value, str) else list(value or [])
        return PROPERTY(theName=name, theType=kind, Index=enum)
    if kind.startswith(('handle:', 'soft:', 'ptr:')):
        if kind.startswith('ptr:') and value:
            raise ValueError(f"Event field '{name}' requires a source chunk reference")
        handle = HANDLE(ChunkHandle=False, ClassName=kind.split(':', 1)[1], DepotPath=value or None,
                        Flags=4 if kind.startswith('soft:') else 0, val=0)
        return PROPERTY(theName=name, theType=kind, Handles=[handle])
    if kind.startswith('array:'):
        element_type = kind.rsplit(',', 1)[1]
        types = spec.get('elements', [])
        elements = [make_field(element_type, dict(types[i], type=element_type) if i < len(types) else element_type, v)
                    for i, v in enumerate(value or [])]
        if element_type.startswith(('handle:', 'soft:', 'ptr:')):
            elements = [p.Handles[0] for p in elements]
        return PROPERTY(theName=name, theType=kind, elements=elements)
    if kind in {'Bool', 'Float', 'CFloat', 'Uint8', 'Uint16', 'Uint32', 'Int8', 'Int16', 'Int32'}:
        return PROPERTY(theName=name, theType='Float' if kind == 'CFloat' else kind, Value=value)
    if isinstance(value, dict):
        fields = {**LEGACY_FIELDS.get(kind, {}), **STRUCTS.get(kind, {}), **spec.get('fields', {})}
        if kind in {'Vector', 'Vector3', 'EulerAngles', 'Color'}:
            fields = {n: 'Uint8' if kind == 'Color' else 'Float' for n in value}
        missing = set(value) - set(fields)
        if missing:
            raise ValueError(f"Event field '{name}' has unknown types: {', '.join(sorted(missing))}")
        return PROPERTY(theName=name, theType=kind, More=[make_field(n, fields[n], v) for n, v in value.items()])
    raise ValueError(f"Unsupported event field '{name}' ({kind})")
