import sys
import struct
import types
import unittest
from contextlib import nullcontext
from io import BytesIO
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

from witcher3_tools.CR2W import CR2W_types, dc_entity  # noqa: E402
from witcher3_tools.CR2W.Types.VariousTypes import CCompressedBuffer  # noqa: E402


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
    def test_skeleton_buffer_header_uses_payload_size_not_resource_version(self):
        values = tuple(float(value) for value in range(1, 13))
        for header in (b"", b"\0", b"\0\0", b"\0\0\0"):
            stream = BytesIO(header + struct.pack("<12f", *values))
            parent = types.SimpleNamespace(classEnd=len(stream.getvalue()))
            cr2w_file = types.SimpleNamespace(HEADER=types.SimpleNamespace(version=153))
            buffer = CCompressedBuffer(stream, cr2w_file, parent)

            buffer.Read(stream, 48, 1)

            parsed = buffer.rigData[0]
            self.assertEqual(tuple(getattr(parsed.position, key) for key in "xyzw"), values[:4])
            self.assertEqual(tuple(getattr(parsed.rotation, key) for key in "xyzw"), values[4:8])
            self.assertEqual(tuple(getattr(parsed.scale, key) for key in "xyzw"), values[8:])

    def test_byte_array_flat_compiled_data_is_parsed_as_cr2w(self):
        parsed = types.SimpleNamespace(fileName=None)
        captured = {}

        def _parse(stream):
            captured["bytes"] = stream.read()
            captured["name"] = stream.name
            return parsed

        prop = types.SimpleNamespace(More=list(b"CR2Wpayload"))
        with mock.patch.object(CR2W_types, "getCR2W", side_effect=_parse):
            result = CR2W_types._parse_flat_compiled_data_property(prop, r"C:\source.w2ent")

        self.assertIs(result, parsed)
        self.assertEqual(captured["bytes"], b"CR2Wpayload")
        self.assertEqual(captured["name"], r"C:\source.w2ent:flatCompiledData")
        self.assertEqual(parsed.fileName, r"C:\source.w2ent")

    def test_uncooked_effect_graph_matches_cooked_effect_schema(self):
        def _ptr(name, *values):
            return types.SimpleNamespace(theName=name, value=list(values))

        template = _EffectChunk("CEntityTemplate", 0)
        template.PROPS.append(_ptr("effects", 2))
        definition = _EffectChunk(
            "CFXDefinition",
            1,
            name="fire",
            length=7.5,
            loopStart=0.25,
            loopEnd=7.0,
            isLooped=True,
        )
        definition.PROPS.append(_ptr("trackGroups", 3))
        group = _EffectChunk("CFXTrackGroup", 2)
        group.PROPS.append(_ptr("tracks", 4))
        track = _EffectChunk("CFXTrack", 3)
        track.PROPS.append(_ptr("trackItems", 5))
        particle = _EffectChunk(
            "CFXTrackItemParticles",
            4,
            particleSystem=(None, r"fx\fire.w2p"),
            spawner=6,
            timeBegin=0.5,
            timeDuration=6.5,
        )
        spawner = _EffectChunk("CFXSpawnerComponent", 5, componentName="torso")

        self.assertEqual(
            dc_entity._extract_uncooked_entity_effects(
                template,
                [template, definition, group, track, particle, spawner],
            ),
            [{
                "name": "fire",
                "length": 7.5,
                "loop_start": 0.25,
                "loop_end": 7.0,
                "is_looped": True,
                "particle_systems": [{
                    "path": r"fx\fire.w2p",
                    "slot": "torso",
                    "time_begin": 0.5,
                    "duration": 6.5,
                }],
            }],
        )

    def test_appearance_metadata_inherits_w3_included_templates(self):
        def _template_chunk(*, includes=(), appearances=(), used=()):
            props = {
                "includes": types.SimpleNamespace(
                    Handles=[types.SimpleNamespace(DepotPath=path) for path in includes],
                ),
                "appearances": types.SimpleNamespace(
                    More=[types.SimpleNamespace(PROPS=[_EffectProp("name", name)]) for name in appearances],
                ),
                "usedAppearances": types.SimpleNamespace(
                    Index=[types.SimpleNamespace(String=name) for name in used],
                ),
            }
            return types.SimpleNamespace(
                Type="CEntityTemplate",
                flatCompiledData=None,
                GetVariableByName=props.get,
            )

        def _file(*chunks):
            return types.SimpleNamespace(
                HEADER=types.SimpleNamespace(version=162),
                CHUNKS=types.SimpleNamespace(CHUNKS=list(chunks)),
            )

        source_file = _file(_template_chunk(includes=[r"characters\included.w2ent"], used=["base", "naked"]))
        included_file = _file(
            _template_chunk(appearances=["base", "naked", "winter"], used=["base", "naked", "winter"]),
            types.SimpleNamespace(
                Type="CAnimatedComponent",
                GetVariableByName={"skeleton": _EffectProp("skeleton", "rig.w2rig")}.get,
            ),
            types.SimpleNamespace(Type="CInventoryDefinition"),
        )

        with (
            mock.patch.object(dc_entity.os.path, "exists", return_value=True),
            mock.patch.object(dc_entity, "read_CR2W", side_effect=[source_file, included_file]),
            mock.patch.object(dc_entity, "materialize_entity_repo_path", return_value=r"C:\included.w2ent"),
            mock.patch.object(dc_entity, "redkit_repo_context", return_value=nullcontext()),
        ):
            metadata = dc_entity.read_entity_template_appearance_metadata(r"C:\source.w2ent")

        self.assertEqual(metadata["all_names"], ["base", "naked", "winter"])
        self.assertEqual(metadata["used_names"], ["base", "naked"])
        self.assertEqual(metadata["default_name"], "base")
        self.assertTrue(metadata["has_armature_root"])
        self.assertTrue(metadata["has_inventory_entries"])

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
