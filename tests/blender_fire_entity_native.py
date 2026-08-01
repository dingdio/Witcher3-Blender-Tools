"""Run with:
    blender --background --factory-startup --python tests/blender_fire_entity_native.py -- \
    gen_brazier_b.w2ent candle_med_b.w2ent REDKIT_UNCOOKED NORMAL_UNCOOK
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import bpy  # noqa: E402
from mathutils import Vector  # noqa: E402
import witcher3_tools  # noqa: E402


def _arguments() -> tuple[Path, Path, Path, Path, Path | None]:
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if len(args) not in {4, 5}:
        raise SystemExit("Pass brazier, candle, REDkit uncooked, normal uncook, and optional render dir")
    brazier, candle = (Path(value).resolve() for value in args[:2])
    redkit_uncooked, normal_uncook = (Path(value).resolve() for value in args[2:4])
    if not brazier.is_file() or not candle.is_file():
        raise SystemExit(f"Missing entity input: {brazier}, {candle}")
    if (brazier.name.casefold(), candle.name.casefold()) != ("gen_brazier_b.w2ent", "candle_med_b.w2ent"):
        raise SystemExit("Expected gen_brazier_b.w2ent and candle_med_b.w2ent fixtures")
    if not redkit_uncooked.is_dir() or not normal_uncook.is_dir():
        raise SystemExit(f"Missing uncook root: {redkit_uncooked}, {normal_uncook}")
    render_dir = Path(args[4]).resolve() if len(args) == 5 else None
    return brazier, candle, redkit_uncooked, normal_uncook, render_dir


def _r4data_root(path: Path) -> Path:
    return next(parent for parent in path.parents if parent.name.casefold() == "r4data")


def _repo_relative(path: str, roots: tuple[Path, ...]) -> str:
    resolved = Path(path).resolve()
    for root in roots:
        try:
            return str(resolved.relative_to(root)).replace("/", "\\")
        except ValueError:
            pass
    return ""


def _particles(source_suffix: str):
    suffix = source_suffix.replace("/", "\\").casefold()
    return [
        obj for obj in bpy.data.objects
        if obj.get("witcher_particle_preview")
        and str(obj.get("witcher_particle_system", "")).replace("/", "\\").casefold().endswith(suffix)
    ]


def _render_preview(owner, path: Path, *, camera_location, target, lens):
    scene = bpy.context.scene
    allowed = {owner, *owner.children_recursive}
    camera_data = bpy.data.cameras.new(f"{path.stem} Camera")
    camera = bpy.data.objects.new(f"{path.stem} Camera", camera_data)
    scene.collection.objects.link(camera)
    camera.location = camera_location
    camera.rotation_euler = (Vector(target) - camera.location).to_track_quat("-Z", "Y").to_euler()
    camera.data.lens = lens
    scene.camera = camera
    allowed.add(camera)

    fill_data = bpy.data.lights.new(f"{path.stem} Fill", type="AREA")
    fill_data.energy = 350.0
    fill_data.shape = "DISK"
    fill_data.size = 2.0
    fill = bpy.data.objects.new(f"{path.stem} Fill", fill_data)
    scene.collection.objects.link(fill)
    fill.location = (camera_location[0] * 0.5, camera_location[1] * 0.5, camera_location[2] + 1.0)
    fill.rotation_euler = (Vector(target) - fill.location).to_track_quat("-Z", "Y").to_euler()
    allowed.add(fill)

    for obj in scene.objects:
        obj.hide_render = obj not in allowed
    try:
        scene.render.engine = "BLENDER_EEVEE_NEXT"
    except TypeError:
        scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = 512
    scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(path)
    scene.render.film_transparent = False
    scene.world.color = (0.025, 0.025, 0.025)
    scene.frame_set(24)
    bpy.ops.render.render(write_still=True)


def main() -> None:
    brazier_path, candle_path, redkit_uncooked, normal_uncook, render_dir = _arguments()
    depot_root = _r4data_root(brazier_path)
    roots = (normal_uncook, depot_root, redkit_uncooked)
    witcher3_tools.register()
    try:
        from witcher3_tools.CR2W.common_blender import (
            clear_repo_override_roots,
            redkit_repo_context,
            set_repo_override_roots,
        )
        from witcher3_tools.importers import import_entity, import_mesh
        from witcher3_tools.materials import material as material_module
        from witcher3_tools.materials.nodes import domain as material_domain

        texture_root = str(redkit_uncooked)
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
            before_brazier = set(bpy.data.objects)
            with redkit_repo_context(str(brazier_path), roots=[str(depot_root), str(redkit_uncooked)]):
                import_entity.import_direct_entity_file(str(brazier_path))
            brazier_objects = set(bpy.data.objects) - before_brazier
            brazier_static_meshes = [
                obj for obj in brazier_objects
                if obj.type == "MESH" and not obj.get("witcher_particle_preview")
            ]
            assert len(brazier_static_meshes) >= 2
            import_entity.import_direct_entity_file(str(candle_path))

            brazier_particles = _particles(r"brazier_fire.w2p")
            candle_particles = _particles(r"candle_flame_fx2.w2p")
            assert Counter(obj["witcher_particle_emitter"] for obj in brazier_particles) == Counter({
                "smoke_vertex": 5,
                "fire_anim": 3,
                "embers": 3,
            })
            assert Counter(obj["witcher_particle_emitter"] for obj in candle_particles) == Counter({
                "flame": 2,
                "flame burst": 2,
            })
            assert all(obj.scale.x / obj.scale.y > 6.0 for obj in candle_particles)
            assert not _particles(r"fire_interactive_fx2.w2p")
            assert not _particles(r"candle_smoke_fx4.w2p")
            assert not _particles(r"candle_sparks_fx1.w2p")

            anchors = [obj for obj in bpy.data.objects if obj.get("witcher_type") == "CFXPreview"]
            by_source = {str(obj.get("witcher_particle_system", "")): obj for obj in anchors}
            brazier_anchor = next(obj for path, obj in by_source.items() if path.endswith("brazier_fire.w2p"))
            candle_anchor = next(obj for path, obj in by_source.items() if path.endswith("candle_flame_fx2.w2p"))
            assert brazier_anchor["witcher_effect_slot"] == "fire_fx1"
            assert candle_anchor["witcher_effect_slot"] == "fire"
            # Rigged anchors use imported slots; static anchors keep the authored transform.
            for anchor, slot_name, expected_z in (
                (brazier_anchor, "fire_fx1", 0.29618895),
                (candle_anchor, "fire", 0.11),
            ):
                if anchor.parent.get("witcher_slot_name") == slot_name:
                    assert tuple(anchor.location) == (0.0, 0.0, 0.0)
                else:
                    assert abs(anchor.location.z - expected_z) < 1e-5
            assert abs(brazier_anchor.matrix_world.translation.z - 0.29618895) < 1e-5
            assert abs(candle_anchor.matrix_world.translation.z - 0.11) < 1e-5

            materials = {
                slot.material
                for obj in brazier_particles + candle_particles
                for slot in obj.material_slots
                if slot.material is not None
            }
            assert materials
            assert all(material.get("witcher_particle_texture_loaded") for material in materials)
            assert any(material.get("witcher_particle_material_style") == "alpha" for material in materials)
            assert any(material.get("witcher_particle_material_style") == "additive" for material in materials)
            assert not any(image.get("witcher_fire_preview_image") for image in bpy.data.images)
            if render_dir is not None:
                render_dir.mkdir(parents=True, exist_ok=True)
                _render_preview(
                    brazier_anchor.parent,
                    render_dir / "brazier_fire_preview.png",
                    camera_location=(2.8, -3.2, 2.2),
                    target=(0.0, 0.0, 1.0),
                    lens=58.0,
                )
                _render_preview(
                    candle_anchor.parent,
                    render_dir / "candle_fire_preview.png",
                    camera_location=(0.42, -0.55, 0.32),
                    target=(0.0, 0.0, 0.16),
                    lens=62.0,
                )
            print("FIRE_ENTITY_REAL_BLENDER_OK")
        finally:
            clear_repo_override_roots()
    finally:
        witcher3_tools.unregister()


if __name__ == "__main__":
    main()
