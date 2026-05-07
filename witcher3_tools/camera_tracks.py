import math


CAMERA_TRACK_DEFAULTS = {
    "hctFOV": 35.0,
    "overrideFactor": 0.0,
    "dofFocusDistFar": 10.0,
    "dofBlurDistFar": 20.0,
    "dofIntensity": 0.0,
    "dofFocusDistNear": 5.0,
    "dofBlurDistNear": 0.0,
    "blenderDofFocusDistance": 0.0,
    "blenderDofFocusDistanceWeight": 0.0,
}

CAMERA_TRACK_NAMES = tuple(CAMERA_TRACK_DEFAULTS.keys())
CAMERA_DOF_TRACK_NAMES = (
    "overrideFactor",
    "dofFocusDistFar",
    "dofBlurDistFar",
    "dofIntensity",
    "dofFocusDistNear",
    "dofBlurDistNear",
    "blenderDofFocusDistance",
    "blenderDofFocusDistanceWeight",
)
CAMERA_CONTROL_BONE = "Camera_Node"
CAMERA_EDIT_BONE = "Camera_ManipulationNode"
CAMERA_SENSOR_HEIGHT = 43.266615300557
BLENDER_DOF_INTENSITY_FSTOP = 2.8
BLENDER_DOF_INTENSITY_BIAS = 0.25
BLENDER_DOF_FOCUS_RANGE_FSTOP = 2.0
BLENDER_DOF_FAR_BLUR_FSTOP = 0.25
BLENDER_DOF_NEAR_BLUR_FSTOP = 0.5


def is_camera_track_name(track_name: str) -> bool:
    return str(track_name or "") in CAMERA_TRACK_DEFAULTS


def ensure_camera_track_properties(armature_obj, track_names=None):
    if armature_obj is None or getattr(armature_obj, "type", None) != "ARMATURE":
        return None
    pose = getattr(armature_obj, "pose", None)
    pose_bones = getattr(pose, "bones", None) if pose else None
    camera_bone = pose_bones.get(CAMERA_CONTROL_BONE) if pose_bones else None
    if camera_bone is None:
        return None

    for track_name in track_names or CAMERA_TRACK_NAMES:
        track_name = str(track_name or "")
        if not track_name:
            continue
        if track_name not in camera_bone:
            camera_bone[track_name] = float(CAMERA_TRACK_DEFAULTS.get(track_name, 0.0))
        try:
            ui = camera_bone.id_properties_ui(track_name)
            ui.update(default=float(CAMERA_TRACK_DEFAULTS.get(track_name, 0.0)))
        except Exception:
            pass
    return camera_bone


def find_camera_preview_object(armature_obj):
    if armature_obj is None:
        return None
    children = list(getattr(armature_obj, "children_recursive", []) or [])
    children.extend(list(getattr(armature_obj, "children", []) or []))
    for obj in children:
        if getattr(obj, "type", None) == "CAMERA":
            return obj
    return None


def _setup_camera_prop_driver(camera_data, data_path, armature_obj, expression, variables):
    driver_curve = None
    anim_data = getattr(camera_data, "animation_data", None)
    for existing in getattr(anim_data, "drivers", []) or []:
        if getattr(existing, "data_path", "") == data_path:
            driver_curve = existing
            break
    if driver_curve is None:
        try:
            driver_curve = camera_data.driver_add(data_path)
        except (TypeError, ValueError):
            return False

    driver = driver_curve.driver
    driver.type = "SCRIPTED"
    driver.expression = expression
    while len(driver.variables) > len(variables):
        driver.variables.remove(driver.variables[len(driver.variables) - 1])
    while len(driver.variables) < len(variables):
        driver.variables.new()
    for idx, (var_name, track_name) in enumerate(variables):
        var = driver.variables[idx]
        var.type = "SINGLE_PROP"
        var.name = var_name
        target = var.targets[0]
        target.id_type = "OBJECT"
        target.id = armature_obj
        target.data_path = f'pose.bones["{CAMERA_CONTROL_BONE}"]["{track_name}"]'
    return True


