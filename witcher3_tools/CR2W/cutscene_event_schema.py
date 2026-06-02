"""RTTI-backed metadata for REDengine cutscene animation events.

W2 and W3 reuse many CExtAnim* class names, but the serialized layouts are not
the same. Keep those layouts as separate snapshots and normalize parsed event
data through the game-specific schema.
"""

from dataclasses import dataclass

from . import w3_types


GAME_W2 = "W2"
GAME_W3 = "W3"


@dataclass(frozen=True)
class CutsceneEventField:
    name: str
    type_name: str


@dataclass(frozen=True)
class CutsceneEventClass:
    name: str
    base: str
    size: int
    own_fields: tuple[CutsceneEventField, ...]


def _field(name, type_name):
    return CutsceneEventField(name, type_name)


def _event_class(name, base, size, fields):
    return CutsceneEventClass(
        name=name,
        base=base,
        size=int(size or 0),
        own_fields=tuple(_field(field_name, type_name) for field_name, type_name in fields),
    )


# W2 REDkit editor RTTI: every class whose base chain reaches CExtAnimEvent.
_W2_EVENT_LAYOUTS = (
    ("CExtAnimEvent", "", 36, (
        ("eventName", "CName"),
        ("startTime", "Float"),
        ("reportToScript", "Bool"),
        ("animationName", "CName"),
        ("trackName", "String"),
    )),
    ("CExtAnimDurationEvent", "CExtAnimEvent", 40, (
        ("duration", "Float"),
    )),
    ("CExtAnimComboEvent", "CExtAnimDurationEvent", 40, ()),
    ("CExtAnimCutsceneActorEffect", "CExtAnimDurationEvent", 44, (
        ("effectName", "CName"),
    )),
    ("CExtAnimCutsceneAmbientEvent", "CExtAnimEvent", 44, (
        ("dbAmbientsVolume", "Float"),
        ("fadeTime", "Float"),
    )),
    ("CExtAnimCutsceneBodyPartEvent", "CExtAnimEvent", 44, (
        ("bodyPart", "CName"),
        ("state", "CName"),
    )),
    ("CExtAnimCutsceneBreakEvent", "CExtAnimEvent", 40, (
        ("iAmHackDoNotUseMeInGame", "Bool"),
    )),
    ("CExtAnimCutsceneDialogEvent", "CExtAnimEvent", 36, ()),
    ("CExtAnimCutsceneEffectEvent", "CExtAnimDurationEvent", 112, (
        ("effect", "CName"),
        ("tag", "TagList"),
        ("template", "~CEntityTemplate"),
        ("spawnPosMS", "Vector"),
        ("spawnRotMS", "EulerAngles"),
    )),
    ("CExtAnimCutsceneEnvironmentEvent", "CExtAnimEvent", 64, (
        ("stabilizeBlending", "Bool"),
        ("instantEyeAdaptation", "Bool"),
        ("instantDissolve", "Bool"),
        ("forceSetupLocalEnvironments", "Bool"),
        ("forceSetupGlobalEnvironments", "Bool"),
        ("environmentName", "String"),
        ("environmentActivate", "Bool"),
        ("forceNoOtherEnvironments", "Bool"),
    )),
    ("CExtAnimCutsceneFadeEvent", "CExtAnimEvent", 48, (
        ("in", "Bool"),
        ("duration", "Float"),
        ("color", "Color"),
    )),
    ("CExtAnimCutsceneFreezeDangleEvent", "CExtAnimDurationEvent", 40, ()),
    ("CExtAnimCutsceneQteEvent", "CExtAnimDurationEvent", 48, (
        ("action", "CName"),
        ("shouldBeChecked", "Bool"),
    )),
    ("CExtAnimCutscenePlayerQteEvent", "CExtAnimCutsceneQteEvent", 48, ()),
    ("CExtAnimCutscenePlayerQteMashEvent", "CExtAnimCutscenePlayerQteEvent", 64, (
        ("initialValue", "Float"),
        ("decayPerSecond", "Float"),
        ("increasePerMash", "Float"),
        ("mashType", "EQteMashType"),
    )),
    ("CExtAnimCutscenePlayerQteSinglePushEvent", "CExtAnimCutscenePlayerQteEvent", 52, (
        ("position", "EQTEPosition"),
    )),
    ("CExtAnimCutsceneQuestEvent", "CExtAnimEvent", 52, (
        ("cutsceneName", "String"),
    )),
    ("CExtAnimCutsceneSoundEvent", "CExtAnimEvent", 60, (
        ("soundEventName", "String"),
        ("bone", "CName"),
        ("useMaterialInfo", "Bool"),
    )),
    ("CExtAnimDropItemEvent", "CExtAnimEvent", 40, (
        ("action", "EDropAction"),
    )),
    ("CExtAnimExplorationEvent", "CExtAnimDurationEvent", 40, ()),
    ("CExtAnimFootstepEvent", "CExtAnimSoundEvent", 64, ()),
    ("CExtAnimHitEvent", "CExtAnimEvent", 40, (
        ("hitLevel", "Uint"),
    )),
    ("CExtAnimItemAnimationEvent", "CExtAnimEvent", 44, (
        ("itemCategory", "CName"),
        ("itemAnimationName", "CName"),
    )),
    ("CExtAnimItemBehaviorEvent", "CExtAnimEvent", 44, (
        ("itemCategory", "CName"),
        ("event", "CName"),
    )),
    ("CExtAnimItemEffectEvent", "CExtAnimEvent", 48, (
        ("effectName", "CName"),
        ("hand", "ECharacterHand"),
        ("action", "EItemEffectAction"),
    )),
    ("CExtAnimItemEvent", "CExtAnimEvent", 72, (
        ("category", "CName"),
        ("itemName_optional", "CName"),
        ("action", "EItemAction"),
        ("restoreOnEnd", "Bool"),
    )),
    ("CExtAnimItemSyncDurationEvent", "CExtAnimDurationEvent", 52, (
        ("equipSlot", "CName"),
        ("holdSlot", "CName"),
        ("action", "EItemLatentAction"),
    )),
    ("CExtAnimItemSyncEvent", "CExtAnimEvent", 48, (
        ("equipSlot", "CName"),
        ("holdSlot", "CName"),
        ("action", "EItemLatentAction"),
    )),
    ("CExtAnimItemSyncWithCorrectionEvent", "CExtAnimDurationEvent", 56, (
        ("equipSlot", "CName"),
        ("holdSlot", "CName"),
        ("action", "EItemLatentAction"),
        ("correctionBone", "CName"),
    )),
    ("CExtAnimLookAtEvent", "CExtAnimDurationEvent", 44, (
        ("level", "ELookAtLevel"),
    )),
    ("CExtAnimMusicEvent", "CExtAnimEvent", 60, (
        ("cue", "String"),
        ("volume", "Float"),
        ("stop", "Bool"),
    )),
    ("CExtAnimProjectileEvent", "CExtAnimEvent", 48, (
        ("spell", "*CEntityTemplate"),
        ("castPosition", "EProjectileCastPosition"),
        ("boneName", "CName"),
    )),
    ("CExtAnimReattachItemEvent", "CExtAnimDurationEvent", 48, (
        ("item", "CName"),
        ("targetSlot", "CName"),
    )),
    ("CExtAnimSoundEvent", "CExtAnimEvent", 64, (
        ("soundEventName", "String"),
        ("maxDistance", "Float"),
        ("bone", "CName"),
        ("useMaterialInfo", "Bool"),
    )),
)


