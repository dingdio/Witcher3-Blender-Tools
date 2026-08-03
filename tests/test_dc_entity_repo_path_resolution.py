import sys
import inspect
import struct
import types
import unittest
import tempfile
from contextlib import nullcontext
from io import BytesIO
from unittest import mock
from pathlib import Path


from _helpers import install_cr2w_stubs

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
install_cr2w_stubs()

from witcher3_tools.CR2W import CR2W_file, CR2W_types, dc_entity  # noqa: E402
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


class _LayerChunk(_EffectChunk):
    def __init__(self, chunk_type, chunk_index=0, components=(), props=None, **attributes):
        super().__init__(chunk_type, chunk_index, **(props or {}))
        self.name = chunk_type
        self.Components = list(components)
        self.__dict__.update(attributes)

    def get_name_prop_string(self):
        return self.name


def _exports(*entries):
    return [types.SimpleNamespace(name=name, parentID=parent) for name, parent in entries]


def _cr2w_source(*chunks, version=162):
    return types.SimpleNamespace(
        HEADER=types.SimpleNamespace(version=version),
        CHUNKS=types.SimpleNamespace(CHUNKS=list(chunks)),
    )


def _template_source(props, *extra_chunks, version=162):
    template = types.SimpleNamespace(
        Type="CEntityTemplate",
        flatCompiledData=None,
        GetVariableByName=props.get,
    )
    return _cr2w_source(template, *extra_chunks, version=version)


