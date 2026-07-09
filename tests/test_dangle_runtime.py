import importlib.util
import pathlib
import sys
import tempfile
import types
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "witcher3_tools"
PHYSICS_ROOT = PACKAGE_ROOT / "physics"


def _install_bpy_stub():
    bpy = types.ModuleType("bpy")
    bpy.types = types.SimpleNamespace(Object=object, Scene=object, Constraint=object, PoseBone=object)
    bpy.data = types.SimpleNamespace(objects=[])
    handlers = types.SimpleNamespace(frame_change_post=[])
    bpy.app = types.SimpleNamespace(handlers=handlers)
    bpy.context = types.SimpleNamespace(scene=types.SimpleNamespace())

    bpy_app = types.ModuleType("bpy.app")
    bpy_app.handlers = handlers
    bpy_handlers = types.ModuleType("bpy.app.handlers")
    bpy_handlers.frame_change_post = handlers.frame_change_post
    bpy_handlers.persistent = lambda func: func

    sys.modules["bpy"] = bpy
    sys.modules["bpy.app"] = bpy_app
    sys.modules["bpy.app.handlers"] = bpy_handlers
    return bpy


def _install_mathutils_stub():
    mathutils = types.ModuleType("mathutils")

    class Matrix:
        @staticmethod
        def Rotation(_angle, _size, _axis):
            return Matrix()

        @staticmethod
        def Identity(_size):
            return Matrix()

        def __matmul__(self, _other):
            return Matrix()

        def copy(self):
            return Matrix()

        def inverted_safe(self):
            return Matrix()

    class Vector:
        def __init__(self, values):
            self.x = float(values[0])
            self.y = float(values[1])
            self.z = float(values[2])

        @property
        def length(self):
            return (self.x * self.x + self.y * self.y + self.z * self.z) ** 0.5

        def normalize(self):
            length = self.length
            if length > 1e-12:
                self.x /= length
                self.y /= length
                self.z /= length

        def __iter__(self):
            return iter((self.x, self.y, self.z))

    mathutils.Matrix = Matrix
    mathutils.Vector = Vector
    sys.modules["mathutils"] = mathutils


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


if "witcher3_tools" not in sys.modules:
    package = types.ModuleType("witcher3_tools")
    package.__path__ = [str(PACKAGE_ROOT)]
    sys.modules["witcher3_tools"] = package
if "witcher3_tools.physics" not in sys.modules:
    physics_package = types.ModuleType("witcher3_tools.physics")
    physics_package.__path__ = [str(PHYSICS_ROOT)]
    sys.modules["witcher3_tools.physics"] = physics_package
    setattr(sys.modules["witcher3_tools"], "physics", physics_package)

bpy = _install_bpy_stub()
_install_mathutils_stub()
_load_module("witcher3_tools.physics.dyng", PHYSICS_ROOT / "dyng.py")
_load_module("witcher3_tools.physics.presets", PHYSICS_ROOT / "presets.py")
_load_module("witcher3_tools.physics.breast", PHYSICS_ROOT / "breast.py")
dyng_blender = _load_module("witcher3_tools.physics.dyng_blender", PHYSICS_ROOT / "dyng_blender.py")
breast_blender = _load_module("witcher3_tools.physics.breast_blender", PHYSICS_ROOT / "breast_blender.py")
armature_merge = _load_module("witcher3_tools.armature_merge", PACKAGE_ROOT / "armature_merge.py")


class FakeObject(dict):
    type = "ARMATURE"

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.name_full = name
        self.data = types.SimpleNamespace()


class FakePoseBone:
    def __init__(self, owner, name, constraints):
        self.id_data = owner
        self.name = name
        self.constraints = constraints


class FakeConstraint:
    def __init__(self, ctype="COPY_TRANSFORMS", target=None, subtarget="", name="Copy Transforms"):
        self.type = ctype
        self.target = target
        self.subtarget = subtarget
        self.name = name
        self.influence = 1.0
        self.mute = False


class FakeConstraints(list):
    def new(self, type):
        constraint = FakeConstraint(type)
        self.append(constraint)
        return constraint


class FakeScene:
    def __init__(self, frame_current=42):
        self.frame_current = frame_current
        self.frame_start = 1
        self.render = types.SimpleNamespace(fps=30, fps_base=1.0)
        self.frame_history = []

    def frame_set(self, frame):
        self.frame_history.append(int(frame))
        self.frame_current = int(frame)


class FakeBakePoint:
    def __init__(self, frame, value=0.0):
        self.co = (float(frame), float(value))


class FakeBakeFCurve:
    def __init__(self, data_path, array_index, frames):
        self.data_path = data_path
        self.array_index = int(array_index)
        self.keyframe_points = [FakeBakePoint(frame) for frame in frames]

    def range(self):
        frames = [point.co[0] for point in self.keyframe_points]
        return min(frames), max(frames)


class FakeBakeAction(dict):
    def __init__(self, name, fcurves=(), *, users=1, slots=None):
        super().__init__()
        self.name = name
        self.fcurves = list(fcurves)
        self.users = users
        self.library = None
        self.is_library_indirect = False
        if slots is not None:
            self.slots = slots


class FakeBakePoseBone:
    def __init__(self, owner, name="dyng_test"):
        self.id_data = owner
        self.name = name
        self.constraints = []
        self.rotation_mode = "XYZ"
        self.location = (0.0, 0.0, 0.0)
        self.rotation_euler = (0.0, 0.0, 0.0)
        self.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
        self.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
        self.scale = (1.0, 1.0, 1.0)

    def path_from_id(self, prop):
        return f'pose.bones["{self.name}"].{prop}'


def _fake_bake_resource_and_object(name="dyng_bake_test"):
    obj = FakeObject(name)
    bone = FakeBakePoseBone(obj)
    obj.pose = types.SimpleNamespace(bones=FakePoseBones([bone]))
    node = types.SimpleNamespace(name=bone.name, distance=0.2)
    resource = dyng_blender.DyngResourceData("", "test", (node,), (), (), ())
    return obj, bone, resource


def _fake_transform_curves(bone, frames):
    curves = []
    for prop, size in (("location", 3), ("rotation_euler", 3), ("scale", 3)):
        data_path = bone.path_from_id(prop)
        curves.extend(FakeBakeFCurve(data_path, index, frames) for index in range(size))
    return curves


class FakePoseBones(list):
    def get(self, name):
        for bone in self:
            if bone.name == name:
                return bone
        return None