def setup_camera_fov_driver(armature_obj, camera_obj, channel="hctFOV"):
    camera_bone = ensure_camera_track_properties(armature_obj, track_names=CAMERA_TRACK_NAMES)
    if camera_bone is None or camera_obj is None or getattr(camera_obj, "type", None) != "CAMERA":
        return False

    camera_data = getattr(camera_obj, "data", None)
    if camera_data is None:
        return False

    camera_data.lens_unit = "FOV"
    camera_data.sensor_fit = "VERTICAL"
    camera_data.sensor_height = CAMERA_SENSOR_HEIGHT
    result = _setup_camera_prop_driver(
        camera_data,
        "lens",
        armature_obj,
        f"{CAMERA_SENSOR_HEIGHT} / ( 2 * tan( pi * {channel} / 360.0 ) )",
        [(channel, channel)],
    )
    armature_obj.update_tag()
    return result


def setup_camera_dof_drivers(armature_obj, camera_obj):
    camera_bone = ensure_camera_track_properties(armature_obj, track_names=CAMERA_TRACK_NAMES)
    if camera_bone is None or camera_obj is None or getattr(camera_obj, "type", None) != "CAMERA":
        return False

    camera_data = getattr(camera_obj, "data", None)
    dof = getattr(camera_data, "dof", None) if camera_data is not None else None
    if camera_data is None or dof is None:
        return False

    dof.use_dof = True
    focus_ok = _setup_camera_prop_driver(
        camera_data,
        "dof.focus_distance",
        armature_obj,
        (
            "blenderDofFocusDistance * blenderDofFocusDistanceWeight + "
            "(dofFocusDistNear + dofFocusDistFar * 0.5) * "
            "(1 - blenderDofFocusDistanceWeight)"
        ),
        [
            ("blenderDofFocusDistance", "blenderDofFocusDistance"),
            ("blenderDofFocusDistanceWeight", "blenderDofFocusDistanceWeight"),
            ("dofFocusDistNear", "dofFocusDistNear"),
            ("dofFocusDistFar", "dofFocusDistFar"),
        ],
    )
    fstop_ok = _setup_camera_prop_driver(
        camera_data,
        "dof.aperture_fstop",
        armature_obj,
        (
            f"({BLENDER_DOF_INTENSITY_FSTOP} + "
            f"dofFocusDistFar * {BLENDER_DOF_FOCUS_RANGE_FSTOP} + "
            f"dofBlurDistFar * {BLENDER_DOF_FAR_BLUR_FSTOP} + "
            f"dofBlurDistNear * {BLENDER_DOF_NEAR_BLUR_FSTOP}) / "
            f"({BLENDER_DOF_INTENSITY_BIAS} + dofIntensity * overrideFactor)"
        ),
        [
            ("dofFocusDistFar", "dofFocusDistFar"),
            ("dofBlurDistFar", "dofBlurDistFar"),
            ("dofBlurDistNear", "dofBlurDistNear"),
            ("dofIntensity", "dofIntensity"),
            ("overrideFactor", "overrideFactor"),
        ],
    )
    armature_obj.update_tag()
    return focus_ok and fstop_ok


def setup_camera_preview_drivers(armature_obj, camera_obj):
    fov_ok = setup_camera_fov_driver(armature_obj, camera_obj, channel="hctFOV")
    dof_ok = setup_camera_dof_drivers(armature_obj, camera_obj)
    return fov_ok and dof_ok


def set_camera_dof_from_distance(camera_bone, distance, *,
                                 far_distance_factor=5.0,
                                 near_distance_factor=0.5,
                                 far_focus_factor=1.0,
                                 near_focus_factor=0.5,
                                 override=1.0,
                                 intensity=1.0,
                                 offset=0.0):
    if camera_bone is None:
        return False
    dist = max(0.0, float(distance or 0.0) + float(offset or 0.0))
    values = {
        "overrideFactor": float(override),
        "dofIntensity": float(intensity),
        "dofBlurDistFar": dist * float(far_distance_factor),
        "dofBlurDistNear": dist * float(near_distance_factor),
        "dofFocusDistFar": dist * float(far_focus_factor),
        "dofFocusDistNear": dist * float(near_focus_factor),
    }
    for track_name, value in values.items():
        camera_bone[track_name] = value
    return True


