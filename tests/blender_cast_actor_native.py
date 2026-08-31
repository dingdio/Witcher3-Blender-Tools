import sys
import traceback
from types import SimpleNamespace
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon

exit_code = 0
registered = False
try:
    addon.register()
    registered = True

    from witcher3_tools.extension_paths import get_dev_override

    prefs = addon._get_prefs(bpy.context)
    uncook_path = str(getattr(prefs, "uncook_path", "") or "")
    if not (Path(uncook_path) / "quests" / "main_npcs" / "cirilla.w2ent").is_file():
        uncook_path = str(get_dev_override("fallback_uncook_path_w3", "") or "")
    prefs.uncook_path = uncook_path
    ciri_path = Path(prefs.uncook_path) / "quests" / "main_npcs" / "cirilla.w2ent"
    assert ciri_path.is_file(), "Configure uncook_path before running this test"
    print(f"UNCOOK IN USE: {prefs.uncook_path} (headless bag: {type(prefs).__name__})")

    from witcher3_tools.repo_paths.casting import resolve_cast
    from witcher3_tools.w3_casting import cast_actor

    hits = resolve_cast("ciri")
    assert hits and hits[0]["path"] == "quests\\main_npcs\\cirilla.w2ent", hits[:1]
    hits = resolve_cast("drowner")
    assert hits and hits[0]["path"] == "characters\\npc_entities\\monsters\\drowner_lvl1.w2ent", hits[:1]

    failures = []
    for query, expected_template, requested_appearance, expected_appearance in (
        ("ciri", "quests\\main_npcs\\cirilla.w2ent", "ciri", "ciri"),
        ("drowner", "characters\\npc_entities\\monsters\\drowner_lvl1.w2ent", "", "drowner_01"),
    ):
        try:
            actor, info = cast_actor(query, appearance=requested_appearance, at=(1.0, 2.0, 0.0))
        except Exception as exc:
            traceback.print_exc()
            failures.append(f"{query}: {exc}")
            continue
        assert actor is not None and actor.type == 'ARMATURE'
        assert str(actor.get("cutscene_actor_name", "")), "actor label missing"
        assert str(actor.get("cutscene_actor_template", "")) == expected_template, actor.get("cutscene_actor_template")
        assert str(actor.get("cutscene_actor_appearance", "")) == expected_appearance
        assert str(actor.get("cutscene_actor_type", "")) == "CAT_Actor"
        armatures = [o for o in bpy.data.objects if o.type == 'ARMATURE' and o.get("cutscene_actor_name") == info["label"]]
        assert armatures and actor in armatures
        print(f"CAST {query}: label={info['label']} template={info['template']} "
              f"appearance={info['appearance']} object={actor.name} armature_hit={bool(armatures)} "
              f"errors={info['errors'][:2]}")

    from witcher3_tools.importers import import_entity

    original_import = import_entity.import_entity_file
    objects_before = {obj.as_pointer() for obj in bpy.data.objects}
    meshes_before = {mesh.as_pointer() for mesh in bpy.data.meshes}
    armatures_before = {armature.as_pointer() for armature in bpy.data.armatures}

    def import_without_armature(*_args, **_kwargs):
        obj = bpy.data.objects.new("cast_no_armature_probe", bpy.data.meshes.new("cast_no_armature_probe_data"))
        bpy.context.scene.collection.objects.link(obj)
        return SimpleNamespace(errors=[], main_object=obj, root_object=obj, created_objects=[obj])

    import_entity.import_entity_file = import_without_armature
    try:
        try:
            cast_actor("quests\\main_npcs\\cirilla.w2ent")
        except ValueError as exc:
            assert "without an armature" in str(exc), exc
        else:
            raise AssertionError("Expected a rigless cast to fail")
    finally:
        import_entity.import_entity_file = original_import
    assert {obj.as_pointer() for obj in bpy.data.objects} == objects_before
    assert {mesh.as_pointer() for mesh in bpy.data.meshes} == meshes_before
    assert {armature.as_pointer() for armature in bpy.data.armatures} == armatures_before
    assert bpy.data.objects.get("cast_no_armature_probe") is None
    assert bpy.data.meshes.get("cast_no_armature_probe_data") is None

    if failures:
        raise AssertionError(failures)
except Exception:
    traceback.print_exc()
    print("W3TB_CAST_ACTOR_NATIVE_FAIL")
    exit_code = 1
finally:
    if registered:
        try:
            addon.unregister()
        except Exception:
            traceback.print_exc()
            if not exit_code:
                print("W3TB_CAST_ACTOR_NATIVE_FAIL")
            exit_code = 1

if exit_code:
    sys.exit(exit_code)
print("W3TB_CAST_ACTOR_NATIVE_OK")