# W3 REDkit editor RTTI: every class whose base chain reaches CExtAnimEvent.
_W3_EVENT_LAYOUTS = (
    ("CExtAnimEvent", "", 40, (
        ("eventName", "CName"),
        ("startTime", "Float"),
        ("reportToScript", "Bool"),
        ("reportToScriptMinWeight", "Float"),
        ("animationName", "CName"),
        ("trackName", "String"),
    )),
    ("CExtAnimDurationEvent", "CExtAnimEvent", 48, (
        ("duration", "Float"),
        ("alwaysFiresEnd", "Bool"),
    )),
    ("CEASEnumEvent", "CExtAnimScriptDurationEvent", 56, (
        ("enumVariant", "SEnumVariant"),
    )),
    ("CEASMultiValueEvent", "CExtAnimScriptDurationEvent", 104, (
        ("callback", "CName"),
        ("properties", "SMultiValue"),
    )),
    ("CEASMultiValueSimpleEvent", "CExtAnimScriptEvent", 96, (
        ("callback", "CName"),
        ("properties", "SMultiValue"),
    )),
    ("CEASSlideToTargetEvent", "CExtAnimScriptDurationEvent", 64, (
        ("properties", "SSlideToTargetEventProps"),
    )),
    ("CExpSlideEvent", "CExtAnimDurationEvent", 56, (
        ("translation", "Bool"),
        ("rotation", "Bool"),
        ("toCollision", "Bool"),
    )),
    ("CExpSyncEvent", "CExtAnimEvent", 48, (
        ("translation", "Bool"),
        ("rotation", "Bool"),
    )),
    ("CExtAnimAttackEvent", "CExtAnimEvent", 48, (
        ("soundAttackType", "CName"),
    )),
    ("CExtAnimComboEvent", "CExtAnimDurationEvent", 48, ()),
    ("CExtAnimCutsceneActorEffect", "CExtAnimDurationEvent", 56, (
        ("effectName", "CName"),
    )),
    ("CExtAnimCutsceneBodyPartEvent", "CExtAnimEvent", 48, (
        ("appearance", "CName"),
    )),
    ("CExtAnimCutsceneBokehDofBlendEvent", "CExtAnimDurationEvent", 88, (
        ("bokehDofParamsStart", "SBokehDofParams"),
        ("bokehDofParamsEnd", "SBokehDofParams"),
    )),
    ("CExtAnimCutsceneBokehDofEvent", "CExtAnimEvent", 64, (
        ("bokehDofParams", "SBokehDofParams"),
    )),
    ("CExtAnimCutsceneBreakEvent", "CExtAnimEvent", 48, (
        ("iAmHackDoNotUseMeInGame", "Bool"),
    )),
    ("CExtAnimCutsceneDialogEvent", "CExtAnimEvent", 40, ()),
    ("CExtAnimCutsceneDisableClothEvent", "CExtAnimEvent", 48, (
        ("weight", "Float"),
        ("blendTime", "Float"),
    )),
    ("CExtAnimCutsceneDisableDangleEvent", "CExtAnimEvent", 48, (
        ("weight", "Float"),
    )),
    ("CExtAnimCutsceneDurationEvent", "CExtAnimDurationEvent", 48, ()),
    ("CExtAnimCutsceneEffectEvent", "CExtAnimDurationEvent", 128, (
        ("effect", "CName"),
        ("tag", "TagList"),
        ("template", "soft:CEntityTemplate"),
        ("spawnPosMS", "Vector"),
        ("spawnRotMS", "EulerAngles"),
    )),
    ("CExtAnimCutsceneEnvironmentEvent", "CExtAnimEvent", 64, (
        ("stabilizeBlending", "Bool"),
        ("instantEyeAdaptation", "Bool"),
        ("instantDissolve", "Bool"),
        ("forceSetupLocalEnvironments", "Bool"),
        ("forceSetupGlobalEnvironments", "Bool"),
        ("environmentName", "String"),
        ("environmentActivate", "Bool"),
        ("forceNoOtherEnvironments", "Bool"),
    )),
    ("CExtAnimCutsceneEvent", "CExtAnimEvent", 40, ()),
    ("CExtAnimCutsceneFadeEvent", "CExtAnimEvent", 56, (
        ("in", "Bool"),
        ("duration", "Float"),
        ("color", "Color"),
    )),
    ("CExtAnimCutsceneHideEntityEvent", "CExtAnimCutsceneEvent", 48, (
        ("entTohideTag", "CName"),
    )),
    ("CExtAnimCutsceneHideTerrainEvent", "CExtAnimCutsceneDurationEvent", 48, ()),
    ("CExtAnimCutsceneLightEvent", "CExtAnimEvent", 80, (
        ("tag", "TagList"),
        ("isEnabled", "Bool"),
        ("radius", "Float"),
        ("brightness", "Float"),
        ("color", "Color"),
        ("lightFlickering", "SLightFlickering"),
    )),
    ("CExtAnimCutsceneQuestEvent", "CExtAnimEvent", 56, (
        ("cutsceneName", "String"),
    )),
    ("CExtAnimCutsceneResetClothAndDangleEvent", "CExtAnimEvent", 48, (
        ("forceRelaxedState", "Bool"),
    )),
    ("CExtAnimCutsceneSetClippingPlanesEvent", "CExtAnimEvent", 56, (
        ("nearPlaneDistance", "ENearPlaneDistance"),
        ("farPlaneDistance", "EFarPlaneDistance"),
        ("customPlaneDistance", "SCustomClippingPlanes"),
    )),
    ("CExtAnimCutsceneSlowMoEvent", "CExtAnimCutsceneDurationEvent", 96, (
        ("enabled", "Bool"),
        ("factor", "Float"),
        ("useWeightCurve", "Bool"),
        ("weightCurve", "SCurveData"),
    )),
    ("CExtAnimCutsceneSoundEvent", "CExtAnimEvent", 64, (
        ("soundEventName", "String"),
        ("bone", "CName"),
        ("useMaterialInfo", "Bool"),
    )),
    ("CExtAnimCutsceneSurfaceEffect", "CExtAnimCutsceneEvent", 96, (
        ("type", "ESceneEventSurfacePostFXType"),
        ("worldPos", "Bool"),
        ("position", "Vector"),
        ("radius", "Float"),
        ("fadeInTime", "Float"),
        ("fadeOutTime", "Float"),
        ("durationTime", "Float"),
    )),
    ("CExtAnimCutsceneWindEvent", "CExtAnimCutsceneDurationEvent", 96, (
        ("enabled", "Bool"),
        ("factor", "Float"),
        ("useWeightCurve", "Bool"),
        ("weightCurve", "SCurveData"),
    )),
    ("CExtAnimDialogKeyPoseDuration", "CExtAnimDurationEvent", 56, (
        ("transition", "Bool"),
        ("keyPose", "Bool"),
    )),
    ("CExtAnimDialogKeyPoseMarker", "CExtAnimEvent", 40, ()),
    ("CExtAnimDisableDialogLookatEvent", "CExtAnimDurationEvent", 56, (
        ("speed", "Float"),
    )),
    ("CExtAnimDropItemEvent", "CExtAnimEvent", 48, (
        ("action", "EDropAction"),
    )),
    ("CExtAnimEffectDurationEvent", "CExtAnimDurationEvent", 56, (
        ("effectName", "CName"),
    )),
    ("CExtAnimEffectEvent", "CExtAnimEvent", 48, (
        ("effectName", "CName"),
        ("action", "EAnimEffectAction"),
    )),
    ("CExtAnimExplorationEvent", "CExtAnimDurationEvent", 48, ()),
    ("CExtAnimFootstepEvent", "CExtAnimSoundEvent", 112, (
        ("fx", "Bool"),
        ("customFxName", "CName"),
    )),
    ("CExtAnimGameplayMimicEvent", "CExtAnimDurationEvent", 56, (
        ("animation", "CName"),
    )),
    ("CExtAnimHitEvent", "CExtAnimEvent", 48, (
        ("hitLevel", "Uint32"),
    )),
    ("CExtAnimItemAnimationEvent", "CExtAnimEvent", 48, (
        ("itemCategory", "CName"),
        ("itemAnimationName", "CName"),
    )),
    ("CExtAnimItemBehaviorEvent", "CExtAnimEvent", 48, (
        ("itemCategory", "CName"),
        ("event", "CName"),
    )),
    ("CExtAnimItemEffectDurationEvent", "CExtAnimDurationEvent", 56, (
        ("effectName", "CName"),
        ("itemSlot", "CName"),
    )),
    ("CExtAnimItemEffectEvent", "CExtAnimEvent", 56, (
        ("effectName", "CName"),
        ("itemSlot", "CName"),
        ("action", "EItemEffectAction"),
    )),
    ("CExtAnimItemEvent", "CExtAnimEvent", 72, (
        ("category", "CName"),
        ("itemName_optional", "CName"),
        ("action", "EItemAction"),
        ("ignoreItemsWithTag", "CName"),
        ("itemGetting", "EGettingItem"),
    )),
    ("CExtAnimItemSyncDurationEvent", "CExtAnimDurationEvent", 64, (
        ("equipSlot", "CName"),
        ("holdSlot", "CName"),
        ("action", "EItemLatentAction"),
    )),
    ("CExtAnimItemSyncEvent", "CExtAnimEvent", 56, (
        ("equipSlot", "CName"),
        ("holdSlot", "CName"),
        ("action", "EItemLatentAction"),
    )),
    ("CExtAnimItemSyncWithCorrectionEvent", "CExtAnimDurationEvent", 64, (
        ("equipSlot", "CName"),
        ("holdSlot", "CName"),
        ("action", "EItemLatentAction"),
        ("correctionBone", "CName"),
    )),
    ("CExtAnimLocationAdjustmentEvent", "CExtAnimDurationEvent", 56, (
        ("locationAdjustmentVar", "CName"),
        ("adjustmentActiveVar", "CName"),
    )),
    ("CExtAnimLookAtEvent", "CExtAnimDurationEvent", 56, (
        ("level", "ELookAtLevel"),
    )),
    ("CExtAnimMaterialBasedFxEvent", "CExtAnimEvent", 48, (
        ("bone", "CName"),
        ("vfxKickup", "Bool"),
        ("vfxFootstep", "Bool"),
    )),
    ("CExtAnimMorphEvent", "CExtAnimDurationEvent", 88, (
        ("morphComponentId", "CName"),
        ("invertWeight", "Bool"),
        ("useCurve", "Bool"),
        ("curve", "SCurveData"),
    )),
    ("CExtAnimOnSlopeEvent", "CExtAnimDurationEvent", 56, (
        ("slopeAngle", "Float"),
    )),
    ("CExtAnimProjectileEvent", "CExtAnimEvent", 56, (
        ("spell", "handle:CEntityTemplate"),
        ("castPosition", "EProjectileCastPosition"),
        ("boneName", "CName"),
    )),
    ("CExtAnimRaiseEventEvent", "CExtAnimEvent", 48, (
        ("eventToBeRaisedName", "CName"),
        ("forceRaiseEvent", "Bool"),
    )),
    ("CExtAnimReattachItemEvent", "CExtAnimDurationEvent", 56, (
        ("item", "CName"),
        ("targetSlot", "CName"),
    )),
    ("CExtAnimRotationAdjustmentEvent", "CExtAnimDurationEvent", 56, (
        ("rotationAdjustmentVar", "CName"),
    )),
    ("CExtAnimRotationAdjustmentLocationBasedEvent", "CExtAnimDurationEvent", 64, (
        ("locationAdjustmentVar", "CName"),
        ("targetLocationVar", "CName"),
        ("adjustmentActiveVar", "CName"),
    )),
    ("CExtAnimScriptDurationEvent", "CExtAnimDurationEvent", 48, ()),
    ("CExtAnimScriptEvent", "CExtAnimEvent", 40, ()),
    ("CExtAnimSoundEvent", "CExtAnimEvent", 104, (
        ("soundEventName", "String"),
        ("maxDistance", "Float"),
        ("bone", "CName"),
        ("switchesToUpdate", "array:2,0,String"),
        ("parametersToUpdate", "array:2,0,String"),
        ("filter", "Bool"),
        ("filterCooldown", "Float"),
        ("useDistanceParameter", "Bool"),
        ("speed", "Float"),
        ("decelDist", "Float"),
    )),
    ("CExtForcedLogicalFootstepAnimEvent", "CExtAnimEvent", 48, (
        ("side", "ESide"),
    )),
    ("CPreAttackEvent", "CExtAnimDurationEvent", 120, (
        ("data", "CPreAttackEventData"),
    )),
)


