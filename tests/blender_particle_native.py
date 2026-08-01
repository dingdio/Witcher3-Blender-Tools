"""Run with:
  blender --background --factory-startup --python tests/blender_particle_native.py
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from math import hypot, isclose, pi
from pathlib import Path
from random import Random
import sys
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import bpy  # noqa: E402
from mathutils import Quaternion  # noqa: E402

from witcher3_tools.CR2W.dc_particle import (  # noqa: E402
    ParticleCurve,
    ParticleCurveKey,
    ParticleBurst,
    ParticleEmitter,
    ParticleEvaluator,
    ParticleLOD,
    ParticleMaterial,
    ParticleModule,
    ParticleSystem,
)
from witcher3_tools.CR2W import common_blender  # noqa: E402
from witcher3_tools.importers import import_particle  # noqa: E402


def _const(value, vector=False):
    return ParticleEvaluator("CEvaluatorVectorConst" if vector else "CEvaluatorFloatConst", value=value)


def _random(minimum, maximum, free_axes=""):
    kind = "Vector" if free_axes else "Float"
    return ParticleEvaluator(
        f"CEvaluator{kind}RandomUniform",
        minimum=minimum,
        maximum=maximum,
        free_axes=free_axes,
    )


def _curve_key(time, value, left=1, right=1, right_tangent=0.0):
    return ParticleCurveKey(
        time=time,
        value=value,
        tangent_left=(-0.1, 0.0, 0.0, 0.0),
        tangent_right=(0.1, right_tangent, 0.0, 0.0),
        curve_type_l=left,
        curve_type_r=right,
    )


def _curve(*values, vector=False, bezier=False):
    keys = tuple(
        _curve_key(
            time,
            value,
            3 if bezier and index == 0 else 1,
            3 if bezier and index == 0 else 1,
            0.25 if bezier and index == 0 else 0.0,
        )
        for index, (time, value) in enumerate(values)
    )
    return ParticleEvaluator(
        "CEvaluatorVectorCurve" if vector else "CEvaluatorFloatCurve",
        curves=(ParticleCurve(keys=keys),),
        free_axes="FVA_One" if vector else "",
    )


def _module(type_name, **evaluators):
    return ParticleModule(type_name, evaluators=evaluators)


def _emitter(name, drawer, birth, lifetime, maximum, radius=0.0, tip=False, ring=False):
    modules = [
        _module("CParticleInitializerLifeTime", lifeTime=_const(lifetime)),
        _module("CParticleInitializerSize", size=_random({"X": 0.1}, {"X": 0.2}, "FVA_One")),
        _module(
            "CParticleModificatorSizeOverLife",
            size=_curve((0.0, 0.2), (1.0, 2.5 if ring else 1.5), vector=True, bezier=True),
        ),
        _module("CParticleModificatorAlphaOverLife", alpha=_curve((0.0, 1.0), (1.0, 0.0))),
        _module("CParticleInitializerSpawnCircle", outerRadius=_const(radius)),
        _module("CParticleInitializerRotation", rotation=_random(0.0, 1.0)),
    ]
    if not ring:
        velocity = (1.0, 1.5) if tip else (0.5, 1.0)
        modules.extend((
            _module("CParticleInitializerVelocity", velocity=_random(velocity[0], velocity[1], "FVA_One")),
            _module("CParticleModificatorAcceleration", direction=_const({"Z": -1.0}, True), scale=_const(2.0)),
        ))
    if tip:
        modules.append(_module("CParticleInitializerRotationRate", rotationRate=_random(-0.3, 0.3)))
    if ring:
        modules.append(_module(
            "CParticleModificatorTextureAnimation",
            initialFrame=_random(0.0, 2.0),
            animationSpeed=_const(30.0),
        ))
    if ring:
        parameters = {
            "normal_and_splash": r"fx\textures\water\water_circle_2x1_normal.xbm",
            "subuvwidth": 2.0,
            "reflection_multiplier": 20.0,
            "reflection_power_exponent": 4.0,
            "refraction_multiplier": 0.025,
            "soft_alpha": 1.0,
            "normal_multiplier": {"X": 1.0, "Y": 1.0, "Z": 1.0, "W": 2.0},
        }
    elif tip:
        parameters = {
            "reflection_multiplier": 20.0,
            "reflection_power_exponent": 5.0,
            "refraction_multiplier": 0.05,
        }
    else:
        parameters = {
            "reflection_multiplier": 20.0,
            "reflection_power_exponent": 6.0,
            "alpha_multiplier": 3.0,
        }
    return ParticleEmitter(
        name=name,
        max_particles=maximum,
        drawer_type=drawer,
        material=ParticleMaterial(r"fx\shaders\water_splash_additive.w2mg", parameters),
        lods=(ParticleLOD(birth_rate=_const(birth)),),
        modules=tuple(modules),
    )


def _fcurves(obj):
    return import_particle._action_fcurves(obj.animation_data.action)


def main() -> None:
    real_bpy = import_particle.bpy
    import_particle.bpy = SimpleNamespace(data=SimpleNamespace())
    try:
        assert import_particle._billboard_bases() == []
    finally:
        import_particle.bpy = real_bpy

    segments = ParticleCurve(keys=(
        _curve_key(0.0, 2.0, right=0),
        _curve_key(1.0, 4.0),
        _curve_key(2.0, 6.0, right=3),
        _curve_key(3.0, 10.0, left=3),
    ))
    assert import_particle._evaluate_curve(segments, 0.5) == 2.0
    assert import_particle._evaluate_curve(segments, 1.0) == 4.0
    assert import_particle._evaluate_curve(segments, 1.5) == 5.0
    assert isclose(import_particle._evaluate_curve(segments, 2.25), 6.625)
    assert import_particle._evaluate_curve(ParticleCurve(keys=(_curve_key(0.0, 3.0),)), 0.5) == 3.0
    sphere_settings = SimpleNamespace(
        spawn_sphere_inner=2.0,
        spawn_sphere_outer=2.0,
        spawn_sphere_surface=True,
        spawn_sphere_axes=(True, False, False, True, True, True),
    )
    sphere_offset = import_particle._sphere_spawn_offset(sphere_settings, Random(1))
    assert sphere_offset[0] >= 0.0 and sphere_offset[1] <= 0.0
    assert isclose(hypot(*sphere_offset), 2.0)
    sphere_settings.spawn_sphere_axes = (False, False, True, True, True, True)
    assert import_particle._sphere_spawn_offset(sphere_settings, Random(1))[0] == 0.0

    handlers_before = tuple(bpy.app.handlers.frame_change_post)
    bpy.context.scene.camera = None
    active_view_rotation = import_particle._active_view_rotation
    import_particle._active_view_rotation = lambda _scene=None: None
    turn_settings = import_particle._settings(ParticleEmitter(modules=(
        _module("CParticleInitializerRotation", rotation=_const(0.25)),
        _module("CParticleInitializerRotationRate", rotationRate=_const(-0.25)),
    )))
    turn_generation = import_particle._generation(turn_settings, Random(0))
    assert isclose(turn_generation["initial_rotation"], 0.5 * pi, abs_tol=1e-7)
    assert isclose(turn_generation["rotation_rate"], -0.5 * pi, abs_tol=1e-7)
    image = bpy.data.images.new("Particle Smoke Alpha", 8, 4, alpha=True)
    system = ParticleSystem(
        source_path=r"fx\water\water_fountain\test.w2p",
        emitters=(
            _emitter("splash", "CParticleDrawerBillboard", 10.0, 1.0, 15, radius=0.65),
            _emitter("rings", "CParticleDrawerEmitterOrientation", 8.0, 2.0, 20, radius=0.6, ring=True),
            _emitter("splash_tip", "CParticleDrawerBillboard", 10.0, 1.5, 55, tip=True),
            _emitter("generic", "CParticleDrawerEmitterOrientation", 1.0, 1.0, 1, radius=0.25, ring=True),
        ),
    )

    objects = import_particle.import_particle_system(system, fps=24, image_loader=lambda _path: image)
    by_name = {name: [obj for obj in objects if obj["witcher_particle_emitter"] == name]
               for name in ("splash", "rings", "splash_tip", "generic")}
    assert {name: len(items) for name, items in by_name.items()} == {
        "splash": 10,
        "rings": 16,
        "splash_tip": 15,
        "generic": 1,
    }
    assert all(len(obj.data.polygons) == 1 for obj in objects)
    assert all(
        obj["witcher_particle_billboard_mode"] == "live_fallback"
        for name in ("splash", "splash_tip")
        for obj in by_name[name]
    )
    assert all(obj.constraints.get("Witcher Particle Camera Facing") for obj in by_name["splash"])

    rings = by_name["rings"]
    assert max(abs(hypot(obj.location.x, obj.location.y) - 0.6) for obj in rings) < 1e-6
    assert all(abs(vertex.co.z) < 1e-7 for vertex in rings[0].data.vertices)
    assert all(obj.animation_data and obj.animation_data.action for obj in objects)
    assert all(
        all(any(modifier.type == "CYCLES" for modifier in curve.modifiers) for curve in _fcurves(obj))
        for obj in objects
    )
    for obj in objects:
        generations = json.loads(obj["witcher_particle_generations"])
        expected_period = 4.0 * obj["witcher_particle_lifetime"] * 24.0
        action = obj.animation_data.action
        assert len(generations) == 4
        assert obj["witcher_particle_generation_count"] == 4
        assert isclose(obj["witcher_particle_action_period_frames"], expected_period, abs_tol=1e-6)
        assert isclose(action.frame_end - action.frame_start, expected_period, abs_tol=1e-5), (
            obj.name,
            action.frame_start,
            action.frame_end,
            expected_period,
        )

    ring_curves = _fcurves(rings[0])
    ring_x = next(curve for curve in ring_curves if curve.data_path == "location" and curve.array_index == 0)
    ring_y = next(curve for curve in ring_curves if curve.data_path == "location" and curve.array_index == 1)
    ring_duration = rings[0]["witcher_particle_lifetime"] * 24.0
    ring_births = []
    for generation_index in range(4):
        birth_frame = rings[0].animation_data.action.frame_start + generation_index * ring_duration
        x = min(ring_x.keyframe_points, key=lambda point: abs(point.co.x - birth_frame))
        y = min(ring_y.keyframe_points, key=lambda point: abs(point.co.x - birth_frame))
        ring_births.append((x.co.y, y.co.y))
    assert len({(round(x, 6), round(y, 6)) for x, y in ring_births}) == 4
    assert max(abs(hypot(x, y) - 0.6) for x, y in ring_births) < 1e-6
    assert any(point.interpolation == "LINEAR" for point in ring_x.keyframe_points)
    assert all(
        point.interpolation == "CONSTANT"
        for point, following in zip(ring_x.keyframe_points, ring_x.keyframe_points[1:])
        if following.co.x - point.co.x <= 0.1001
    )

    rotation_curves = [curve for curve in _fcurves(by_name["splash"][0])
                       if curve.data_path == "rotation_quaternion"]
    assert {curve.array_index for curve in rotation_curves} == {0, 1, 2, 3}
    assert any(len({round(point.co.y, 6) for point in curve.keyframe_points}) > 1
               for curve in rotation_curves)
    assert not any(curve.data_path == "rotation_euler" for curve in _fcurves(by_name["splash"][0]))
    splash_generation = json.loads(by_name["splash"][0]["witcher_particle_generations"])[0]
    tip_generations = json.loads(by_name["splash_tip"][0]["witcher_particle_generations"])
    assert 0.0 <= splash_generation["initial_rotation"] <= 2.0 * pi
    assert all(-0.3 * 2.0 * pi <= generation["rotation_rate"] <= 0.3 * 2.0 * pi
               for generation in tip_generations)

    for name, low, high in (("splash", 0.5, 1.0), ("splash_tip", 1.0, 1.5)):
        values = [obj["witcher_particle_velocity_z"] for obj in by_name[name]]
        assert min(values) >= low and max(values) <= high
        location_curve = next(
            curve for curve in _fcurves(by_name[name][0])
            if curve.data_path == "location" and curve.array_index == 2
        )
        heights = [point.co.y for point in location_curve.keyframe_points]
        assert max(heights) > heights[0] and heights[-1] < max(heights)

    material = rings[0].data.materials[0]
    nodes = material.node_tree.nodes
    texture_alpha = nodes["W3 Particle Texture Alpha"]
    assert texture_alpha.inputs[0].is_linked and texture_alpha.inputs[1].is_linked
    assert nodes.get("W3 Particle Additive Approximation") is not None
    texture = nodes["W3 Particle Texture"]
    normal_map = nodes["W3 Particle Normal Map"]
    fresnel = nodes["W3 Particle Fresnel"]
    reflection_power = nodes["W3 Particle Reflection Power"]
    reflection_gain = nodes["W3 Particle Reflection Gain"]
    refraction_gain = nodes["W3 Particle Refraction Gain"]
    alpha_gain = nodes["W3 Particle Alpha Multiplier"]
    detail_limit = nodes["W3 Particle Detail Limit"]
    additive_strength = nodes["W3 Particle Additive Strength"]
    preview_gain = nodes["W3 Particle Preview Gain"]
    assert normal_map.inputs["Color"].links[0].from_node == texture
    assert fresnel.inputs["Normal"].links[0].from_node == normal_map
    assert reflection_power.inputs[0].links[0].from_node == fresnel
    assert reflection_gain.inputs[0].links[0].from_node == reflection_power
    assert refraction_gain.inputs[0].links[0].from_node == reflection_gain
    assert additive_strength.inputs[0].links[0].from_node == alpha_gain
    assert additive_strength.inputs[1].links[0].from_node == detail_limit
    assert preview_gain.inputs[0].links[0].from_node == additive_strength
    assert nodes["W3 Particle Additive Emission"].inputs["Strength"].links[0].from_node == preview_gain
    assert nodes.get("W3 Particle Alpha Strength") is None
    assert isclose(normal_map.inputs["Strength"].default_value, 2.0, abs_tol=1e-6)
    assert isclose(reflection_power.inputs[1].default_value, 0.5, abs_tol=1e-6)
    assert isclose(reflection_gain.inputs[1].default_value, 0.625, abs_tol=1e-6)
    assert isclose(refraction_gain.inputs[1].default_value, 0.1, abs_tol=1e-6)
    assert isclose(alpha_gain.inputs[1].default_value, 1.0, abs_tol=1e-6)
    assert isclose(detail_limit.inputs[1].default_value, 0.8, abs_tol=1e-6)
    assert isclose(preview_gain.inputs[1].default_value, 6.0, abs_tol=1e-6)
    assert isclose(material["witcher_particle_soft_alpha"], 1.0, abs_tol=1e-6)
    assert isclose(material["witcher_particle_preview_gain"], 6.0, abs_tol=1e-6)
    assert isclose(material["witcher_particle_reflection_multiplier"], 20.0, abs_tol=1e-6)
    assert isclose(material["witcher_particle_refraction_multiplier"], 0.025, abs_tol=1e-6)
    splash_material = by_name["splash"][0].data.materials[0]
    assert isclose(splash_material["witcher_particle_preview_gain"], 6.0, abs_tol=1e-6)
    assert isclose(splash_material["witcher_particle_alpha_multiplier"], 3.0, abs_tol=1e-6)
    assert isclose(
        splash_material.node_tree.nodes["W3 Particle Alpha Multiplier"].inputs[1].default_value,
        3.0,
        abs_tol=1e-6,
    )
    assert material["witcher_particle_atlas_width"] == 2
    assert not material.use_backface_culling
    if hasattr(material, "surface_render_method"):
        assert material.surface_render_method == "BLENDED"
    if hasattr(material, "use_transparency_overlap"):
        assert material.use_transparency_overlap
    else:
        assert material.blend_method == "BLEND"
    frame_curve = next(curve for curve in _fcurves(rings[0]) if curve.data_path == "color" and curve.array_index == 0)
    assert all(point.interpolation == "LINEAR" for point in frame_curve.keyframe_points)
    assert isclose(
        frame_curve.keyframe_points[15].co.y - frame_curve.keyframe_points[0].co.y,
        30.0 * 2.0,
        abs_tol=1e-5,
    )

    smoke_modules = (
        _module("CParticleInitializerPosition", position=_const({"X": 0.0, "Y": 0.0, "Z": 0.1}, True)),
        _module("CParticleInitializerSize", size=_random({"X": 0.4}, {"X": 0.6}, "FVA_One")),
        _module(
            "CParticleInitializerVelocity",
            velocity=_random(
                {"X": -0.2, "Y": -0.2, "Z": 0.4},
                {"X": 0.2, "Y": 0.2, "Z": 0.6},
                "FVA_Three",
            ),
        ),
        _module("CParticleInitializerLifeTime", lifeTime=_random(2.0, 3.0)),
        ParticleModule(
            "CParticleModificatorTextureAnimation",
            properties={"animationMode": "TAM_LifeTime"},
            evaluators={"initialFrame": _const(0.0), "animationSpeed": _const(63.0)},
        ),
        _module("CParticleModificatorAlphaOverLife", alpha=_curve((0.0, 0.0), (0.2, 1.0), (1.0, 0.0))),
        _module(
            "CParticleModificatorAcceleration",
            direction=_const({"Z": -1.0}, True),
            scale=_const(-0.5),
        ),
        _module(
            "CParticleModificatorVelocityOverLife",
            velocity=_const({"X": 1.0, "Y": 1.0, "Z": 1.0}, True),
        ),
    )
    fire_modules = (
        _module("CParticleInitializerLifeTime", lifeTime=_const(3.5)),
        _module("CParticleInitializerSize", size=_random({"X": 0.2}, {"X": 0.3}, "FVA_One")),
        _module("CParticleInitializerPosition", position=_const({"Z": 0.2}, True)),
        ParticleModule(
            "CParticleInitializerVelocity",
            enabled=False,
            evaluators={"velocity": _const({"Z": 10.0}, True)},
        ),
        ParticleModule(
            "CParticleInitializerRotationRate",
            enabled=False,
            evaluators={"rotationRate": _const(1.0)},
        ),
        ParticleModule(
            "CParticleModificatorTextureAnimation",
            properties={"animationMode": "TAM_Speed"},
            evaluators={"initialFrame": _random(0.0, 20.0), "animationSpeed": _const(25.0)},
        ),
    )
    ember_modules = (
        _module(
            "CParticleInitializerSize",
            size=_random(
                {"X": 0.006, "Y": 0.0035},
                {"X": 0.009, "Y": 0.0055},
                "FVA_Two",
            ),
        ),
        _module(
            "CParticleInitializerVelocity",
            velocity=_random(
                {"X": -1.0, "Y": -1.0, "Z": -0.5},
                {"X": 1.0, "Y": 1.0, "Z": 2.0},
                "FVA_Three",
            ),
        ),
        _module("CParticleInitializerLifeTime", lifeTime=_random(0.4, 0.7)),
        _module("CParticleInitializerSpawnSphere", innerRadius=_const(0.0), outerRadius=_const(0.1)),
        _module("CParticleInitializerSpawnBox", extents=_const({"X": 0.1, "Y": 0.1, "Z": 0.1}, True)),
    )
    fire_system = ParticleSystem(
        source_path=r"dlc\bob\data\fx\gameplay\light_sources\brazier_fire.w2p",
        emitters=(
            ParticleEmitter(
                name="smoke_vertex",
                max_particles=7,
                drawer_type="CParticleDrawerBillboard",
                material=ParticleMaterial(
                    r"fx\shaders\subuv_alpha_blend.w2mg",
                    {"color_multiplier": 0.4, "alpha_value": 0.05},
                ),
                lods=(ParticleLOD(birth_rate=_const(2.0)),),
                modules=smoke_modules,
            ),
            ParticleEmitter(
                name="fire_anim",
                max_particles=4,
                drawer_type="CParticleDrawerSphereAligned",
                material=ParticleMaterial(
                    r"dlc\bob\data\fx\shaders\fire_glow.w2mg",
                    {
                        "tex": r"dlc\bob\data\fx\textures\fire\fire_8x8.xbm",
                        "subuUVwidth": 8.0,
                        "subUVheight": 8.0,
                        "color": {"Red": 255, "Green": 150, "Blue": 100, "Alpha": 255},
                        "color_multiplier": 1.0,
                        "glow_color": {"Red": 255, "Green": 75, "Blue": 50, "Alpha": 255},
                        "glow_multiplier": 0.8,
                    },
                ),
                lods=(ParticleLOD(bursts=(ParticleBurst(),), duration=1.5),),
                modules=fire_modules,
            ),
            ParticleEmitter(
                name="embers",
                max_particles=18,
                drawer_type="CParticleDrawerMotionBlur",
                material=ParticleMaterial(
                    r"fx\shaders\addtive_standard.w2mg",
                    {
                        "diffuse": r"fx\textures\fire\fire_spark_04.xbm",
                        "subuvwidth": 2.0,
                        "subuvheight": 2.0,
                        "color_multiply_value": 20.0,
                    },
                ),
                lods=(ParticleLOD(birth_rate=_const(5.0)),),
                modules=ember_modules,
            ),
        ),
    )
    loaded_fire_textures = []

    def fire_image_loader(path):
        loaded_fire_textures.append(path)
        return image

    fire_objects = import_particle.import_particle_system(
        fire_system,
        fps=24,
        image_loader=fire_image_loader,
    )
    fire_by_name = {
        name: [obj for obj in fire_objects if obj["witcher_particle_emitter"] == name]
        for name in ("smoke_vertex", "fire_anim", "embers")
    }
    assert {name: len(items) for name, items in fire_by_name.items()} == {
        "smoke_vertex": 5,
        "fire_anim": 3,
        "embers": 3,
    }
    assert r"fx\textures\smoke\puffy_smoke_8x8.xbm" in loaded_fire_textures
    smoke_material = fire_by_name["smoke_vertex"][0].data.materials[0]
    assert smoke_material["witcher_particle_material_style"] == "alpha"
    assert smoke_material["witcher_particle_atlas_width"] == 8
    assert smoke_material["witcher_particle_atlas_height"] == 8
    smoke_frames = next(
        curve for curve in _fcurves(fire_by_name["smoke_vertex"][0])
        if curve.data_path == "color" and curve.array_index == 0
    )
    assert max(point.co.y for point in smoke_frames.keyframe_points) > 7
    assert all(obj["witcher_particle_burst_count"] == 1 for obj in fire_by_name["fire_anim"])
    fire_material = fire_by_name["fire_anim"][0].data.materials[0]
    assert fire_material["witcher_particle_atlas_width"] == 8
    assert fire_material["witcher_particle_atlas_height"] == 8
    assert all(obj["witcher_particle_atlas_width"] == 8 for obj in fire_by_name["fire_anim"])
    fire_frames = next(
        curve for curve in _fcurves(fire_by_name["fire_anim"][0])
        if curve.data_path == "color" and curve.array_index == 0
    )
    assert all(point.interpolation == "LINEAR" for point in fire_frames.keyframe_points)
    assert isclose(
        fire_frames.keyframe_points[15].co.y - fire_frames.keyframe_points[0].co.y,
        25.0 * 3.5,
        abs_tol=1e-5,
    )
    assert fire_material["witcher_particle_packed_channels"] == "R=color,G=glow"
    assert "W3 Particle Fire Red" in fire_material.node_tree.nodes
    assert "W3 Particle Fire Green" in fire_material.node_tree.nodes
    assert "W3 Particle Fire Color" in fire_material.node_tree.nodes
    assert "W3 Particle Fire Gamma To Linear" in fire_material.node_tree.nodes
    assert "W3 Particle Fire Silhouette" in fire_material.node_tree.nodes
    assert "W3 Particle Fire Masked Color" in fire_material.node_tree.nodes
    assert "W3 Particle Texture Alpha" not in fire_material.node_tree.nodes
    assert isclose(
        fire_material.node_tree.nodes["W3 Particle Alpha Multiplier"].inputs[1].default_value,
        1.0,
        abs_tol=1e-6,
    )
    fire_generation = json.loads(fire_by_name["fire_anim"][0]["witcher_particle_generations"])[0]
    assert fire_generation["velocity"] == [0.0, 0.0, 0.0]
    assert all(
        abs(value) < 1e-8
        for value in fire_by_name["fire_anim"][0]["witcher_particle_acceleration"]
    )
    ember_generation = json.loads(fire_by_name["embers"][0]["witcher_particle_generations"])[0]
    assert any(abs(value) > 1e-5 for value in ember_generation["spawn_position"])
    assert not isclose(ember_generation["base_size"][0], ember_generation["base_size"][1])

    candle_system = ParticleSystem(
        source_path=r"fx\light_sources\candles\candle_flame_fx2.w2p",
        emitters=(ParticleEmitter(
            name="flame",
            max_particles=2,
            drawer_type="CParticleDrawerMotionBlur",
            material=ParticleMaterial(
                r"fx\shaders\addtive_standard.w2mg",
                {
                    "diffuse": r"fx\textures\fire\candle_flame_02.xbm",
                    "color_multiply": {"Red": 255, "Green": 145, "Blue": 106, "Alpha": 255},
                    "color_multiply_value": 2.0,
                },
            ),
            lods=(ParticleLOD(birth_rate=_const(2.0)),),
            modules=(
                _module(
                    "CParticleInitializerSize",
                    size=_random(
                        {"X": 0.1, "Y": 0.015},
                        {"X": 0.11, "Y": 0.015},
                        "FVA_Two",
                    ),
                ),
                _module("CParticleInitializerLifeTime", lifeTime=_const(1.0)),
            ),
        ),),
    )
    candle = import_particle.import_particle_system(
        candle_system,
        fps=24,
        image_loader=lambda _path: image,
    )
    assert len(candle) == 2
    assert all(obj.scale.x / obj.scale.y > 6.0 for obj in candle)
    assert candle[0].data.materials[0]["witcher_particle_material_style"] == "additive"
    assert [tuple(loop.uv) for loop in candle[0].data.uv_layers.active.data] == [
        (0.0, 1.0),
        (0.0, 0.0),
        (1.0, 0.0),
        (1.0, 1.0),
    ]

    repeated = import_particle.import_particle_system(system, fps=24, image_loader=lambda _path: image)
    for first, second in zip(objects, repeated):
        assert first["witcher_particle_seed"] == second["witcher_particle_seed"]
        assert first["witcher_particle_spawn_angle"] == second["witcher_particle_spawn_angle"]
        assert first["witcher_particle_velocity_z"] == second["witcher_particle_velocity_z"]
        assert first["witcher_particle_generations"] == second["witcher_particle_generations"]

    target = bpy.data.objects.new("Particle Billboard Target", None)
    bpy.context.scene.collection.objects.link(target)
    target.rotation_mode = "QUATERNION"
    target.rotation_quaternion = Quaternion((0.0, 1.0, 0.0), 0.45)
    parent = bpy.data.objects.new("Particle Billboard Parent", None)
    bpy.context.scene.collection.objects.link(parent)
    parent.rotation_mode = "QUATERNION"
    parent.rotation_quaternion = Quaternion((1.0, 0.0, 0.0), -0.3)
    bpy.context.view_layer.update()
    billboard_system = ParticleSystem(
        source_path="billboard.w2p",
        emitters=(_emitter("billboard", "CParticleDrawerBillboard", 1.0, 1.0, 1),),
    )
    billboard = import_particle.import_particle_system(
        billboard_system,
        fps=24,
        image_loader=lambda _path: image,
        billboard_target=target,
        parent=parent,
    )[0]
    assert len(billboard.data.polygons) == 1
    constraint = billboard.constraints.get("Witcher Particle Camera Facing")
    assert constraint and constraint.target.get(import_particle.PARTICLE_BILLBOARD_BASIS_PROP)
    assert billboard["witcher_particle_billboard_mode"] == "live_target"
    first_generation = json.loads(billboard["witcher_particle_generations"])[0]
    expected_orientation = (
        target.matrix_world.to_quaternion()
        @ Quaternion((0.0, 0.0, 1.0), pi / 2.0 - first_generation["initial_rotation"])
    ).normalized()
    bpy.context.view_layer.update()
    evaluated = billboard.evaluated_get(bpy.context.evaluated_depsgraph_get())
    assert abs(abs(evaluated.matrix_world.to_quaternion().dot(expected_orientation)) - 1.0) < 1e-6

    import_particle._active_view_rotation = lambda _scene=None: Quaternion((0.0, 0.0, 1.0), 0.25)
    viewport_billboard = import_particle.import_particle_system(
        billboard_system,
        fps=24,
        image_loader=lambda _path: image,
    )[0]
    assert viewport_billboard["witcher_particle_billboard_mode"] == "live_viewport"
    viewport_basis = viewport_billboard.constraints["Witcher Particle Camera Facing"].target
    before_follow = viewport_billboard.evaluated_get(
        bpy.context.evaluated_depsgraph_get()
    ).matrix_world.to_quaternion()
    next_view = Quaternion((1.0, 0.0, 0.0), 0.55)
    import_particle._active_view_rotation = lambda _scene=None: next_view
    assert import_particle._follow_particle_viewports() > 0.0
    bpy.context.view_layer.update()
    after_follow = viewport_billboard.evaluated_get(
        bpy.context.evaluated_depsgraph_get()
    ).matrix_world.to_quaternion()
    assert abs(before_follow.dot(after_follow)) < 0.999

    camera_data = bpy.data.cameras.new("Particle Smoke Camera")
    camera = bpy.data.objects.new("Particle Smoke Camera", camera_data)
    bpy.context.scene.collection.objects.link(camera)
    camera.rotation_mode = "QUATERNION"
    camera.rotation_quaternion = Quaternion((1.0, 0.0, 0.0), 0.7)
    bpy.context.scene.camera = camera
    bpy.context.view_layer.update()
    import_particle._restore_particle_billboards(bpy.context.scene)
    bpy.context.view_layer.update()
    assert abs(
        abs(viewport_basis.matrix_world.to_quaternion().dot(camera.rotation_quaternion)) - 1.0
    ) < 1e-6
    viewport_with_camera = import_particle.import_particle_system(
        billboard_system,
        fps=24,
        image_loader=lambda _path: image,
    )[0]
    assert viewport_with_camera["witcher_particle_billboard_mode"] == "live_viewport"

    import_particle._active_view_rotation = lambda _scene=None: None
    camera_billboard = import_particle.import_particle_system(
        billboard_system,
        fps=24,
        image_loader=lambda _path: image,
    )[0]
    assert camera_billboard["witcher_particle_billboard_mode"] == "live_camera"
    viewport_after_import = Quaternion((0.0, 0.0, 1.0), 0.4)
    import_particle._active_view_rotation = lambda _scene=None: viewport_after_import
    camera_basis = camera_billboard.constraints["Witcher Particle Camera Facing"].target
    import_particle._update_billboard_basis(camera_basis, bpy.context.scene)
    bpy.context.view_layer.update()
    assert abs(abs(camera_basis.matrix_world.to_quaternion().dot(viewport_after_import)) - 1.0) < 1e-6

    context_paths = []
    context_active = [False]
    original_redkit_context = common_blender.redkit_repo_context

    @contextmanager
    def tracked_redkit_context(source_path=None, roots=None):
        context_paths.append(source_path)
        context_active[0] = True
        try:
            yield
        finally:
            context_active[0] = False

    common_blender.redkit_repo_context = tracked_redkit_context
    absolute_source = str(REPO_ROOT / "absolute_particle_test.w2p")
    absolute_system = ParticleSystem(
        source_path=absolute_source,
        emitters=(_emitter("absolute", "CParticleDrawerBillboard", 1.0, 1.0, 1),),
    )
    try:
        import_particle.import_particle_system(
            absolute_system,
            fps=24,
            image_loader=lambda _path: image if context_active[0] else None,
        )
    finally:
        common_blender.redkit_repo_context = original_redkit_context
    assert context_paths == [absolute_source]
    import_particle._active_view_rotation = active_view_rotation
    assert tuple(bpy.app.handlers.frame_change_post) == handlers_before
    print("PARTICLE_BLENDER_SMOKE_OK")


if __name__ == "__main__":
    main()