def engine_dof_planes_to_camera_tracks(*,
                                      dof_focus_near=0.0,
                                      dof_focus_far=0.0,
                                      dof_blur_near=0.0,
                                      dof_blur_far=0.0,
                                      dof_intensity=0.0,
                                      override_factor=1.0):
    """Convert scene camera DOF planes to the raw camera rig track layout.

    Story scene camera definitions store absolute engine planes. Camera rig
    animation tracks store near focus plus widths; CCamera expands those widths
    back into absolute planes before rendering.
    """
    near_focus = max(0.0, float(dof_focus_near or 0.0))
    far_focus = max(0.0, float(dof_focus_far or 0.0))
    near_blur = max(0.0, float(dof_blur_near or 0.0))
    far_blur = max(0.0, float(dof_blur_far or 0.0))

    near_focus = min(near_focus, far_focus)
    near_blur = min(near_blur, near_focus)
    far_blur = max(far_blur, far_focus)

    intensity = max(0.0, min(1.0, float(dof_intensity or 0.0)))
    override = max(0.0, float(override_factor or 0.0))
    return {
        "overrideFactor": override,
        "dofIntensity": intensity,
        "dofFocusDistNear": near_focus,
        "dofFocusDistFar": max(0.0, far_focus - near_focus),
        "dofBlurDistNear": max(0.0, near_focus - near_blur),
        "dofBlurDistFar": max(0.0, far_blur - far_focus),
    }


def get_blender_camera_focus_distance(camera_obj):
    if camera_obj is None or getattr(camera_obj, "type", None) != "CAMERA":
        return 0.0
    camera_data = getattr(camera_obj, "data", None)
    dof = getattr(camera_data, "dof", None) if camera_data is not None else None
    if dof is None:
        return 0.0

    focus_object = getattr(dof, "focus_object", None)
    if focus_object is not None:
        return (focus_object.matrix_world.translation - camera_obj.matrix_world.translation).length
    try:
        return max(0.0, float(dof.focus_distance))
    except Exception:
        return 0.0


def blender_fstop_to_witcher_intensity(aperture_fstop, use_dof=True):
    if not use_dof:
        return 0.0
    try:
        fstop = max(0.05, float(aperture_fstop))
    except Exception:
        fstop = BLENDER_DOF_INTENSITY_FSTOP
    return BLENDER_DOF_INTENSITY_FSTOP / fstop


def set_camera_dof_from_blender_camera(camera_bone, camera_obj, *,
                                       far_distance_factor=5.0,
                                       near_distance_factor=0.5,
                                       far_focus_factor=1.0,
                                       near_focus_factor=0.5):
    if camera_bone is None or camera_obj is None or getattr(camera_obj, "type", None) != "CAMERA":
        return False
    camera_data = getattr(camera_obj, "data", None)
    dof = getattr(camera_data, "dof", None) if camera_data is not None else None
    if dof is None:
        return False

    use_dof = bool(getattr(dof, "use_dof", False))
    distance = get_blender_camera_focus_distance(camera_obj)
    intensity = blender_fstop_to_witcher_intensity(getattr(dof, "aperture_fstop", BLENDER_DOF_INTENSITY_FSTOP), use_dof)
    return set_camera_dof_from_distance(
        camera_bone,
        distance,
        far_distance_factor=far_distance_factor,
        near_distance_factor=near_distance_factor,
        far_focus_factor=far_focus_factor,
        near_focus_factor=near_focus_factor,
        override=1.0 if use_dof else 0.0,
        intensity=intensity,
    )


def fov_to_lens(fov: float) -> float:
    fov = float(fov)
    if fov <= 1.0 or fov >= 179.0:
        return 50.0
    return CAMERA_SENSOR_HEIGHT / (2.0 * math.tan(math.pi * fov / 360.0))
