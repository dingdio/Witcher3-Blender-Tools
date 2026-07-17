"""Regression tests for sparse terrain texture-parameter arrays."""

import io
import struct
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg
if "witcher3_tools.CR2W" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools.CR2W")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools" / "CR2W")]
    _pkg.__package__ = "witcher3_tools.CR2W"
    sys.modules["witcher3_tools.CR2W"] = _pkg
if "witcher3_tools.unreal_export" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools.unreal_export")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools" / "unreal_export")]
    _pkg.__package__ = "witcher3_tools.unreal_export"
    sys.modules["witcher3_tools.unreal_export"] = _pkg

from witcher3_tools.CR2W import CR2W_types as cr2w_types
from witcher3_tools.unreal_export import terrain_material


class TestCountedTerrainParameterElements(unittest.TestCase):

    def test_empty_struct_keeps_its_source_index(self):
        # Element 0 contains one fake property; element 1 is entirely empty.
        payload = (
            b"\x00" + struct.pack("<H", 1) + b"v" + struct.pack("<H", 0)
            + b"\x00" + struct.pack("<H", 0)
        )
        stream = io.BytesIO(payload)
        parent = types.SimpleNamespace(
            Count=2,
            classEnd=len(payload),
            dataEnd=len(payload),
        )

        def fake_property(handle, _cr2w, _parent):
            self.assertEqual(cr2w_types.readUShort(handle), 1)
            name = handle.read(1).decode("ascii")
            return types.SimpleNamespace(Type=object(), theName=name)

        with mock.patch.object(cr2w_types, "PROPERTY", side_effect=fake_property):
            elements = cr2w_types._read_counted_struct_elements(
                stream, object(), parent, 2, "STerrainTextureParameters")

        self.assertEqual(stream.tell(), len(payload))
        self.assertEqual([item.ElementIdx for item in elements], [0, 1])
        self.assertEqual([prop.theName for prop in elements[0].MoreProps], ["v"])
        self.assertEqual(elements[1].MoreProps, [])

    def test_missing_val_uses_engine_zero_defaults(self):
        element = types.SimpleNamespace(GetVariableByName=lambda _name: None)
        params = types.SimpleNamespace(More=[element])
        clip = types.SimpleNamespace(
            GetVariableByName=lambda name: params if name == "textureParams" else None)
        world = types.SimpleNamespace(terrainClipMap=clip)

        rows = terrain_material.get_terrain_texture_params(world)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["blend_sharpness"], 0.0)
        self.assertEqual(rows[0]["slope_base_dampening"], 0.0)
        self.assertEqual(rows[0]["slope_normal_dampening"], 0.5)


if __name__ == "__main__":
    unittest.main()
