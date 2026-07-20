"""Optional Blender smoke test against a real Witcher 3 ``.w2w`` file.

Run with the world path after ``--``. The nearest ``r4data`` ancestor is used as
the read-only source depot. A nearby uncooked depot is detected when the source
depot does not contain the sky meshes; existing single-path invocations remain
supported.
"""

from __future__ import annotations

import math
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import bpy  # noqa: E402
import witcher3_tools  # noqa: E402
from mathutils import Vector  # noqa: E402


def _world_argument() -> Path:
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if not args:
        raise SystemExit("Pass an absolute .w2w path after --")
    path = Path(args[0]).resolve()
    if not path.is_file():
        raise SystemExit(f"World does not exist: {path}")
    return path


def _r4data_root(path: Path) -> Path:
    for parent in (path.parent, *path.parents):
        if parent.name.lower() == "r4data":
            return parent
    raise SystemExit(f"World is not below an r4data depot: {path}")


def _uncooked_root(depot_root: Path) -> Path:
    sun_mesh = Path("environment/skyboxes/sun/sun.w2mesh")
    candidates = (
        depot_root,
        depot_root.parent / "uncooked",
        depot_root.parent.parent / "redkit",
        depot_root.parent.parent / "uncooked",
    )
    return next((root for root in candidates if (root / sun_mesh).is_file()), depot_root)


def _repo_relative(path: str, roots: tuple[Path, ...]) -> str:
    resolved = Path(path).resolve()
    for root in roots:
        try:
            return str(resolved.relative_to(root)).replace("/", "\\")
        except ValueError:
            continue
    raise ValueError(f"Path is outside the test depots: {resolved}")


