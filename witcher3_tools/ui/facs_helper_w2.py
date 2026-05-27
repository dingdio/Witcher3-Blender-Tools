"""Witcher 2 ARKit/FACS control mapping for native mimic morphs.

This module is intentionally separate from facs_helper.py.  Witcher 3 uses the
w3fac-style morph names; Witcher 2 uses float-track names from
characters/templates/mimics/floattracks.w2rig.
"""

from .facs_helper import ARKIT_FACS_CHANNELS


# Witcher 2 support: ARKit/FACS channels mapped onto W2 native mimic poses.
# These are practical approximations over the W2 track set, not a 1:1 ARKit rig.
FACS_TO_W2_MORPHS = {
    "browDownLeft": [("EyebrowLeftIn", 0.75), ("SquintLeft", 0.2)],
    "browDownRight": [("EyebrowRightIn", 0.75), ("SquintRight", 0.2)],
    "browInnerUp": [("EyebrowLeftUp", 0.45), ("EyebrowRightUp", 0.45)],
    "browOuterUpLeft": [("EyebrowLeftUp", 0.85)],
    "browOuterUpRight": [("EyebrowRightUp", 0.85)],
    "cheekPuff": [("ChickLeftUp", 0.35), ("ChickRightUp", 0.35), ("Pout", 0.25)],
    "cheekSquintLeft": [("ChickLeftUp", 0.75), ("SquintLeft", 0.35)],
    "cheekSquintRight": [("ChickRightUp", 0.75), ("SquintRight", 0.35)],
    "eyeBlinkLeft": [("BlinkLeft", 1.0)],
    "eyeBlinkRight": [("BlinkRight", 1.0)],
    "eyeLookDownLeft": [("EyeLeftV", 0.5), ("EyeLeftDownTrackLeft", 0.25), ("EyeLeftDownTrackRight", 0.25)],
    "eyeLookDownRight": [("EyeRightV", 0.5), ("EyeRightDownTrackLeft", 0.25), ("EyeRightDownTrackRight", 0.25)],
    "eyeLookInLeft": [("EyeLeftH", 0.5), ("EyeLeftUpTrackRight", 0.25), ("EyeLeftDownTrackRight", 0.25)],
    "eyeLookInRight": [("EyeRightH", 0.5), ("EyeRightUpTrackLeft", 0.25), ("EyeRightDownTrackLeft", 0.25)],
    "eyeLookOutLeft": [("EyeLeftH", 0.5), ("EyeLeftUpTrackLeft", 0.25), ("EyeLeftDownTrackLeft", 0.25)],
    "eyeLookOutRight": [("EyeRightH", 0.5), ("EyeRightUpTrackRight", 0.25), ("EyeRightDownTrackRight", 0.25)],
    "eyeLookUpLeft": [("EyeLeftV", 0.5), ("EyeLeftUpTrackLeft", 0.25), ("EyeLeftUpTrackRight", 0.25)],
    "eyeLookUpRight": [("EyeRightV", 0.5), ("EyeRightUpTrackLeft", 0.25), ("EyeRightUpTrackRight", 0.25)],
    "eyeSquintLeft": [("SquintLeft", 1.0)],
    "eyeSquintRight": [("SquintRight", 1.0)],
    "eyeWideLeft": [("EyeLidUpperTracking", 0.35)],
    "eyeWideRight": [("EyeLidUpperTracking", 0.35)],
    "jawForward": [("Jaw_Front", 1.0)],
    "jawLeft": [("Jaw_Left", 1.0)],
    "jawOpen": [("Open", 1.0)],
    "jawRight": [("Jaw_Right", 1.0)],
    "mouthClose": [("LipLowInLeft", 0.35), ("LipLowInRight", 0.35), ("LipHiInLeft", 0.35), ("LipHiInRight", 0.35)],
    "mouthDimpleLeft": [("LipLowInLeft", 0.45), ("LipHiInLeft", 0.35), ("MouthLeft", 0.2)],
    "mouthDimpleRight": [("LipLowInRight", 0.45), ("LipHiInRight", 0.35), ("MouthRight", 0.2)],
    "mouthFrownLeft": [("FrownLeft", 1.0)],
    "mouthFrownRight": [("FrownRight", 1.0)],
    "mouthFunnel": [("Narrow", 0.65), ("Pout", 0.45), ("WhistleUp", 0.25), ("WhistleDown", 0.25)],
    "mouthLeft": [("MouthLeft", 1.0)],
    "mouthLowerDownLeft": [("LipLowDownLeft", 1.0)],
    "mouthLowerDownRight": [("LipLowDownRight", 1.0)],
    "mouthPressLeft": [("LipLowInLeft", 0.6), ("LipHiInLeft", 0.35)],
    "mouthPressRight": [("LipLowInRight", 0.6), ("LipHiInRight", 0.35)],
    "mouthPucker": [("Pout", 0.75), ("Narrow", 0.35), ("WhistleUp", 0.2), ("WhistleDown", 0.2)],
    "mouthRight": [("MouthRight", 1.0)],
    "mouthRollLower": [("LipLowInLeft", 0.65), ("LipLowInRight", 0.65)],
    "mouthRollUpper": [("LipHiInLeft", 0.65), ("LipHiInRight", 0.65)],
    "mouthShrugLower": [("Mouth_Up", 0.45), ("LipLowInLeft", 0.25), ("LipLowInRight", 0.25)],
    "mouthShrugUpper": [("Mouth_Up", 0.55), ("LipHiInLeft", 0.25), ("LipHiInRight", 0.25)],
    "mouthSmileLeft": [("SmileLeft", 1.0)],
    "mouthSmileRight": [("SmileRight", 1.0)],
    "mouthStretchLeft": [("SmileLeft", 0.45), ("MouthLeft", 0.35)],
    "mouthStretchRight": [("SmileRight", 0.45), ("MouthRight", 0.35)],
    "mouthUpperUpLeft": [("LipHiUpLeft", 1.0)],
    "mouthUpperUpRight": [("LipHiUpRight", 1.0)],
    "noseSneerLeft": [("Nose", 0.45), ("ChickLeftUp", 0.25)],
    "noseSneerRight": [("Nose", 0.45), ("ChickRightUp", 0.25)],
    "tongueOut": [("TongueUp", 0.65), ("TongueDown", 0.35)],
}


def get_facs_channels():
    return list(ARKIT_FACS_CHANNELS)


def build_witcher_morph_terms(available_morphs=None, existing_facs_props=None):
    """Return {w2_morph: {facs_channel: weight}} for available W2 morphs."""
    available = set(available_morphs or [])
    existing = None if existing_facs_props is None else set(existing_facs_props)
    terms = {}
    for facs_name, morph_terms in FACS_TO_W2_MORPHS.items():
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
    for morph_name, weight in FACS_TO_W2_MORPHS.get(facs_name, []):
        if available and morph_name not in available:
            continue
        terms.append((morph_name, float(weight)))
    return terms
