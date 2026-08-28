"""Import Geralt + Ciri from ONE game install with every mesh and texture forced through
that install's bundles + texture.cache (fresh sandbox for uncook, caches and dev config).

Run once per install with Blender 4.5+:
  blender --background --factory-startup --python tests/blender_character_textures_native.py -- \
      --game-root "C:\\GOG Games\\The Witcher 3 Wild Hunt" [--manifest out.json] [--keep]

Exit 1 (and the sandbox is kept) on any MISSING/unloadable/foreign texture or a cancelled import.
"""

import argparse
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import traceback
from pathlib import Path

import bpy

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

CHARACTERS = (("Geralt", "import_geralt"), ("Ciri", "import_ciri"))
MIN_TEXTURES_PER_CHARACTER = 10
TEXTURE_NODE_TYPES = {"ShaderNodeTexImage", "ShaderNodeTexEnvironment"}
TEXTURE_CACHE_MAGIC = 1415070536


def _args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-root", required=True)
    parser.add_argument("--manifest", default="", help="JSON report path (default: %TEMP%/w3tb_character_textures_<root>.json)")
    parser.add_argument("--keep", action="store_true", help="keep the sandbox on success too")
    return parser.parse_args(argv)


def _texture_cache_versions(game_root):
    out = {}
    for cache in sorted(game_root.rglob("texture.cache")):
        if cache.stat().st_size < 32:
            out[cache.relative_to(game_root).as_posix()] = {"version": None, "entries": 0, "magic_ok": False}
            continue
        with cache.open("rb") as handle:
            handle.seek(-32, os.SEEK_END)
            _crc, _pages, entries, _strings, _mips, magic, version = struct.unpack("QIIIIII", handle.read(32))
        out[cache.relative_to(game_root).as_posix()] = {
            "version": version, "entries": entries, "magic_ok": magic == TEXTURE_CACHE_MAGIC,
        }
    return out


def _under(path, root):
    try:
        path_key = os.path.normcase(os.path.abspath(str(path)))
        root_key = os.path.normcase(os.path.abspath(str(root)))
        return os.path.commonpath([path_key, root_key]) == root_key
    except ValueError:
        return False


def _iter_texture_nodes(tree, seen_trees):
    for node in tree.nodes:
        if node.bl_idname in TEXTURE_NODE_TYPES:
            yield node
        elif node.bl_idname == "ShaderNodeGroup" and node.node_tree:
            key = node.node_tree.as_pointer()
            if key not in seen_trees:
                seen_trees.add(key)
                yield from _iter_texture_nodes(node.node_tree, seen_trees)


def _texture_rows(label, meshes, sandbox, uncook, manager):
    rows = []
    seen_trees = set()
    for obj in meshes:
        for slot in obj.material_slots:
            mat = slot.material
            if not mat or not mat.node_tree or mat.node_tree.as_pointer() in seen_trees:
                continue
            seen_trees.add(mat.node_tree.as_pointer())
            for node in _iter_texture_nodes(mat.node_tree, seen_trees):
                row = {
                    "character": label, "material": mat.name, "node": node.label or node.name,
                    "source": str(node.get("witcher_texture_source_path", "") or ""),
                    "file": "", "size": [0, 0], "status": "ok",
                }
                img = node.image
                if node.label.startswith("MISSING:"):
                    row["status"] = "missing"
                elif img is None:
                    continue
                elif img.packed_file or img.library or img.source != "FILE" or img.filepath.startswith("//"):
                    continue  # asset shipped inside witcher3_materials.blend
                else:
                    filepath = bpy.path.abspath(img.filepath)
                    row["file"] = filepath
                    if _under(filepath, REPO_ROOT):
                        continue
                    if not _under(filepath, sandbox):
                        row["status"] = "foreign"  # resolved outside this install's extraction sandbox
                    elif not os.path.isfile(filepath):
                        row["status"] = "no_file"
                    else:
                        row["size"] = [int(img.size[0]), int(img.size[1])]
                        row["cache_edge"] = _cache_declared_edge(manager, img, uncook)
                        if row["size"][0] <= 0 or row["size"][1] <= 0:
                            row["status"] = "unloadable"
                        elif max(row["size"]) < row["cache_edge"]:
                            row["status"] = "lowres"  # got the .xbm resident mip instead of the texture.cache data
                rows.append(row)
    return rows


def _texture_manager():
    from witcher3_tools.CR2W.witcher_cache.TextureCache import LoadTextureManager
    return LoadTextureManager()


def _cache_declared_edge(manager, image, uncook):
    """Largest edge texture.cache holds for this image's .xbm, or 0 when the cache has no entry (resident-only)."""
    source = str(image.get("witcher_original_texture_path", "") or "") or bpy.path.abspath(image.filepath)
    if not _under(source, uncook):
        return 0
    rel = os.path.splitext(os.path.relpath(source, uncook))[0].replace("/", "\\") + ".xbm"
    items = manager.find_item_by_path_name(rel) or manager.find_item_by_path_name(rel.lower()) or []
    return max((max(int(item.BaseWidth), int(item.BaseHeight)) for item in items), default=0)