def _make_schema(layouts):
    return {
        name: _event_class(name, base, size, fields)
        for name, base, size, fields in layouts
    }


W2_CEXT_ANIM_EVENT_CLASSES = _make_schema(_W2_EVENT_LAYOUTS)
W3_CEXT_ANIM_EVENT_CLASSES = _make_schema(_W3_EVENT_LAYOUTS)
_SCHEMAS = {
    GAME_W2: W2_CEXT_ANIM_EVENT_CLASSES,
    GAME_W3: W3_CEXT_ANIM_EVENT_CLASSES,
}


_CTOR_FIELD_NAMES = {"eventName", "startTime", "duration", "animationName", "trackName"}
_FIELD_ATTR_ALIASES = {
    "appearance": "appearance",
    "bodyPart": "body_part",
    "effect": "effect_name",
    "effectName": "effect_name",
    "soundEventName": "sound_event_name",
}


def normalize_game(game):
    game = str(game or "").strip().upper()
    if game in {"W2", "TW2", "WITCHER2"}:
        return GAME_W2
    if game in {"W3", "TW3", "WITCHER3"}:
        return GAME_W3
    return game


def get_event_schema(game):
    return _SCHEMAS.get(normalize_game(game), {})


def get_event_class(game, class_name):
    return get_event_schema(game).get(str(class_name or ""))