def _appearance_chunk(name, included_templates=()):
    props = {
        "name": _EffectProp("name", name),
        "includedTemplates": types.SimpleNamespace(
            Handles=[
                types.SimpleNamespace(DepotPath=path)
                for path in included_templates
            ],
        ),
    }
    return types.SimpleNamespace(PROPS=list(props.values()), GetVariableByName=props.get)


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

    def test_unknown_direct_layer_subclass_is_parsed_structurally(self):
        chunks = [_LayerChunk("CLayer"), _LayerChunk("CModCustomEntity")]
        cr2w_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=159),
            CHUNKS=types.SimpleNamespace(CHUNKS=chunks),
            CR2WExport=_exports(("CLayer", 0), ("CModCustomEntity", 1)),
            fileName="custom_layer.w2l",
        )

        level = CR2W_file.create_level(cr2w_file, "custom_layer.w2l")

        self.assertEqual(level.expectedEntityCount, 1)
        self.assertEqual(level.parsedEntityCount, 1)
        self.assertEqual(level.Entities[0].type, "CModCustomEntity")

    def test_unknown_standalone_w2ent_root_is_entity_by_asset_structure(self):
        source_file = types.SimpleNamespace(
            fileName=r"C:\mods\custom.w2ent",
            CR2WExport=_exports(("CModCustomEntity", 0)),
        )
        chunk = types.SimpleNamespace(
            Type="CModCustomEntity",
            ChunkIndex=0,
            PROPS=[],
            GetVariableByName=lambda _name: None,
        )
        setattr(chunk, "_W_CLASS__CR2WFILE", source_file)

        self.assertTrue(CR2W_types.is_entity_chunk(chunk))

        non_entity_file = types.SimpleNamespace(
            fileName=r"C:\mods\custom.w2mesh",
            CR2WExport=source_file.CR2WExport,
        )
        non_entity_chunk = types.SimpleNamespace(
            Type="CModCustomEntity",
            ChunkIndex=0,
            PROPS=[],
            GetVariableByName=lambda _name: None,
        )
        setattr(non_entity_chunk, "_W_CLASS__CR2WFILE", non_entity_file)
        self.assertFalse(CR2W_types.is_entity_chunk(non_entity_chunk))

    def test_entity_owned_unknown_component_is_explicitly_unsupported(self):
        template_path = r"environment\templates\custom.w2ent"
        entity_chunk = _LayerChunk(
            "CGameplayEntity",
            isCreatedFromTemplate=True,
            Template=types.SimpleNamespace(Handles=[
                types.SimpleNamespace(DepotPath=template_path),
            ]),
        )
        chunks = [_LayerChunk("CLayer"), entity_chunk, _LayerChunk("CUnknownVisualComponent")]
        source_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=159),
            CHUNKS=types.SimpleNamespace(CHUNKS=chunks),
            CR2WExport=_exports(
                ("CLayer", 0),
                ("CGameplayEntity", 1),
                ("CUnknownVisualComponent", 2),
            ),
            fileName="custom_layer.w2l",
        )

        level = CR2W_file.create_level(
            source_file,
            "custom_layer.w2l",
            dependency_loader=lambda _path: types.SimpleNamespace(entityAsset=object()),
            dependency_resolver=lambda path, *_args: path,
        )

        self.assertEqual(len(level.Entities), 1)
        self.assertEqual(level.Entities[0].entityAssetError, "")
        asset = level.Entities[0].entityAsset
        self.assertEqual(asset.type, "CGameplayEntity")
        self.assertIn("CUnknownVisualComponent", asset.unsupported_components)

    def test_child_entity_export_is_not_treated_as_a_component(self):
        chunks = [_LayerChunk("CLayer"), _LayerChunk("CGameplayEntity"), _LayerChunk("CItemEntity")]
        source_file = types.SimpleNamespace(
            CR2WExport=_exports(
                ("CLayer", 0),
                ("CGameplayEntity", 1),
                ("CItemEntity", 2),
            ),
        )

        self.assertEqual(
            CR2W_file._entity_component_parent_map(source_file, chunks),
            {},
        )

    def test_scoped_layer_entity_graphs_are_disjoint_and_transitive(self):
        chunks = [
            _LayerChunk("CLayer"),
            _LayerChunk("CGameplayEntity", components=[3]),
            _LayerChunk("CAnimatedComponent"),
            _LayerChunk("CHardAttachment"),
            _LayerChunk("CItemEntity", components=[6]),
            _LayerChunk("CMorphedMeshComponent"),
        ]
        source_file = types.SimpleNamespace(
            CR2WExport=_exports(
                ("CLayer", 0),
                ("CGameplayEntity", 1),
                ("CAnimatedComponent", 2),
                ("CHardAttachment", 3),
                ("CItemEntity", 1),
                ("CMorphedMeshComponent", 5),
            ),
        )

        ownership = CR2W_file._entity_component_parent_map(source_file, chunks)
        first_scope, first_error = CR2W_file._scoped_layer_entity_chunk_ids(
            chunks, 2, ownership,
        )
        second_scope, second_error = CR2W_file._scoped_layer_entity_chunk_ids(
            chunks, 5, ownership,
        )

        self.assertEqual(first_error, "")
        self.assertEqual(second_error, "")
        self.assertEqual(first_scope, {2, 3, 4})
        self.assertEqual(second_scope, {5, 6})

    def test_scoped_layer_entity_rejects_cross_sibling_references(self):
        chunks = [
            _LayerChunk("CLayer"),
            _LayerChunk("CGameplayEntity"),
            _LayerChunk("CHardAttachment", props={"child": 5}),
            _LayerChunk("CItemEntity"),
            _LayerChunk("CMorphedMeshComponent"),
        ]
        source_file = types.SimpleNamespace(
            CR2WExport=_exports(
                ("CLayer", 0),
                ("CGameplayEntity", 1),
                ("CHardAttachment", 2),
                ("CItemEntity", 1),
                ("CMorphedMeshComponent", 4),
            ),
        )
        ownership = CR2W_file._entity_component_parent_map(source_file, chunks)

        _scope, error = CR2W_file._scoped_layer_entity_chunk_ids(
            chunks, 2, ownership,
        )

        self.assertIn("owned by entity #4", error)

    def test_scoped_layer_entity_rejects_shared_unparented_component_claims(self):
        chunks = [
            _LayerChunk("CLayer"),
            _LayerChunk("CGameplayEntity", components=[4]),
            _LayerChunk("CItemEntity", components=[4]),
            _LayerChunk("CHardAttachment"),
        ]
        source_file = types.SimpleNamespace(
            CR2WExport=_exports(
                ("CLayer", 0),
                ("CGameplayEntity", 1),
                ("CItemEntity", 1),
                ("CHardAttachment", 0),
            ),
        )
        ownership = CR2W_file._entity_component_parent_map(source_file, chunks)

        for entity_chunk_id in (2, 3):
            with self.subTest(entity_chunk_id=entity_chunk_id):
                _scope, error = CR2W_file._scoped_layer_entity_chunk_ids(
                    chunks,
                    entity_chunk_id,
                    ownership,
                )
                self.assertIn(
                    "component #4 is claimed by sibling entities #2, #3",
                    error,
                )

    def test_shared_unparented_component_claims_become_entity_asset_errors(self):
        chunks = [
            _LayerChunk("CLayer"),
            _LayerChunk("CGameplayEntity", components=[4]),
            _LayerChunk("CItemEntity", components=[4]),
            _LayerChunk("CHardAttachment"),
        ]
        source_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=159),
            CHUNKS=types.SimpleNamespace(CHUNKS=chunks),
            CR2WExport=_exports(
                ("CLayer", 0),
                ("CGameplayEntity", 1),
                ("CItemEntity", 1),
                ("CHardAttachment", 0),
            ),
            fileName="shared_component_layer.w2l",
        )

        level = CR2W_file.create_level(source_file, source_file.fileName)

        self.assertEqual(len(level.Entities), 2)
        self.assertTrue(all(
            "component #4 is claimed by sibling entities #2, #3"
            in entity.entityAssetError
            for entity in level.Entities
        ))

    def test_scoped_layer_view_preserves_indices_and_compiler_disables_global_fallbacks(self):
        chunks = [
            _LayerChunk("CLayer", 0),
            _LayerChunk("CGameplayEntity", 1),
            _LayerChunk("CAnimatedComponent", 2),
            _LayerChunk("CItemEntity", 3),
        ]
        source_file = types.SimpleNamespace(
            CHUNKS=types.SimpleNamespace(CHUNKS=chunks),
            CR2WExport=_exports(
                ("CLayer", 0),
                ("CGameplayEntity", 1),
                ("CAnimatedComponent", 2),
                ("CItemEntity", 1),
            ),
        )
        scoped_file = CR2W_file._copy_scoped_entity_cr2w(
            source_file,
            chunks,
            {2, 3},
        )

        self.assertEqual(len(scoped_file.CHUNKS.CHUNKS), len(chunks))
        self.assertEqual(scoped_file.CHUNKS.CHUNKS[3].ChunkIndex, 3)
        self.assertEqual(scoped_file.CHUNKS.CHUNKS[3].Type, "CEntityTemplateParam")
        self.assertFalse(hasattr(scoped_file.CHUNKS.CHUNKS[3], "Components"))
        compiler_source = inspect.getsource(CR2W_file._compile_scoped_layer_entity_asset)
        self.assertIn("_allow_unscoped_import_fallbacks=False", compiler_source)
        self.assertIn("asset.type =", compiler_source)

    def test_only_unknown_visual_chunk_types_are_unsupported(self):
        cases = {
            "CUnknownVisualComponent": ["CUnknownVisualComponent"],
            "CGameplayLightComponent": [],
            "CHeadAttachment": [],
            "CInventoryComponent": [],
        }
        for chunk_type, expected in cases.items():
            with self.subTest(chunk_type=chunk_type):
                self.assertEqual(
                    dc_entity._unsupported_entity_visual_chunk_types([
                        _EffectChunk(chunk_type, 0),
                    ]),
                    expected,
                )

    def test_destruction_chunks_compile_to_json_plan_components(self):
        base_path = r"environment\architecture\fence.reddest"
        visual_path = r"environment\architecture\fence.redapex"
        destruction = _EffectChunk(
            "CDestructionComponent",
            1,
            m_baseResource=base_path,
            name="destruction",
        )
        destruction_system = _EffectChunk(
            "CDestructionSystemComponent",
            2,
            m_resource=visual_path,
            name="apex",
        )

        base_component = dc_entity._destruction_plan_component_from_chunk(destruction)
        apex_component = dc_entity._destruction_plan_component_from_chunk(destruction_system)

        self.assertEqual(base_component["kind"], "component_mesh")
        self.assertEqual(base_component["repo_path"], base_path)
        self.assertEqual(base_component["component_type"], "CDestructionComponent")
        self.assertEqual(apex_component["kind"], "cloth")
        self.assertEqual(apex_component["repo_path"], visual_path)
        self.assertEqual(apex_component["component_type"], "CDestructionSystemComponent")

    def test_malformed_component_references_are_explicitly_unsupported(self):
        entity = CR2W_file.CEntity()
        root = types.SimpleNamespace(
            name="CGameplayEntity",
            Components=[0, "not-an-index", 99],
        )

        CR2W_file._append_entity_components(
            entity,
            root,
            [root],
            {},
            "broken_layer.w2l",
            1,
        )

        self.assertEqual(
            entity.unsupportedComponents,
            [
                "malformed component reference #0",
                "malformed component reference 'not-an-index'",
                "malformed component reference #99",
            ],
        )

    def test_nonvisual_component_references_are_ignored_and_deduplicated(self):
        entity = CR2W_file.CEntity()
        root = types.SimpleNamespace(name="CGameplayEntity", Components=[2, 3])
        inventory = types.SimpleNamespace(Type="CInventoryComponent")
        mesh = types.SimpleNamespace(Type="CMeshComponent")

        CR2W_file._append_entity_components(
            entity,
            root,
            [root, inventory, mesh],
            {1: [(2, inventory), (3, mesh)]},
            "container.w2ent",
            1,
        )

        self.assertEqual(entity.Components, [mesh])
        self.assertEqual(entity.unsupportedComponents, [])

    def test_direct_layer_resources_and_components_are_not_entities(self):
        for chunk_type in (
            "CSectorData",
            "CFoliageResource",
            "CPointLightComponent",
            "CAnimatedAttachment",
        ):
            with self.subTest(chunk_type=chunk_type):
                chunk = types.SimpleNamespace(
                    name=chunk_type,
                    Type=chunk_type,
                    PROPS=[],
                    GetVariableByName=lambda _name: None,
                )
                source_file = types.SimpleNamespace(
                    CR2WExport=_exports(("CLayer", 0), (chunk_type, 1)),
                )

                self.assertFalse(CR2W_file._is_level_entity_chunk(source_file, chunk, 2))

    def test_w2_template_reference_identifies_unknown_entity_root(self):
        template = _EffectChunk("CEntityTemplate", 0, entityObject=2)
        source_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=115),
            CR2WExport=[
                types.SimpleNamespace(name="CEntityTemplate"),
                types.SimpleNamespace(name="CModCustomEntity"),
            ],
        )

        self.assertTrue(
            CR2W_types._is_w2_template_entity_root(source_file, [template], 1)
        )

    def test_direct_root_chunk_type_is_native_class_fallback(self):
        root = _EffectChunk("CModCustomEntity", 0)

        self.assertEqual(
            dc_entity._entity_class_from_chunks([root]),
            "CModCustomEntity",
        )

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

    def test_entity_class_falls_back_to_referenced_root_chunk(self):
        template = _EffectChunk("CEntityTemplate", 0, entityObject=2)
        root = _EffectChunk("CWitcherSword", 1)
        root.Components = []
        source_file = types.SimpleNamespace(
            CHUNKS=types.SimpleNamespace(CHUNKS=[template, root]),
        )

        self.assertEqual(dc_entity._entity_class_from_cr2w(source_file), "CWitcherSword")

    def test_entity_class_handle_reference_is_zero_based(self):
        template = _EffectChunk("CEntityTemplate", 0)
        template.PROPS.append(types.SimpleNamespace(
            theName="entityObject",
            Handles=[types.SimpleNamespace(Reference=1)],
        ))
        root = _EffectChunk("CItemEntity", 1)
        root.Components = []
        source_file = types.SimpleNamespace(
            CHUNKS=types.SimpleNamespace(CHUNKS=[template, root]),
        )

        self.assertEqual(dc_entity._entity_class_from_cr2w(source_file), "CItemEntity")

    def test_entity_type_survives_json_construction(self):
        entity = dc_entity.w3_types.Entity.from_json({
            "name": "item",
            "type": "CItemEntity",
            "plan_components": [{"kind": "component_mesh", "repo_path": "item.reddest"}],
        })
        aliased = dc_entity.w3_types.Entity.from_json({"name": "sword", "entity_class": "CWitcherSword"})

        self.assertEqual(entity.type, "CItemEntity")
        self.assertEqual(vars(entity)["type"], "CItemEntity")
        self.assertEqual(entity.plan_components[0]["repo_path"], "item.reddest")
        self.assertEqual(aliased.type, "CWitcherSword")

    def test_template_cache_key_tracks_file_version_and_depot_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = Path(temp_dir) / "item.w2ent"
            template_path.write_bytes(b"one")
            with mock.patch.object(
                dc_entity,
                "get_repo_resolution_context",
                return_value=((str(Path(temp_dir) / "depot_a"),), True, (), ()),
            ):
                first = dc_entity._template_cache_key(str(template_path), 115)
                version_changed = dc_entity._template_cache_key(str(template_path), 162)
                template_path.write_bytes(b"different-size")
                file_changed = dc_entity._template_cache_key(str(template_path), 115)
            with mock.patch.object(
                dc_entity,
                "get_repo_resolution_context",
                return_value=((str(Path(temp_dir) / "depot_b"),), False, (), ()),
            ):
                depot_changed = dc_entity._template_cache_key(str(template_path), 115)

        self.assertNotEqual(first, version_changed)
        self.assertNotEqual(first, file_changed)
        self.assertNotEqual(file_changed, depot_changed)

    def test_appearance_metadata_inherits_w3_included_templates(self):
        def _template_chunk(*, includes=(), appearances=(), used=(), entity_class=""):
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
            if entity_class:
                props["entityClass"] = _EffectProp("entityClass", entity_class)
            return types.SimpleNamespace(
                Type="CEntityTemplate",
                flatCompiledData=None,
                GetVariableByName=props.get,
            )

        source_file = _cr2w_source(_template_chunk(
            includes=[r"characters\included.w2ent"],
            used=["base", "naked"],
            entity_class="CNewNPC",
        ))
        included_file = _cr2w_source(
            _template_chunk(
                appearances=["base", "naked", "winter"],
                used=["base", "naked", "winter"],
                entity_class="CItemEntity",
            ),
            types.SimpleNamespace(
                Type="CAnimatedComponent",
                GetVariableByName={"skeleton": _EffectProp("skeleton", "rig.w2rig")}.get,
            ),
            types.SimpleNamespace(Type="CMeshComponent"),
            types.SimpleNamespace(Type="CClothComponent"),
            types.SimpleNamespace(Type="CInventoryDefinition"),
        )

        with (
            mock.patch.object(dc_entity.os.path, "exists", return_value=True),
            mock.patch.object(
                dc_entity,
                "_read_template_dependency_cr2w",
                side_effect=[source_file, included_file],
            ),
            mock.patch.object(dc_entity, "materialize_entity_repo_path", return_value=r"C:\included.w2ent"),
            mock.patch.object(dc_entity, "redkit_repo_context", return_value=nullcontext()),
        ):
            metadata = dc_entity.read_entity_template_appearance_metadata(r"C:\source.w2ent")

        self.assertEqual(metadata["all_names"], ["base", "naked", "winter"])
        self.assertEqual(metadata["used_names"], ["base", "naked"])
        self.assertEqual(metadata["default_name"], "base")
        self.assertEqual(metadata["entity_class"], "CNewNPC")
        self.assertTrue(metadata["component_metadata_known"])
        self.assertTrue(metadata["has_armature_root"])
        self.assertTrue(metadata["has_mesh_components"])
        self.assertTrue(metadata["has_cloth_components"])
        self.assertTrue(metadata["has_inventory_entries"])

    def test_appearance_metadata_tracks_cloth_in_appearance_template(self):
        cloth_template_path = r"characters\models\ciri\cloth.w2ent"
        external_appearance = _appearance_chunk("external", [cloth_template_path])
        source_file = _template_source(
            {
                "appearances": types.SimpleNamespace(More=[
                    _appearance_chunk("player", [cloth_template_path]),
                    _appearance_chunk("naked"),
                ]),
                "usedAppearances": types.SimpleNamespace(
                    Index=[types.SimpleNamespace(String="player")],
                ),
            },
            types.SimpleNamespace(
                Type="CEntityExternalAppearance",
                GetVariableByName={"appearance": external_appearance}.get,
            ),
        )
        cloth_file = _template_source(
            {"appearances": types.SimpleNamespace(More=[_appearance_chunk("nested")])},
            types.SimpleNamespace(Type="CClothComponent"),
        )

        with (
            mock.patch.object(dc_entity.os.path, "exists", return_value=True),
            mock.patch.object(
                dc_entity,
                "_read_template_dependency_cr2w",
                side_effect=[source_file, cloth_file],
            ),
            mock.patch.object(
                dc_entity,
                "materialize_entity_repo_path",
                return_value=r"C:\cloth.w2ent",
            ),
            mock.patch.object(dc_entity, "redkit_repo_context", return_value=nullcontext()),
        ):
            metadata = dc_entity.read_entity_template_appearance_metadata(r"C:\source.w2ent")

        self.assertTrue(metadata["component_metadata_known"])
        self.assertTrue(metadata["has_cloth_components"])
        self.assertFalse(metadata["base_has_cloth_components"])
        self.assertEqual(metadata["all_names"], ["player", "naked", "external"])
        self.assertEqual(metadata["cloth_appearance_names"], ["player", "external"])

    def test_appearance_metadata_is_unknown_when_appearance_template_cannot_resolve(self):
        source_file = _template_source({
            "appearances": types.SimpleNamespace(More=[
                _appearance_chunk("player", [r"missing\cloth.w2ent"]),
            ]),
        })

        with (
            mock.patch.object(
                dc_entity.os.path,
                "exists",
                side_effect=lambda path: str(path).lower().endswith("source.w2ent"),
            ),
            mock.patch.object(
                dc_entity,
                "_read_template_dependency_cr2w",
                return_value=source_file,
            ),
            mock.patch.object(
                dc_entity,
                "materialize_entity_repo_path",
                return_value=r"C:\missing\cloth.w2ent",
            ),
            mock.patch.object(dc_entity, "redkit_repo_context", return_value=nullcontext()),
        ):
            metadata = dc_entity.read_entity_template_appearance_metadata(r"C:\source.w2ent")

        self.assertFalse(metadata["component_metadata_known"])

    def test_appearance_metadata_is_unknown_for_opaque_entity_buffer(self):
        entity_chunk = types.SimpleNamespace(
            Type="CGameplayEntity",
            BufferV1=[object()],
            BufferV2=False,
            PROPS=[],
            GetVariableByName=lambda _name: None,
        )
        source_file = _cr2w_source(entity_chunk)

        with (
            mock.patch.object(dc_entity.os.path, "exists", return_value=True),
            mock.patch.object(dc_entity, "_read_template_dependency_cr2w", return_value=source_file),
            mock.patch.object(dc_entity, "redkit_repo_context", return_value=nullcontext()),
        ):
            metadata = dc_entity.read_entity_template_appearance_metadata(r"C:\opaque.w2ent")

        self.assertFalse(metadata["component_metadata_known"])

    def test_appearance_metadata_is_unknown_when_include_cannot_resolve(self):
        source_file = _template_source({
            "includes": types.SimpleNamespace(
                Handles=[types.SimpleNamespace(DepotPath=r"missing\child.w2ent")]
            ),
        })

        def _exists(path):
            return str(path).lower().endswith("source.w2ent")

        with (
            mock.patch.object(dc_entity.os.path, "exists", side_effect=_exists),
            mock.patch.object(dc_entity, "_read_template_dependency_cr2w", return_value=source_file),
            mock.patch.object(
                dc_entity,
                "materialize_entity_repo_path",
                return_value=r"C:\missing\child.w2ent",
            ),
            mock.patch.object(
                dc_entity,
                "_resolve_repo_paths_from_array",
                return_value=[r"missing\child.w2ent"],
            ),
            mock.patch.object(dc_entity, "redkit_repo_context", return_value=nullcontext()),
        ):
            metadata = dc_entity.read_entity_template_appearance_metadata(r"C:\source.w2ent")

        self.assertFalse(metadata["component_metadata_known"])

    def test_streamed_entity_keeps_distinct_components_with_the_same_mesh(self):
        mesh_path = r"fx\water\water_fountain\fountain_splash.w2mesh"
        owner = _EffectChunk("W3FireSource", 0)
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
        fur = _EffectChunk("CFurComponent", 3)
        fur.component_name = "CFurComponent0"
        fur.mesh = mesh_path

        source_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=159),
            CHUNKS=types.SimpleNamespace(CHUNKS=[owner]),
            CR2WImport=[],
            fileName="<memory>",
        )
        buffer_file = types.SimpleNamespace(
            CHUNKS=types.SimpleNamespace(CHUNKS=[upper, lower, fur]),
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

        for owner_type in ("W3FireSource", "W3FireSourceLifeRegen", "W3ModdedFireSource"):
            owner.Type = owner_type
            with (
                self.subTest(owner_type=owner_type),
                mock.patch.object(dc_entity, "_flat_compiled_file", return_value=None),
                mock.patch.object(dc_entity, "getCR2W", return_value=buffer_file),
                mock.patch.object(dc_entity, "CMeshComponent", _Converter),
                mock.patch.object(dc_entity, "_resolve_mesh_path", side_effect=lambda _chunk, value: value),
            ):
                entity = dc_entity.create_CEntity(source_file)

                meshes = [
                    chunk for chunk in entity.staticMeshes.chunks
                    if chunk.type in {"CMeshComponent", "CFurComponent"}
                ]
                self.assertEqual(
                    [chunk.name for chunk in meshes],
                    ["CMeshComponent0", "CMeshComponent1", "CFurComponent0"],
                )
                self.assertEqual([chunk.chunkIndex for chunk in meshes], [1, 2, 3])

    def test_level_uses_generic_wrapper_for_streamed_entity_subclasses(self):
        template = types.SimpleNamespace(
            name="CEntityTemplate",
            GetVariableByName=lambda _name: None,
        )

        def entity_chunk(chunk_type, instance_name, *, streamed=False):
            streaming_buffer = types.SimpleNamespace() if streamed else None
            return types.SimpleNamespace(
                name=chunk_type,
                Type=chunk_type,
                Components=[],
                isCreatedFromTemplate=False,
                GetVariableByName=lambda name: streaming_buffer if name == "streamingDataBuffer" else None,
                get_name_prop_string=lambda: instance_name,
            )

        source_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=159),
            CHUNKS=types.SimpleNamespace(CHUNKS=[
                template,
                entity_chunk("W3FireSource", "brazier"),
                entity_chunk("W3FireSourceLifeRegen", "campfire"),
                entity_chunk("W3ModdedFireSource", "modded campfire", streamed=True),
            ]),
            CR2WExport=[],
        )

        level = dc_entity.create_level(source_file, "<memory>")

        self.assertEqual([entity.type for entity in level.Entities], [
            "W3FireSource",
            "W3FireSourceLifeRegen",
            "W3ModdedFireSource",
        ])
        self.assertEqual(
            [entity.name for entity in level.Entities],
            ["brazier", "campfire", "modded campfire"],
        )

    def test_level_preserves_entity_instance_metadata_and_buffers(self):
        template = types.SimpleNamespace(
            name="CEntityTemplate",
            GetVariableByName=lambda _name: None,
        )
        buffer_v1 = [types.SimpleNamespace(Buffer=b"opaque")]
        buffer_v2 = [types.SimpleNamespace(componentName="torch", variables=[])]
        props = {
            "guid": types.SimpleNamespace(
                GUID=types.SimpleNamespace(GuidString="01234567-89ab-cdef-0123-456789abcdef")
            ),
            "id": _EffectProp("id", "entity-42"),
            "actionName": _EffectProp("actionName", "ignite"),
            "visible": _EffectProp("visible", False),
        }
        entity_chunk = types.SimpleNamespace(
            name="CGameplayEntity",
            Type="CGameplayEntity",
            Components=[],
            BufferV1=buffer_v1,
            BufferV2=buffer_v2,
            isCreatedFromTemplate=False,
            GetVariableByName=props.get,
            get_name_prop_string=lambda: "torch_entity",
        )
        source_file = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=159),
            CHUNKS=types.SimpleNamespace(CHUNKS=[template, entity_chunk]),
            CR2WExport=[],
        )

        entity = dc_entity.create_level(source_file, "<memory>").Entities[0]

        self.assertEqual(entity.guid, "01234567-89ab-cdef-0123-456789abcdef")
        self.assertEqual(entity.id, "entity-42")
        self.assertEqual(entity.actionName, "ignite")
        self.assertFalse(entity.visible)
        self.assertIs(entity.BufferV1, buffer_v1)
        self.assertIs(entity.BufferV2, buffer_v2)

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