def _unloaded_texture_caches(game_root, caches, manager):
    from witcher3_tools.CR2W.witcher_cache.common_cache.WitcherArchiveManager import WitcherArchiveManager

    vanilla = {name.lower() for name in WitcherArchiveManager.VANILLA_DLC_LIST}
    expected = set()
    for rel, info in caches.items():
        parts = rel.split("/")
        if info["entries"] and (parts[0] == "content" or (parts[0] == "dlc" and parts[1].lower() in vanilla)):
            expected.add(os.path.normcase(os.path.normpath(os.path.join(str(game_root), rel))))
    loaded = {
        os.path.normcase(os.path.normpath(item.ParentFile))
        for items in manager.HashDict.values() for item in items
    }
    return sorted(expected - loaded)


def main():
    args = _args()
    game_root = Path(args.game_root).resolve()
    bundles = sorted(game_root.glob("content/content*/bundles/*.bundle")) + sorted(
        game_root.glob("dlc/*/content/bundles/*.bundle"))
    if not bundles:
        raise SystemExit(f"{game_root} has no .bundle files")

    sandbox = Path(tempfile.mkdtemp(prefix="w3tb_char_tex_"))
    user_dir = sandbox / "user"
    uncook = sandbox / "uncook"
    uncook.mkdir()
    # Route the extension user dir (caches, converted textures, dev config) into the sandbox
    # before the addon imports, so nothing from a previous install or run can leak in.
    bpy.utils.extension_path_user = lambda package, *, path="", create=False: str(user_dir)

    import witcher3_tools as addon  # noqa: E402
    from witcher3_tools.dev import dev_config  # noqa: E402

    manifest_path = Path(args.manifest) if args.manifest else Path(tempfile.gettempdir()) / (
        "w3tb_character_textures_" + game_root.name.replace(" ", "_") + ".json")
    manifest = {
        "game_root": str(game_root), "bundles": len(bundles),
        "texture_caches": _texture_cache_versions(game_root),
        "sandbox": str(sandbox), "characters": {}, "textures": [],
    }
    failed = True
    addon.register()
    try:
        prefs = addon._get_prefs(bpy.context)
        prefs.witcher_game_path = str(game_root)
        prefs.uncook_path = str(uncook)
        prefs.use_separate_texture_uncook_path = False
        assert _under(dev_config.get_config_path(), sandbox), f"dev config not sandboxed: {dev_config.get_config_path()}"
        print(f"GAME ROOT: {game_root} ({len(bundles)} bundles, texture caches: "
              f"{ {k: v['version'] for k, v in manifest['texture_caches'].items()} })")
        print(f"SANDBOX: {sandbox}")

        problems = 0
        for label, op_name in CHARACTERS:
            before = {obj.name for obj in bpy.data.objects}
            started = time.perf_counter()
            result = getattr(bpy.ops.witcher, op_name)()
            seconds = time.perf_counter() - started
            new_objects = [obj for obj in bpy.data.objects if obj.name not in before]
            meshes = [obj for obj in new_objects if obj.type == "MESH"]
            manager = _texture_manager()
            rows = _texture_rows(label, meshes, sandbox, uncook, manager)
            bad = [row for row in rows if row["status"] != "ok"]
            ok_files = {row["file"].lower() for row in rows if row["status"] == "ok"}
            summary = {
                "result": sorted(result), "seconds": round(seconds, 1), "objects": len(new_objects),
                "meshes": len(meshes), "textures_ok": len(ok_files), "problems": len(bad),
            }
            manifest["characters"][label] = summary
            manifest["textures"].extend(rows)
            print(f"{label}: {summary}")
            for row in bad:
                print(f"  {row['status'].upper()}: {row['material']} / {row['node']} -> {row['file'] or row['source']}")
            if result != {"FINISHED"} or not meshes or bad or len(ok_files) < MIN_TEXTURES_PER_CHARACTER:
                problems += 1

        from witcher3_tools.CR2W.witcher_cache.common_cache.WitcherArchiveManager import refresh_game_configuration_path
        manager = _texture_manager()
        for label, root in (("archive root", refresh_game_configuration_path()), ("texture manager root", manager.base_path)):
            manifest[label.replace(" ", "_")] = str(root or "")
            if os.path.normcase(os.path.normpath(str(root or ""))) != os.path.normcase(os.path.normpath(str(game_root))):
                print(f"  WRONG {label.upper()}: {root} (expected {game_root})")
                problems += 1
        unloaded = _unloaded_texture_caches(game_root, manifest["texture_caches"], manager)
        manifest["texture_caches_not_loaded"] = unloaded
        for path in unloaded:
            print(f"  TEXTURE CACHE NOT LOADED: {path}")
        problems += bool(unloaded)

        manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        print(f"MANIFEST: {manifest_path}")
        failed = problems > 0
    except Exception:
        traceback.print_exc()
    finally:
        addon.unregister()
        if failed:
            print(f"SANDBOX KEPT: {sandbox}")
            print("W3TB_CHARACTER_TEXTURES_NATIVE_FAIL")
        else:
            if not args.keep:
                shutil.rmtree(sandbox, ignore_errors=True)
            print("W3TB_CHARACTER_TEXTURES_NATIVE_OK")
    sys.exit(1 if failed else 0)


main()
