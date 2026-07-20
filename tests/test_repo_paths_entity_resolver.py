import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _install_namespace_stub(qualified_name: str, package_path: Path) -> None:
    if qualified_name in sys.modules:
        return
    module = types.ModuleType(qualified_name)
    module.__path__ = [str(package_path)]
    module.__package__ = qualified_name
    sys.modules[qualified_name] = module


_install_namespace_stub("witcher3_tools", REPO_ROOT / "witcher3_tools")
_install_namespace_stub("witcher3_tools.CR2W", REPO_ROOT / "witcher3_tools" / "CR2W")

from witcher3_tools.repo_paths import (  # noqa: E402
    EQUIPMENT_ENTITY_EXTENSIONS,
    clear_entity_path_resolver_caches,
    materialize_entity_repo_path,
    resolve_materialized_entity_path,
    resolve_entity_repo_path,
    source_root_candidates_from_file,
)


def _touch(root: str, repo_path: str) -> str:
    full_path = os.path.join(root, *repo_path.split("\\"))
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as handle:
        handle.write("")
    return full_path


class _BundleManager:
    base_path = "bundle-smoke"

    def __init__(self, paths):
        self.Items = {path: object() for path in paths}


class EntityPathResolverTests(unittest.TestCase):
    def setUp(self):
        clear_entity_path_resolver_caches()

    def tearDown(self):
        clear_entity_path_resolver_caches()

    def test_w2_short_id_resolves_from_nested_items_root(self):
        with tempfile.TemporaryDirectory() as root:
            _touch(root, r"items\geralt\geralt_hair\hair_leather_armour_01.w2ent")

            result = resolve_entity_repo_path(
                "hair_leather_armour_01",
                source_game="w2",
                search_roots=[root],
                extensions=EQUIPMENT_ENTITY_EXTENSIONS,
                allow_bundle_index=False,
            )

        self.assertIsNotNone(result)
        self.assertEqual(
            result.repo_path,
            r"items\geralt\geralt_hair\hair_leather_armour_01.w2ent",
        )
        self.assertEqual(result.source, "root_index")

    def test_w2_short_id_prefers_entity_over_mesh(self):
        manager = _BundleManager(
            [
                r"items\geralt\geralt_hair\hair_leather_armour_01.w2mesh",
                r"items\geralt\geralt_hair\hair_leather_armour_01.w2ent",
            ]
        )

        result = resolve_entity_repo_path(
            "hair_leather_armour_01",
            source_game="w2",
            bundle_manager=manager,
            extensions=EQUIPMENT_ENTITY_EXTENSIONS,
            include_non_items=True,
        )

        self.assertIsNotNone(result)
        self.assertEqual(
            result.repo_path,
            r"items\geralt\geralt_hair\hair_leather_armour_01.w2ent",
        )

    def test_full_repo_path_resolves_existing_file(self):
        with tempfile.TemporaryDirectory() as root:
            _touch(root, r"items\weapons\swords\steel_18_long.w2ent")

            result = resolve_entity_repo_path(
                r"items\weapons\swords\steel_18_long.w2ent",
                source_game="w2",
                search_roots=[root],
                allow_bundle_index=False,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.source, "existing_root")
        self.assertEqual(result.repo_path, r"items\weapons\swords\steel_18_long.w2ent")

    def test_non_items_bundle_paths_are_opt_in(self):
        manager = _BundleManager([r"characters\templates\camera\scene_camera.w2ent"])

        without_non_items = resolve_entity_repo_path(
            "scene_camera",
            source_game="w3",
            bundle_manager=manager,
            include_non_items=False,
        )
        with_non_items = resolve_entity_repo_path(
            "scene_camera",
            source_game="w3",
            bundle_manager=manager,
            include_non_items=True,
        )

        self.assertIsNone(without_non_items)
        self.assertIsNotNone(with_non_items)
        self.assertEqual(
            with_non_items.repo_path,
            r"characters\templates\camera\scene_camera.w2ent",
        )

    def test_materializer_uses_source_game_version(self):
        calls = []
        module_name = "witcher3_tools.CR2W.common_blender"
        previous_module = sys.modules.get(module_name)
        common_blender = types.ModuleType("witcher3_tools.CR2W.common_blender")

        def repo_file(filepath, version=999, is_abs_path=False):
            calls.append((filepath, version, is_abs_path))
            return f"resolved:{version}:{filepath}"

        common_blender.repo_file = repo_file
        sys.modules[module_name] = common_blender
        try:
            self.assertEqual(
                materialize_entity_repo_path(r"items\geralt\test.w2ent", source_game="w2"),
                r"resolved:115:items\geralt\test.w2ent",
            )
            self.assertEqual(
                materialize_entity_repo_path(r"items\geralt\test.w2ent", source_game="w3"),
                r"resolved:999:items\geralt\test.w2ent",
            )
            self.assertEqual(
                resolve_materialized_entity_path(
                    "hair_leather_armour_01",
                    source_game="w2",
                    bundle_manager=_BundleManager(
                        [r"items\geralt\geralt_hair\hair_leather_armour_01.w2ent"]
                    ),
                    include_non_items=True,
                ),
                r"resolved:115:items\geralt\geralt_hair\hair_leather_armour_01.w2ent",
            )
            self.assertEqual(
                calls,
                [
                    (r"items\geralt\test.w2ent", 115, False),
                    (r"items\geralt\test.w2ent", 999, False),
                    (
                        r"items\geralt\geralt_hair\hair_leather_armour_01.w2ent",
                        115,
                        False,
                    ),
                ],
            )
        finally:
            if previous_module is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous_module

    def test_source_root_candidates_from_file_uses_markers_and_optional_parents(self):
        with tempfile.TemporaryDirectory() as temp_root:
            repo_root = os.path.join(temp_root, "r4data")
            asset_dir = os.path.join(repo_root, "items", "geralt")
            os.makedirs(asset_dir, exist_ok=True)
            source_file = os.path.join(asset_dir, "player.w2ent")
            with open(source_file, "w", encoding="utf-8") as handle:
                handle.write("")

            marker_only = source_root_candidates_from_file(source_file)
            with_parents = source_root_candidates_from_file(source_file, include_parents=True)

        self.assertEqual(marker_only, [repo_root])
        self.assertEqual(with_parents[0], repo_root)
        self.assertIn(asset_dir, with_parents)
        self.assertEqual(len(with_parents), len({os.path.normcase(path) for path in with_parents}))

    def test_active_redkit_depots_win_over_workspace_override(self):
        from witcher3_tools.CR2W import common_blender

        repo_path = r"characters\models\main_npc\ciri\model\body_01_wa__ciri.w2mesh"
        with tempfile.TemporaryDirectory() as root:
            workspace = os.path.join(root, "workspace")
            depot = os.path.join(root, "r4data")
            uncooked = os.path.join(root, "redkit")
            workspace_copy = _touch(workspace, repo_path)
            uncooked_copy = _touch(uncooked, repo_path)
            common_blender.set_repo_override_roots([workspace], read_only=True)
            try:
                with (
                    mock.patch.object(common_blender, "_get_repo_roots_from_prefs", return_value=("", "", "", False)),
                    common_blender.redkit_repo_context(roots=[depot, uncooked]),
                ):
                    resolved = common_blender.repo_file(repo_path)
            finally:
                common_blender.clear_repo_override_roots()

        self.assertNotEqual(resolved, workspace_copy)
        self.assertEqual(resolved, uncooked_copy)


if __name__ == "__main__":
    unittest.main()
