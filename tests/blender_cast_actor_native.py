import sys
import traceback
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import witcher3_tools as addon

addon.register()
try:
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
    for query, expected_template in (
        ("ciri", "quests\\main_npcs\\cirilla.w2ent"),
        ("drowner", "characters\\npc_entities\\monsters\\drowner_lvl1.w2ent"),
    ):
        try:
            actor, info = cast_actor(query, at=(1.0, 2.0, 0.0))
        except Exception as exc:
            traceback.print_exc()
            failures.append(f"{query}: {exc}")
            continue
        assert actor is not None
        assert str(actor.get("cutscene_actor_name", "")), "actor label missing"
        assert str(actor.get("cutscene_actor_template", "")) == expected_template, actor.get("cutscene_actor_template")
        assert str(actor.get("cutscene_actor_type", "")) == "CAT_Actor"
        armatures = [o for o in bpy.data.objects if o.type == 'ARMATURE' and o.get("cutscene_actor_name") == info["label"]]
        print(f"CAST {query}: label={info['label']} template={info['template']} "
              f"appearance={info['appearance']} object={actor.name} armature_hit={bool(armatures)} "
              f"errors={info['errors'][:2]}")

    if failures:
        print("W3TB_CAST_ACTOR_NATIVE_FAIL", failures)
        sys.exit(1)
    print("W3TB_CAST_ACTOR_NATIVE_OK")
finally:
    addon.unregister()
