from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import bpy  # noqa: E402
import witcher3_tools  # noqa: E402


def _arguments() -> tuple[Path, Path]:
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if len(args) != 2:
        raise SystemExit("Pass the fountain .w2ent and REDkit uncooked root after --")
    entity, uncooked = (Path(value).resolve() for value in args)
    if not entity.is_file() or not uncooked.is_dir():
        raise SystemExit(f"Missing test input: {entity}, {uncooked}")
    return entity, uncooked


def _r4data_root(path: Path) -> Path:
    for parent in path.parents:
        if parent.name.casefold() == "r4data":
            return parent
    raise SystemExit(f"Entity is not below r4data: {path}")


def _repo_relative(path: str, roots: tuple[Path, ...]) -> str:
    resolved = Path(path).resolve()
    for root in roots:
        try:
            return str(resolved.relative_to(root)).replace("/", "\\")
        except ValueError:
            continue
    return ""


def main() -> None:
    entity_path, uncooked_root = _arguments()
    depot_root = _r4data_root(entity_path)
    roots = (depot_root, uncooked_root)
    witcher3_tools.register()
    try:
        from witcher3_tools.CR2W.common_blender import (
            clear_repo_override_roots,
            redkit_repo_context,
            set_repo_override_roots,
            win_path_isfile,
        )
        from witcher3_tools.importers import import_entity, import_mesh, import_particle
        from witcher3_tools.materials import material as material_module
        from witcher3_tools.materials.nodes import domain as material_domain

        texture_root = str(uncooked_root)
        witcher3_tools.get_uncook_path = lambda context=None: texture_root
        witcher3_tools.get_texture_path = lambda context=None: texture_root
        import_entity.get_uncook_path = lambda context=None: texture_root
        import_mesh.get_uncook_path = lambda context=None: texture_root
        import_mesh.get_texture_path = lambda context=None: texture_root
        import_mesh.get_mod_directory = lambda context=None: ""
        import_mesh.get_modded_texture_path = lambda context=None: ""
        import_mesh.get_repo_from_abs_path = lambda path: _repo_relative(path, roots)
        material_module.get_uncook_path = lambda context=None: texture_root
        material_module.get_texture_path = lambda context=None: texture_root
        material_module.get_mod_directory = lambda context=None: ""
        material_module.get_modded_texture_path = lambda context=None: ""
        material_module.get_tex_ext = lambda context=None: ".dds"
        material_module._apply_image_texture_metadata = lambda *args, **kwargs: None
        material_domain.auto_load_base_material_snapshot = lambda *args, **kwargs: None

        set_repo_override_roots([str(root) for root in roots], read_only=True)
        try:
            with redkit_repo_context(str(entity_path), roots=[str(root) for root in roots]):
                import_entity.import_direct_entity_file(str(entity_path))

            splash_path = r"fx\water\water_fountain\fountain_splash.w2mesh"
            splash_objects = [
                obj for obj in bpy.data.objects
                if obj.type == "MESH"
                and str(obj.get("witcher_path", "")).replace("/", "\\").casefold()
                == splash_path.casefold()
            ]
            assert Counter(obj.get("witcher_name", "") for obj in splash_objects) == Counter({
                "CMeshComponent0": 1,
                "CMeshComponent1": 1,
            })

            material_base = r"fx\water\water_fountain\m_fountain_cascade.w2mg"
            materials = {
                slot.material
                for obj in splash_objects
                for slot in obj.material_slots
                if slot.material is not None
                and str(slot.material.get("witcher3_mat_base", "")).replace("/", "\\").casefold()
                == material_base.casefold()
            }
            assert materials
            for material in materials:
                groups = [
                    node for node in material.node_tree.nodes
                    if node.type == "GROUP" and node.node_tree
                    and node.node_tree.name == "m_fountain_cascade"
                ]
                assert len(groups) == 1
                graph = groups[0]
                assert graph.node_tree["witcher_material_graph_version"] == 6
                cascade_visibility = graph.node_tree.nodes["Cascade Visibility"]
                refraction_fresnel = graph.node_tree.nodes["Refraction Fresnel"]
                refraction_fresnel_weight = graph.node_tree.nodes["Refraction Fresnel Weight"]
                refraction_tint = graph.node_tree.nodes["Refraction Tint"]
                combined_color = graph.node_tree.nodes["Water + Refraction Tint"]
                assert cascade_visibility.inputs[2].links[0].from_node == graph.node_tree.nodes["Additive Water"]
                assert refraction_fresnel.inputs["Normal"].links[0].from_node == graph.node_tree.nodes["Waterfall Normal"]
                assert {socket.links[0].from_node for socket in refraction_fresnel_weight.inputs[:2]} == {
                    refraction_fresnel, graph.node_tree.nodes["Refraction Weight"],
                }
                assert refraction_tint.inputs["Scale"].links[0].from_node == refraction_fresnel_weight
                assert {socket.links[0].from_node for socket in combined_color.inputs[:2]} == {
                    graph.node_tree.nodes["Water Tint"], refraction_tint,
                }
                assert graph.node_tree.nodes["Additive Water"].inputs["Color"].links[0].from_node == combined_color
                splash = material.node_tree.nodes.get("normal_and_splash")
                cubemap = material.node_tree.nodes.get("cubemap")
                assert splash is not None and splash.type == "TEX_IMAGE" and splash.image
                assert cubemap is not None and cubemap.type == "TEX_ENVIRONMENT" and cubemap.image
                for image in (splash.image, cubemap.image):
                    assert image.packed_file or win_path_isfile(bpy.path.abspath(image.filepath)), image.filepath
                assert material.surface_render_method == "BLENDED"
                assert not material.use_transparency_overlap
                assert not material.use_transparent_shadow
                assert splash.inputs["Vector"].links[0].from_node.name == "W3 Fountain Animated UV"
                direction = material.node_tree.nodes["W3 Fountain Direction"]
                assert tuple(direction.inputs[1].default_value) == (1.0, -1.0, 1.0)
                assert material.node_tree.nodes["W3 Fountain Flow"].inputs[0].links[0].from_node == direction
                assert graph.inputs["fountain_uv"].links[0].from_node.name == "W3 Fountain UV"
                assert cubemap.inputs["Vector"].links[0].from_node.name == "W3 Fountain Reflection"
                speed = graph.inputs["texture_speed"].links[0].from_node
                assert abs(speed.inputs["Y"].default_value + 0.2) < 1e-6
                assert direction.inputs[0].links[0].from_node == speed

                time_output = material.node_tree.nodes["W3 Fountain Time"].outputs[0]
                time_path = time_output.path_from_id("default_value")
                driver = next(
                    curve.driver for curve in material.node_tree.animation_data.drivers
                    if curve.data_path == time_path
                )
                assert set(driver.variables.keys()) == {"fps", "fps_base"}
                bpy.context.scene.render.fps = 30
                bpy.context.scene.render.fps_base = 1.0
                bpy.context.scene.frame_set(1)
                start_time = time_output.default_value
                bpy.context.scene.frame_set(31)
                assert abs((time_output.default_value - start_time) - 1.0) < 1e-6

            pool_base = r"engine\materials\graphs\transparent_reflective.w2mg"
            pool_materials = {
                slot.material
                for obj in bpy.data.objects
                if obj.type == "MESH"
                for slot in obj.material_slots
                if slot.material is not None
                and str(slot.material.get("witcher3_mat_base", "")).replace("/", "\\").casefold()
                == pool_base.casefold()
            }
            assert len(pool_materials) == 1
            pool = pool_materials.pop()
            pool_groups = [
                node for node in pool.node_tree.nodes
                if node.type == "GROUP" and node.node_tree
                and node.node_tree.name == "transparent_reflective"
            ]
            assert len(pool_groups) == 1
            pool_graph = pool_groups[0]
            assert pool_graph.node_tree["witcher_material_graph_version"] == 3
            assert pool.surface_render_method == "BLENDED"
            assert not pool.use_transparency_overlap
            assert not pool.use_transparent_shadow

            normal = pool_graph.inputs["Normal"].links[0].from_node
            normal_big = pool_graph.inputs["NormalBig"].links[0].from_node
            assert normal.type == "TEX_IMAGE" and normal.image
            assert normal_big == pool.node_tree.nodes["W3 Water Normal Big"]
            assert normal_big.image == normal.image
            assert normal.inputs["Vector"].links[0].from_node.name == "W3 Water Small UV"
            assert normal_big.inputs["Vector"].links[0].from_node.name == "W3 Water Big UV"
            assert pool.node_tree.nodes["W3 Water Small UV"].operation == "ADD"
            assert pool.node_tree.nodes["W3 Water Big UV"].operation == "SUBTRACT"

            def source_node(socket_name):
                return pool_graph.inputs[socket_name].links[0].from_node

            small_tile = source_node("SmallWavesTile")
            big_tile = source_node("BigWavesTile")
            wind = source_node("WindSpeed")
            assert abs(small_tile.inputs["X"].default_value - 18.0) < 1e-6
            assert abs(small_tile.inputs["Y"].default_value - 18.0) < 1e-6
            assert abs(big_tile.inputs["X"].default_value - 12.0) < 1e-6
            assert abs(big_tile.inputs["Y"].default_value - 12.0) < 1e-6
            assert abs(wind.inputs["X"].default_value - 0.025) < 1e-6
            assert abs(wind.inputs["Y"].default_value - 0.015) < 1e-6
            assert abs(source_node("NormalIntensity").outputs[0].default_value - 0.15) < 1e-6
            assert abs(source_node("Transparency").outputs[0].default_value - 0.8) < 1e-6
            assert pool.node_tree.nodes["W3 Water Small Scale"].inputs[1].links[0].from_node == small_tile
            assert pool.node_tree.nodes["W3 Water Big Scale"].inputs[1].links[0].from_node == big_tile
            assert pool.node_tree.nodes["W3 Water Flow"].inputs[0].links[0].from_node == wind

            water_time = pool.node_tree.nodes["W3 Water Time"].outputs[0]
            water_time_path = water_time.path_from_id("default_value")
            water_driver = next(
                curve.driver for curve in pool.node_tree.animation_data.drivers
                if curve.data_path == water_time_path
            )
            assert set(water_driver.variables.keys()) == {"fps", "fps_base"}
            bpy.context.scene.frame_set(1)
            water_start = water_time.default_value
            bpy.context.scene.frame_set(31)
            assert abs((water_time.default_value - water_start) - 1.0) < 1e-6

            owners = [obj for obj in bpy.data.objects if obj.get("witcher_entity_root") is True]
            assert len(owners) == 1
            owner = owners[0]
            water_anchors = [
                child for child in owner.children
                if child.get("witcher_type") == "CFXFountainPreview"
                and child.get("witcher_water_preview_kind") in {"impact", "tip"}
            ]
            assert len(water_anchors) == 2
            anchors_by_kind = {anchor["witcher_water_preview_kind"]: anchor for anchor in water_anchors}
            impact = anchors_by_kind["impact"]
            tip = anchors_by_kind["tip"]
            assert abs(impact.location.z - 0.45) < 1e-6
            assert abs(tip.location.z - 1.767488956) < 1e-6
            assert json.loads(impact["witcher_particle_emitters"]) == ["splash", "rings"]
            assert json.loads(tip["witcher_particle_emitters"]) == ["splash_tip"]
            assert impact["witcher_effect_slot"] == "down_splash"
            assert tip["witcher_effect_slot"] == "fountain_tip"
            splash_particles = [child for child in impact.children if child.get("witcher_particle_emitter") == "splash"]
            ring_particles = [child for child in impact.children if child.get("witcher_particle_emitter") == "rings"]
            tip_particles = [child for child in tip.children if child.get("witcher_particle_emitter") == "splash_tip"]
            assert (len(splash_particles), len(ring_particles), len(tip_particles)) == (10, 16, 15)

            ring = ring_particles[0]
            assert ring["witcher_particle_preview_version"] == import_particle.PARTICLE_PREVIEW_VERSION
            assert ring["witcher_particle_generation_count"] == 4
            ring_material = ring.data.materials[0]
            assert ring_material["witcher_particle_texture_loaded"] is True
            ring_texture = ring_material.node_tree.nodes["W3 Particle Texture"]
            assert ring_texture.image is not None
            assert ring_texture.image["witcher_source_texture"] == r"fx\textures\water\water_circle_2x1_normal.xbm"
            assert ring_texture.outputs["Alpha"].is_linked
            assert len(ring.data.polygons) == 1
            assert all(abs(vertex.co.z) < 1e-7 for vertex in ring.data.vertices)
            assert max(
                abs((particle.location.x ** 2 + particle.location.y ** 2) ** 0.5 - 0.6)
                for particle in ring_particles
            ) < 1e-6
            bpy.context.scene.frame_set(25)
            ring_positions_a = [(particle.location.x, particle.location.y) for particle in ring_particles]
            bpy.context.scene.frame_set(73)
            ring_positions_b = [(particle.location.x, particle.location.y) for particle in ring_particles]
            assert any(
                abs(ax - bx) + abs(ay - by) > 1e-4
                for (ax, ay), (bx, by) in zip(ring_positions_a, ring_positions_b)
            )
            assert abs(ring_material["witcher_particle_preview_gain"] - 6.0) < 1e-6

            splash_material = splash_particles[0].data.materials[0]
            splash_texture = splash_material.node_tree.nodes["W3 Particle Texture"]
            assert splash_material["witcher_particle_texture_loaded"] is True
            assert splash_texture.image["witcher_source_texture"] == r"fx\textures\water\splash_with_normal.xbm"
            assert splash_material.node_tree.nodes.get("W3 Particle Additive Approximation") is not None
            assert abs(splash_material["witcher_particle_preview_gain"] - 6.0) < 1e-6
            assert all(particle.data.materials[0].use_transparency_overlap for particle in tip_particles)
            assert all(len(particle.data.polygons) == 1 for particle in tip_particles)
            assert all(particle.animation_data and particle.animation_data.action for particle in splash_particles + ring_particles + tip_particles)
        finally:
            clear_repo_override_roots()
    finally:
        witcher3_tools.unregister()

    print("FOUNTAIN_MATERIAL_BLENDER_SMOKE_OK")


if __name__ == "__main__":
    main()
