"""Run with Blender 4.5+:
  blender --background --factory-startup --python tests/blender_geralt_preset_native.py
"""

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

PRESET_ID = "shipped_witcher_wolf_enhanced"
PRESET_CATEGORIES = {"pants", "armor", "gloves", "boots", "steelsword", "silversword"}
SWORD_CATEGORIES = {"steelsword", "silversword"}

import witcher3_tools as addon  # noqa: E402

addon.register()
try:
    from witcher3_tools.extension_paths import get_dev_override

    prefs = addon._get_prefs(bpy.context)
    rel_player = os.path.join("gameplay", "templates", "characters", "player", "player.w2ent")
    uncook_path = str(getattr(prefs, "uncook_path", "") or "")
    if not (Path(uncook_path) / rel_player).is_file():
        uncook_path = str(get_dev_override("fallback_uncook_path_w3", "") or "")
        prefs.uncook_path = uncook_path
    assert (Path(uncook_path) / rel_player).is_file(), "Configure uncook_path before running this test"
    game_path = str(getattr(prefs, "witcher_game_path", "") or "")
    bundles = list(Path(game_path).glob("content/content*/bundles/*.bundle")) if game_path else []
    from witcher3_tools.dev import dev_config
    assert bundles, (f"game root {game_path!r} has no .bundle files; fix witcher_game_path in "
                     f"{dev_config.get_config_path()} or the addon preferences")
    print(f"GAME ROOT: {game_path} ({len(bundles)} bundles)")

    # Sandbox the catalog + bundle XML caches so both rebuild from game data.
    from witcher3_tools.ui import equipment_catalog as ec

    sandbox = Path(tempfile.mkdtemp(prefix="w3tb_catalog_sandbox_"))
    ec.get_cache_root = lambda create=True: str(sandbox)
    ec._CATEGORY_CACHE_FILE = sandbox / "equipment_categories.json"
    ec.reset_w3_category_cache_runtime()

    result = bpy.ops.witcher.import_geralt(inventory_preset_id=PRESET_ID)
    assert result == {'FINISHED'}, result

    # Bundle-only DLC items
    _cats, attrs = ec.get_equipment_catalog("w3")
    assert len(attrs) > 1400, f"catalog too small ({len(attrs)} items): bundle XML extraction likely failed"
    sword_attrs = attrs.get("Wolf School steel sword 1")
    assert sword_attrs, "DLC10 wolf sword missing from catalog (bundle XMLs not extracted)"
    assert sword_attrs.get("bound_items"), "wolf sword catalog entry lost its bound_items (scabbard)"
    xml_root = Path(ec.get_equipment_xml_bundle_cache_root())
    assert any(xml_root.rglob("*.xml")), "bundle XML cache is empty after catalog build"

    main = None
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE" and getattr(obj.data, "witcherui_RigSettings", None) is not None:
            rp = (getattr(obj.data.witcherui_RigSettings, "repo_path", "") or "").replace("/", "\\").lower()
            if rp.endswith("player\\player.w2ent"):
                main = obj
                break
    assert main is not None, "Geralt armature not found"
    rig_settings = main.data.witcherui_RigSettings

    by_guid = {}
    bound_by_guid = {}
    for obj in bpy.data.objects:
        guid = obj.get("witcher_equip_guid")
        if guid:
            by_guid.setdefault(guid, []).append(obj)
        parent_guid = obj.get("witcher_bound_parent_guid")
        if parent_guid:
            bound_by_guid.setdefault(parent_guid, []).append(obj)

    # Visible geometry for every preset category
    slots_by_category = {}
    for slot in rig_settings.equipment_slots:
        slots_by_category[slot.category] = slot
    for category in sorted(PRESET_CATEGORIES):
        slot = slots_by_category.get(category)
        assert slot is not None, f"preset category '{category}' has no slot"
        assert slot.is_loaded and slot.equip_guid, f"preset category '{category}' did not load"
        meshes = [o for o in by_guid.get(slot.equip_guid, []) if o.type == "MESH"]
        assert meshes, f"preset category '{category}' loaded no meshes"
        assert slot.item_name in attrs, (
            f"'{category}' item '{slot.item_name}' did not resolve via the catalog"
        )

    # Mounted sword scabbards
    deps = bpy.context.evaluated_depsgraph_get()
    for category in sorted(SWORD_CATEGORIES):
        slot = slots_by_category[category]
        bound_names = json.loads(slot.bound_items_json or "[]")
        assert bound_names, f"'{category}' slot has no bound items (scabbard lost)"
        bound_objs = bound_by_guid.get(slot.equip_guid, [])
        assert bound_objs, f"'{category}' bound scabbard objects were not imported"
        scab_meshes = [o for o in bound_objs if o.type == "MESH"]
        assert scab_meshes, f"'{category}' scabbard has no mesh"
        sword_meshes = [
            o for o in by_guid.get(slot.equip_guid, [])
            if o.type == "MESH" and o not in bound_objs
        ]
        assert any(o.parent is not None for o in sword_meshes), f"'{category}' sword is unmounted"
        for mesh_obj in scab_meshes:
            ev = mesh_obj.evaluated_get(deps)
            me = ev.to_mesh()
            assert me.vertices, f"'{category}' scabbard mesh is empty"
            zs = [(ev.matrix_world @ v.co).z for v in me.vertices]
            center_z = sum(zs) / len(zs)
            ev.to_mesh_clear()
            assert 0.5 < center_z < 2.0, (
                f"'{category}' scabbard hangs at z={center_z:.2f} — not mounted on the body"
            )

    print("W3TB_GERALT_PRESET_NATIVE_OK")
except Exception:
    traceback.print_exc()
    print("W3TB_GERALT_PRESET_NATIVE_FAIL")
    sys.exit(1)
finally:
    addon.unregister()