def event_inherits_from(game, class_name, base_name):
    schema = get_event_schema(game)
    class_name = str(class_name or "")
    base_name = str(base_name or "")
    seen = set()
    while class_name and class_name not in seen:
        if class_name == base_name:
            return True
        seen.add(class_name)
        event_class = schema.get(class_name)
        class_name = event_class.base if event_class else ""
    return False


def iter_event_classes(game):
    return tuple(get_event_schema(game).values())


def event_declared_fields(game, class_name):
    schema = get_event_schema(game)
    event_class = schema.get(str(class_name or ""))
    if event_class is None:
        return ()
    fields = []
    if event_class.base:
        fields.extend(event_declared_fields(game, event_class.base))
    fields.extend(event_class.own_fields)
    return tuple(fields)


def event_declared_field_names(game, class_name):
    return tuple(field.name for field in event_declared_fields(game, class_name))


def _coerce_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _str_value(value):
    if value is None:
        return ""
    return str(value)


def build_event_data(game, type_name, raw_fields):
    """Build a CExtAnimEventData while preserving game-specific raw fields."""
    game = normalize_game(game)
    type_name = str(type_name or "")
    raw_fields = dict(raw_fields or {})

    extra = {
        "raw_fields": raw_fields,
        "source_game": game,
        "schema_known": get_event_class(game, type_name) is not None,
    }
    for field_name, value in raw_fields.items():
        if field_name not in _CTOR_FIELD_NAMES:
            extra[field_name] = value

        alias = _FIELD_ATTR_ALIASES.get(field_name)
        if alias:
            extra[alias] = value

    return w3_types.CExtAnimEventData(
        type_name=type_name,
        event_name=_str_value(raw_fields.get("eventName")),
        start_time=_coerce_float(raw_fields.get("startTime"), 0.0),
        duration=_coerce_float(raw_fields.get("duration"), 0.0),
        animation_name=_str_value(raw_fields.get("animationName")),
        track_name=_str_value(raw_fields.get("trackName")),
        **extra,
    )
