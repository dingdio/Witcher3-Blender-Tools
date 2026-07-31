import struct
import sys
import types
import unittest
from io import BytesIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _install_namespace_stub(qualified_name, package_path):
    if qualified_name in sys.modules:
        return
    module = types.ModuleType(qualified_name)
    module.__path__ = [str(package_path)]
    module.__package__ = qualified_name
    sys.modules[qualified_name] = module


_install_namespace_stub("witcher3_tools", REPO_ROOT / "witcher3_tools")
_install_namespace_stub("witcher3_tools.CR2W", REPO_ROOT / "witcher3_tools" / "CR2W")

from witcher3_tools.CR2W import CR2W_types  # noqa: E402
from witcher3_tools.CR2W.CR2W_helpers import Enums  # noqa: E402


def _block_base(x):
    return struct.pack("<12fHHI", *(1.0,) * 9, x, 2.0, 3.0, 0, 0, 0)


def _light_common():
    return struct.pack(
        "<I7f3f4BI",
        0xFFFFFFFF,
        *(10.0, 20.0, 1.0, 100.0, 50.0, 25.0, 0.5),
        *(0.0, 0.0, 0.0),
        *(0, 0, 0, 0),
        0,
    )


def _updated_pointlight_block(x):
    return _block_base(x) + _light_common() + struct.pack("<2f", 0.0, 1.0)


def _updated_spotlight_block(x):
    return (
        _block_base(x)
        + _light_common()
        + struct.pack("<2f3f", 0.0, 1.0, 15.0, 30.0, 0.25)
    )


def _legacy_spotlight_block(x):
    return (
        _block_base(x)
        + _light_common()
        + struct.pack("<6fHH", 15.0, 30.0, 0.25, 45.0, 0.125, -0.25, 7, 0)
    )


class SectorBlockDataTests(unittest.TestCase):
    def test_updated_light_records_keep_following_blocks_aligned(self):
        point_bytes = _updated_pointlight_block(11.0)
        spot_bytes = _updated_spotlight_block(22.0)
        self.assertEqual(len(point_bytes), 116)
        self.assertEqual(len(spot_bytes), 128)
        stream = BytesIO(point_bytes + spot_bytes)

        point = CR2W_types.SBlockData(
            stream, 116, Enums.BlockDataObjectType.PointLight
        )
        self.assertEqual(stream.tell(), 116)
        spot = CR2W_types.SBlockData(
            stream, 128, Enums.BlockDataObjectType.SpotLight
        )

        self.assertAlmostEqual(point.position.x, 11.0)
        self.assertAlmostEqual(spot.position.x, 22.0)
        self.assertAlmostEqual(spot.packedObject.innerAngle, 15.0)
        self.assertAlmostEqual(spot.packedObject.outerAngle, 30.0)
        self.assertAlmostEqual(spot.packedObject.softness, 0.25)
        self.assertIsNone(spot.packedObject.projectionTexture)
        self.assertEqual(stream.tell(), 244)

    def test_legacy_spotlight_projection_tail_still_parses(self):
        block_bytes = _legacy_spotlight_block(33.0)
        self.assertEqual(len(block_bytes), 136)
        stream = BytesIO(block_bytes)

        spot = CR2W_types.SBlockData(
            stream, 136, Enums.BlockDataObjectType.SpotLight
        )

        self.assertAlmostEqual(spot.packedObject.innerAngle, 15.0)
        self.assertAlmostEqual(spot.packedObject.projectionTextureAngle, 45.0)
        self.assertAlmostEqual(spot.packedObject.projectionTexureUBias, 0.125)
        self.assertAlmostEqual(spot.packedObject.projectionTexureVBias, -0.25)
        self.assertEqual(spot.packedObject.projectionTexture, 7)
        self.assertEqual(stream.tell(), 136)


if __name__ == "__main__":
    unittest.main()
