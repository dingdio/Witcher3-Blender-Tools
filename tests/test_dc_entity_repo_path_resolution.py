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
    @staticmethod
    def _cname(value):
        return types.SimpleNamespace(name=types.SimpleNamespace(value=value))

    def test_chunk_end_comes_from_export_range(self):
        for version, payload in ((155, b"\x02abc"), (163, b"\0abc")):
            data_offset = 3
            stream = BytesIO(b"pad" + payload)
            export = types.SimpleNamespace(
                dataOffset=data_offset,
                dataSize=len(payload),
                name="CTestChunk",
            )
            cr2w_file = types.SimpleNamespace(
                start=0,
                HEADER=types.SimpleNamespace(version=version),
                CR2WExport=[export],
                currentExport=0,
            )
            parent = types.SimpleNamespace(currentClass="")

            CR2W_types._seek_class_payload(stream, cr2w_file, 0)
            chunk = CR2W_types.W_CLASS(stream, cr2w_file, parent, 0)

            self.assertEqual(chunk.classEnd, data_offset + len(payload))

    def test_exports_before_version_158_start_at_data_offset(self):
        for first_byte in (0, 1, 0x80, 2):
            data_offset = 3
            stream = BytesIO(b"pad" + bytes((first_byte,)) + b"payload")
            cr2w_file = types.SimpleNamespace(
                start=0,
                HEADER=types.SimpleNamespace(version=155),
                CR2WExport=[types.SimpleNamespace(dataOffset=data_offset)],
            )

            CR2W_types._seek_class_payload(stream, cr2w_file, 0)

            self.assertEqual(stream.tell(), data_offset)

    def test_export_flag_is_one_byte_from_version_158(self):
        data_offset = 3
        for flag in (0, 1, 0x80, 2):
            stream = BytesIO(b"pad" + bytes((flag,)) + b"payload")
            cr2w_file = types.SimpleNamespace(
                start=0,
                HEADER=types.SimpleNamespace(version=158),
                CR2WExport=[types.SimpleNamespace(dataOffset=data_offset)],
            )

            CR2W_types._seek_class_payload(stream, cr2w_file, 0)

            self.assertEqual(stream.tell(), data_offset + 1)

    def test_variant_prefixed_string_array_uses_normalized_element_type(self):
        payload = struct.pack("<I", 2) + b"\x84root\x84dyng"
        prop_type = CR2W_types.PROPSTART_BLANK()
        prop_type.size = len(payload) + 4
        prop_type.name = "nodeNames"
        prop_type.type = "array:5,0,String"
        cr2w_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=163),
            CNAMES=[],
        )

        prop = CR2W_types.PROPERTY(
            BytesIO(payload),
            cr2w_file,
            types.SimpleNamespace(classEnd=len(payload)),
            False,
            prop_type,
        )

        self.assertEqual([value.String for value in prop.elements], ["root", "dyng"])

    def test_unsupported_export_payload_encoding_fails_explicitly(self):
        stream = BytesIO(b"pad\x04payload")
        cr2w_file = types.SimpleNamespace(
            start=0,
            HEADER=types.SimpleNamespace(version=158),
            CR2WExport=[types.SimpleNamespace(dataOffset=3)],
        )

        with self.assertRaisesRegex(NotImplementedError, "export payload encoding"):
            CR2W_types._seek_class_payload(stream, cr2w_file, 0)

    def test_entity_component_payload_tracks_legacy_matrix_version_boundary(self):
        matrix = struct.pack(
            "<16f",
            1, 0, 0, 0,
            0, 1, 0, 0,
            0, 0, 1, 0,
            0, 0, 0, 1,
        )
        for version, legacy_matrix in ((161, matrix), (162, b"")):
            payload = b"\0" * 10 + legacy_matrix + b"\x02" + struct.pack("<ii", 2, 3)
            stream = BytesIO(payload)
            cr2w_file = types.SimpleNamespace(
                HEADER=types.SimpleNamespace(version=version),
                fileName="<memory>",
                CR2WExport=[None, None, None],
            )
            entity_chunk = types.SimpleNamespace(ChunkIndex=4)

            pos = CR2W_types._entity_payload_start(
                stream, cr2w_file, entity_chunk, 0, len(payload)
            )
            components = CR2W_types._read_entity_components_from_payload(
                stream, cr2w_file, entity_chunk, pos, len(payload)
            )

            self.assertEqual(components, [2, 3])

    def test_entity_component_payload_skips_nonempty_node_attachment_lists(self):
        payload = (
            b"\0\0"
            + struct.pack("<Iii", 2, 4, 5)
            + struct.pack("<Ii", 1, 6)
            + b"\x02"
            + struct.pack("<ii", 2, 3)
        )
        stream = BytesIO(payload)
        cr2w_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=162),
            fileName="<memory>",
            CR2WExport=[None, None, None, None, None, None],
        )

        entity_chunk = types.SimpleNamespace(ChunkIndex=1)
        pos = CR2W_types._entity_payload_start(
            stream, cr2w_file, entity_chunk, 0, len(payload)
        )
        components = CR2W_types._read_entity_components_from_payload(
            stream, cr2w_file, entity_chunk, pos, len(payload)
        )

        self.assertEqual(components, [2, 3])

    def test_entity_node_tail_rejects_out_of_range_attachment_reference(self):
        payload = (
            b"\0\0"
            + struct.pack("<Ii", 1, 7)
            + struct.pack("<I", 0)
            + b"\x80"
        )
        stream = BytesIO(payload)
        cr2w_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=162),
            fileName="<memory>",
            CR2WExport=[None] * 6,
            CR2WImport=[],
        )

        with self.assertRaisesRegex(ValueError, "parent attachment reference 7"):
            CR2W_types._entity_payload_start(
                stream,
                cr2w_file,
                types.SimpleNamespace(ChunkIndex=1),
                0,
                len(payload),
            )

    def test_entity_node_tail_cursor_lands_on_component_count(self):
        payload = (
            b"\0\0"
            + struct.pack("<Ii", 1, 4)
            + struct.pack("<Ii", 1, 5)
            + b"\x02"
            + struct.pack("<ii", 2, 3)
        )
        stream = BytesIO(payload)
        cr2w_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=162),
            fileName="<memory>",
            CR2WExport=[None] * 6,
            CR2WImport=[],
        )

        payload_pos = CR2W_types._entity_payload_start(
            stream,
            cr2w_file,
            types.SimpleNamespace(ChunkIndex=1),
            0,
            len(payload),
        )

        self.assertEqual(payload_pos, 18)
        self.assertEqual(stream.tell(), payload_pos)
        self.assertEqual(stream.read(1), b"\x02")

    def test_local_entity_template_handle_is_not_treated_as_null(self):
        template = types.SimpleNamespace(
            Handles=[types.SimpleNamespace(val=2, Reference=1, DepotPath=None)]
        )

        self.assertTrue(CR2W_types._entity_has_template(template))
        self.assertFalse(CR2W_types._entity_has_template(
            types.SimpleNamespace(
                Handles=[types.SimpleNamespace(val=0, Reference=None, DepotPath=None)]
            )
        ))

    def test_entity_buffer_v1_uses_bounded_length_prefixed_blocks(self):
        payload = (
            struct.pack("<H", 1)
            + bytes(range(16))
            + struct.pack("<I", 7)
            + b"abc"
            + b"\0\0"
        )
        stream = BytesIO(payload)
        cr2w_file = types.SimpleNamespace(
            CNAMES=[self._cname(""), self._cname("CTestComponent")],
            CR2WImport=[],
            fileName="<memory>",
        )

        entries = CR2W_types._read_entity_buffer_v1_safe(
            stream,
            cr2w_file,
            types.SimpleNamespace(ChunkIndex=1),
            len(payload),
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].Buffer.Bytes, b"abc")
        self.assertEqual(stream.tell(), len(payload))

        malformed = payload[:18] + struct.pack("<I", 100) + payload[22:]
        with self.assertRaisesRegex(ValueError, "past"):
            CR2W_types._read_entity_buffer_v1_safe(
                BytesIO(malformed),
                cr2w_file,
                types.SimpleNamespace(ChunkIndex=1),
                len(malformed),
            )

    def test_entity_buffer_v2_uses_bounded_outer_and_nested_blocks(self):
        payload = struct.pack("<IIHI", 1, 10, 1, 0)
        cr2w_file = types.SimpleNamespace(
            CNAMES=[self._cname(""), self._cname("CTestComponent")],
            fileName="<memory>",
        )
        stream = BytesIO(payload)

        entries = CR2W_types._read_entity_buffer_v2_safe(
            stream,
            cr2w_file,
            types.SimpleNamespace(ChunkIndex=1),
            len(payload),
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].sizeofdata, 10)
        self.assertEqual(stream.tell(), len(payload))

        scalar = (
            struct.pack("<IIHI", 1, 19, 1, 1)
            + struct.pack("<IHHB", 9, 2, 3, 1)
        )
        scalar_file = types.SimpleNamespace(
            CNAMES=[
                self._cname(""),
                self._cname("CTestComponent"),
                self._cname("Bool"),
                self._cname("isEnabled"),
            ],
            HEADER=types.SimpleNamespace(version=163),
            fileName="<memory>",
        )
        scalar_entries = CR2W_types._read_entity_buffer_v2_safe(
            BytesIO(scalar),
            scalar_file,
            types.SimpleNamespace(ChunkIndex=1),
            len(scalar),
        )
        variable = scalar_entries[0].variables.elements[0]
        self.assertEqual(variable.classEnd, 5)
        self.assertEqual(variable.PROP.dataEnd, 5)
        self.assertEqual(variable.PROP.Value, 1)

        malformed = struct.pack("<IIHI", 1, 12, 1, 0)
        with self.assertRaisesRegex(ValueError, "past"):
            CR2W_types._read_entity_buffer_v2_safe(
                BytesIO(malformed),
                cr2w_file,
                types.SimpleNamespace(ChunkIndex=1),
                len(malformed),
            )

        nested = struct.pack("<IIHIIHH", 1, 18, 1, 1, 20, 0, 0)
        with self.assertRaisesRegex(ValueError, "override value ends"):
            CR2W_types._read_entity_buffer_v2_safe(
                BytesIO(nested),
                cr2w_file,
                types.SimpleNamespace(ChunkIndex=1),
                len(nested),
            )

    def test_entity_buffer_counter_does_not_replace_export_chunk_index(self):
        exports = [
            types.SimpleNamespace(dataOffset=0, dataSize=8, name="CTest")
            for _ in range(4)
        ]
        exports[3] = types.SimpleNamespace(dataOffset=0, dataSize=8, name="CEntity")
        cr2w_file = types.SimpleNamespace(
            start=0,
            HEADER=types.SimpleNamespace(version=163),
            CR2WExport=exports,
            currentExport=3,
            CR2WTable=[types.SimpleNamespace(itemCount=0) for _ in range(5)],
            CNAMES=[],
            entity_count=0,
            fileName="<memory>",
        )

        with (
            mock.patch.object(CR2W_types, "detectedProp", return_value=False),
            mock.patch.object(CR2W_types, "_entity_payload_start", return_value=0),
            mock.patch.object(CR2W_types, "_read_entity_components_from_payload", return_value=[]),
            mock.patch.object(CR2W_types, "_read_entity_buffer_v1_safe", return_value=[]),
        ):
            chunk = CR2W_types.W_CLASS(
                BytesIO(b"12345678"),
                cr2w_file,
                types.SimpleNamespace(currentClass=""),
                3,
            )

        self.assertEqual(chunk.ChunkIndex, 3)

    def test_entity_postfix_buffers_follow_format_version_gates(self):
        def parse_entity(version, postfix, template=None):
            prefix = b"\0" * 10
            if version < 162:
                prefix += b"\0" * 64
            payload = prefix + postfix
            cr2w_file = types.SimpleNamespace(
                start=0,
                HEADER=types.SimpleNamespace(version=version),
                CR2WExport=[types.SimpleNamespace(
                    dataOffset=0,
                    dataSize=len(payload),
                    name="CEntity",
                )],
                CR2WImport=[],
                currentExport=0,
                CR2WTable=[types.SimpleNamespace(itemCount=0) for _ in range(5)],
                CNAMES=[self._cname("")],
                entity_count=0,
                fileName="<memory>",
            )
            with (
                mock.patch.object(CR2W_types, "detectedProp", return_value=False),
                mock.patch.object(
                    CR2W_types.W_CLASS,
                    "GetVariableByName",
                    return_value=template,
                ),
            ):
                return CR2W_types.W_CLASS(
                    BytesIO(payload),
                    cr2w_file,
                    types.SimpleNamespace(currentClass=""),
                    0,
                )

        with (
            mock.patch.object(
                CR2W_types,
                "_read_entity_buffer_v1_safe",
                wraps=CR2W_types._read_entity_buffer_v1_safe,
            ) as read_v1,
            mock.patch.object(
                CR2W_types,
                "_read_entity_buffer_v2_safe",
                wraps=CR2W_types._read_entity_buffer_v2_safe,
            ) as read_v2,
        ):
            pre_additional_data = parse_entity(130, b"\x80")
            self.assertEqual(pre_additional_data.Components, [])
            read_v1.assert_not_called()
            read_v2.assert_not_called()

        template = types.SimpleNamespace(
            Handles=[types.SimpleNamespace(val=1, Reference=0, DepotPath=None)]
        )
        with mock.patch.object(
            CR2W_types,
            "_read_entity_buffer_v2_safe",
            wraps=CR2W_types._read_entity_buffer_v2_safe,
        ) as read_v2:
            version_148 = parse_entity(148, b"\0\0", template)
            self.assertTrue(version_148.isCreatedFromTemplate)
            self.assertEqual(version_148.BufferV1, [])
            read_v2.assert_not_called()

        version_149 = parse_entity(149, b"\0\0" + struct.pack("<I", 0), template)
        self.assertEqual(version_149.BufferV2, [])

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

    def test_streaming_buffer_keeps_distinct_components_with_the_same_mesh(self):
        mesh_path = r"fx\water\water_fountain\fountain_splash.w2mesh"
        owner = _EffectChunk("CEntity", 0)
        owner.Components = []
        owner.PROPS.append(types.SimpleNamespace(
            theName="streamingDataBuffer",
            Bufferdata=types.SimpleNamespace(Bytes=b"stream"),
        ))

        upper = _EffectChunk("CMeshComponent", 1)
        upper.component_name = "CMeshComponent0"
        upper.mesh = mesh_path
        lower = _EffectChunk("CMeshComponent", 2)
        lower.component_name = "CMeshComponent1"
        lower.mesh = mesh_path

        source_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=159),
            CHUNKS=types.SimpleNamespace(CHUNKS=[owner]),
            CR2WImport=[],
            fileName="<memory>",
        )
        buffer_file = types.SimpleNamespace(
            CHUNKS=types.SimpleNamespace(CHUNKS=[upper, lower]),
        )

        class _Converter:
            def __init__(self, chunk):
                self.chunk = chunk

            def convert_for_io(self):
                return types.SimpleNamespace(
                    name=self.chunk.component_name,
                    mesh=self.chunk.mesh,
                    transform=None,
                    transformParent=None,
                )

        with (
            mock.patch.object(dc_entity, "_flat_compiled_file", return_value=None),
            mock.patch.object(dc_entity, "getCR2W", return_value=buffer_file),
            mock.patch.object(dc_entity, "CMeshComponent", _Converter),
            mock.patch.object(dc_entity, "_resolve_mesh_path", side_effect=lambda _chunk, value: value),
        ):
            entity = dc_entity.create_CEntity(source_file)

        meshes = [
            chunk for chunk in entity.staticMeshes.chunks
            if chunk.type == "CMeshComponent"
        ]
        self.assertEqual([chunk.name for chunk in meshes], ["CMeshComponent0", "CMeshComponent1"])
        self.assertEqual([chunk.chunkIndex for chunk in meshes], [1, 2])

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