class PreMergedPhysicsTests(unittest.TestCase):
    def test_dangle_driver_armatures_are_not_merge_targets(self):
        driver = FakeObject("dyng_driver")
        driver["witcher_type"] = "CAnimDangleConstraint_Dyng"

        self.assertTrue(armature_merge._is_dangle_constraint_armature(driver))
        self.assertFalse(armature_merge._is_dangle_constraint_armature(FakeObject("body")))

    def test_dangle_buffer_armatures_are_not_merge_targets(self):
        buffer = FakeObject("dangle_buffer")
        buffer["witcher_type"] = "CAnimDangleBufferComponent"

        self.assertTrue(armature_merge._is_dangle_buffer_armature(buffer))
        self.assertFalse(armature_merge._is_dangle_buffer_armature(FakeObject("body")))

    def test_dangle_driver_constraints_are_copied_to_merged_bones(self):
        driver = FakeObject("dyng_driver")
        driver["witcher_type"] = "CAnimDangleConstraint_Dyng"
        master = FakeObject("master")
        child = FakeObject("child")
        master_bone = FakePoseBone(master, "dyng_01", FakeConstraints())
        source_constraint = FakeConstraint(target=driver, subtarget="dyng_01", name="W3_DANGLE_dyng_01")
        child_bone = FakePoseBone(child, "dyng_01", FakeConstraints([source_constraint]))
        master.pose = types.SimpleNamespace(bones=FakePoseBones([master_bone]))
        child.pose = types.SimpleNamespace(bones=FakePoseBones([child_bone]))

        self.assertEqual(armature_merge._copy_dangle_driver_constraints_to_master(child, master), 1)
        copied = master_bone.constraints[0]
        self.assertEqual(copied.type, "COPY_TRANSFORMS")
        self.assertIs(copied.target, driver)
        self.assertEqual(copied.subtarget, "dyng_01")
        self.assertEqual(copied.name, "W3_DANGLE_dyng_01")
        self.assertEqual(armature_merge._copy_dangle_driver_constraints_to_master(child, master), 0)

    def test_non_dangle_constraints_are_not_copied_to_merged_bones(self):
        regular_target = FakeObject("regular")
        master = FakeObject("master")
        child = FakeObject("child")
        master_bone = FakePoseBone(master, "spine", FakeConstraints())
        child_bone = FakePoseBone(child, "spine", FakeConstraints([FakeConstraint(target=regular_target, subtarget="spine")]))
        master.pose = types.SimpleNamespace(bones=FakePoseBones([master_bone]))
        child.pose = types.SimpleNamespace(bones=FakePoseBones([child_bone]))

        self.assertEqual(armature_merge._copy_dangle_driver_constraints_to_master(child, master), 0)
        self.assertEqual(master_bone.constraints, [])

    def test_dyng_driver_constraints_can_be_added_to_merged_armature(self):
        driver = FakeObject("dyng_driver")
        driver["witcher_type"] = "CAnimDangleConstraint_Dyng"
        master = FakeObject("master")
        master_bone = FakePoseBone(master, "dyng_01", FakeConstraints())
        master.pose = types.SimpleNamespace(bones=FakePoseBones([master_bone]))
        driver.pose = types.SimpleNamespace(bones=FakePoseBones([
            FakePoseBone(driver, "dyng_01", FakeConstraints()),
            FakePoseBone(driver, "spine", FakeConstraints()),
        ]))

        self.assertEqual(armature_merge.copy_dangle_driver_constraints_to_armature(master, driver), 1)
        copied = master_bone.constraints[0]
        self.assertIs(copied.target, driver)
        self.assertEqual(copied.subtarget, "dyng_01")
        self.assertEqual(copied.name, "W3_DANGLE_dyng_01")
        self.assertEqual(armature_merge.copy_dangle_driver_constraints_to_armature(master, driver), 0)

    def test_breast_driver_constraints_can_be_added_to_merged_armature(self):
        driver = FakeObject("breast_driver")
        driver["witcher_type"] = "CAnimDangleConstraint_Breast"
        master = FakeObject("master")
        l_boob = FakePoseBone(master, "l_boob", FakeConstraints())
        spine = FakePoseBone(master, "spine", FakeConstraints())
        master.pose = types.SimpleNamespace(bones=FakePoseBones([l_boob, spine]))
        driver.pose = types.SimpleNamespace(bones=FakePoseBones([
            FakePoseBone(driver, "l_boob", FakeConstraints()),
            FakePoseBone(driver, "spine", FakeConstraints()),
        ]))

        self.assertEqual(armature_merge.copy_dangle_driver_constraints_to_armature(master, driver), 1)
        self.assertEqual(l_boob.constraints[0].subtarget, "l_boob")
        self.assertEqual(spine.constraints, [])

    def test_dangle_anchor_constraints_are_added_to_driver_only(self):
        driver = FakeObject("dyng_driver")
        driver["witcher_type"] = "CAnimDangleConstraint_Dyng"
        master = FakeObject("master")
        master.pose = types.SimpleNamespace(bones=FakePoseBones([
            FakePoseBone(master, "head", FakeConstraints()),
            FakePoseBone(master, "dyng_01", FakeConstraints()),
        ]))
        head = FakePoseBone(driver, "head", FakeConstraints())
        dyng = FakePoseBone(driver, "dyng_01", FakeConstraints())
        driver.pose = types.SimpleNamespace(bones=FakePoseBones([head, dyng]))

        self.assertEqual(armature_merge.copy_dangle_anchor_constraints_to_driver(driver, master), 1)
        self.assertIs(head.constraints[0].target, master)
        self.assertEqual(head.constraints[0].subtarget, "head")
        self.assertEqual(dyng.constraints, [])
        self.assertEqual(armature_merge.copy_dangle_anchor_constraints_to_driver(driver, master), 0)

    def test_dangle_buffer_anchor_constraints_skip_dynamic_bones(self):
        buffer = FakeObject("buffer")
        master = FakeObject("master")
        pelvis = FakePoseBone(buffer, "pelvis", FakeConstraints())
        dyng = FakePoseBone(buffer, "dyng_dagger_01", FakeConstraints())
        boob = FakePoseBone(buffer, "l_boob", FakeConstraints())
        buffer.pose = types.SimpleNamespace(bones=FakePoseBones([pelvis, dyng, boob]))
        master.pose = types.SimpleNamespace(bones=FakePoseBones([
            FakePoseBone(master, "pelvis", FakeConstraints()),
            FakePoseBone(master, "dyng_dagger_01", FakeConstraints()),
            FakePoseBone(master, "l_boob", FakeConstraints()),
        ]))

        self.assertEqual(armature_merge.copy_dangle_anchor_constraints_to_armature(buffer, master), 1)
        self.assertIs(pelvis.constraints[0].target, master)
        self.assertEqual(pelvis.constraints[0].subtarget, "pelvis")
        self.assertEqual(dyng.constraints, [])
        self.assertEqual(boob.constraints, [])


