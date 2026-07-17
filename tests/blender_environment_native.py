"""Blender-native smoke test for the Environment N-panel and preview."""

from __future__ import annotations

import math
from pathlib import Path
import sys
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import bpy  # noqa: E402
import witcher3_tools  # noqa: E402
from mathutils import Vector  # noqa: E402


def main() -> None:
    witcher3_tools.register()
    scene = None
    original_camera = None
    original_world = None
    original_volumetric_end = None
    original_view_exposure = None
    collision_materials = []
    temporary_materials = []
    temporary_cameras = []
    temporary_objects = []
    temporary_meshes = []
    try:
        scene = bpy.context.scene
        original_view_exposure = float(scene.view_settings.exposure)
        eevee = getattr(scene, "eevee", None)
        if eevee is not None and hasattr(eevee, "volumetric_end"):
            original_volumetric_end = float(eevee.volumetric_end)
        assert hasattr(scene, "witcher_environment")
        original_camera = scene.camera
        # Exercise the late-camera path explicitly: the preview is created while
        # no render camera is assigned, then cameras are added without refreshing.
        scene.camera = None

        original_world = bpy.data.worlds.new("Environment Smoke Original World")
        scene.world = original_world

        for name in (
            "W3 Environment Sun Preview",
            "W3 Environment Sun Fallback",
        ):
            material = bpy.data.materials.new(name)
            material["user_owned_test_material"] = True
            collision_materials.append(material)

        settings = scene.witcher_environment
        settings.fake_day_seconds = 21600.0

        from witcher3_tools.importers import import_w2w

        water_material = bpy.data.materials.new("Environment Smoke Water")
        temporary_materials.append(water_material)
        water_material["witcher_world_water_material"] = True
        water_material.use_nodes = True
        water_nodes = water_material.node_tree.nodes
        water_nodes.clear()
        tint = water_nodes.new("ShaderNodeRGB")
        tint.name = "W3 Water Tint"
        for node_name in (
            "W3 Water Fresnel Gain",
            "W3 Water Ambient Scale",
            "W3 Water Diffuse Scale",
            "W3 Water Flow",
            "W3 Water Foam",
        ):
            value = water_nodes.new("ShaderNodeValue")
            value.name = node_name
        water_mesh = bpy.data.meshes.new("Environment Smoke Water Mesh")
        temporary_meshes.append(water_mesh)
        water_object = bpy.data.objects.new("Environment Smoke Water", water_mesh)
        temporary_objects.append(water_object)
        scene.collection.objects.link(water_object)
        water_object[import_w2w.WORLD_WATER_OBJECT_PROP] = True
        water_object.data.materials.append(water_material)

        from witcher3_tools.importers import import_environment
        from witcher3_tools.environment_catalog import ENVIRONMENT_OFF_IDENTIFIER
        from witcher3_tools.ui import ui_environment

        selector_items = ui_environment._rebuild_environment_selector_cache(bpy.context, force=True)
        assert settings.preview_environment == ENVIRONMENT_OFF_IDENTIFIER
        assert selector_items[0][0] == ENVIRONMENT_OFF_IDENTIFIER
        assert selector_items[0][1] == "ENV - OFF"
        invoke_result = bpy.ops.witcher.environment_selector_select("INVOKE_DEFAULT")
        assert invoke_result in ({"RUNNING_MODAL"}, {"FINISHED"})
        if len(selector_items) > 1:
            base_path = settings.environment_path
            result = bpy.ops.witcher.environment_selector_select(environment=selector_items[1][0])
            assert result == {"FINISHED"}
            assert settings.preview_enabled
            assert ui_environment.environment_runtime(scene).get("selector_environment") is not None
            assert settings.environment_path == base_path
            result = bpy.ops.witcher.environment_selector_select(environment=ENVIRONMENT_OFF_IDENTIFIER)
            assert result == {"FINISHED"}
            assert not settings.preview_enabled
            assert ui_environment.environment_runtime(scene).get("selector_environment") is None
            assert settings.environment_path == base_path

        ui_environment.clear_environment_runtime(scene)
        assert abs(settings.moon_size_scale - 1.0) < 1e-6
        assert abs(settings.key_light_energy - 1.0) < 1e-6
        assert abs(settings.ambient_light_energy - 1.0) < 1e-6
        assert abs(settings.stars_brightness - 1.1) < 1e-6
        assert abs(settings.cloud_amount - 0.45) < 1e-6
        assert abs(settings.sun_brightness - 5.0) < 1e-6
        assert abs(settings.moon_brightness - 1.0) < 1e-6
        preview_defaults = ui_environment._preview_values(scene)
        assert abs(preview_defaults["moon_size"] - 1.0) < 1e-6
        assert abs(preview_defaults["fog_density"]) < 1e-6
        assert abs(preview_defaults["fog_final_exp"] - 1.0) < 1e-6
        assert preview_defaults["aerial_color_middle"] == (1.0, 1.0, 1.0)
        assert abs(preview_defaults["aerial_final_exp"] - 1.0) < 1e-6
        assert preview_defaults["water_color"] == (0.0, 0.0, 0.0)
        assert abs(preview_defaults["water_fresnel"] - 1.0) < 1e-6
        assert abs(preview_defaults["water_ambient_scale"] - 0.1) < 1e-6
        assert abs(preview_defaults["water_diffuse_scale"] - 0.4) < 1e-6
        assert abs(preview_defaults["water_flow_intensity"] - 0.6) < 1e-6
        assert abs(preview_defaults["water_foam_intensity"]) < 1e-6
        assert abs(preview_defaults["tone_exposure_ev"]) < 1e-6

        from witcher3_tools.importers import environment_balance_preview

        balance_image = bpy.data.images.new("Environment Balance Bloom Smoke", 8, 8)
        balance_tree = environment_balance_preview._build_tree(
            balance_image,
            {"exposure": 0.0},
            1.0,
            1.0,
            0.0,
            (0.22, 0.30, 0.10, 0.20, 0.01, 0.30),
            1.0,
        )
        try:
            assert not any(node.type == "GLARE" for node in balance_tree.nodes)
            assert len({node.location.x for node in balance_tree.nodes}) > 1
        finally:
            bpy.data.node_groups.remove(balance_tree)
            bpy.data.images.remove(balance_image)

        class ScalarCurve:
            is_scalar = True
            points = ()

            def __init__(self, value):
                self.value = float(value)

            def evaluate(self, _time):
                return (0.0, 0.0, 0.0, self.value)

        class ColorCurve:
            is_scalar = False
            points = ()

            def __init__(self, value):
                self.value = tuple(value)

            def evaluate(self, _time):
                return self.value

        runtime = ui_environment.environment_runtime(scene)
        runtime["selector_environment"] = SimpleNamespace(
            source_path="C:\\environment\\gui_character_environment.env",
            params={
                "m_globalLight": {
                    "activatedFactorLightDir": 1.0,
                },
                "m_cameraLightsSetup": {
                    "activated": True,
                    "gameplayLight0": {"activated": True},
                    "gameplayLight1": {"activated": True},
                },
                "m_finalColorBalance": {
                    "activatedBalanceMap": True,
                    "balanceMap0": "environment\\definitions\\test_balance.xbm",
                },
            },
            curves={
                "m_globalLight.sunColor": ColorCurve((255.0, 255.0, 255.0, 60.0)),
                "m_globalLight.envProbeAmbientScaleLight": ScalarCurve(0.25),
                "m_globalLight.envProbeBaseLightingReflection.colorSkyAdd": ColorCurve(
                    (255.0, 255.0, 255.0, 0.48898953199386597)
                ),
                "m_globalLight.charactersLightingBoostReflectionShadow": ScalarCurve(0.5),
                "m_globalLight.envProbeReflectionScaleShadow": ScalarCurve(0.2),
                "m_globalLight.forcedLightDirAnglesYaw": ScalarCurve(40.0),
                "m_globalLight.forcedLightDirAnglesPitch": ScalarCurve(30.0),
                "m_globalLight.forcedSunDirAnglesYaw": ScalarCurve(275.0),
                "m_globalLight.forcedSunDirAnglesPitch": ScalarCurve(30.0),
                "m_globalLight.forcedMoonDirAnglesYaw": ScalarCurve(135.0),
                "m_globalLight.forcedMoonDirAnglesPitch": ScalarCurve(30.0),
                "m_toneMapping.exposureScale": ScalarCurve(2.0),
                "m_toneMapping.postScale": ScalarCurve(0.75),
                "m_toneMapping.luminanceLimitShape": ScalarCurve(0.5),
                "m_toneMapping.luminanceLimitMin": ScalarCurve(0.0),
                "m_toneMapping.luminanceLimitMax": ScalarCurve(4.0),
                "m_globalFog.fogColorFront": ColorCurve((255.0, 128.0, 64.0, 0.5)),
                "m_globalFog.fogColorMiddle": ColorCurve((64.0, 64.0, 64.0, 0.25)),
                "m_globalFog.fogColorBack": ColorCurve((1.0, 2.0, 3.0, -0.5)),
                "m_globalFog.aerialColorFront": ColorCurve((253.0, 213.0, 142.0, 8.4912)),
                "m_globalFog.aerialColorMiddle": ColorCurve((250.0, 171.0, 134.0, 2.30078)),
                "m_globalFog.aerialColorBack": ColorCurve((147.0, 164.0, 195.0, 7.03083)),
                "m_globalFog.aerialFinalExp": ScalarCurve(2.75035),
                "m_globalFog.fogDistClamp": ScalarCurve(4338.0),
                "m_finalColorBalance.balanceMapAmount": ScalarCurve(0.4),
                "m_finalColorBalance.balancePostBrightness": ScalarCurve(1.25),
                "m_cameraLightsSetup.gameplayLight0.color": ColorCurve(
                    (201.0, 240.0, 248.0, 50.0)
                ),
                "m_cameraLightsSetup.gameplayLight0.radius": ScalarCurve(17.4),
                "m_cameraLightsSetup.gameplayLight0.attenuation": ScalarCurve(0.0),
                "m_cameraLightsSetup.gameplayLight0.offsetFront": ScalarCurve(8.0),
                "m_cameraLightsSetup.gameplayLight0.offsetRight": ScalarCurve(3.0),
                "m_cameraLightsSetup.gameplayLight0.offsetUp": ScalarCurve(0.5),
                "m_cameraLightsSetup.gameplayLight1.color": ColorCurve(
                    (250.0, 233.0, 183.0, 120.0)
                ),
                "m_cameraLightsSetup.gameplayLight1.radius": ScalarCurve(20.0),
                "m_cameraLightsSetup.gameplayLight1.attenuation": ScalarCurve(0.25),
                "m_cameraLightsSetup.gameplayLight1.offsetFront": ScalarCurve(5.5),
                "m_cameraLightsSetup.gameplayLight1.offsetRight": ScalarCurve(-2.25),
                "m_cameraLightsSetup.gameplayLight1.offsetUp": ScalarCurve(2.75),
            }
        )
        settings.preview_environment = "TEST_SELECTOR_ENVIRONMENT"
        settings.full_effects = True
        authored_values = ui_environment._preview_values(scene)
        assert abs(authored_values["ambient_energy"] - 0.15) < 1e-6
        shape_range = 11.2 * 2.0
        shaped_key = (0.5 / shape_range) ** 0.5 * shape_range
        assert abs(
            authored_values["tone_exposure_ev"] - math.log2(2.0 / shaped_key)
        ) < 1e-6
        assert authored_values["camera_lights"] == ()
        assert abs(authored_values["key_energy"] - 60.0) < 1e-6
        assert authored_values["balance_map_path"].endswith("test_balance.xbm")
        assert abs(authored_values["balance_map_amount"] - 0.4) < 1e-6
        assert abs(authored_values["balance_post_brightness"] - 1.25) < 1e-6
        assert authored_values["tone_curve_parameters"] == (
            0.22,
            0.30,
            0.10,
            0.20,
            0.01,
            0.30,
        )
        assert abs(authored_values["tone_post_scale"] - 0.75) < 1e-6
        assert abs(authored_values["fog_dist_clamp"] - 4338.0) < 1e-6
        assert authored_values["fog_color_back"] == (0.0, 0.0, 0.0)
        assert authored_values["aerial_color_front"] == ui_environment._curve_color(
            (253.0, 213.0, 142.0, 8.4912),
            (255.0, 255.0, 255.0, 1.0),
        )
        assert abs(authored_values["aerial_final_exp"] - 2.75035) < 1e-6
        expected_key_direction = ui_environment._euler_direction(40.0, 30.0)
        assert all(
            abs(actual - expected) < 1e-6
            for actual, expected in zip(
                authored_values["key_direction"],
                expected_key_direction,
            )
        )
        assert authored_values["source_path"].endswith("gui_character_environment.env")
        authored_camera_lights = ui_environment._camera_light_values(
            runtime["selector_environment"],
            settings.fake_day_seconds,
            1.0,
        )
        assert len(authored_camera_lights) == 2
        assert authored_camera_lights[0]["name"] == "Gameplay 1"
        assert abs(authored_camera_lights[0]["radius"] - 17.4) < 1e-6
        assert authored_camera_lights[1]["energy"] > authored_camera_lights[0]["energy"]

        runtime["selector_environment"].curves[
            "m_globalLight.envProbeAmbientScaleLight"
        ] = ScalarCurve(0.0)
        reflection_values = ui_environment._preview_values(scene)
        expected_reflection_fill = 60.0 * 0.48898953199386597 * 0.5 * 0.2 * 1.2
        assert reflection_values["ambient_color"] == (1.0, 1.0, 1.0)
        assert abs(reflection_values["ambient_energy"] - expected_reflection_fill) < 1e-6

        settings.preview_environment = ENVIRONMENT_OFF_IDENTIFIER
        runtime["environment"] = runtime.pop("selector_environment")
        runtime["direct_environment"] = True
        manual_values = ui_environment._preview_values(scene)
        assert manual_values["camera_lights"] == ()
        assert abs(manual_values["ambient_energy"] - expected_reflection_fill) < 1e-6
        ui_environment.clear_environment_runtime(scene)
        raw_water = ui_environment._linear_color(
            (31.8886987873, 35.8815087077, 18.9390292482, 1.0),
            (0.0, 0.0, 0.0, 1.0),
        )
        assert all(
            abs(actual - expected / 255.0) < 1e-8
            for actual, expected in zip(
                raw_water,
                (31.8886987873, 35.8815087077, 18.9390292482),
            )
        )

        result = import_environment.ensure_preview(
            bpy.context,
            sun_direction=(1.0, 0.0, 0.25),
            moon_direction=(-1.0, 0.0, -0.25),
            key_direction=(0.0, 1.0, 1.0),
            sky_zenith_color=(0.1, 0.2, 0.7),
            sky_horizon_color=(0.8, 0.45, 0.2),
            sun_horizon_color=(0.85, 0.9, 1.0),
            sun_back_horizon_color=(0.15, 0.25, 0.35),
            tone_exposure_ev=0.5,
            sky_day_factor=1.0,
            camera_lights=authored_camera_lights,
            time_seconds=settings.fake_day_seconds,
        )
        assert result.success
        assert result.used_fallback
        assert result.sky_world_name
        assert result.stars_mode == "NONE"
        assert scene.world is not original_world
        assert abs(scene.view_settings.exposure - (original_view_exposure + 0.5)) < 1e-6
        assert all(material.get("user_owned_test_material") for material in collision_materials)
        assert import_environment._restore_render_anchor in bpy.app.handlers.render_init
        assert import_environment._restore_render_anchor in bpy.app.handlers.render_pre
        sun_geometry = bpy.data.objects[result.sun_object_names[0]]
        assert sun_geometry.data.materials[0] not in collision_materials

        sky_world = scene.world
        sky_world_name = sky_world.name
        assert sky_world.name == result.sky_world_name
        assert sky_world.use_nodes
        nodes = sky_world.node_tree.nodes
        assert nodes.get("W3 Sky Gradient") is not None
        assert nodes.get("W3 Sky Stars") is not None
        assert nodes["W3 Sky Stars"].image is None
        assert nodes.get("W3 Sky Day Factor") is not None
        assert abs(nodes["W3 Sky Day Factor"].inputs[0].default_value) < 1e-6
        assert nodes.get("W3 Sky Global Brightness") is not None
        assert nodes.get("W3 Sky Fog Mix") is not None
        assert nodes["W3 Sky Cloud Mix"].blend_type == "ADD"
        assert abs(nodes["W3 Sky Brightness"].inputs[1].default_value - 1.0) < 1e-6
        assert all(
            abs(actual - expected) < 1e-6
            for actual, expected in zip(
                nodes[import_environment._SKY_NODE_SUN_HORIZON_BACK]
                .outputs[0]
                .default_value,
                (0.15, 0.25, 0.35, 1.0),
            )
        )

        sun_xy_dot = nodes["W3 Sky Sun Horizon Dot XY"]
        sun_facing = nodes["W3 Sky Sun Horizon Facing"]
        sun_horizon_direction = nodes[import_environment._SKY_NODE_SUN_HORIZON_MIX]
        assert sun_xy_dot.operation == "DOT_PRODUCT"
        assert sun_facing.operation == "MULTIPLY_ADD" and sun_facing.use_clamp
        assert abs(sun_facing.inputs[1].default_value - 0.5) < 1e-6
        assert abs(sun_facing.inputs[2].default_value - 0.5) < 1e-6
        assert sun_facing.inputs[0].links[0].from_node == sun_xy_dot
        assert sun_horizon_direction.inputs[0].links[0].from_node == sun_facing
        assert (
            sun_horizon_direction.inputs[1].links[0].from_node
            == nodes[import_environment._SKY_NODE_SUN_HORIZON_BACK]
        )
        assert (
            sun_horizon_direction.inputs[2].links[0].from_node
            == nodes[import_environment._SKY_NODE_SUN_HORIZON_FRONT]
        )

        camera_height = nodes[import_environment._SKY_NODE_HORIZON_CAMERA_HEIGHT]
        attenuation = nodes[import_environment._SKY_NODE_HORIZON_ATTENUATION]
        horizon_reciprocal = nodes["W3 Sky Horizon Reciprocal"]
        horizon_power = nodes[import_environment._SKY_NODE_HORIZON_POWER]
        assert abs(camera_height.outputs[0].default_value - 0.71) < 1e-6
        assert abs(attenuation.inputs[1].default_value - 1.8) < 1e-6
        assert horizon_reciprocal.operation == "DIVIDE" and horizon_reciprocal.use_clamp
        assert abs(horizon_reciprocal.inputs[0].default_value - 1.0) < 1e-6
        assert horizon_power.operation == "POWER"
        assert abs(horizon_power.inputs[1].default_value - 2.8) < 1e-6
        assert nodes["W3 Sky Gradient"].inputs[0].links[0].from_node.name == "W3 Sky Horizon Factor"

        collection = bpy.data.collections[result.collection_name]
        anchor = bpy.data.objects[import_environment.ENVIRONMENT_ANCHOR_NAME]
        camera_lights = import_environment._find_role_objects(
            collection,
            import_environment._ROLE_CAMERA_LIGHT,
        )
        assert len(camera_lights) == 2
        assert len(result.camera_light_names) == 2
        assert all(light.type == "LIGHT" and light.data.type == "POINT" for light in camera_lights)
        assert all(not light.data.use_shadow for light in camera_lights)
        assert all(light.parent is anchor for light in camera_lights)
        assert abs(camera_lights[0].data.cutoff_distance - 17.4) < 1e-6
        assert (camera_lights[0].location - Vector((3.0, -8.0, 0.5))).length < 1e-6
        assert import_environment._update_camera_light_positions(collection) == 0
        ambient_lights = import_environment._find_role_objects(
            collection,
            import_environment._ROLE_AMBIENT_LIGHT,
        )
        assert len(ambient_lights) == 4
        assert all(not light.data.use_shadow for light in ambient_lights)
        assert all(abs(light.data.specular_factor) < 1e-6 for light in ambient_lights)
        assert all(light.parent is None for light in ambient_lights)
        ambient_to_light = [
            light.rotation_quaternion @ Vector((0.0, 0.0, 1.0))
            for light in ambient_lights
        ]
        assert all(direction.z > 0.5 for direction in ambient_to_light)
        assert {
            (1 if direction.x > 0.0 else -1, 1 if direction.y > 0.0 else -1)
            for direction in ambient_to_light
        } == {(1, 1), (1, -1), (-1, 1), (-1, -1)}

        cloud_mesh = bpy.data.meshes.new("Environment Cloud Graph Smoke")
        cloud_mesh.from_pydata(((-1.0, -1.0, 0.0), (1.0, -1.0, 0.0), (0.0, 1.0, 0.0)), (), ((0, 1, 2),))
        cloud_object = bpy.data.objects.new("Environment Cloud Graph Smoke", cloud_mesh)
        collection.objects.link(cloud_object)
        import_environment._tag(cloud_object, "cloud_graph_smoke")
        cloud_material = import_environment._ensure_cloud_preview_material((cloud_object,))
        cloud_nodes = cloud_material.node_tree.nodes
        coverage_shape = cloud_nodes["W3 Cloud Coverage Shape"]
        assert coverage_shape.inputs[0].links[0].from_socket.name == "Red"
        assert abs(coverage_shape.color_ramp.elements[0].position - 0.5) < 1e-6
        assert abs(coverage_shape.color_ramp.elements[1].position - 1.0) < 1e-6
        assert cloud_nodes["W3 Cloud Detail Plus Coverage"].operation == "ADD"
        cloud_mask = cloud_nodes["W3 Cloud Texture Mask"]
        assert cloud_mask.operation == "SUBTRACT" and cloud_mask.use_clamp
        assert abs(cloud_mask.inputs[1].default_value - 1.0) < 1e-6
        for element in cloud_nodes["W3 Cloud Tone"].color_ramp.elements:
            assert element.color[0] > element.color[1] > element.color[2] > 0.4

        reach_root = import_environment._new_empty(
            collection,
            "Environment Cloud Reach Smoke",
            import_environment._ROLE_CLOUD_ROOT,
        )
        reach_root.parent = anchor
        reach_mesh = bpy.data.meshes.new("Environment Cloud Reach Smoke")
        reach_mesh.from_pydata(
            ((-20.0, 0.0, 0.0), (20.0, 0.0, 0.0), (0.0, 20.0, 0.0)),
            (),
            ((0, 1, 2),),
        )
        reach_object = bpy.data.objects.new("Environment Cloud Reach Smoke", reach_mesh)
        collection.objects.link(reach_object)
        import_environment._tag(reach_object, import_environment._ROLE_CLOUD_GEOMETRY)
        reach_object.parent = reach_root
        reach_object.matrix_parent_inverse.identity()
        local_radius = max(Vector(corner).length for corner in reach_object.bound_box)
        assert local_radius * 180.0 > import_environment._BLENDER_CLOUD_MAX_REACH
        bounded_scale = import_environment._set_cloud_preview_scale(
            reach_root,
            (reach_object,),
            180.0,
        )
        assert abs(local_radius * bounded_scale - import_environment._BLENDER_CLOUD_MAX_REACH) < 1e-4
        assert abs(
            import_environment._celestial_reach(collection)
            - import_environment._BLENDER_CLOUD_MAX_REACH
        ) < 1e-4
        import_environment._remove_object(reach_object)
        import_environment._remove_object(reach_root)

        anchor = bpy.data.objects[import_environment.ENVIRONMENT_ANCHOR_NAME]
        constraint = anchor.constraints.get(import_environment.ENVIRONMENT_CAMERA_CONSTRAINT_NAME)
        assert constraint is None

        camera_a_data = bpy.data.cameras.new("Environment Smoke Camera A")
        camera_a = bpy.data.objects.new("Environment Smoke Camera A", camera_a_data)
        scene.collection.objects.link(camera_a)
        temporary_cameras.append((camera_a, camera_a_data))
        camera_a.location = (12.0, -7.0, 4.0)
        camera_a.rotation_mode = "QUATERNION"
        camera_a.rotation_quaternion = Vector((1.0, 0.0, 0.0)).to_track_quat("-Z", "Y")
        camera_a_data.clip_end = 10.0

        camera_b_data = bpy.data.cameras.new("Environment Smoke Camera B")
        camera_b = bpy.data.objects.new("Environment Smoke Camera B", camera_b_data)
        scene.collection.objects.link(camera_b)
        temporary_cameras.append((camera_b, camera_b_data))
        camera_b.location = (-23.0, 9.0, 6.0)
        camera_b_data.clip_end = 10.0
        preview_space = SimpleNamespace(clip_end=25.0)
        assert import_environment._ensure_view_clip(scene, preview_space, 250.0)
        assert abs(preview_space.clip_end - 250.0) < 1e-6
        assert not import_environment._ensure_view_clip(scene, preview_space, 250.0)

        # Assigning a camera after preview creation must create the follower and
        # expand the camera clipping range without requiring a Preview refresh.
        scene.camera = camera_a
        assert import_environment._preview_anchors(), [
            (candidate.name, item.name)
            for candidate in bpy.data.scenes
            for item in candidate.collection.children
        ]
        assert import_environment._restore_render_anchor(scene) == 1
        constraint = anchor.constraints.get(import_environment.ENVIRONMENT_CAMERA_CONSTRAINT_NAME)
        assert constraint is None
        required_reach = import_environment._celestial_reach(
            bpy.data.collections[result.collection_name]
        ) * 1.1
        assert camera_a_data.clip_end >= required_reach
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated_anchor = anchor.evaluated_get(depsgraph).matrix_world.translation
        assert (evaluated_anchor - camera_a.matrix_world.translation).length < 1e-5, (
            tuple(anchor.location),
            tuple(evaluated_anchor),
            tuple(camera_a.matrix_world.translation),
        )
        assert (camera_lights[0].location - Vector((8.0, 3.0, 0.5))).length < 1e-5

        # A local viewport camera takes precedence only while the viewport's
        # local-camera override is enabled.
        fake_window = SimpleNamespace(scene=scene)
        fake_space = SimpleNamespace(use_local_camera=True, camera=camera_b)
        assert import_environment._view_camera(fake_window, fake_space) is camera_b
        fake_space.use_local_camera = False
        assert import_environment._view_camera(fake_window, fake_space) is camera_a

        # Switching the scene camera must retarget the existing constraint and
        # update evaluated placement, again without touching Preview controls.
        scene.camera = camera_b
        assert import_environment._restore_render_anchor(scene) == 1
        constraint = anchor.constraints.get(import_environment.ENVIRONMENT_CAMERA_CONSTRAINT_NAME)
        assert constraint is None
        assert camera_b_data.clip_end >= required_reach
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated_anchor = anchor.evaluated_get(depsgraph).matrix_world.translation
        assert (evaluated_anchor - camera_b.matrix_world.translation).length < 1e-5, (
            tuple(anchor.location),
            tuple(evaluated_anchor),
            tuple(camera_b.matrix_world.translation),
        )

        star_rotation = nodes.get("W3 Sky Star Rotation")
        assert star_rotation is not None
        first_rotation = (
            tuple(star_rotation.inputs["Axis"].default_value),
            star_rotation.inputs["Angle"].default_value,
        )

        moon_material = bpy.data.materials.get("W3 Environment Moon Preview")
        assert moon_material is not None
        moon_nodes = moon_material.node_tree.nodes
        assert moon_nodes.get("W3 Moon Phase Light") is not None
        # Fallback disc has no moon_n image: the phase term must be bypassed.
        assert abs(moon_nodes["W3 Moon Phase Mix"].inputs[0].default_value) < 1e-6

        key_light = bpy.data.objects[result.key_light_name]
        assert key_light.data.use_shadow
        if hasattr(key_light.data, "shadow_maximum_resolution"):
            assert (
                key_light.data.shadow_maximum_resolution
                >= import_environment._BLENDER_KEY_SHADOW_RESOLUTION - 1.0e-6
            )
        key_light.data.specular_factor = 0.125
        for ambient_light in ambient_lights:
            ambient_light.data.specular_factor = 0.5
            ambient_light.data.use_shadow = True
        import_environment.update_preview(
            bpy.context,
            sun_direction=(1.0, 0.0, 0.25),
            moon_direction=(0.0, 1.0, 0.5),
            key_direction=(0.0, 1.0, 1.0),
            moon_color=(2.0, 4.0, 8.0),
            moon_brightness=0.5,
            ambient_color=(0.25, 0.5, 1.0),
            ambient_energy=0.25,
            tone_exposure_ev=0.75,
            sun_back_horizon_color=(0.12, 0.34, 0.56),
            sky_brightness=0.37,
            fog_color=(0.08, 0.30, 0.42),
            fog_color_front=(0.50, 0.25, 0.10),
            fog_color_back=(0.01, 0.02, 0.03),
            aerial_color_front=(2.0, 1.0, 0.5),
            aerial_color_middle=(1.0, 1.5, 2.0),
            aerial_color_back=(0.25, 0.5, 1.0),
            fog_sky_density=0.5,
            fog_density=0.001,
            fog_dist_clamp=1000.0,
            fog_appear_distance=1.0,
            fog_appear_range=66.0,
            fog_final_exp=0.976,
            aerial_final_exp=2.0,
            fog_vert_offset=39.43,
            fog_vert_density=-0.023,
            water_color=(0.01, 0.02, 0.03),
            water_fresnel=0.0735,
            water_ambient_scale=0.295,
            water_diffuse_scale=1.0625,
            water_flow_intensity=1.0,
            water_foam_intensity=-0.365,
            sky_day_factor=1.0,
            time_seconds=settings.fake_day_seconds,
        )
        second_rotation = (
            tuple(star_rotation.inputs["Axis"].default_value),
            star_rotation.inputs["Angle"].default_value,
        )
        assert first_rotation != second_rotation
        assert abs(scene.view_settings.exposure - (original_view_exposure + 0.75)) < 1e-6
        assert abs(key_light.data.specular_factor - 0.25) < 1e-6
        assert all(abs(light.data.energy - 0.25) < 1e-6 for light in ambient_lights)
        assert all(not light.data.use_shadow for light in ambient_lights)
        assert all(abs(light.data.specular_factor) < 1e-6 for light in ambient_lights)
        assert abs(nodes["W3 Sky Global Brightness"].inputs["Scale"].default_value - 0.37) < 1e-6
        assert abs(nodes["W3 Sky Brightness"].inputs[1].default_value - 1.0) < 1e-6
        direction_z = nodes["W3 Sky Direction Z"]
        assert direction_z.inputs[0].links[0].from_socket.name == "Normal"
        assert nodes["W3 Sky Fog Mix"].inputs[0].is_linked
        assert nodes[import_environment._SKY_NODE_FOG_BASE_OPACITY].outputs[0].default_value > 0.0
        raw_fog_opacity = 1.0 - math.exp(-0.001 * 0.5 * 1000.0)
        assert abs(
            nodes[import_environment._SKY_NODE_AERIAL_BASE_OPACITY].outputs[0].default_value
            - raw_fog_opacity**2.0
        ) < 1e-6
        aerial_mix = nodes[import_environment._SKY_NODE_AERIAL_MIX]
        assert aerial_mix.inputs[1].links[0].from_node == nodes[import_environment._SKY_NODE_CLOUD_MIX]
        assert nodes[import_environment._SKY_NODE_FOG_MIX].inputs[1].links[0].from_node == aerial_mix
        assert tuple(nodes[import_environment._SKY_NODE_AERIAL_COLOR_FRONT].outputs[0].default_value) == (
            2.0,
            1.0,
            0.5,
            1.0,
        )
        fog_direction = nodes[import_environment._SKY_NODE_FOG_DIRECTION]
        expected_fog_direction = Vector((0.0, 1.0, 1.0)).normalized()
        assert all(
            abs(fog_direction.inputs[index].default_value - expected_fog_direction[index]) < 1e-6
            for index in range(3)
        )
        assert abs(camera_height.outputs[0].default_value - 0.716) < 1e-6
        assert all(
            abs(actual - expected) < 1e-6
            for actual, expected in zip(
                nodes[import_environment._SKY_NODE_SUN_HORIZON_BACK]
                .outputs[0]
                .default_value,
                (0.12, 0.34, 0.56, 1.0),
            )
        )
        expected_sky_fog = (0.08 / 1.42, 0.30 / 1.42, 0.42 / 1.42, 1.0)
        assert all(
            abs(actual - expected) < 1e-6
            for actual, expected in zip(
                nodes[import_environment._SKY_NODE_FOG_COLOR].outputs[0].default_value,
                expected_sky_fog,
            )
        )

        fog_objects = import_environment._find_role_objects(
            collection,
            import_environment._ROLE_FOG_VOLUME,
        )
        assert len(fog_objects) == 1
        fog_object = fog_objects[0]
        assert fog_object.parent is anchor
        assert tuple(fog_object.scale) == (
            import_environment._BLENDER_FOG_HALF_EXTENT,
            import_environment._BLENDER_FOG_HALF_EXTENT,
            import_environment._BLENDER_FOG_HALF_HEIGHT,
        )
        fog_material = fog_object.data.materials[0]
        fog_nodes = fog_material.node_tree.nodes
        fog_output = fog_nodes["W3 Fog Output"]
        assert not fog_output.inputs["Surface"].is_linked
        assert fog_output.inputs["Volume"].is_linked
        assert abs(fog_nodes["W3 Fog Base Density"].outputs[0].default_value - 0.001) < 1e-8
        assert abs(fog_object["witcher_environment_fog_appear_range"] - 66.0) < 1e-6
        if eevee is not None:
            assert eevee.volumetric_end >= import_environment._BLENDER_FOG_HALF_EXTENT
        fog_tint = tuple(fog_nodes["W3 Fog Scatter"].inputs["Color"].default_value)
        assert all(
            abs(actual - expected) < 1e-6
            for actual, expected in zip(fog_tint, expected_sky_fog)
        )
        world_output = next(node for node in nodes if node.type == "OUTPUT_WORLD")
        assert not world_output.inputs["Volume"].is_linked

        expected_water = {
            "W3 Water Fresnel Gain": 0.0735,
            "W3 Water Ambient Scale": 0.295,
            "W3 Water Diffuse Scale": 1.0625,
            "W3 Water Flow": 1.0,
            "W3 Water Foam": -0.365,
        }
        assert all(
            abs(water_nodes[name].outputs["Value"].default_value - value) < 1e-6
            for name, value in expected_water.items()
        )
        assert all(
            abs(actual - expected) < 1e-6
            for actual, expected in zip(
                water_nodes["W3 Water Tint"].outputs["Color"].default_value,
                (0.01, 0.02, 0.03, 1.0),
            )
        )

        moon_tint = tuple(moon_nodes["W3 Moon Color"].inputs[2].default_value)
        assert all(abs(a - b) < 1e-6 for a, b in zip(moon_tint, (0.25, 0.5, 1.0, 1.0)))
        assert abs(moon_nodes["W3 Moon Emission"].inputs["Strength"].default_value - 1.44) < 1e-6

        # Pushing the anchor out scales the discs, preserving angular size.
        import_environment.update_preview(
            bpy.context,
            sun_direction=(1.0, 0.0, 0.25),
            moon_direction=(0.0, 1.0, 0.5),
            anchor_distance=200.0,
            time_seconds=settings.fake_day_seconds,
        )
        sun_root = bpy.data.objects["W3 Environment Sun"]
        assert abs(sun_root.location.length - 200.0) < 1e-3
        assert all(abs(value - 20.0) < 1e-3 for value in sun_root.scale)
        assert not import_environment._find_role_objects(
            collection,
            import_environment._ROLE_FOG_VOLUME,
        )
        if eevee is not None:
            assert abs(eevee.volumetric_end - original_volumetric_end) < 1e-6

        user_exposure_while_active = original_view_exposure + 0.2
        scene.view_settings.exposure = user_exposure_while_active
        updated = import_environment.update_preview(
            bpy.context,
            sun_direction=(1.0, 0.0, 0.25),
            moon_direction=(-1.0, 0.0, -0.25),
            key_direction=(0.0, 1.0, 1.0),
            tone_exposure_ev=0.4,
            sky_day_factor=0.0,
            time_seconds=0.0,
            import_materials=False,
        )
        assert updated.success
        assert abs(scene.view_settings.exposure - (user_exposure_while_active + 0.4)) < 1e-6
        assert scene.world is sky_world
        # No stars image is loaded here, so the stars mix must stay at zero even
        # at night: an unassigned Environment Texture would tint the sky magenta.
        assert abs(nodes["W3 Sky Day Factor"].inputs[0].default_value) < 1e-6
        flat_sun = bpy.data.objects[updated.sun_object_names[0]].data.materials[0]
        assert flat_sun.node_tree.nodes.get("W3 Flat Emission") is not None

        assert import_environment.clear_preview(bpy.context) >= 6
        assert abs(preview_space.clip_end - 25.0) < 1e-6
        assert abs(camera_a_data.clip_end - 10.0) < 1e-6
        assert abs(camera_b_data.clip_end - 10.0) < 1e-6
        assert abs(scene.view_settings.exposure - user_exposure_while_active) < 1e-6
        assert scene.world is original_world
        assert bpy.data.worlds.get(sky_world_name) is None

        # Reloading Python loses the in-memory balance-preview state. A tagged
        # compositor left in the .blend must still be removable on the fast path.
        from witcher3_tools.importers import environment_balance_preview

        orphan_scene = bpy.data.scenes.new("Environment Balance Orphan Smoke")
        orphan_tree = bpy.data.node_groups.new(
            "Environment Balance Orphan Smoke",
            "CompositorNodeTree",
        )
        previous_tree = bpy.data.node_groups.new(
            "Environment Balance Previous Smoke",
            "CompositorNodeTree",
        )
        orphan_tree_name = orphan_tree.name
        orphan_tree[environment_balance_preview._OWNER_PROP] = True
        orphan_scene[environment_balance_preview._PREVIOUS_TREE_PROP] = previous_tree.name
        orphan_scene[environment_balance_preview._VIEW_SETTINGS_PROP] = {
            "view_transform": "AgX",
            "look": "None",
            "exposure": 0.25,
            "gamma": 1.0,
            "use_curve_mapping": False,
            "use_white_balance": False,
        }
        orphan_scene.view_settings.view_transform = "Standard"
        orphan_scene.compositing_node_group = orphan_tree
        orphan_context = SimpleNamespace(
            scene=orphan_scene,
            window_manager=bpy.context.window_manager,
        )
        assert environment_balance_preview.clear_balance_preview(orphan_context)
        assert orphan_scene.compositing_node_group is previous_tree
        assert orphan_scene.view_settings.view_transform == "AgX"
        assert abs(orphan_scene.view_settings.exposure - 0.25) < 1e-6
        assert orphan_tree_name not in bpy.data.node_groups
        bpy.data.scenes.remove(orphan_scene)
        bpy.data.node_groups.remove(previous_tree)

        # An edit after the last refresh must win over managed restoration.
        import_environment._ensure_view_exposure(scene, 0.5)
        user_exposure_after_refresh = original_view_exposure + 0.9
        scene.view_settings.exposure = user_exposure_after_refresh
        assert import_environment.clear_preview(bpy.context) == 0
        assert abs(scene.view_settings.exposure - user_exposure_after_refresh) < 1e-6
        assert scene.world is original_world
    finally:
        try:
            from witcher3_tools.importers import import_environment

            import_environment.clear_preview(bpy.context)
        except Exception:
            pass
        if scene is not None:
            try:
                scene.camera = original_camera
            except Exception:
                pass
            eevee = getattr(scene, "eevee", None)
            if eevee is not None and original_volumetric_end is not None:
                eevee.volumetric_end = original_volumetric_end
            if original_view_exposure is not None:
                scene.view_settings.exposure = original_view_exposure
        for camera_obj, camera_data in reversed(temporary_cameras):
            if camera_obj.name in bpy.data.objects:
                bpy.data.objects.remove(camera_obj, do_unlink=True)
            if camera_data.name in bpy.data.cameras and not camera_data.users:
                bpy.data.cameras.remove(camera_data)
        for obj in temporary_objects:
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in temporary_meshes:
            if mesh.name in bpy.data.meshes and not mesh.users:
                bpy.data.meshes.remove(mesh)
        if original_world is not None:
            if bpy.context.scene.world is original_world:
                bpy.context.scene.world = None
            if original_world.name in bpy.data.worlds:
                bpy.data.worlds.remove(original_world)
        for material in collision_materials:
            if material.name in bpy.data.materials:
                bpy.data.materials.remove(material)
        for material in temporary_materials:
            if material.name in bpy.data.materials:
                bpy.data.materials.remove(material)
        witcher3_tools.unregister()
        assert import_environment._restore_render_anchor not in bpy.app.handlers.render_init
        assert import_environment._restore_render_anchor not in bpy.app.handlers.render_pre
        assert not bpy.app.timers.is_registered(import_environment._follow_viewports)

    print("ENVIRONMENT_BLENDER_SMOKE_OK")


if __name__ == "__main__":
    main()
