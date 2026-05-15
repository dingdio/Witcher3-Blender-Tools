"""ARKit/FACS control mapping for Witcher mimic morphs."""

ARKIT_FACS_CHANNELS = [
    "browDownLeft",
    "browDownRight",
    "browInnerUp",
    "browOuterUpLeft",
    "browOuterUpRight",
    "cheekPuff",
    "cheekSquintLeft",
    "cheekSquintRight",
    "eyeBlinkLeft",
    "eyeBlinkRight",
    "eyeLookDownLeft",
    "eyeLookDownRight",
    "eyeLookInLeft",
    "eyeLookInRight",
    "eyeLookOutLeft",
    "eyeLookOutRight",
    "eyeLookUpLeft",
    "eyeLookUpRight",
    "eyeSquintLeft",
    "eyeSquintRight",
    "eyeWideLeft",
    "eyeWideRight",
    "jawForward",
    "jawLeft",
    "jawOpen",
    "jawRight",
    "mouthClose",
    "mouthDimpleLeft",
    "mouthDimpleRight",
    "mouthFrownLeft",
    "mouthFrownRight",
    "mouthFunnel",
    "mouthLeft",
    "mouthLowerDownLeft",
    "mouthLowerDownRight",
    "mouthPressLeft",
    "mouthPressRight",
    "mouthPucker",
    "mouthRight",
    "mouthRollLower",
    "mouthRollUpper",
    "mouthShrugLower",
    "mouthShrugUpper",
    "mouthSmileLeft",
    "mouthSmileRight",
    "mouthStretchLeft",
    "mouthStretchRight",
    "mouthUpperUpLeft",
    "mouthUpperUpRight",
    "noseSneerLeft",
    "noseSneerRight",
    "tongueOut",
]