class RuntimeOptInTests(unittest.TestCase):
    def setUp(self):
        bpy.data.objects = []
        bpy.app.handlers.frame_change_post.clear()
        setattr(bpy.context.scene, dyng_blender.SCENE_LIVE_PREVIEW_ATTR, True)
        dyng_blender._RESOURCE_CACHES.clear()
        dyng_blender._RUNTIME_OBJECT_NAMES.clear()
        breast_blender._RUNTIME_OBJECT_NAMES.clear()

    def test_legacy_dyng_enabled_prop_does_not_activate_runtime(self):
        obj = FakeObject("legacy_dyng")
        obj[dyng_blender.DYNG_DATA_PROP] = "{}"
        obj[dyng_blender.DYNG_ENABLED_PROP] = True
        bpy.data.objects = [obj]

        dyng_blender.ensure_default_props(obj)

        self.assertFalse(dyng_blender.is_dyng_runtime_enabled(obj))
        self.assertEqual(dyng_blender.enabled_dyng_objects(bpy.context.scene), [])

    def test_saved_dyng_opt_in_rebuilds_active_runtime_name(self):
        obj = FakeObject("explicit_dyng")
        obj[dyng_blender.DYNG_DATA_PROP] = "{}"
        obj[dyng_blender.DYNG_ENABLED_PROP] = True
        obj[dyng_blender.DYNG_RUNTIME_OPT_IN_PROP] = True
        bpy.data.objects = [obj]

        self.assertTrue(dyng_blender.is_dyng_runtime_enabled(obj))
        self.assertEqual(dyng_blender.enabled_dyng_objects(bpy.context.scene), [obj])
        dyng_blender.ensure_frame_handler()
        self.assertIn(dyng_blender._state_key(obj), dyng_blender._RUNTIME_OBJECT_NAMES)

    def test_legacy_breast_enabled_prop_does_not_activate_runtime(self):
        obj = FakeObject("legacy_breast")
        obj["witcher_type"] = "CAnimDangleConstraint_Breast"
        obj[breast_blender.BREAST_ENABLED_PROP] = True
        bpy.data.objects = [obj]

        breast_blender.ensure_default_props(obj)

        self.assertFalse(breast_blender.is_breast_runtime_enabled(obj))
        self.assertEqual(breast_blender.enabled_breast_objects(bpy.context.scene), [])

    def test_saved_breast_opt_in_rebuilds_active_runtime_name(self):
        obj = FakeObject("explicit_breast")
        obj["witcher_type"] = "CAnimDangleConstraint_Breast"
        obj[breast_blender.BREAST_ENABLED_PROP] = True
        obj[breast_blender.BREAST_RUNTIME_OPT_IN_PROP] = True
        bpy.data.objects = [obj]

        self.assertTrue(breast_blender.is_breast_runtime_enabled(obj))
        self.assertEqual(breast_blender.enabled_breast_objects(bpy.context.scene), [obj])
        breast_blender.ensure_frame_handler()
        self.assertIn(breast_blender._state_key(obj), breast_blender._RUNTIME_OBJECT_NAMES)

    def test_imported_dyng_defaults_to_live_runtime_enabled(self):
        obj = FakeObject("imported_dyng")
        obj[dyng_blender.DYNG_DATA_PROP] = "{}"
        bpy.data.objects = [obj]
        setattr(bpy.context.scene, dyng_blender.SCENE_LIVE_PREVIEW_ATTR, False)

        dyng_blender.configure_imported_dyng(obj)

        self.assertTrue(bpy.context.scene.witcher_physics_live_preview_enabled)
        self.assertTrue(obj[dyng_blender.DYNG_ENABLED_PROP])
        self.assertTrue(obj[dyng_blender.DYNG_RUNTIME_OPT_IN_PROP])
        self.assertEqual(obj[dyng_blender.DYNG_BLEND_PROP], 1.0)
        self.assertTrue(obj[dyng_blender.DYNG_ACCESSORY_PREVIEW_PROP])
        self.assertTrue(dyng_blender.is_dyng_runtime_enabled(obj))
        self.assertEqual(bpy.app.handlers.frame_change_post, [dyng_blender.dyng_frame_change_post])

    def test_imported_breast_defaults_to_live_runtime_enabled(self):
        obj = FakeObject("imported_breast")
        obj["witcher_type"] = "CAnimDangleConstraint_Breast"
        obj.pose = types.SimpleNamespace(bones={"l_boob": object(), "r_boob": object()})
        bpy.data.objects = [obj]
        setattr(bpy.context.scene, dyng_blender.SCENE_LIVE_PREVIEW_ATTR, False)

        breast_blender.configure_imported_breast(obj)

        self.assertTrue(bpy.context.scene.witcher_physics_live_preview_enabled)
        self.assertTrue(obj[breast_blender.BREAST_ENABLED_PROP])
        self.assertTrue(obj[breast_blender.BREAST_RUNTIME_OPT_IN_PROP])
        self.assertTrue(breast_blender.is_breast_runtime_enabled(obj))
        self.assertEqual(bpy.app.handlers.frame_change_post, [breast_blender.breast_frame_change_post])

    def test_restore_dyng_runtime_ready_objects_from_disabled_default_window(self):
        obj = FakeObject("ready_dyng")
        obj[dyng_blender.DYNG_DATA_PROP] = "{}"
        obj[dyng_blender.DYNG_ENABLED_PROP] = False
        obj[dyng_blender.DYNG_RUNTIME_OPT_IN_PROP] = False
        obj[dyng_blender.DYNG_SIM_STATUS_PROP] = "Dyng runtime ready"
        bpy.data.objects = [obj]

        restored = dyng_blender.restore_import_default_runtime(bpy.context.scene)

        self.assertEqual(restored, 1)
        self.assertTrue(obj[dyng_blender.DYNG_ENABLED_PROP])
        self.assertTrue(obj[dyng_blender.DYNG_RUNTIME_OPT_IN_PROP])
        self.assertTrue(dyng_blender.is_dyng_runtime_enabled(obj))
        self.assertEqual(bpy.app.handlers.frame_change_post, [dyng_blender.dyng_frame_change_post])

    def test_restore_breast_runtime_ready_objects_from_disabled_default_window(self):
        obj = FakeObject("ready_breast")
        obj["witcher_type"] = "CAnimDangleConstraint_Breast"
        obj.pose = types.SimpleNamespace(bones={"l_boob": object(), "r_boob": object()})
        obj[breast_blender.BREAST_ENABLED_PROP] = False
        obj[breast_blender.BREAST_RUNTIME_OPT_IN_PROP] = False
        obj[breast_blender.BREAST_SIM_STATUS_PROP] = "Breast runtime ready"
        bpy.data.objects = [obj]

        restored = breast_blender.restore_import_default_runtime(bpy.context.scene)

        self.assertEqual(restored, 1)
        self.assertTrue(obj[breast_blender.BREAST_ENABLED_PROP])
        self.assertTrue(obj[breast_blender.BREAST_RUNTIME_OPT_IN_PROP])
        self.assertTrue(breast_blender.is_breast_runtime_enabled(obj))
        self.assertEqual(bpy.app.handlers.frame_change_post, [breast_blender.breast_frame_change_post])

    def test_dyng_runtime_does_not_write_back_to_external_copy_sources(self):
        self.assertFalse(hasattr(dyng_blender, "_write_desired_to_external_copy_targets"))

    def test_dyng_runtime_does_not_double_convert_rot90_resource_offsets(self):
        self.assertFalse(hasattr(dyng_blender, "_rot90_converted_resource"))

    def test_dyng_runtime_defaults_to_rot90_enabled(self):
        obj = FakeObject("dyng_rot90_default")

        self.assertTrue(dyng_blender._rig_uses_rot90(obj))

    def test_dyng_runtime_respects_explicit_rot90_off(self):
        obj = FakeObject("dyng_rot90_off")
        obj.data.witcherui_RigSettings = types.SimpleNamespace(
            rot90_imported=False,
            rot90_compensate=False,
            rot90_state="OFF",
        )

        self.assertFalse(dyng_blender._rig_uses_rot90(obj))

    def test_breast_runtime_defaults_to_rot90_enabled(self):
        obj = FakeObject("breast_rot90_default")

        self.assertTrue(breast_blender._rig_uses_rot90(obj))

    def test_breast_runtime_respects_explicit_rot90_off(self):
        obj = FakeObject("breast_rot90_off")
        obj.data.witcherui_RigSettings = types.SimpleNamespace(
            rot90_imported=False,
            rot90_compensate=False,
            rot90_state="OFF",
        )

        self.assertFalse(breast_blender._rig_uses_rot90(obj))

    def test_breast_state_key_rebuilds_when_rot90_changes(self):
        obj = FakeObject("breast_rot90_state")
        obj["witcher_type"] = "CAnimDangleConstraint_Breast"
        obj.data.witcherui_RigSettings = types.SimpleNamespace(rot90_state="ON")
        parent_transforms = {"l_boob": (), "r_boob": ()}
        original_simulator = breast_blender.BreastSimulator
        original_local_transforms = breast_blender._local_transforms
        try:
            class FakeBreastSimulator:
                def __init__(self, _local_transforms, _settings):
                    pass

                def reset(self, _parent_transforms):
                    pass

                def set_settings(self, _settings):
                    pass

            breast_blender._STATES.clear()
            breast_blender.BreastSimulator = FakeBreastSimulator
            breast_blender._local_transforms = lambda _obj: parent_transforms

            state_on = breast_blender._get_state(obj, parent_transforms=parent_transforms)
            obj.data.witcherui_RigSettings = types.SimpleNamespace(
                rot90_imported=False,
                rot90_compensate=False,
                rot90_state="OFF",
            )
            state_off = breast_blender._get_state(obj, parent_transforms=parent_transforms)
        finally:
            breast_blender.BreastSimulator = original_simulator
            breast_blender._local_transforms = original_local_transforms
            breast_blender._STATES.clear()

        self.assertIsNot(state_on, state_off)

    def test_dyng_runtime_uses_solver_rotation(self):
        self.assertFalse(hasattr(dyng_blender, "_blender_matrix_from_simulation"))
        self.assertFalse(hasattr(dyng_blender, "_matrix_aim_y_at"))

    def test_dyng_resource_cache_reuses_parsed_payload(self):
        obj = FakeObject("cached_dyng")
        obj[dyng_blender.DYNG_DATA_PROP] = "payload-a"
        resource = dyng_blender.DyngResourceData("", "cached", (), (), (), ())
        calls = []
        original = dyng_blender.resource_from_object
        try:
            def fake_resource_from_object(_obj):
                calls.append(_obj)
                return resource

            dyng_blender.resource_from_object = fake_resource_from_object

            self.assertIs(dyng_blender.load_resource_for_object(obj), resource)
            self.assertIs(dyng_blender.load_resource_for_object(obj), resource)
        finally:
            dyng_blender.resource_from_object = original

        self.assertEqual(len(calls), 1)

    def test_dyng_resource_cache_invalidates_when_payload_changes(self):
        obj = FakeObject("cached_dyng")
        obj[dyng_blender.DYNG_DATA_PROP] = "payload-a"
        resources = [
            dyng_blender.DyngResourceData("", "first", (), (), (), ()),
            dyng_blender.DyngResourceData("", "second", (), (), (), ()),
        ]
        original = dyng_blender.resource_from_object
        try:
            def fake_resource_from_object(_obj):
                return resources.pop(0)

            dyng_blender.resource_from_object = fake_resource_from_object

            first = dyng_blender.load_resource_for_object(obj)
            obj[dyng_blender.DYNG_DATA_PROP] = "payload-b"
            second = dyng_blender.load_resource_for_object(obj)
        finally:
            dyng_blender.resource_from_object = original

        self.assertEqual(first.name, "first")
        self.assertEqual(second.name, "second")

    def test_resource_offsets_do_not_override_component_default(self):
        obj = FakeObject("raw_dyng")
        resource = types.SimpleNamespace(
            nodes=(types.SimpleNamespace(distance=0.2),),
            triangles=(),
            collisions=(types.SimpleNamespace(transform=((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0.2, 0, 1))),),
        )

        dyng_blender._ensure_resource_default_props(obj, resource)

        self.assertFalse(obj[dyng_blender.DYNG_USE_OFFSETS_PROP])
        self.assertFalse(obj[dyng_blender.DYNG_PLANE_COLLISION_PROP])
        self.assertEqual(obj[dyng_blender.DYNG_WIND_PROP], 0.0)
        self.assertEqual(obj[dyng_blender.DYNG_BLEND_PROP], 1.0)
        self.assertTrue(obj[dyng_blender.DYNG_ACCESSORY_PREVIEW_PROP])

        self.assertTrue(dyng_blender._resource_has_authored_offsets(resource))

    def test_identity_dynamic_offsets_remain_disabled_by_default(self):
        obj = FakeObject("plain_dyng")
        resource = types.SimpleNamespace(
            nodes=(types.SimpleNamespace(distance=0.2),),
            triangles=(),
            collisions=(types.SimpleNamespace(transform=((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1))),),
        )

        dyng_blender._ensure_resource_default_props(obj, resource)

        self.assertFalse(obj[dyng_blender.DYNG_USE_OFFSETS_PROP])

    def test_explicit_offset_setting_is_not_replaced_by_resource_default(self):
        obj = FakeObject("explicit_offsets_off")
        obj[dyng_blender.DYNG_USE_OFFSETS_PROP] = False
        resource = types.SimpleNamespace(
            nodes=(types.SimpleNamespace(distance=0.2),),
            triangles=(),
            collisions=(types.SimpleNamespace(transform=((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0.2, 0, 1))),),
        )

        dyng_blender._ensure_resource_default_props(obj, resource)

        self.assertFalse(obj[dyng_blender.DYNG_USE_OFFSETS_PROP])

    def test_ciri_hair_preset_enables_authored_offsets(self):
        preset = dyng_blender._user_preset_by_name("Ciri_Hair_Reactive")

        self.assertIsNotNone(preset)
        self.assertTrue(preset.use_offsets)

    def test_hair_like_dyng_resource_defaults_to_full_blend(self):
        obj = FakeObject("hair_dyng")
        resource = types.SimpleNamespace(nodes=(object(),), triangles=(object(),), collisions=())

        dyng_blender._ensure_resource_default_props(obj, resource)

        self.assertEqual(obj[dyng_blender.DYNG_BLEND_PROP], 1.0)

    def test_weighted_accessory_preset_opts_into_accessory_preview(self):
        obj = FakeObject("accessory_dyng")
        obj[dyng_blender.DYNG_DATA_PROP] = "{}"
        resource = dyng_blender.DyngResourceData("", "accessory", (), (), (), ())
        original = dyng_blender.load_resource_for_object
        try:
            dyng_blender.load_resource_for_object = lambda _obj: resource

            self.assertTrue(dyng_blender.apply_user_preset(obj, "Weighted_Accessory"))
        finally:
            dyng_blender.load_resource_for_object = original

        self.assertTrue(obj[dyng_blender.DYNG_ACCESSORY_PREVIEW_PROP])

    def test_breast_user_presets_are_saved_json_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "presets.json"
            store_module = breast_blender.physics_presets
            original_store_path = store_module._store_path
            store_module._store_path = lambda: str(path)
            try:
                self.assertEqual(breast_blender.user_preset_names(), ())

                store_module.save_preset("breast", "My Breast Preset", {"simTime": 0.2})

                self.assertEqual(breast_blender.user_preset_names(), ("My Breast Preset",))
            finally:
                store_module._store_path = original_store_path

    def test_breast_available_presets_include_custom_redkit_then_saved_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "presets.json"
            store_module = breast_blender.physics_presets
            original_store_path = store_module._store_path
            store_module._store_path = lambda: str(path)
            try:
                store_module.save_preset("breast", "My Breast Preset", {"simTime": 0.2})

                self.assertEqual(
                    breast_blender.available_preset_names(),
                    (
                        breast_blender.CUSTOM_PRESET_NAME,
                        "Default_Naked",
                        "Natural_Normal",
                        "Unnatural",
                        "Clothed",
                        "My Breast Preset",
                    ),
                )
            finally:
                store_module._store_path = original_store_path

    def test_breast_apply_preset_loads_saved_json_preset(self):
        obj = FakeObject("breast")
        obj["witcher_type"] = "CAnimDangleConstraint_Breast"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "presets.json"
            store_module = breast_blender.physics_presets
            original_store_path = store_module._store_path
            store_module._store_path = lambda: str(path)
            try:
                store_module.save_preset("breast", "My Breast Preset", {"simTime": 0.2, "gravity": -0.03})

                self.assertTrue(breast_blender.apply_preset(obj, "My Breast Preset"))

                self.assertEqual(obj[breast_blender.BREAST_PRESET_PROP], "My Breast Preset")
                self.assertAlmostEqual(obj[breast_blender.BREAST_SIM_TIME_PROP], 0.2)
                self.assertAlmostEqual(obj[breast_blender.BREAST_GRAVITY_PROP], -0.03)
            finally:
                store_module._store_path = original_store_path

    def test_breast_custom_preset_restores_imported_values_after_redkit_preset(self):
        obj = FakeObject("imported_breast")
        obj["witcher_type"] = "CAnimDangleConstraint_Breast"
        obj.pose = types.SimpleNamespace(bones={"l_boob": object(), "r_boob": object()})
        obj[breast_blender.BREAST_PRESET_PROP] = breast_blender.CUSTOM_PRESET_NAME
        obj[breast_blender.BREAST_SIM_TIME_PROP] = 0.032
        obj[breast_blender.BREAST_GRAVITY_PROP] = -0.0065
        obj[breast_blender.BREAST_MOVEMENT_WEIGHT_PROP] = 0.12

        breast_blender.configure_imported_breast(obj, enabled=False)
        self.assertTrue(breast_blender.apply_preset(obj, "Clothed"))
        self.assertAlmostEqual(obj[breast_blender.BREAST_GRAVITY_PROP], -0.001)
        self.assertTrue(breast_blender.apply_preset(obj, breast_blender.CUSTOM_PRESET_NAME))

        self.assertEqual(obj[breast_blender.BREAST_PRESET_PROP], breast_blender.CUSTOM_PRESET_NAME)
        self.assertAlmostEqual(obj[breast_blender.BREAST_SIM_TIME_PROP], 0.032)
        self.assertAlmostEqual(obj[breast_blender.BREAST_GRAVITY_PROP], -0.0065)
        self.assertAlmostEqual(obj[breast_blender.BREAST_MOVEMENT_WEIGHT_PROP], 0.12)

    def test_breast_reserved_preset_names_cannot_be_saved_as_user_presets(self):
        obj = FakeObject("breast")
        obj["witcher_type"] = "CAnimDangleConstraint_Breast"

        with self.assertRaisesRegex(ValueError, "Reserved Breast preset names"):
            breast_blender.save_user_preset(obj, "clothed")
        with self.assertRaisesRegex(ValueError, "Reserved Breast preset names"):
            breast_blender.save_user_preset(obj, breast_blender.CUSTOM_PRESET_NAME)

    def test_entity_dyng_constraint_settings_override_raw_resource_defaults(self):
        obj = FakeObject("entity_dyng")
        obj[dyng_blender.DYNG_USE_OFFSETS_PROP] = True
        obj[dyng_blender.DYNG_PLANE_COLLISION_PROP] = True
        resource = types.SimpleNamespace(nodes=(), triangles=(), collisions=())

        dyng_blender._ensure_resource_default_props(obj, resource)

        self.assertTrue(obj[dyng_blender.DYNG_USE_OFFSETS_PROP])
        self.assertTrue(obj[dyng_blender.DYNG_PLANE_COLLISION_PROP])

    def test_restore_external_constraints_only_touches_recorded_mutes(self):
        owner = FakeObject("dyng")
        target = FakeObject("source")
        constraint = types.SimpleNamespace(
            type="COPY_TRANSFORMS",
            target=target,
            subtarget="bone",
            name="Copy Transforms",
            mute=True,
        )
        pose_bone = FakePoseBone(owner, "dyng_bone", [constraint])
        owner.pose = types.SimpleNamespace(bones=[pose_bone])

        dyng_blender._restore_external_constraints(owner)

        self.assertTrue(constraint.mute)

    def test_dyng_frame_handler_registration_is_reload_safe(self):
        obj = FakeObject("explicit_dyng")
        obj[dyng_blender.DYNG_DATA_PROP] = "{}"
        obj[dyng_blender.DYNG_ENABLED_PROP] = True
        obj[dyng_blender.DYNG_RUNTIME_OPT_IN_PROP] = True
        bpy.data.objects = [obj]
        setattr(bpy.context.scene, dyng_blender.SCENE_LIVE_PREVIEW_ATTR, True)

        def stale_handler(_scene):
            pass

        stale_handler.__name__ = "dyng_frame_change_post"
        stale_handler.__module__ = "old_addon.dyng_blender"
        bpy.app.handlers.frame_change_post.append(stale_handler)

        dyng_blender.ensure_frame_handler()
        dyng_blender.ensure_frame_handler()

        self.assertEqual(bpy.app.handlers.frame_change_post, [dyng_blender.dyng_frame_change_post])

    def test_dyng_frame_handler_requires_live_preview_gate(self):
        obj = FakeObject("explicit_dyng")
        obj[dyng_blender.DYNG_DATA_PROP] = "{}"
        obj[dyng_blender.DYNG_ENABLED_PROP] = True
        obj[dyng_blender.DYNG_RUNTIME_OPT_IN_PROP] = True
        bpy.data.objects = [obj]
        dyng_blender._RUNTIME_OBJECT_NAMES.add(dyng_blender._state_key(obj))
        setattr(bpy.context.scene, dyng_blender.SCENE_LIVE_PREVIEW_ATTR, False)

        dyng_blender.ensure_frame_handler()

        self.assertEqual(bpy.app.handlers.frame_change_post, [])
        self.assertEqual(dyng_blender.enabled_dyng_objects(bpy.context.scene), [])

    def test_dyng_frame_handler_removes_itself_when_no_effective_targets(self):
        dyng_blender.ensure_frame_handler()

        dyng_blender.dyng_frame_change_post(bpy.context.scene)

        self.assertEqual(bpy.app.handlers.frame_change_post, [])

    def test_dyng_frame_handler_autostarts_from_saved_opt_in(self):
        obj = FakeObject("saved_dyng")
        obj[dyng_blender.DYNG_DATA_PROP] = "{}"
        obj[dyng_blender.DYNG_ENABLED_PROP] = True
        obj[dyng_blender.DYNG_RUNTIME_OPT_IN_PROP] = True
        bpy.data.objects = [obj]

        dyng_blender.ensure_frame_handler()

        self.assertEqual(bpy.app.handlers.frame_change_post, [dyng_blender.dyng_frame_change_post])
        self.assertIn(dyng_blender._state_key(obj), dyng_blender._RUNTIME_OBJECT_NAMES)

    def test_dyng_frame_dt_uses_memory_not_id_props(self):
        obj = FakeObject("dyng")
        scene = types.SimpleNamespace(
            frame_current=10,
            render=types.SimpleNamespace(fps=30, fps_base=1.0),
        )

        dt, reset = dyng_blender.frame_dt(scene, obj)
        scene.frame_current = 11
        next_dt, next_reset = dyng_blender.frame_dt(scene, obj)

        self.assertTrue(reset)
        self.assertFalse(next_reset)
        self.assertAlmostEqual(dt, 1.0 / 30.0)
        self.assertAlmostEqual(next_dt, 1.0 / 30.0)
        self.assertNotIn(dyng_blender.DYNG_LAST_FRAME_PROP, obj)

    def test_dyng_frame_dt_resets_on_skipped_playback_frames(self):
        obj = FakeObject("dyng")
        scene = types.SimpleNamespace(
            frame_current=10,
            render=types.SimpleNamespace(fps=30, fps_base=1.0),
        )

        dyng_blender.frame_dt(scene, obj)
        scene.frame_current = 12
        dt, reset = dyng_blender.frame_dt(scene, obj)

        self.assertTrue(reset)
        self.assertAlmostEqual(dt, 1.0 / 30.0)

    def test_scene_wind_speed_is_scaled_for_blender_preview(self):
        scene = types.SimpleNamespace()
        setattr(scene, dyng_blender.SCENE_WIND_ENABLED_ATTR, True)
        setattr(scene, dyng_blender.SCENE_WIND_SPEED_ATTR, 5.0)
        setattr(scene, dyng_blender.SCENE_WIND_DIRECTION_ATTR, (10.0, 0.0, 0.0))

        speed, direction = dyng_blender._scene_wind(scene)

        self.assertAlmostEqual(speed, 0.5)
        self.assertEqual(direction, (1.0, 0.0, 0.0))

    def test_live_preview_gate_accepts_scene_custom_property(self):
        scene = {dyng_blender.SCENE_LIVE_PREVIEW_ATTR: True}

        self.assertTrue(dyng_blender.live_preview_enabled(scene))

    def test_breast_frame_handler_registration_is_reload_safe(self):
        obj = FakeObject("explicit_breast")
        obj["witcher_type"] = "CAnimDangleConstraint_Breast"
        obj[breast_blender.BREAST_ENABLED_PROP] = True
        obj[breast_blender.BREAST_RUNTIME_OPT_IN_PROP] = True
        bpy.data.objects = [obj]
        setattr(bpy.context.scene, dyng_blender.SCENE_LIVE_PREVIEW_ATTR, True)

        def stale_handler(_scene):
            pass

        stale_handler.__name__ = "breast_frame_change_post"
        stale_handler.__module__ = "old_addon.breast_blender"
        bpy.app.handlers.frame_change_post.append(stale_handler)

        breast_blender.ensure_frame_handler()
        breast_blender.ensure_frame_handler()

        self.assertEqual(bpy.app.handlers.frame_change_post, [breast_blender.breast_frame_change_post])

    def test_breast_frame_handler_requires_live_preview_gate(self):
        obj = FakeObject("explicit_breast")
        obj["witcher_type"] = "CAnimDangleConstraint_Breast"
        obj[breast_blender.BREAST_ENABLED_PROP] = True
        obj[breast_blender.BREAST_RUNTIME_OPT_IN_PROP] = True
        bpy.data.objects = [obj]
        breast_blender._RUNTIME_OBJECT_NAMES.add(breast_blender._state_key(obj))
        setattr(bpy.context.scene, dyng_blender.SCENE_LIVE_PREVIEW_ATTR, False)

        breast_blender.ensure_frame_handler()

        self.assertEqual(bpy.app.handlers.frame_change_post, [])
        self.assertEqual(breast_blender.enabled_breast_objects(bpy.context.scene), [])

    def test_breast_frame_handler_removes_itself_when_no_effective_targets(self):
        breast_blender.ensure_frame_handler()

        breast_blender.breast_frame_change_post(bpy.context.scene)

        self.assertEqual(bpy.app.handlers.frame_change_post, [])

    def test_breast_frame_handler_autostarts_from_saved_opt_in(self):
        obj = FakeObject("saved_breast")
        obj["witcher_type"] = "CAnimDangleConstraint_Breast"
        obj[breast_blender.BREAST_ENABLED_PROP] = True
        obj[breast_blender.BREAST_RUNTIME_OPT_IN_PROP] = True
        bpy.data.objects = [obj]

        breast_blender.ensure_frame_handler()

        self.assertEqual(bpy.app.handlers.frame_change_post, [breast_blender.breast_frame_change_post])
        self.assertIn(breast_blender._state_key(obj), breast_blender._RUNTIME_OBJECT_NAMES)

    def test_breast_frame_dt_uses_memory_not_id_props(self):
        obj = FakeObject("breast")
        scene = types.SimpleNamespace(
            frame_current=10,
            render=types.SimpleNamespace(fps=30, fps_base=1.0),
        )

        dt, reset = breast_blender.frame_dt(scene, obj)
        scene.frame_current = 11
        next_dt, next_reset = breast_blender.frame_dt(scene, obj)

        self.assertTrue(reset)
        self.assertFalse(next_reset)
        self.assertAlmostEqual(dt, 1.0 / 30.0)
        self.assertAlmostEqual(next_dt, 1.0 / 30.0)
        self.assertNotIn(breast_blender.BREAST_LAST_FRAME_PROP, obj)

    def test_dyng_bake_restores_original_frame_on_step_error(self):
        obj = FakeObject("dyng_bake")
        scene = FakeScene(frame_current=42)
        context = types.SimpleNamespace(scene=scene)
        resource = dyng_blender.DyngResourceData("", "empty", (), (), (), ())
        original_load = dyng_blender.load_resource_for_object
        original_reset = dyng_blender.reset_object
        original_step = dyng_blender.step_object
        original_suppressed = dyng_blender._SUPPRESS_FRAME_HANDLER
        try:
            dyng_blender._SUPPRESS_FRAME_HANDLER = False
            dyng_blender.load_resource_for_object = lambda _obj: resource
            dyng_blender.reset_object = lambda *_args, **_kwargs: True

            def fail_step(*_args, **_kwargs):
                raise RuntimeError("step failed")

            dyng_blender.step_object = fail_step

            with self.assertRaisesRegex(RuntimeError, "step failed"):
                dyng_blender.bake_object(context, obj, 1, 2)
        finally:
            dyng_blender.load_resource_for_object = original_load
            dyng_blender.reset_object = original_reset
            dyng_blender.step_object = original_step
            dyng_blender._SUPPRESS_FRAME_HANDLER = original_suppressed

        self.assertEqual(scene.frame_current, 42)
        self.assertEqual(scene.frame_history[-1], 42)

    def test_dyng_managed_bake_requires_matching_object_and_action_id(self):
        class FakeActions(list):
            def get(self, name):
                return next((action for action in self if action.name == name), None)

        class FakeAction(dict):
            def __init__(self, name):
                super().__init__()
                self.name = name

        obj = FakeObject("dyng_managed")
        action = FakeAction("dyng_managed_DYNG_Bake")
        action[dyng_blender.DYNG_BAKE_GENERATED_PROP] = True
        action[dyng_blender.DYNG_BAKE_ID_PROP] = "bake-id"
        obj[dyng_blender.DYNG_BAKE_ACTION_PROP] = action.name
        obj[dyng_blender.DYNG_BAKE_ID_PROP] = "bake-id"
        obj.animation_data = types.SimpleNamespace(action=action, nla_tracks=[])
        resource = dyng_blender.DyngResourceData("", "empty", (), (), (), ())
        original_actions = getattr(bpy.data, "actions", None)
        original_load = dyng_blender.load_resource_for_object
        original_stats = dyng_blender._action_curve_stats
        try:
            bpy.data.actions = FakeActions([action])
            dyng_blender.load_resource_for_object = lambda _obj: resource
            dyng_blender._action_curve_stats = lambda *_args: (True, 3, 90, 1.0, 10.0)

            self.assertTrue(dyng_blender.bake_info(obj).managed)
            action.name = "renamed_DYNG_Bake"
            renamed_info = dyng_blender.bake_info(obj)
            self.assertTrue(renamed_info.managed)
            self.assertIs(renamed_info.action, action)
            self.assertEqual(obj[dyng_blender.DYNG_BAKE_ACTION_PROP], "dyng_managed_DYNG_Bake")
            obj[dyng_blender.DYNG_BAKE_ID_PROP] = "different-id"
            self.assertFalse(dyng_blender.bake_info(obj).managed)
        finally:
            if original_actions is None:
                delattr(bpy.data, "actions")
            else:
                bpy.data.actions = original_actions
            dyng_blender.load_resource_for_object = original_load
            dyng_blender._action_curve_stats = original_stats

    def test_dyng_delete_refuses_unmanaged_legacy_bake(self):
        obj = FakeObject("legacy_bake")
        original_info = dyng_blender.bake_info
        try:
            dyng_blender.bake_info = lambda _obj: dyng_blender.DyngBakeInfo(
                action=object(),
                legacy=True,
            )
            self.assertFalse(dyng_blender.delete_bake(obj))
        finally:
            dyng_blender.bake_info = original_info

        self.assertEqual(obj[dyng_blender.DYNG_BAKE_STATUS_PROP], "No managed bake to delete")

    def test_dyng_legacy_delete_refuses_shared_action(self):
        obj = FakeObject("legacy_shared")
        action = types.SimpleNamespace(users=2)
        obj.animation_data = types.SimpleNamespace(action=action)
        original_info = dyng_blender.bake_info
        try:
            dyng_blender.bake_info = lambda _obj: dyng_blender.DyngBakeInfo(
                action=action,
                legacy=True,
                active=True,
            )
            self.assertFalse(dyng_blender.delete_legacy_bake(obj))
        finally:
            dyng_blender.bake_info = original_info

    def test_dyng_bake_tracking_cleanup_includes_runtime_state(self):
        obj = FakeObject("tracked_bake")
        for key in (
            dyng_blender.DYNG_BAKE_ID_PROP,
            dyng_blender.DYNG_BAKE_ACTION_PROP,
            dyng_blender.DYNG_BAKE_PREVIOUS_ACTION_PROP,
            dyng_blender.DYNG_BAKE_PREVIOUS_SLOT_PROP,
            dyng_blender.DYNG_BAKE_PREVIOUS_RUNTIME_PROP,
        ):
            obj[key] = "value"

        dyng_blender._clear_bake_tracking(obj)

        for key in (
            dyng_blender.DYNG_BAKE_ID_PROP,
            dyng_blender.DYNG_BAKE_ACTION_PROP,
            dyng_blender.DYNG_BAKE_PREVIOUS_ACTION_PROP,
            dyng_blender.DYNG_BAKE_PREVIOUS_SLOT_PROP,
            dyng_blender.DYNG_BAKE_PREVIOUS_RUNTIME_PROP,
        ):
            self.assertNotIn(key, obj)

    def test_blender_5_action_slot_identifier_is_preserved(self):
        slot = types.SimpleNamespace(identifier="OBdyng", handle=17)

        self.assertEqual(dyng_blender._action_slot_identifier(slot), "OBdyng")

    def test_blender_5_action_slot_name_display_is_preserved(self):
        slot = types.SimpleNamespace(name_display="Dyng Slot", handle=17)

        self.assertEqual(dyng_blender._action_slot_identifier(slot), "Dyng Slot")

    def test_assign_action_with_missing_slot_does_not_fall_back(self):
        obj = FakeObject("slot_target")
        original_action = object()
        original_slot = object()
        obj.animation_data = types.SimpleNamespace(action=original_action, action_slot=original_slot)
        action = FakeBakeAction(
            "layered",
            slots=[types.SimpleNamespace(identifier="OBfirst", name_display="First")],
        )

        self.assertFalse(dyng_blender._assign_action_with_slot(obj, action, "OBmissing"))
        self.assertIs(obj.animation_data.action, original_action)
        self.assertIs(obj.animation_data.action_slot, original_slot)

    def test_renamed_previous_action_recovers_only_exact_slot(self):
        class FakeActions(list):
            def get(self, name):
                return next((action for action in self if action.name == name), None)

        obj = FakeObject("renamed_source")
        obj[dyng_blender.DYNG_BAKE_PREVIOUS_ACTION_PROP] = "old_name"
        obj[dyng_blender.DYNG_BAKE_PREVIOUS_SLOT_PROP] = "OBsource"
        expected = FakeBakeAction(
            "new_name",
            slots=[types.SimpleNamespace(identifier="OBsource")],
        )
        unrelated = FakeBakeAction(
            "unrelated",
            slots=[types.SimpleNamespace(identifier="OBother")],
        )
        original_actions = getattr(bpy.data, "actions", None)
        try:
            bpy.data.actions = FakeActions([unrelated, expected])
            action, slot = dyng_blender._find_previous_action(obj)
        finally:
            if original_actions is None:
                delattr(bpy.data, "actions")
            else:
                bpy.data.actions = original_actions

        self.assertIs(action, expected)
        self.assertEqual(slot, "OBsource")

    def test_owned_bake_remains_deletable_when_resource_is_unavailable(self):
        class FakeActions(list):
            def get(self, name):
                return next((action for action in self if action.name == name), None)

        obj = FakeObject("resource_missing")
        action = FakeBakeAction("resource_missing_DYNG_Bake")
        action[dyng_blender.DYNG_BAKE_GENERATED_PROP] = True
        action[dyng_blender.DYNG_BAKE_ID_PROP] = "managed-id"
        action[dyng_blender.DYNG_BAKE_OWNER_PROP] = obj.name
        action[dyng_blender.DYNG_BAKE_FRAME_START_PROP] = -3
        action[dyng_blender.DYNG_BAKE_FRAME_END_PROP] = 2
        action[dyng_blender.DYNG_BAKE_BONE_COUNT_PROP] = 4
        action[dyng_blender.DYNG_BAKE_SAMPLE_COUNT_PROP] = 24
        action[dyng_blender.DYNG_BAKE_KEY_COUNT_PROP] = 240
        obj[dyng_blender.DYNG_BAKE_ACTION_PROP] = action.name
        obj[dyng_blender.DYNG_BAKE_ID_PROP] = "managed-id"
        obj[dyng_blender.DYNG_ENABLED_PROP] = True
        obj[dyng_blender.DYNG_RUNTIME_OPT_IN_PROP] = True
        obj.animation_data = types.SimpleNamespace(action=action, nla_tracks=[])
        original_actions = getattr(bpy.data, "actions", None)
        original_load = dyng_blender.load_resource_for_object
        try:
            bpy.data.actions = FakeActions([action])
            dyng_blender.load_resource_for_object = lambda _obj: None
            info = dyng_blender.bake_info(obj)
            deleted = dyng_blender.delete_bake(obj)
        finally:
            if original_actions is None:
                delattr(bpy.data, "actions")
            else:
                bpy.data.actions = original_actions
            dyng_blender.load_resource_for_object = original_load

        self.assertTrue(info.managed)
        self.assertFalse(info.verified)
        self.assertTrue(info.overridden)
        self.assertEqual((info.frame_start, info.frame_end), (-3, 2))
        self.assertEqual((info.bone_count, info.sample_count, info.key_count), (4, 24, 240))
        self.assertTrue(deleted)
        self.assertNotIn(dyng_blender.DYNG_BAKE_ID_PROP, obj)

    def test_managed_bake_validity_detects_missing_and_duplicate_curves(self):
        class FakeActions(list):
            def get(self, name):
                return next((action for action in self if action.name == name), None)

        obj, bone, resource = _fake_bake_resource_and_object("managed_validity")
        action = FakeBakeAction("managed_validity_DYNG_Bake", _fake_transform_curves(bone, (-2, 1, 2, 3, 7)))
        action[dyng_blender.DYNG_BAKE_GENERATED_PROP] = True
        action[dyng_blender.DYNG_BAKE_ID_PROP] = "validity-id"
        action[dyng_blender.DYNG_BAKE_OWNER_PROP] = obj.name
        action[dyng_blender.DYNG_BAKE_FRAME_START_PROP] = 1
        action[dyng_blender.DYNG_BAKE_FRAME_END_PROP] = 3
        action[dyng_blender.DYNG_BAKE_BONE_COUNT_PROP] = 1
        action[dyng_blender.DYNG_BAKE_SAMPLE_COUNT_PROP] = 3
        action[dyng_blender.DYNG_BAKE_KEY_COUNT_PROP] = 27
        obj[dyng_blender.DYNG_BAKE_ACTION_PROP] = action.name
        obj[dyng_blender.DYNG_BAKE_ID_PROP] = "validity-id"
        obj.animation_data = types.SimpleNamespace(action=action, nla_tracks=[])
        original_actions = getattr(bpy.data, "actions", None)
        original_load = dyng_blender.load_resource_for_object
        try:
            bpy.data.actions = FakeActions([action])
            dyng_blender.load_resource_for_object = lambda _obj: resource
            self.assertTrue(dyng_blender.bake_info(obj).valid)

            missing_point = action.fcurves[0].keyframe_points.pop(1)
            self.assertFalse(dyng_blender.bake_info(obj).valid)
            action.fcurves[0].keyframe_points.insert(1, missing_point)

            action.fcurves.append(FakeBakeFCurve(action.fcurves[0].data_path, 0, (1, 2, 3)))
            self.assertFalse(dyng_blender.bake_info(obj).valid)
        finally:
            if original_actions is None:
                delattr(bpy.data, "actions")
            else:
                bpy.data.actions = original_actions
            dyng_blender.load_resource_for_object = original_load

    def test_legacy_bake_signature_rejects_duplicate_channels(self):
        obj, bone, resource = _fake_bake_resource_and_object("legacy_signature")
        curves = _fake_transform_curves(bone, (1, 2, 3))
        curves.append(FakeBakeFCurve(curves[0].data_path, curves[0].array_index, (1, 2, 3)))
        action = FakeBakeAction(f"{obj.name}Action", curves)

        self.assertFalse(dyng_blender._is_legacy_bake_action(obj, resource, action, verify_all_frames=True))

    def test_cache_summary_ignores_stale_persisted_status(self):
        obj = FakeObject("stale_cache")
        obj[dyng_blender.DYNG_CACHE_STATUS_PROP] = "Cached 1-250"
        dyng_blender._FRAME_CACHES.pop(dyng_blender._state_key(obj), None)

        self.assertEqual(dyng_blender.cache_summary(obj), "No memory cache")

    def test_dyng_cache_restores_original_frame_on_step_error(self):
        obj = FakeObject("dyng_cache")
        scene = FakeScene(frame_current=37)
        context = types.SimpleNamespace(scene=scene)
        resource = dyng_blender.DyngResourceData("", "empty", (), (), (), ())
        original_load = dyng_blender.load_resource_for_object
        original_reset = dyng_blender.reset_object
        original_step = dyng_blender.step_object
        original_suppressed = dyng_blender._SUPPRESS_FRAME_HANDLER
        try:
            dyng_blender._SUPPRESS_FRAME_HANDLER = False
            dyng_blender.load_resource_for_object = lambda _obj: resource
            dyng_blender.reset_object = lambda *_args, **_kwargs: True

            def fail_step(*_args, **_kwargs):
                raise RuntimeError("cache failed")

            dyng_blender.step_object = fail_step

            with self.assertRaisesRegex(RuntimeError, "cache failed"):
                dyng_blender.build_cache_for_object(context, obj, 1, 2)
        finally:
            dyng_blender.load_resource_for_object = original_load
            dyng_blender.reset_object = original_reset
            dyng_blender.step_object = original_step
            dyng_blender._SUPPRESS_FRAME_HANDLER = original_suppressed

        self.assertEqual(scene.frame_current, 37)
        self.assertEqual(scene.frame_history[-1], 37)

    def test_dyng_cache_reversed_negative_range_resets_only_first_frame(self):
        obj = FakeObject("dyng_negative_cache")
        scene = FakeScene(frame_current=42)
        context = types.SimpleNamespace(scene=scene)
        resource = dyng_blender.DyngResourceData("", "empty", (), (), (), ())
        reset_frames = []
        step_frames = []
        original_load = dyng_blender.load_resource_for_object
        original_reset = dyng_blender.reset_object
        original_step = dyng_blender.step_object
        original_capture = dyng_blender._capture_dynamic_matrices
        try:
            dyng_blender.load_resource_for_object = lambda _obj: resource
            dyng_blender.reset_object = lambda *_args, **_kwargs: reset_frames.append(scene.frame_current) or True
            dyng_blender.step_object = lambda *_args, **_kwargs: step_frames.append(scene.frame_current) or True
            dyng_blender._capture_dynamic_matrices = lambda *_args: {}

            count = dyng_blender.build_cache_for_object(context, obj, 1, -3)
        finally:
            dyng_blender.load_resource_for_object = original_load
            dyng_blender.reset_object = original_reset
            dyng_blender.step_object = original_step
            dyng_blender._capture_dynamic_matrices = original_capture
            dyng_blender._FRAME_CACHES.pop(dyng_blender._state_key(obj), None)

        self.assertEqual(count, 5)
        self.assertEqual(reset_frames, [-3])
        self.assertEqual(step_frames, [-2, -1, 0, 1])
        self.assertEqual(scene.frame_current, 42)

    def test_breast_bake_restores_original_frame_on_step_error(self):
        obj = FakeObject("breast_bake")
        obj["witcher_type"] = "CAnimDangleConstraint_Breast"
        pose_bone = types.SimpleNamespace(
            rotation_mode="XYZ",
            keyframe_insert=lambda *_args, **_kwargs: None,
        )
        obj.pose = types.SimpleNamespace(bones={"l_boob": pose_bone, "r_boob": pose_bone})
        scene = FakeScene(frame_current=23)
        context = types.SimpleNamespace(scene=scene)
        original_reset = breast_blender.reset_object
        original_step = breast_blender.step_object
        original_suppressed = breast_blender._SUPPRESS_FRAME_HANDLER
        try:
            breast_blender._SUPPRESS_FRAME_HANDLER = False
            breast_blender.reset_object = lambda *_args, **_kwargs: True

            def fail_step(*_args, **_kwargs):
                raise RuntimeError("breast step failed")

            breast_blender.step_object = fail_step

            with self.assertRaisesRegex(RuntimeError, "breast step failed"):
                breast_blender.bake_object(context, obj, 1, 2)
        finally:
            breast_blender.reset_object = original_reset
            breast_blender.step_object = original_step
            breast_blender._SUPPRESS_FRAME_HANDLER = original_suppressed

        self.assertEqual(scene.frame_current, 23)
        self.assertEqual(scene.frame_history[-1], 23)


if __name__ == "__main__":
    unittest.main()
