"""Blender-native checks for explicit NULL material texture round-tripping.

Run with Blender 4.5+:
  blender --background --factory-startup --python tests/blender_material_null_texture_roundtrip_native.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from witcher3_tools.CR2W import cr2w_writer  # noqa: E402
from witcher3_tools.CR2W.mesh_builder import Build_CMaterialInstance_Chunk  # noqa: E402
from witcher3_tools.exporters.export_mesh import (  # noqa: E402
    _merge_unrepresented_null_material_props,
)
from witcher3_tools.materials.reader import read_instance_params  # noqa: E402


def _null_param(name="Normal", handle_type="handle:ITexture"):
    handle = SimpleNamespace(
        ChunkHandle=True,
        Reference=None,
        val=0,
        DepotPath=None,
    )
    prop = SimpleNamespace(
        theName=name,
        theType=handle_type,
        Handles=[handle],
    )
    return SimpleNamespace(PROP=prop)


def _check_reader_retains_null_handle_type():
    material = SimpleNamespace(
        CMaterialInstance=SimpleNamespace(
            InstanceParameters=SimpleNamespace(
                elements=[_null_param(handle_type="handle:CTextureArray")],
            ),
        ),
    )
    params = {}
    read_instance_params(material, params)
    assert params == {"Normal": ("handle:CTextureArray", "NULL")}


def _check_export_merge_is_null_only():
    xml_text = """\
        <material>
          <param name="Diffuse" type="handle:ITexture" value="old_diffuse.xbm" />
          <param name="Normal" type="handle:ITexture" value="NULL" />
          <param name="Mask" type="handle:ITexture" value="NULL" />
        </material>
    """
    derived = [
        {"name": "Diffuse", "type": "TEX_IMAGE", "value": "new_diffuse.xbm"},
        {"name": "Mask", "type": "TEX_IMAGE", "value": "new_mask.xbm"},
    ]
    merged = _merge_unrepresented_null_material_props(derived, xml_text)
    assert merged == derived + [
        {"name": "Normal", "type": "handle:ITexture", "value": "NULL"},
    ]


def _check_builder_writes_local_null_handle():
    cr2w = SimpleNamespace(
        HEADER=SimpleNamespace(numChunks=0),
        CR2WExport=[],
        CHUNKS=SimpleNamespace(CHUNKS=[]),
        childrendict={},
    )
    Build_CMaterialInstance_Chunk(cr2w, {
        "witcher_props": {
            "base_custom": r"engine\materials\graphs\pbr_std.w2mg",
            "enableMask": False,
            "input_props": [{
                "name": "Normal",
                "type": "handle:ITexture",
                "value": "NULL",
            }],
        },
    })
    chunk = cr2w.CHUNKS.CHUNKS[0]
    prop = chunk.CMaterialInstance.InstanceParameters.elements[0].PROP
    handle = prop.Handles[0]
    assert prop.theName == "Normal"
    assert prop.theType == "handle:ITexture"
    assert handle.ChunkHandle is True
    assert handle.Reference is None
    assert handle.val == 0
    assert cr2w_writer._encode_handle_value(handle, {}) == struct.pack("<i", 0)


_check_reader_retains_null_handle_type()
_check_export_merge_is_null_only()
_check_builder_writes_local_null_handle()
print("material NULL texture round-trip checks passed")