FACS_TO_WITCHER_MORPHS = {
    "browDownLeft": [("brow_left_down", 1.0), ("brow_left_in", 0.35)],
    "browDownRight": [("brow_right_down", 1.0), ("brow_right_in", 0.35)],
    "browInnerUp": [("brow_inner_left_up", 1.0), ("brow_inner_right_up", 1.0)],
    "browOuterUpLeft": [("brow_outer_left_up", 1.0)],
    "browOuterUpRight": [("brow_outer_right_up", 1.0)],
    "cheekPuff": [("lips_blow", 0.75), ("cheek_left_up", 0.25), ("cheek_right_up", 0.25)],
    "cheekSquintLeft": [("cheek_left_up", 0.75), ("eyelids_lower_left_up", 0.35)],
    "cheekSquintRight": [("cheek_right_up", 0.75), ("eyelids_lower_right_up", 0.35)],
    "eyeBlinkLeft": [("eyelids_upper_left_down", 0.85), ("eyelids_lower_left_up", 0.35)],
    "eyeBlinkRight": [("eyelids_upper_right_down", 0.85), ("eyelids_lower_right_up", 0.35)],
    "eyeLookDownLeft": [("eye_left_down", 1.0)],
    "eyeLookDownRight": [("eye_right_down", 1.0)],
    "eyeLookInLeft": [("eye_left_right", 1.0)],
    "eyeLookInRight": [("eye_right_left", 1.0)],
    "eyeLookOutLeft": [("eye_left_left", 1.0)],
    "eyeLookOutRight": [("eye_right_right", 1.0)],
    "eyeLookUpLeft": [("eye_left_up", 1.0)],
    "eyeLookUpRight": [("eye_right_up", 1.0)],
    "eyeSquintLeft": [("eyelids_upper_left_down", 0.35), ("eyelids_lower_left_up", 0.6)],
    "eyeSquintRight": [("eyelids_upper_right_down", 0.35), ("eyelids_lower_right_up", 0.6)],
    "eyeWideLeft": [("eyelids_upper_left_up", 0.8), ("eyelids_lower_left_down", 0.35)],
    "eyeWideRight": [("eyelids_upper_right_up", 0.8), ("eyelids_lower_right_down", 0.35)],
    "jawForward": [("jaw_front", 1.0)],
    "jawLeft": [("jaw_left", 1.0)],
    "jawOpen": [("jaw_open_a", 1.0)],
    "jawRight": [("jaw_right", 1.0)],
    "mouthClose": [("lips_in", 0.45), ("lip_lower_center_up", 0.35), ("lip_upper_center_down", 0.35)],
    "mouthDimpleLeft": [("corner_left_in", 0.65), ("corner_left_tight", 0.35)],
    "mouthDimpleRight": [("corner_right_in", 0.65), ("corner_right_tight", 0.35)],
    "mouthFrownLeft": [("corner_left_down", 1.0)],
    "mouthFrownRight": [("corner_right_down", 1.0)],
    "mouthFunnel": [("lips_out", 0.65), ("lip_upper_front", 0.55), ("lip_lower_front", 0.55)],
    "mouthLeft": [("lips_left", 1.0)],
    "mouthLowerDownLeft": [("lip_lower_left_down", 1.0)],
    "mouthLowerDownRight": [("lip_lower_right_down", 1.0)],
    "mouthPressLeft": [("corner_left_tight", 0.65), ("lip_lower_left_up", 0.25)],
    "mouthPressRight": [("corner_right_tight", 0.65), ("lip_lower_right_up", 0.25)],
    "mouthPucker": [("lips_blow", 0.75), ("lips_out", 0.35)],
    "mouthRight": [("lips_right", 1.0)],
    "mouthRollLower": [("lip_lower_in", 0.85), ("lips_in", 0.25)],
    "mouthRollUpper": [("lip_upper_in", 0.85), ("lips_in", 0.25)],
    "mouthShrugLower": [("lip_lower_center_up", 0.8), ("lip_lower_left_up", 0.35), ("lip_lower_right_up", 0.35)],
    "mouthShrugUpper": [("lip_upper_center_down", 0.8), ("lip_upper_left_down", 0.35), ("lip_upper_right_down", 0.35)],
    "mouthSmileLeft": [("corner_left_up", 0.85), ("corner_left_spread", 0.45)],
    "mouthSmileRight": [("corner_right_up", 0.85), ("corner_right_spread", 0.45)],
    "mouthStretchLeft": [("corner_left_spread", 1.0)],
    "mouthStretchRight": [("corner_right_spread", 1.0)],
    "mouthUpperUpLeft": [("lip_upper_left_up", 1.0)],
    "mouthUpperUpRight": [("lip_upper_right_up", 1.0)],
    "noseSneerLeft": [("nose_left", 0.55), ("nose_up", 0.35), ("nostrils_out", 0.25)],
    "noseSneerRight": [("nose_right", 0.55), ("nose_up", 0.35), ("nostrils_out", 0.25)],
    "tongueOut": [("tongue_front", 1.0)],
}


def get_facs_channels():
    return list(ARKIT_FACS_CHANNELS)


def build_witcher_morph_terms(available_morphs=None, existing_facs_props=None):
    """Return {witcher_morph: {facs_channel: weight}} for available morphs."""
    available = set(available_morphs or [])
    existing = None if existing_facs_props is None else set(existing_facs_props)
    terms = {}
    for facs_name, morph_terms in FACS_TO_WITCHER_MORPHS.items():
        if existing is not None and facs_name not in existing:
            continue
        for morph_name, weight in morph_terms:
            if available and morph_name not in available:
                continue
            terms.setdefault(morph_name, {})[facs_name] = float(weight)
    return terms


def get_mapped_witcher_morphs(available_morphs=None):
    return sorted(build_witcher_morph_terms(available_morphs).keys())


def get_witcher_morphs_for_facs(facs_name, available_morphs=None):
    available = set(available_morphs or [])
    terms = []
    for morph_name, weight in FACS_TO_WITCHER_MORPHS.get(facs_name, []):
        if available and morph_name not in available:
            continue
        terms.append((morph_name, float(weight)))
    return terms
