import importlib
import math
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class _Quaternion:
    def __init__(self, *args):
        if len(args) == 2:
            axis, angle = args
            x, y, z = axis
            length = (x * x + y * y + z * z) ** 0.5 or 1.0
            scale = math.sin(angle / 2.0) / length
            self.w = math.cos(angle / 2.0)
            self.x = x * scale
            self.y = y * scale
            self.z = z * scale
        else:
            self.w, self.x, self.y, self.z = args[0]


class _Euler:
    def __init__(self, values, mode="XYZ"):
        self.values = values
        self.mode = mode

    def to_quaternion(self):
        x, y, z = self.values
        cx, sx = math.cos(x / 2.0), math.sin(x / 2.0)
        cy, sy = math.cos(y / 2.0), math.sin(y / 2.0)
        cz, sz = math.cos(z / 2.0), math.sin(z / 2.0)
        if self.mode != "XYZ":
            raise NotImplementedError(self.mode)
        return _Quaternion((
            cx * cy * cz - sx * sy * sz,
            sx * cy * cz + cx * sy * sz,
            cx * sy * cz - sx * cy * sz,
            cx * cy * sz + sx * sy * cz,
        ))


class ExportAnimRotationTests(unittest.TestCase):
    def _import_export_anims_with_stubs(self):
        created = []
        previous = {}
        sentinel = object()

        def put(name, module):
            if name not in previous:
                previous[name] = sys.modules.get(name, sentinel)
            sys.modules[name] = module
            created.append(name)

        def restore_modules():
            for name in reversed(created):
                old = previous.get(name, sentinel)
                if old is sentinel:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old

        pkg = types.ModuleType("witcher3_tools")
        pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
        pkg.__package__ = "witcher3_tools"
        pkg.get_rig_rot90_enabled = lambda *_args, **_kwargs: False
        put("witcher3_tools", pkg)

        bpy = types.ModuleType("bpy")
        bpy.types = types.SimpleNamespace(Keyframe=object, FCurve=object)
        put("bpy", bpy)

        mathutils = types.ModuleType("mathutils")
        mathutils.Quaternion = _Quaternion
        mathutils.Euler = _Euler
        mathutils.Vector = lambda seq: list(seq)
        mathutils.Matrix = types.SimpleNamespace(Rotation=lambda *_args, **_kwargs: None)
        put("mathutils", mathutils)

        action_compat = types.ModuleType("witcher3_tools.action_compat")
        action_compat.iter_action_fcurves = lambda *_args, **_kwargs: []
        put("witcher3_tools.action_compat", action_compat)

        constants = types.ModuleType("witcher3_tools.w3_armature_constants")
        constants.human_bone_order = []
        put("witcher3_tools.w3_armature_constants", constants)

        camera_tracks = types.ModuleType("witcher3_tools.camera_tracks")
        camera_tracks.CAMERA_TRACK_NAMES = []
        put("witcher3_tools.camera_tracks", camera_tracks)

        cr2w = types.ModuleType("witcher3_tools.CR2W")
        cr2w.__path__ = []
        put("witcher3_tools.CR2W", cr2w)
        for name in ("w3_types", "anims_builder", "cr2w_writer"):
            put(f"witcher3_tools.CR2W.{name}", types.ModuleType(f"witcher3_tools.CR2W.{name}"))

        importers = types.ModuleType("witcher3_tools.importers")
        importers.__path__ = []
        put("witcher3_tools.importers", importers)
        motion_tools = types.ModuleType("witcher3_tools.importers.motion_tools")
        motion_tools.cline_from_per_frame = lambda *_args, **_kwargs: None
        put("witcher3_tools.importers.motion_tools", motion_tools)

        ik_rig = types.ModuleType("witcher3_tools.ik_rig")
        ik_rig.has_unbaked_ik_controls = lambda *_args, **_kwargs: False
        put("witcher3_tools.ik_rig", ik_rig)

        ui = types.ModuleType("witcher3_tools.ui")
        ui.__path__ = []
        put("witcher3_tools.ui", ui)
        armature_context = types.ModuleType("witcher3_tools.ui.armature_context")
        armature_context.get_main_armature = lambda *_args, **_kwargs: None
        put("witcher3_tools.ui.armature_context", armature_context)

        for name in ("witcher3_tools.exporters.export_anims", "witcher3_tools.exporters"):
            if name not in previous:
                previous[name] = sys.modules.get(name, sentinel)
            sys.modules.pop(name, None)
            created.append(name)
        module = importlib.import_module("witcher3_tools.exporters.export_anims")
        self.addCleanup(restore_modules)
        return module

    def test_euler_rotation_sample_exports_as_quaternion(self):
        export_anims = self._import_export_anims_with_stubs()

        quat = export_anims._sampled_rotation_to_quaternion(
            "rotation_euler",
            "XYZ",
            [0.0],
            [0.0],
            [0.0],
            [math.pi / 2.0],
        )

        self.assertAlmostEqual(quat.w, math.sqrt(0.5), places=6)
        self.assertAlmostEqual(quat.x, 0.0, places=6)
        self.assertAlmostEqual(quat.y, 0.0, places=6)
        self.assertAlmostEqual(quat.z, math.sqrt(0.5), places=6)

    def test_euler_default_values_do_not_store_rotation_mode_string(self):
        export_anims = self._import_export_anims_with_stubs()
        bone = types.SimpleNamespace(
            rotation_mode="XYZ",
            rotation_euler=(0.0, 0.0, math.pi / 2.0),
        )

        values = export_anims._rotation_default_values(bone)

        self.assertEqual(len(values), 4)
        self.assertTrue(all(isinstance(value, float) for value in values))
        self.assertEqual(values[0], 0.0)

    def test_rotation_selection_prefers_active_bone_representation(self):
        export_anims = self._import_export_anims_with_stubs()
        curves = {
            "rotation_quaternion": [object(), object(), object(), object()],
            "rotation_axis_angle": [],
            "rotation_euler": [object()],
        }

        self.assertEqual(export_anims._select_rotation_prop(curves, "XYZ"), "rotation_euler")
        self.assertEqual(export_anims._select_rotation_prop(curves, "QUATERNION"), "rotation_quaternion")

    def test_sparse_mismatched_quaternion_defaults_use_active_euler_pose(self):
        export_anims = self._import_export_anims_with_stubs()
        bone = types.SimpleNamespace(
            rotation_mode="XYZ",
            rotation_euler=(0.0, 0.0, math.pi / 2.0),
        )

        values = export_anims._rotation_default_values_for_prop(
            bone,
            "rotation_quaternion",
            "QUATERNION",
        )

        self.assertAlmostEqual(values[0], math.sqrt(0.5), places=6)
        self.assertAlmostEqual(values[1], 0.0, places=6)
        self.assertAlmostEqual(values[2], 0.0, places=6)
        self.assertAlmostEqual(values[3], math.sqrt(0.5), places=6)


if __name__ == "__main__":
    unittest.main()
