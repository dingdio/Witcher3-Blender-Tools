import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools import attachment_math


class TestHardAttachmentMath(unittest.TestCase):
    def test_coerce_attachment_flags_from_engine_shapes(self):
        value_object = types.SimpleNamespace(Value=attachment_math.HAF_FREE_ROTATION)
        property_object = types.SimpleNamespace(strings=[
            "HAF_FreeRotation",
            "HAF_FreePositionAxisX",
        ])

        self.assertEqual(attachment_math.coerce_attachment_flags(value_object), attachment_math.HAF_FREE_ROTATION)
        self.assertEqual(
            attachment_math.coerce_attachment_flags("HAF_FreeRotation|HAF_FreePositionAxisX"),
            attachment_math.HAF_FREE_ROTATION | attachment_math.HAF_FREE_POSITION_AXIS_X,
        )
        self.assertEqual(
            attachment_math.coerce_attachment_flags(["HAF_FreePositionAxisY", "HAF_FreePositionAxisZ"]),
            attachment_math.HAF_FREE_POSITION_AXIS_Y | attachment_math.HAF_FREE_POSITION_AXIS_Z,
        )
        self.assertEqual(
            attachment_math.coerce_attachment_flags(property_object),
            attachment_math.HAF_FREE_ROTATION | attachment_math.HAF_FREE_POSITION_AXIS_X,
        )
        self.assertEqual(
            attachment_math.attachment_flags_text(property_object),
            "HAF_FreePositionAxisX|HAF_FreeRotation",
        )

    def test_normalize_engine_transform_fills_identity_channels(self):
        result = attachment_math.normalize_engine_transform({"X": "1.5", "Scale_y": 2})
        self.assertEqual(result["X"], 1.5)
        self.assertEqual(result["Y"], 0.0)
        self.assertEqual(result["Scale_x"], 1.0)
        self.assertEqual(result["Scale_y"], 2.0)
        self.assertTrue(attachment_math.engine_transform_is_identity(None))
        self.assertFalse(attachment_math.engine_transform_is_identity({"Roll": 0.1}))

    def test_bone_name_from_slot_index(self):
        self.assertEqual(
            attachment_math.bone_name_from_slot_index(["Root", "Trajectory01", "head"], 1),
            "Trajectory01",
        )
        self.assertEqual(attachment_math.bone_name_from_slot_index(["Root"], 4), "")


if __name__ == "__main__":
    unittest.main()
