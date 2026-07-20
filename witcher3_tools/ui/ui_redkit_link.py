import math
from pathlib import Path, PureWindowsPath
from urllib.request import urlopen


HTTP_SERVICE_URL = "http://127.0.0.1:37010/"


def _parse_ini(text):
    sections = {}
    current = ""
    for raw_line in text.replace("\0", "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().casefold()
            sections.setdefault(current, {})
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            sections.setdefault(current, {})[key.strip().casefold()] = value.strip()
    return sections


def _xyz(section, label):
    try:
        values = tuple(float(section[axis]) for axis in ("x", "y", "z"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} has no valid X/Y/Z values") from exc
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"{label} contains a non-finite value")
    return values


def _last_world_from_ini(main_text):
    main = _parse_ini(main_text)
    global_settings = main.get("global", {})
    world = global_settings.get("lastsession", "").strip().strip('"')
    if not world:
        raise ValueError("REDkit has no LastSession world")
    return world, global_settings.get("workspacepath", "").strip().strip('"')


def _world_parts(world):
    path = PureWindowsPath(str(world or "").replace("/", "\\"))
    if path.suffix.casefold() != ".w2w":
        raise ValueError("REDkit LastSession is not a .w2w")
    if path.is_absolute() or path.drive or ".." in path.parts:
        raise ValueError("REDkit LastSession is not a safe repo-relative path")
    return path.parts


def _camera_from_ini(main_text, sessions_text):
    world, _workspace_path = _last_world_from_ini(main_text)

    sessions = _parse_ini(sessions_text)
    prefix = f"session/{world}/camera/".casefold()
    try:
        position_section = sessions[prefix + "position"]
        rotation_section = sessions[prefix + "rotation"]
    except KeyError as exc:
        raise ValueError(f"No saved camera for {world}") from exc

    position = _xyz(position_section, "Camera position")
    rotation_xyz = _xyz(rotation_section, "Camera rotation")
    # Session X/Y/Z are REDengine Roll/Pitch/Yaw respectively.
    return world, position, rotation_xyz


def read_last_editor_camera(redkit_root):
    bin_dir = Path(redkit_root) / "bin"
    main_text = (bin_dir / "r4LavaEditor2.ini").read_text(encoding="utf-8", errors="replace")
    sessions_text = (bin_dir / "r4LavaEditor2.sessions.ini").read_text(
        encoding="utf-8", errors="replace"
    )
    return _camera_from_ini(main_text, sessions_text)


def read_last_editor_world(redkit_root):
    main_text = (Path(redkit_root) / "bin" / "r4LavaEditor2.ini").read_text(
        encoding="utf-8", errors="replace"
    )
    return _last_world_from_ini(main_text)


def _format_number(value):
    value = 0.0 if abs(float(value)) < 0.0000005 else float(value)
    return f"{value:.6f}".rstrip("0").rstrip(".")


def format_camera_reference(position, rotation):
    x, y, z = position
    roll, pitch, yaw = rotation
    # REDkit clipboard format is Position X/Y/Z/W | Pitch/Yaw/Roll.
    return (
        f"[[{_format_number(x)} {_format_number(y)} {_format_number(z)} 1|"
        f"{_format_number(pitch)} {_format_number(yaw)} {_format_number(roll)}]]"
    )


def parse_camera_reference(text):
    value = str(text or "").strip()
    if not (value.startswith("[[") and value.endswith("]]")):
        raise ValueError("Expected [[X Y Z 1|Pitch Yaw Roll]]")
    try:
        position_text, rotation_text = value[2:-2].split("|", 1)
        position_values = tuple(float(item) for item in position_text.replace(",", " ").split())
        rotation_values = tuple(float(item) for item in rotation_text.replace(",", " ").split())
    except ValueError as exc:
        raise ValueError("Camera values must be numbers") from exc
    if len(position_values) not in {3, 4} or len(rotation_values) != 3:
        raise ValueError("Expected [[X Y Z 1|Pitch Yaw Roll]]")
    if not all(math.isfinite(item) for item in (*position_values, *rotation_values)):
        raise ValueError("Camera reference contains a non-finite value")

    pitch, yaw, roll = rotation_values
    return position_values[:3], (roll, pitch, yaw)


def _self_check():
    main = (
        "orphan=value\n[Global]\n"
        "LastSession=levels\\test\\test.w2w\n"
        "workspacePath=C:\\projects\\test\\test.w3edit\n"
    )
    sessions = """
[Session/levels\\test\\test.w2w/Camera/Rotation]
Z=1254
X=-3.5
Y=-5.25
[Session/levels\\test\\test.w2w/Camera/Position]
Z=3
X=1
Y=2
\0
"""
    world, position, rotation = _camera_from_ini(main, sessions)
    assert world == r"levels\test\test.w2w"
    assert _last_world_from_ini(main)[1] == r"C:\projects\test\test.w3edit"
    assert _world_parts(world) == ("levels", "test", "test.w2w")
    try:
        _world_parts(r"..\outside.w2w")
        raise AssertionError("Path traversal was accepted")
    except ValueError:
        pass
    assert position == (1.0, 2.0, 3.0)
    assert rotation == (-3.5, -5.25, 1254.0)
    reference = format_camera_reference(position, rotation)
    assert reference == "[[1 2 3 1|-5.25 1254 -3.5]]"
    assert parse_camera_reference(reference) == (position, rotation)


if __name__ == "__main__":
    _self_check()
    print("REDkit Link self-check passed")
    raise SystemExit


import bpy
from bpy.props import StringProperty
from mathutils import Quaternion, Vector

from . import ui_map


def _find_redkit_root(context):
    from .. import get_all_addon_prefs

    prefs = get_all_addon_prefs(context)
    configured_paths = (
        getattr(prefs, "redkit_depot_path", ""),
        getattr(prefs, "redkit_uncooked_path", ""),
        getattr(prefs, "witcher_game_path", ""),
    )
    checked = set()
    for configured_path in configured_paths:
        if not configured_path:
            continue
        path = Path(bpy.path.abspath(configured_path)).expanduser()
        candidates = (path, *tuple(path.parents)[:2])
        for candidate in candidates:
            key = str(candidate).casefold()
            if key in checked:
                continue
            checked.add(key)
            bin_dir = candidate / "bin"
            if (bin_dir / "r4LavaEditor2.ini").is_file() and (
                bin_dir / "r4LavaEditor2.sessions.ini"
            ).is_file():
                return candidate
    raise FileNotFoundError("REDkit install not found from the configured REDkit Depot Path")


def _resolve_last_world(context, redkit_root, world, workspace_path):
    from .. import get_all_addon_prefs

    roots = []
    if workspace_path:
        roots.append(Path(workspace_path).expanduser().parent / "workspace")
    prefs = get_all_addon_prefs(context)
    for name in ("redkit_depot_path", "redkit_uncooked_path"):
        value = getattr(prefs, name, "")
        if value:
            roots.append(Path(bpy.path.abspath(value)).expanduser())
    roots.append(Path(redkit_root) / "r4data")

    parts = _world_parts(world)
    checked = set()
    for root in roots:
        key = str(root).casefold()
        if key in checked:
            continue
        checked.add(key)
        candidate = root.joinpath(*parts)
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Last REDkit W2W was not found: {world}")


def _http_service_online():
    try:
        with urlopen(HTTP_SERVICE_URL, timeout=0.5) as response:
            return response.getcode() == 200
    except Exception:
        return False


def _view_region(context):
    area = ui_map._get_current_view3d_area(context)
    if area is None:
        raise RuntimeError("No 3D View is available")
    region_3d = getattr(area.spaces.active, "region_3d", None)
    if region_3d is None:
        raise RuntimeError("No 3D View region is available")
    return area, region_3d


def _engine_quaternion(rotation):
    roll, pitch, yaw = rotation
    return (
        Quaternion((0.0, 0.0, 1.0), math.radians(yaw))
        @ Quaternion((1.0, 0.0, 0.0), math.radians(pitch))
        @ Quaternion((0.0, 1.0, 0.0), math.radians(roll))
    ).normalized()


def _blender_camera_reference(context):
    _area, region_3d = _view_region(context)
    position = ui_map._get_camera_position(context)
    if position is None:
        raise RuntimeError("Blender camera position is unavailable")

    view_quaternion = region_3d.view_matrix.inverted().to_quaternion().normalized()
    engine_quaternion = view_quaternion @ Quaternion(
        (1.0, 0.0, 0.0), math.radians(-90.0)
    )
    engine_euler = engine_quaternion.to_euler('YXZ')
    rotation = (
        math.degrees(engine_euler.y),
        math.degrees(engine_euler.x),
        math.degrees(engine_euler.z),
    )
    return format_camera_reference(position, rotation)


def _move_view_to_camera(context, position, rotation):
    if bpy.app.background:
        raise RuntimeError("Moving a viewport requires interactive Blender")
    area, region_3d = _view_region(context)
    engine_quaternion = _engine_quaternion(rotation)
    view_quaternion = engine_quaternion @ Quaternion(
        (1.0, 0.0, 0.0), math.radians(90.0)
    )
    distance = max(abs(float(region_3d.view_distance)), 0.01)
    forward = engine_quaternion @ Vector((0.0, 1.0, 0.0))

    region_3d.view_perspective = 'PERSP'
    region_3d.view_rotation = view_quaternion
    region_3d.view_distance = distance
    region_3d.view_location = Vector(position) + forward * distance
    region_3d.update()
    area.tag_redraw()


class WITCHER_OT_refresh_redkit_link(bpy.types.Operator):
    bl_idname = "witcher.refresh_redkit_link"
    bl_label = "Refresh REDkit Link"
    bl_description = "Refresh REDkit, capture the Blender view, and check the local HTTP service"

    def execute(self, context):
        scene = context.scene
        scene.witcher_redkit_http_status = (
            "HTTP Service: Online" if _http_service_online() else "HTTP Service: Offline"
        )

        errors = []
        world = ""
        redkit_root = None
        try:
            redkit_root = _find_redkit_root(context)
            world, workspace_path = read_last_editor_world(redkit_root)
            scene.witcher_redkit_last_world = world
        except (OSError, ValueError) as exc:
            scene.witcher_redkit_last_world = ""
            scene.witcher_redkit_last_world_path = ""
            errors.append(str(exc))

        if world:
            try:
                scene.witcher_redkit_last_world_path = str(
                    _resolve_last_world(context, redkit_root, world, workspace_path)
                )
            except (OSError, ValueError) as exc:
                scene.witcher_redkit_last_world_path = ""
                errors.append(str(exc))

        if redkit_root is not None and world:
            try:
                _camera_world, position, rotation = read_last_editor_camera(redkit_root)
                scene.witcher_redkit_camera_reference = format_camera_reference(position, rotation)
            except (OSError, ValueError) as exc:
                scene.witcher_redkit_camera_reference = ""
                errors.append(str(exc))
        else:
            scene.witcher_redkit_camera_reference = ""

        try:
            scene.witcher_blender_camera_reference = _blender_camera_reference(context)
        except RuntimeError as exc:
            scene.witcher_blender_camera_reference = ""
            errors.append(str(exc))

        if errors:
            self.report({'WARNING'}, "; ".join(errors))
        else:
            self.report({'INFO'}, f"Loaded REDkit camera for {world}")
        return {'FINISHED'}


class WITCHER_OT_move_view_to_redkit_camera(bpy.types.Operator):
    bl_idname = "witcher.move_view_to_redkit_camera"
    bl_label = "Move View to REDkit"
    bl_description = "Move the Blender viewport to the REDkit camera pose shown above"

    @classmethod
    def poll(cls, context):
        return bool(
            context.scene
            and getattr(context.scene, "witcher_redkit_camera_reference", "").strip()
        )

    def execute(self, context):
        try:
            position, rotation = parse_camera_reference(
                context.scene.witcher_redkit_camera_reference
            )
            _move_view_to_camera(context, position, rotation)
            context.scene.witcher_blender_camera_reference = _blender_camera_reference(context)
        except (RuntimeError, ValueError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "Moved Blender view to the REDkit camera")
        return {'FINISHED'}


def draw_redkit_link(layout, context):
    scene = context.scene
    status = getattr(scene, "witcher_redkit_http_status", "HTTP Service: Not checked")
    if status.endswith("Online"):
        icon = 'CHECKMARK'
    elif status.endswith("Offline"):
        icon = 'UNLINKED'
    else:
        icon = 'QUESTION'

    row = layout.row(align=True)
    row.label(text=status, icon=icon)
    row.operator("witcher.refresh_redkit_link", text="", icon='FILE_REFRESH')
    layout.prop(scene, "witcher_redkit_last_world", text="Last W2W")
    layout.prop(scene, "witcher_redkit_camera_reference", text="REDkit")
    layout.prop(scene, "witcher_blender_camera_reference", text="Blender")
    actions = layout.row(align=True)
    actions.operator("witcher.move_view_to_redkit_camera", text="Move View", icon='VIEW_CAMERA')
    load_row = actions.row(align=True)
    load_row.enabled = bool(scene.witcher_redkit_last_world_path)
    load_row.operator_context = 'INVOKE_DEFAULT'
    load = load_row.operator("witcher.import_w2w", text="Load Terrain", icon='IMPORT')
    load.filepath = scene.witcher_redkit_last_world_path


_CLASSES = (
    WITCHER_OT_refresh_redkit_link,
    WITCHER_OT_move_view_to_redkit_camera,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.witcher_redkit_http_status = StringProperty(
        name="REDkit HTTP Status",
        default="HTTP Service: Not checked",
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_redkit_camera_reference = StringProperty(
        name="REDkit Camera",
        description="Last camera autosaved by REDkit; compatible with REDkit Ctrl+Alt+V",
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_redkit_last_world = StringProperty(
        name="Last REDkit W2W",
        description="Repo-relative world most recently open in REDkit",
        options={'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_redkit_last_world_path = StringProperty(
        name="Resolved Last REDkit W2W",
        options={'HIDDEN', 'SKIP_SAVE'},
    )
    bpy.types.Scene.witcher_blender_camera_reference = StringProperty(
        name="Blender Camera",
        description="Blender view captured by Refresh or Move, in REDkit's copy/paste format",
        options={'SKIP_SAVE'},
    )


def unregister():
    for name in (
        "witcher_blender_camera_reference",
        "witcher_redkit_last_world_path",
        "witcher_redkit_last_world",
        "witcher_redkit_camera_reference",
        "witcher_redkit_http_status",
    ):
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
