import sys
import types
import unittest
from unittest import mock
from pathlib import Path


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

from witcher3_tools.CR2W import dc_entity  # noqa: E402


class _PathLikeIndex:
    def __init__(self, path, numeric_index):
        self.path = path
        self.Index = numeric_index

    def __str__(self):
        return self.path


class _MeshProp:
    theName = "mesh"
    Handles = []
    Value = None

    def __init__(self, index):
        self.Index = index

    def ToString(self):
        return "CStaticMeshComponent"


class _Chunk:
    Type = "CStaticMeshComponent"
    PROPS = []

    def __init__(self, prop, cr2w_file):
        self._prop = prop
        setattr(self, "_W_CLASS__CR2WFILE", cr2w_file)

    def GetVariableByName(self, name):
        return self._prop if name == "mesh" else None


class _EffectProp:
    def __init__(self, name, value=None, *, path=""):
        self.theName = name
        self.Value = value
        self.Index = types.SimpleNamespace(Path=path) if path else None

    def ToString(self):
        return str(self.Value) if self.Value is not None else ""


class _EffectChunk:
    def __init__(self, chunk_type, chunk_index, **props):
        self.Type = chunk_type
        self.ChunkIndex = chunk_index
        self.PROPS = []
        for name, value in props.items():
            if isinstance(value, tuple):
                value, path = value
                self.PROPS.append(_EffectProp(name, value, path=path))
            else:
                self.PROPS.append(_EffectProp(name, value))

    def GetVariableByName(self, name):
        return next((prop for prop in self.PROPS if prop.theName == name), None)


class RepoPathResolutionTests(unittest.TestCase):
    def test_path_like_property_index_wins_over_import_table_number(self):
        roof_path = (
            r"environment\architecture\human\redania\nomans_land"
            r"\thatched_buildings\tawern_tower_part_roof.w2mesh"
        )
        wood_path = (
            r"environment\architecture\human\redania\nomans_land"
            r"\thatched_buildings\tawern_tower_part_wood_roof.w2mesh"
        )
        cr2w_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=156),
            CR2WImport=[
                types.SimpleNamespace(path=roof_path),
                types.SimpleNamespace(path=wood_path),
            ],
        )
        chunk = _Chunk(_MeshProp(_PathLikeIndex(roof_path, numeric_index=1)), cr2w_file)

        self.assertEqual(dc_entity._resolve_mesh_path(chunk, None), roof_path)

    def test_cooked_particle_effect_maps_definition_spawner_and_track(self):
        particle_path = r"fx\light_sources\candles\candle_flame_fx2.w2p"
        chunks = [
            _EffectChunk(
                "CFXDefinition",
                0,
                name="fire",
                length=7.5,
                loopStart=0.25,
                loopEnd=7.0,
                isLooped=True,
            ),
            _EffectChunk("CFXSpawnerComponent", 1, componentName="fire"),
            _EffectChunk(
                "CFXTrackItemParticles",
                2,
                particleSystem=(None, particle_path),
                spawner=2,
                timeBegin=0.5,
                timeDuration=6.5,
            ),
        ]
        effect_file = types.SimpleNamespace(CHUNKS=types.SimpleNamespace(CHUNKS=chunks))
        buffer_prop = types.SimpleNamespace(Bufferdata=types.SimpleNamespace(Bytes=b"effect"))

        with mock.patch.object(dc_entity, "getCR2W", return_value=effect_file):
            effect = dc_entity._parse_cooked_effect_buffer(buffer_prop)

        self.assertEqual(effect["name"], "fire")
        self.assertEqual(effect["length"], 7.5)
        self.assertEqual(effect["loop_start"], 0.25)
        self.assertEqual(effect["loop_end"], 7.0)
        self.assertTrue(effect["is_looped"])
        self.assertEqual(effect["particle_systems"], [{
            "path": particle_path,
            "slot": "fire",
            "time_begin": 0.5,
            "duration": 6.5,
        }])


if __name__ == "__main__":
    unittest.main()