def main() -> None:
    world_path = _world_argument()
    depot_root = _r4data_root(world_path)
    uncooked_root = _uncooked_root(depot_root)
    repo_roots = tuple(dict.fromkeys((depot_root, uncooked_root)))

    witcher3_tools.register()
    original_world = None
    try:
        from witcher3_tools.CR2W import CR2W_reader
        from witcher3_tools.CR2W.common_blender import (
            clear_repo_override_roots,
            set_repo_override_roots,
        )
        from witcher3_tools.importers import import_environment
        from witcher3_tools.importers import import_mesh
        from witcher3_tools.ui import ui_environment

        # A directly registered source checkout has no AddonPreferences entry.
        # Supply the read-only depot to preference-backed mesh/material helpers.
        witcher3_tools.get_uncook_path = lambda context=None: str(uncooked_root)
        witcher3_tools.get_texture_path = lambda context=None: str(uncooked_root)
        import_mesh.get_uncook_path = lambda context=None: str(uncooked_root)
        import_mesh.get_texture_path = lambda context=None: str(uncooked_root)
        import_mesh.get_mod_directory = lambda context=None: ""
        import_mesh.get_repo_from_abs_path = lambda path: _repo_relative(path, repo_roots)
        set_repo_override_roots([str(root) for root in repo_roots], read_only=True)
        try:
            original_world = bpy.data.worlds.new("Environment World Smoke Original")
            bpy.context.scene.world = original_world

            world_file = CR2W_reader.load_w2w(str(world_path), include_groups=False)
            world = world_file.environment
            assert world is not None
            synced = ui_environment.sync_world_import(
                bpy.context,
                world_file,
                str(world_path),
            )
            assert synced.ok, synced.message

            settings = bpy.context.scene.witcher_environment
            assert settings.environment_path
            assert len(settings.weather_presets) > 0
            assert settings.full_effects
            full_values = ui_environment._preview_values(bpy.context.scene)
            assert full_values["balance_map_path"]
            assert full_values["fog_density"] > 0.0
            settings.full_effects = False
            fast_values = ui_environment._preview_values(bpy.context.scene)
            assert fast_values["balance_map_path"] == ""
            assert fast_values["fog_density"] == 0.0
            settings.full_effects = True

            settings.preview_enabled = True
            preview = ui_environment._refresh_preview(bpy.context, ensure=True)
            assert preview.success
            assert preview.sun_mode == "GAME", preview.warnings
            assert preview.moon_mode == "GAME", preview.warnings
            assert preview.stars_mode == "GAME", preview.warnings
            assert preview.cloud_mode == "GAME", preview.warnings
            assert Path(preview.stars_resolved_path).is_file()
            assert Path(preview.stars_resolved_path).suffix.lower() == ".w2cube"
            assert Path(preview.cloud_resolved_path).is_file()
            assert Path(preview.cloud_resolved_path).name.lower() == "cloud_dome.w2mesh"

            sky_world = bpy.context.scene.world
            assert sky_world is not None and sky_world is not original_world
            sky_world_name = sky_world.name
            assert sky_world.name == preview.sky_world_name
            assert sky_world.use_nodes
            sky_nodes = sky_world.node_tree.nodes
            stars_node = sky_nodes.get("W3 Sky Stars")
            assert stars_node is not None
            assert stars_node.image is not None
            assert tuple(stars_node.image.size)[0] == 2 * tuple(stars_node.image.size)[1]
            assert (
                stars_node.image.packed_file is not None
                or Path(bpy.path.abspath(stars_node.image.filepath)).is_file()
            ), stars_node.image.filepath

            sun_root = bpy.data.objects["W3 Environment Sun"]
            moon_root = bpy.data.objects["W3 Environment Moon"]
            depsgraph = bpy.context.evaluated_depsgraph_get()
            for root in (sun_root, moon_root):
                assert root.rotation_mode == "XYZ"
                assert all(abs(float(angle)) < 1e-6 for angle in root.rotation_euler)
                assert min(float(value) for value in root.scale) > 0.5
                root_pos = root.evaluated_get(depsgraph).matrix_world.translation
                for child in root.children_recursive:
                    child_pos = child.evaluated_get(depsgraph).matrix_world.translation
                    assert (child_pos - root_pos).length < 1e-3, child.name

            background = sky_nodes.get("W3 Sky Brightness")
            assert background is not None
            assert abs(background.inputs[1].default_value - 1.0) < 1e-6
            assert sky_nodes.get("W3 Sky Global Brightness") is not None
            assert sky_nodes.get("W3 Sky Fog Mix") is not None
            assert sky_nodes["W3 Sky Cloud Mix"].blend_type == "ADD"

            preview_collection = bpy.data.collections[preview.collection_name]
            ambient_lights = import_environment._find_role_objects(
                preview_collection,
                import_environment._ROLE_AMBIENT_LIGHT,
            )
            assert len(ambient_lights) == 4
            assert all(not light.data.use_shadow for light in ambient_lights)
            assert all(abs(light.data.specular_factor) < 1e-6 for light in ambient_lights)
            assert all(light.parent is None for light in ambient_lights)
            assert all(
                (light.rotation_quaternion @ Vector((0.0, 0.0, 1.0))).z > 0.5
                for light in ambient_lights
            )

            cloud_root = bpy.data.objects[import_environment.ENVIRONMENT_CLOUD_NAME]
            cloud_objects = import_environment._find_role_objects(
                preview_collection,
                import_environment._ROLE_CLOUD_GEOMETRY,
            )
            assert len(cloud_objects) == 1
            assert abs(
                sky_nodes[import_environment._SKY_NODE_CLOUD_OPACITY]
                .inputs[1]
                .default_value
            ) < 1e-6
            cloud_radius = max(
                Vector(corner).length
                for cloud_object in cloud_objects
                for corner in cloud_object.bound_box
            )
            expected_cloud_scale = min(
                180.0,
                import_environment._BLENDER_CLOUD_MAX_REACH / cloud_radius,
            )
            assert all(
                abs(value - expected_cloud_scale) < 1e-4
                for value in cloud_root.scale
            )
            cloud_material = cloud_objects[0].data.materials[0]
            cloud_nodes = cloud_material.node_tree.nodes
            assert cloud_nodes["W3 Cloud Detail"].image is not None
            assert cloud_nodes["W3 Cloud Coverage"].image is not None
            coverage_channels = cloud_nodes["W3 Cloud Coverage Channels"]
            coverage_shape = cloud_nodes["W3 Cloud Coverage Shape"]
            coverage_ramp = coverage_shape.color_ramp
            assert coverage_ramp.interpolation == "LINEAR"
            assert abs(coverage_ramp.elements[0].position - 0.5) < 1e-6
            assert abs(coverage_ramp.elements[1].position - 1.0) < 1e-6
            assert coverage_shape.inputs[0].links[0].from_node == coverage_channels
            assert coverage_shape.inputs[0].links[0].from_socket.name == "Red"
            detail_plus_coverage = cloud_nodes["W3 Cloud Detail Plus Coverage"]
            cloud_mask = cloud_nodes["W3 Cloud Texture Mask"]
            assert detail_plus_coverage.operation == "ADD"
            assert detail_plus_coverage.inputs[0].links[0].from_node == cloud_nodes["W3 Cloud Detail"]
            assert detail_plus_coverage.inputs[0].links[0].from_socket.name == "Alpha"
            assert cloud_mask.operation == "SUBTRACT" and cloud_mask.use_clamp
            assert cloud_mask.inputs[0].links[0].from_node == detail_plus_coverage
            assert abs(cloud_mask.inputs[1].default_value - 1.0) < 1e-6
            cloud_tone = cloud_nodes["W3 Cloud Tone"].color_ramp
            for element in cloud_tone.elements:
                assert element.color[0] > element.color[1] > element.color[2] > 0.4

            sun_material = bpy.data.materials.get("W3 Environment Sun Preview")
            moon_material = bpy.data.materials.get("W3 Environment Moon Preview")
            assert sun_material is not None and sun_material.use_nodes
            assert sun_material.node_tree.nodes.get("W3 Sun Limb") is not None
            assert moon_material is not None and moon_material.use_nodes
            moon_nodes = moon_material.node_tree.nodes
            moon_detail = moon_nodes.get("W3 Moon Detail")
            assert moon_detail is not None and moon_detail.image is not None
            assert (
                moon_detail.image.packed_file is not None
                or Path(bpy.path.abspath(moon_detail.image.filepath)).is_file()
            ), moon_detail.image.filepath

            # A loaded moon normal map enables the time-driven phase term.
            assert moon_nodes["W3 Moon Phase Mix"].inputs[0].default_value > 0.99
            assert sky_nodes.get("W3 Sky Star Rotation") is not None
            phase_light = moon_nodes["W3 Moon Phase Light"]
            settings.fake_day_number = 0
            full_moon = tuple(inp.default_value for inp in phase_light.inputs)
            settings.fake_day_number = 15
            new_moon = tuple(inp.default_value for inp in phase_light.inputs)
            assert full_moon != new_moon
            settings.fake_day_number = 0

            preview_values = ui_environment._preview_values(bpy.context.scene)
            assert abs(preview_values["ambient_energy"] - 0.3353744056) < 1e-5
            tone_scale = 1.7806699276
            tone_range = 11.2 * tone_scale
            tone_key = min(max(0.5, 0.4126310349), 1.7041705847)
            tone_shaped_key = (tone_key / tone_range) ** 0.5 * tone_range
            assert abs(
                preview_values["tone_exposure_ev"]
                - math.log2(tone_scale / tone_shaped_key)
            ) < 1e-5, preview_values["tone_exposure_ev"]
            assert all(
                abs(actual - expected) < 1e-5
                for actual, expected in zip(
                    preview_values["sun_back_horizon_color"],
                    (0.12759415, 0.30217053, 0.58597488),
                )
            ), preview_values["sun_back_horizon_color"]
            assert abs(
                sky_nodes["W3 Sky Global Brightness"].inputs["Scale"].default_value
                - preview_values["sky_brightness"]
            ) < 1e-6

            # Migrating an older managed World graph must retain its already
            # loaded stars image when no texture replacement was requested.
            stars_image_before_migration = stars_node.image
            sky_nodes.remove(sky_nodes[import_environment._SKY_NODE_HORIZON_POWER])
            migrated = import_environment.update_preview(bpy.context, **preview_values)
            assert migrated.success
            sky_nodes = bpy.context.scene.world.node_tree.nodes
            assert sky_nodes.get(import_environment._SKY_NODE_HORIZON_POWER) is not None
            stars_node = sky_nodes["W3 Sky Stars"]
            assert stars_node.image is stars_image_before_migration

            without_native_textures = dict(preview_values)
            without_native_textures["skybox_material_path"] = ""
            without_native_textures["moon_material_path"] = ""
            textures_cleared = import_environment.update_preview(
                bpy.context,
                **without_native_textures,
            )
            # A world without a sky material falls back to the vanilla stars
            # cube instead of leaving the Environment Texture magenta.
            assert textures_cleared.stars_mode == "GAME", textures_cleared.warnings
            assert stars_node.image is not None
            assert moon_detail.image is None

            textures_restored = import_environment.update_preview(
                bpy.context,
                **preview_values,
            )
            assert textures_restored.stars_mode == "GAME", textures_restored.warnings
            assert stars_node.image is not None
            assert moon_detail.image is not None

            settings.fake_day_seconds = 0.0
            midnight = tuple(bpy.data.objects["W3 Environment Sun"].location)
            midnight_stars = sky_nodes["W3 Sky Day Factor"].inputs[0].default_value
            midnight_scroll = moon_nodes["W3 Moon Mapping"].inputs["Location"].default_value[0]
            midnight_star_angle = sky_nodes["W3 Sky Star Rotation"].inputs["Angle"].default_value
            settings.fake_day_seconds = 43200.0
            midday = tuple(bpy.data.objects["W3 Environment Sun"].location)
            midday_stars = sky_nodes["W3 Sky Day Factor"].inputs[0].default_value
            midday_scroll = moon_nodes["W3 Moon Mapping"].inputs["Location"].default_value[0]
            midday_star_angle = sky_nodes["W3 Sky Star Rotation"].inputs["Angle"].default_value
            assert midnight != midday
            assert midnight_stars > midday_stars
            # The lunar face scrolls with the day fraction and the star field
            # wheels with the moon trajectory.
            assert abs(midnight_scroll - midday_scroll) > 1e-3
            assert abs(midnight_star_angle - midday_star_angle) > 1e-3

            overlay_index = next(
                (index for index, item in enumerate(settings.weather_presets) if item.environment_path),
                None,
            )
            if overlay_index is not None:
                settings.weather_index = overlay_index
                runtime = ui_environment.environment_runtime(bpy.context.scene)
                assert runtime.get("weather_environment") is not None

            selected_weather = settings.active_weather_name
            ui_environment.clear_environment_runtime(bpy.context.scene)
            settings.fake_day_seconds = 3600.0
            restored = ui_environment.environment_runtime(bpy.context.scene)
            assert restored.get("world") is not None
            assert settings.active_weather_name == selected_weather

            assert import_environment.clear_preview(bpy.context) > 0
            assert bpy.context.scene.world is original_world
            assert bpy.data.worlds.get(sky_world_name) is None
        finally:
            try:
                import_environment.clear_preview(bpy.context)
            except Exception:
                pass
            clear_repo_override_roots()
    finally:
        if original_world is not None:
            if bpy.context.scene.world is original_world:
                bpy.context.scene.world = None
            if original_world.name in bpy.data.worlds:
                bpy.data.worlds.remove(original_world)
        witcher3_tools.unregister()

    print("ENVIRONMENT_WORLD_BLENDER_SMOKE_OK")


if __name__ == "__main__":
    main()
