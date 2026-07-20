import json
import math
import os
import pathlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "witcher3_tools"
if "witcher3_tools" not in sys.modules:
    package = types.ModuleType("witcher3_tools")
    package.__path__ = [str(PACKAGE_ROOT)]
    sys.modules["witcher3_tools"] = package

from witcher3_tools.physics import breast, dyng, presets as physics_presets
from witcher3_tools.CR2W import dc_skeleton

BreastSettings = breast.BreastSettings
BreastSimulator = breast.BreastSimulator
DyngResourceData = dyng.DyngResourceData
DyngSimulator = dyng.DyngSimulator
IDENTITY_MATRIX = dyng.IDENTITY_MATRIX
load_dyng_resource = dyng.load_dyng_resource
matrix_mul = dyng.matrix_mul
matrix_translation = dyng.matrix_translation
parse_dyng_chunk = dyng.parse_dyng_chunk
transform_from_axes = dyng.transform_from_axes


def assert_vector_almost_equal(testcase, actual, expected, places=6):
    testcase.assertEqual(len(actual), len(expected))
    for got, want in zip(actual, expected):
        testcase.assertAlmostEqual(got, want, places=places)


def axis_angle_between(a, b):
    a_len = sum(component * component for component in a) ** 0.5
    b_len = sum(component * component for component in b) ** 0.5
    if a_len <= 1e-12 or b_len <= 1e-12:
        return 0.0
    dot = sum(a[i] * b[i] for i in range(3)) / (a_len * b_len)
    dot = max(-1.0, min(1.0, dot))
    return math.acos(dot)


class FakeProp:
    def __init__(self, name, prop_type, **attrs):
        self.theName = name
        self.theType = prop_type
        for key, value in attrs.items():
            setattr(self, key, value)


class FakeChunk:
    name = "CDyngResource"
    Type = "CDyngResource"

    def __init__(self, props):
        self.PROPS = props

    def GetVariableByName(self, name):
        for prop in self.PROPS:
            if prop.theName == name:
                return prop
        return None


def matrix_element(matrix):
    row_names = ("X", "Y", "Z", "W")
    col_names = ("X", "Y", "Z", "W")
    row_props = []
    for row_name, row in zip(row_names, matrix):
        values = [
            FakeProp(col_name, "Float", Value=float(value))
            for col_name, value in zip(col_names, row)
        ]
        row_props.append(FakeProp(row_name, "Vector", More=values))
    return SimpleNamespace(MoreProps=row_props)


def fake_dyng_chunk():
    root = IDENTITY_MATRIX
    child = transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 0, 0))
    return FakeChunk(
        [
            FakeProp("nodeNames", "array:2,0,String", elements=["root", "dyng_child"]),
            FakeProp("nodeParents", "array:2,0,String", elements=["", "root"]),
            FakeProp("nodeMasses", "array:2,0,Float", value=[0.0, 1.0]),
            FakeProp("nodeStifnesses", "array:2,0,Float", value=[0.0, 0.5]),
            FakeProp("nodeDistances", "array:2,0,Float", value=[0.0, 1.0]),
            FakeProp("nodeTransforms", "array:2,0,Matrix", More=[matrix_element(root), matrix_element(child)]),
            FakeProp("linkTypes", "array:2,0,Int32", value=[0]),
            FakeProp("linkLengths", "array:2,0,Float", value=[1.0]),
            FakeProp("linkAs", "array:2,0,Int32", value=[0]),
            FakeProp("linkBs", "array:2,0,Int32", value=[1]),
            FakeProp("triangleAs", "array:2,0,Int32", value=[0]),
            FakeProp("triangleBs", "array:2,0,Int32", value=[1]),
            FakeProp("triangleCs", "array:2,0,Int32", value=[-1]),
            FakeProp("collisionParents", "array:2,0,String", elements=["root", "dyng_child"]),
            FakeProp("collisionRadiuses", "array:2,0,Float", value=[0.0, 0.0]),
            FakeProp("collisionHeights", "array:2,0,Float", value=[0.006, 0.006]),
            FakeProp("collisionTransforms", "array:2,0,Matrix", More=[matrix_element(root), matrix_element(root)]),
        ]
    )


def local_breast_transforms():
    return {
        "l_boob": transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (-0.1, 0.0, 0.0)),
        "r_boob": transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.1, 0.0, 0.0)),
    }


class BreastSettingsTests(unittest.TestCase):
    def test_default_fallback_uses_imported_scale(self):
        settings = BreastSettings.from_mapping({})

        self.assertEqual(settings.preset, "CUSTOM_PRESET")
        self.assertAlmostEqual(settings.sim_time, 0.01)
        self.assertAlmostEqual(settings.gravity, -0.006)
        self.assertAlmostEqual(settings.movement_bone_weight, 0.15)

    def test_preset_name_loads_redkit_values(self):
        settings = BreastSettings.from_mapping({"preset": "Clothed"})

        self.assertEqual(settings.preset, "Clothed")
        self.assertAlmostEqual(settings.vel_damp, 0.8)
        self.assertAlmostEqual(settings.movement_bone_weight, 0.05)
        self.assertAlmostEqual(settings.rotation_bone_weight, 0.05)

    def test_code_presets_are_redkit_presets_only(self):
        self.assertEqual(breast.BREAST_PRESETS, breast.REDKIT_BREAST_PRESETS)
        self.assertEqual(
            {preset.name for preset in breast.BREAST_PRESETS},
            {"Default_Naked", "Natural_Normal", "Unnatural", "Clothed"},
        )

    def test_unknown_named_preset_is_not_a_shipped_user_preset(self):
        settings = BreastSettings.from_mapping({"preset": "My JSON Preset"})

        self.assertEqual(settings.preset, "My JSON Preset")
        self.assertAlmostEqual(settings.sim_time, 0.01)
        self.assertAlmostEqual(settings.gravity, -0.006)
        self.assertAlmostEqual(settings.movement_bone_weight, 0.15)
        self.assertAlmostEqual(settings.rotation_bone_weight, 0.3)

    def test_custom_values_override_preset(self):
        settings = BreastSettings.from_mapping(
            {
                "preset": "Clothed",
                "gravity": -0.25,
                "ellipse": (0.0, 0.02, 0.5, 0.25),
                "blend": 0.5,
            }
        )

        self.assertEqual(settings.preset, "Clothed")
        self.assertAlmostEqual(settings.gravity, -0.25)
        self.assertEqual(settings.ellipse, (0.0, 0.02, 0.5, 0.25))
        self.assertAlmostEqual(settings.blend, 0.5)


class BreastSimulatorTests(unittest.TestCase):
    def test_simulator_is_deterministic_for_same_inputs(self):
        settings = BreastSettings.from_mapping({"preset": "Natural_Normal"})
        sim_a = BreastSimulator(local_breast_transforms(), settings)
        sim_b = BreastSimulator(local_breast_transforms(), settings)
        parents = {"l_boob": IDENTITY_MATRIX, "r_boob": IDENTITY_MATRIX}

        outputs_a = {}
        outputs_b = {}
        for index in range(5):
            outputs_a = sim_a.step(parents, 1.0 / 60.0, reset=index == 0)
            outputs_b = sim_b.step(parents, 1.0 / 60.0, reset=index == 0)

        self.assertEqual(outputs_a, outputs_b)

    def test_parent_motion_changes_simulated_output(self):
        settings = BreastSettings.from_mapping({"preset": "Natural_Normal"})
        calm = BreastSimulator(local_breast_transforms(), settings)
        moved = BreastSimulator(local_breast_transforms(), settings)
        parents = {"l_boob": IDENTITY_MATRIX, "r_boob": IDENTITY_MATRIX}
        shifted = {
            "l_boob": transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.2, 0.0, 0.0)),
            "r_boob": transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.2, 0.0, 0.0)),
        }

        calm.step(parents, 1.0 / 60.0, reset=True)
        moved.step(parents, 1.0 / 60.0, reset=True)
        calm_out = calm.step(parents, 1.0 / 60.0)
        moved_out = moved.step(shifted, 1.0 / 60.0)

        self.assertNotEqual(calm_out["l_boob"][3], moved_out["l_boob"][3])

    def test_higher_inertia_scaler_throws_the_mass_further(self):
        rest = {"l_boob": IDENTITY_MATRIX, "r_boob": IDENTITY_MATRIX}
        moved = {
            "l_boob": transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.0, 0.0, 0.2)),
            "r_boob": transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.0, 0.0, 0.2)),
        }

        def peak_response(scaler):
            settings = BreastSettings.from_mapping(
                {"preset": "Natural_Normal", "gravity": 0.0, "inertiaScaler": scaler}
            )
            simulator = BreastSimulator(local_breast_transforms(), settings)
            simulator.step(rest, 1.0 / 60.0, reset=True)
            rigid = matrix_translation(matrix_mul(local_breast_transforms()["l_boob"], moved["l_boob"]))
            best = 0.0
            for _ in range(6):
                out = matrix_translation(simulator.step(moved, 1.0 / 60.0)["l_boob"])
                best = max(best, sum((out[i] - rigid[i]) ** 2 for i in range(3)) ** 0.5)
            return best

        # inertiaScaler drives how hard skeletal acceleration throws the mass
        self.assertGreater(peak_response(3.0), peak_response(0.5) + 1e-6)

    def test_reset_step_stays_near_parented_rest_target(self):
        settings = BreastSettings.from_mapping({})
        simulator = BreastSimulator(local_breast_transforms(), settings)
        parent = transform_from_axes((0, 1, 0), (-1, 0, 0), (0, 0, 1), (0.25, -0.1, 1.25))
        parents = {"l_boob": parent, "r_boob": parent}

        outputs = simulator.step(parents, 1.0 / 30.0, reset=True)
        target = matrix_mul(local_breast_transforms()["l_boob"], parent)
        output_pos = matrix_translation(outputs["l_boob"])
        target_pos = matrix_translation(target)
        distance = sum((output_pos[i] - target_pos[i]) ** 2 for i in range(3)) ** 0.5

        self.assertLess(distance, 0.01)

    def test_gravity_moves_solver_point_at_imported_sim_time_scale(self):
        settings = BreastSettings.from_mapping({"simTime": 1.0, "gravity": -0.25})
        simulator = BreastSimulator(local_breast_transforms(), settings)
        parents = {"l_boob": IDENTITY_MATRIX, "r_boob": IDENTITY_MATRIX}

        before = matrix_translation(simulator.step(parents, 1.0 / 60.0, reset=True)["l_boob"])
        after = matrix_translation(simulator.step(parents, 1.0 / 60.0)["l_boob"])

        self.assertNotEqual(before, after)

    def test_default_sim_time_still_produces_visible_output_motion(self):
        settings = BreastSettings.from_mapping({"preset": "Natural_Normal"})
        simulator = BreastSimulator(local_breast_transforms(), settings)
        parents = {"l_boob": IDENTITY_MATRIX, "r_boob": IDENTITY_MATRIX}

        reset_out = simulator.step(parents, 1.0 / 60.0, reset=True)["l_boob"]
        moved_out = simulator.step(parents, 1.0 / 60.0)["l_boob"]
        reset_pos = matrix_translation(reset_out)
        moved_pos = matrix_translation(moved_out)
        distance = sum((moved_pos[i] - reset_pos[i]) ** 2 for i in range(3)) ** 0.5

        self.assertGreater(distance, 1e-5)

    def test_natural_normal_baseline_preserves_imported_motion_scale(self):
        settings = BreastSettings.from_mapping({"preset": "Natural_Normal"})
        simulator = BreastSimulator(local_breast_transforms(), settings)
        parents = {"l_boob": IDENTITY_MATRIX, "r_boob": IDENTITY_MATRIX}

        positions = []
        for frame in range(5):
            output = simulator.step(parents, 1.0 / 60.0, reset=frame == 0)["l_boob"]
            positions.append(matrix_translation(output))

        z_offsets = [abs(position[2]) for position in positions]
        self.assertGreater(z_offsets[-1], z_offsets[0])  # gravity sag develops
        self.assertLess(z_offsets[-1], 0.01)             # and stays small/bounded
        for position in positions:
            self.assertAlmostEqual(position[0], -0.1, places=6)

    def test_natural_normal_rotation_output_stays_near_legacy_scale(self):
        settings = BreastSettings.from_mapping({"preset": "Natural_Normal"})
        simulator = BreastSimulator(local_breast_transforms(), settings)
        parents = {"l_boob": IDENTITY_MATRIX, "r_boob": IDENTITY_MATRIX}

        output = IDENTITY_MATRIX
        for frame in range(30):
            output = simulator.step(parents, 1.0 / 60.0, reset=frame == 0)["l_boob"]

        target = matrix_mul(local_breast_transforms()["l_boob"], IDENTITY_MATRIX)
        angle = axis_angle_between(output[0][:3], target[0][:3])

        self.assertGreater(angle, 0.005)
        self.assertLess(angle, 0.08)

    def test_ellipse_response_keeps_public_output_bounded(self):
        settings = BreastSettings.from_mapping(
            {
                "simTime": 1.0,
                "ellipse": (0.0, 0.0, 0.1, 0.2),
                "inAcc": 1.0,
                "gravity": -0.5,
            }
        )
        simulator = BreastSimulator(local_breast_transforms(), settings)

        output = IDENTITY_MATRIX
        for frame in range(20):
            output = simulator.step({"l_boob": IDENTITY_MATRIX, "r_boob": IDENTITY_MATRIX}, 1.0 / 60.0, reset=frame == 0)["l_boob"]

        position = matrix_translation(output)
        self.assertLess(abs(position[1]), 0.1)
        self.assertLess(abs(position[2]), 0.1)

    def test_sim_point_stays_inside_authored_ellipse(self):
        # in_acc=1.0 in every shipped preset makes _project_inside a hard clamp,
        # so the lag point can never leave the ellipse even under big impulses.
        settings = BreastSettings.from_mapping({"preset": "Natural_Normal", "gravity": -2.0})
        simulator = BreastSimulator(local_breast_transforms(), settings)
        rest = {"l_boob": IDENTITY_MATRIX, "r_boob": IDENTITY_MATRIX}
        shoved = {
            "l_boob": transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.0, 5.0, 0.0)),
            "r_boob": transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.0, 5.0, 0.0)),
        }
        simulator.step(rest, 1.0 / 60.0, reset=True)
        cx, cy, rx, ry = settings.ellipse
        for _ in range(30):
            simulator.step(shoved, 1.0 / 60.0)
            for name in ("l_boob", "r_boob"):
                point = simulator.bones[name].point
                reach = ((point.lat - cx) / rx) ** 2 + ((point.lift - cy) / ry) ** 2
                self.assertLessEqual(reach, 1.0 + 1e-6, f"{name} escaped ellipse: {reach}")

    def test_presets_produce_divergent_trajectories(self):
        rest = {"l_boob": IDENTITY_MATRIX, "r_boob": IDENTITY_MATRIX}
        moved = {
            "l_boob": transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.0, 0.0, 0.3)),
            "r_boob": transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.0, 0.0, 0.3)),
        }

        def trajectory(preset_name):
            settings = BreastSettings.from_mapping({"preset": preset_name, "gravity": -0.05})
            simulator = BreastSimulator(local_breast_transforms(), settings)
            simulator.step(rest, 1.0 / 60.0, reset=True)
            return [
                matrix_translation(simulator.step(moved, 1.0 / 60.0)["l_boob"])
                for _ in range(8)
            ]

        names = [preset.name for preset in breast.BREAST_PRESETS]
        trajectories = {name: trajectory(name) for name in names}
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                self.assertNotEqual(
                    trajectories[names[i]],
                    trajectories[names[j]],
                    f"{names[i]} and {names[j]} produced identical motion",
                )



class DyngParserTests(unittest.TestCase):
    @staticmethod
    def _skeleton_chunk(names, parents, rig_data):
        return SimpleNamespace(
            name="CSkeleton",
            PROPS=[
                SimpleNamespace(
                    theName="bones",
                    More=[SimpleNamespace(elementName=name) for name in names],
                ),
                SimpleNamespace(theName="parentIndices", value=list(parents)),
            ],
            rigData=SimpleNamespace(rigData=list(rig_data)),
        )

    def test_non_finite_skeleton_rotation_uses_identity_default(self):
        rig_data = [SimpleNamespace(
            position=SimpleNamespace(x=1.0, y=2.0, z=3.0, w=1.0),
            rotation=SimpleNamespace(x=-math.inf, y=math.inf, z=-0.0, w=math.inf),
            scale=SimpleNamespace(x=1.0, y=1.0, z=1.0, w=1.0),
        )]

        with self.assertLogs(dc_skeleton.log.name, level="WARNING"):
            skeleton = dc_skeleton.read_skelly(
                self._skeleton_chunk(["tail"], [-1], rig_data)
            )

        self.assertEqual(
            (skeleton.positions[0].x, skeleton.positions[0].y, skeleton.positions[0].z),
            (1.0, 2.0, 3.0),
        )
        self.assertEqual(
            tuple(getattr(skeleton.rotations[0], axis) for axis in "XYZW"),
            (0.0, 0.0, 0.0, 1.0),
        )
        self.assertEqual(
            (skeleton.scales[0].x, skeleton.scales[0].y, skeleton.scales[0].z),
            (1.0, 1.0, 1.0),
        )

    def test_skeleton_rejects_invalid_parent_hierarchy(self):
        with self.assertRaisesRegex(ValueError, "invalid parent index"):
            dc_skeleton.read_skelly(
                self._skeleton_chunk(["root", "child"], [-1, 1], [])
            )

    def test_named_skeleton_rejects_missing_reference_pose(self):
        with self.assertRaisesRegex(ValueError, "no reference-pose transforms"):
            dc_skeleton.read_skelly(
                self._skeleton_chunk(["root"], [-1], [])
            )

    def test_witcher2_named_skeleton_keeps_legacy_default_pose(self):
        chunk = self._skeleton_chunk(["root"], [-1], [])
        setattr(
            chunk,
            "_W_CLASS__CR2WFILE",
            SimpleNamespace(
                HEADER=SimpleNamespace(version=115),
                fileName="<memory>",
            ),
        )

        skeleton = dc_skeleton.read_skelly(chunk)

        self.assertEqual(len(skeleton.positions), 1)
        self.assertEqual(len(skeleton.rotations), 1)
        self.assertEqual(len(skeleton.scales), 1)

    def test_flattened_skeleton_track_properties_prefer_cname_once(self):
        skeleton = dc_skeleton.read_skelly(SimpleNamespace(
            name="CSkeleton",
            PROPS=[SimpleNamespace(
                theName="tracks",
                More=[
                    SimpleNamespace(theName="name", ToString=lambda: "Track0"),
                    SimpleNamespace(theName="nameAsCName", ToString=lambda: "Track0"),
                ],
            )],
            rigData=SimpleNamespace(rigData=[]),
        ))

        self.assertEqual(skeleton.tracks, ["Track0"])

    def test_uncooked_dyng_rebuilds_empty_skeleton_rig_data(self):
        root = IDENTITY_MATRIX
        child = transform_from_axes(
            (0.0, 1.0, 0.0),
            (-1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0),
            (1.0, 2.0, 3.0),
        )
        dyng_chunk = fake_dyng_chunk()
        transforms = dyng_chunk.GetVariableByName("nodeTransforms")
        transforms.More = [matrix_element(root), matrix_element(child)]
        skeleton_chunk = self._skeleton_chunk(
            ["root", "dyng_child"],
            [-1, 0],
            [],
        )
        source_file = SimpleNamespace(
            CHUNKS=SimpleNamespace(CHUNKS=[skeleton_chunk, dyng_chunk]),
        )

        skeleton = dc_skeleton.create_Skeleton(source_file)

        self.assertEqual(skeleton.names, ["root", "dyng_child"])
        self.assertEqual(skeleton.parentIdx, [-1, 0])
        self.assertEqual(
            (skeleton.positions[1].x, skeleton.positions[1].y, skeleton.positions[1].z),
            (1.0, 2.0, 3.0),
        )
        self.assertAlmostEqual(skeleton.rotations[1].X, 0.0)
        self.assertAlmostEqual(skeleton.rotations[1].Y, 0.0)
        self.assertAlmostEqual(skeleton.rotations[1].Z, math.sqrt(0.5))
        self.assertAlmostEqual(skeleton.rotations[1].W, math.sqrt(0.5))

    def test_uncooked_dyng_does_not_require_skeleton_chunk(self):
        source_file = SimpleNamespace(
            CHUNKS=SimpleNamespace(CHUNKS=[fake_dyng_chunk()]),
        )

        skeleton = dc_skeleton.create_Skeleton(source_file)

        self.assertEqual(skeleton.names, ["root", "dyng_child"])
        self.assertEqual(skeleton.parentIdx, [-1, 0])
        self.assertEqual(len(skeleton.positions), 2)

    def test_parse_dyng_chunk_extracts_physics_arrays(self):
        data = parse_dyng_chunk(fake_dyng_chunk(), source_path="sample.w3dyng")

        self.assertEqual([node.name for node in data.nodes], ["root", "dyng_child"])
        self.assertEqual(data.nodes[1].parent, "root")
        self.assertAlmostEqual(data.nodes[1].stiffness, 0.5)
        self.assertEqual(len(data.links), 1)
        self.assertEqual(data.links[0].a, 0)
        self.assertEqual(data.links[0].b, 1)
        self.assertEqual(len(data.triangles), 1)
        self.assertEqual(len(data.collisions), 2)
        self.assertAlmostEqual(data.nodes[1].transform[3][0], 1.0)

    def test_resource_json_roundtrip(self):
        data = parse_dyng_chunk(fake_dyng_chunk(), source_path="sample.w3dyng")
        roundtrip = DyngResourceData.from_json(data.to_json())

        self.assertEqual(roundtrip, data)

    def test_parse_allows_omitted_triangle_arrays(self):
        chunk = fake_dyng_chunk()
        chunk.PROPS = [prop for prop in chunk.PROPS if not prop.theName.startswith("triangle")]

        data = parse_dyng_chunk(chunk, source_path="no_triangles.w3dyng")

        self.assertEqual(len(data.nodes), 2)
        self.assertEqual(len(data.links), 1)
        self.assertEqual(len(data.triangles), 0)

    def test_actual_ciri_asset_when_configured(self):
        path = os.environ.get("WITCHER3_TOOLS_CIRI_DYNG_PATH", "")
        if not path:
            self.skipTest("WITCHER3_TOOLS_CIRI_DYNG_PATH is not set")
        data = load_dyng_resource(path)

        self.assertGreater(len(data.nodes), 0)
        self.assertGreaterEqual(len(data.collisions), 0)
        lower_path = path.replace("/", "\\").lower()
        if lower_path.endswith(r"c_05_wa__ciri.w3dyng"):
            self.assertEqual(len(data.nodes), 34)
            self.assertEqual(len(data.links), 26)
            self.assertEqual(len(data.triangles), 24)
            self.assertEqual(len(data.collisions), 34)
            self.assertEqual(data.nodes[3].name, "dyng_hair_b_01")
        elif lower_path.endswith(r"dyng_body_01_wa__ciri.w3dyng"):
            self.assertEqual(len(data.nodes), 6)
            self.assertEqual(len(data.links), 2)
            self.assertEqual(len(data.triangles), 0)
            self.assertEqual(len(data.collisions), 6)


class DyngSolverTests(unittest.TestCase):
    def test_simulator_moves_dynamic_node_and_keeps_fixed_anchor(self):
        data = parse_dyng_chunk(fake_dyng_chunk(), source_path="sample.w3dyng")
        targets = [node.transform for node in data.nodes]
        simulator = DyngSimulator(data, targets)

        before_root = simulator.positions[0]
        before_child = simulator.positions[1]
        simulator.step(targets, 1.0 / 60.0)

        self.assertEqual(simulator.positions[0], before_root)
        self.assertLess(simulator.positions[1][2], before_child[2])

    def test_zero_dt_refreshes_fixed_anchor_without_evaluating_constraints(self):
        data = parse_dyng_chunk(fake_dyng_chunk(), source_path="sample.w3dyng")
        targets = [node.transform for node in data.nodes]
        moved_root = transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (2.0, 3.0, 4.0))
        moved_targets = [moved_root, targets[1]]
        simulator = DyngSimulator(data, targets)

        simulator.step(moved_targets, 0.0)

        assert_vector_almost_equal(self, simulator.positions[0], (2.0, 3.0, 4.0))

    def test_simulator_is_deterministic_for_same_inputs(self):
        data = parse_dyng_chunk(fake_dyng_chunk(), source_path="sample.w3dyng")
        targets = [node.transform for node in data.nodes]
        sim_a = DyngSimulator(data, targets)
        sim_b = DyngSimulator(data, targets)

        for _ in range(5):
            sim_a.step(targets, 1.0 / 60.0)
            sim_b.step(targets, 1.0 / 60.0)

        self.assertEqual(sim_a.positions, sim_b.positions)
        self.assertEqual(sim_a.global_transforms, sim_b.global_transforms)

    def test_simulator_wind_is_deterministic_for_same_inputs(self):
        data = parse_dyng_chunk(fake_dyng_chunk(), source_path="sample.w3dyng")
        targets = [node.transform for node in data.nodes]
        sim_a = DyngSimulator(data, targets)
        sim_b = DyngSimulator(data, targets)

        for _ in range(5):
            sim_a.step(targets, 1.0 / 60.0, gravity=0.0, wind=1.0, wind_vector=(0.0, 1.0, 0.0))
            sim_b.step(targets, 1.0 / 60.0, gravity=0.0, wind=1.0, wind_vector=(0.0, 1.0, 0.0))

        self.assertEqual(sim_a.positions, sim_b.positions)
        self.assertEqual(sim_a.global_transforms, sim_b.global_transforms)

    def test_simulator_applies_wind_vector(self):
        data = parse_dyng_chunk(fake_dyng_chunk(), source_path="sample.w3dyng")
        targets = [node.transform for node in data.nodes]
        calm = DyngSimulator(data, targets)
        windy = DyngSimulator(data, targets)

        calm.step(targets, 1.0 / 60.0, gravity=0.0)
        windy.step(targets, 1.0 / 60.0, gravity=0.0, wind=1.0, wind_vector=(0.0, 1.0, 0.0))

        self.assertGreater(windy.positions[1][1], calm.positions[1][1])

    def test_free_link_uses_opposite_mass_shares(self):
        point_a = transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.0, 0.0, 0.0))
        point_b = transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (2.0, 0.0, 0.0))
        data = DyngResourceData(
            "",
            "weighted-link",
            (
                dyng.DyngNode("a", "", 1.0, 0.0, 10.0, point_a),
                dyng.DyngNode("b", "", 3.0, 0.0, 10.0, point_b),
            ),
            (dyng.DyngLink(0, 1.0, 0, 1),),
            (),
            (),
        )
        simulator = DyngSimulator(data, (point_a, point_b))

        simulator.step((point_a, point_b), 1.0 / 60.0, gravity=0.0, dampening=1.0, max_link_iterations=1)

        self.assertAlmostEqual(simulator.positions[0][0], 0.75)
        self.assertAlmostEqual(simulator.positions[1][0], 1.75)

    def test_preintegration_tether_correction_updates_position_and_velocity(self):
        target = transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.0, 0.0, 0.0))
        data = DyngResourceData(
            "",
            "tether-order",
            (dyng.DyngNode("point", "", 1.0, 0.0, 1.0, target),),
            (),
            (),
            (),
        )
        simulator = DyngSimulator(data, (target,))
        simulator.positions[0] = (2.0, 0.0, 0.0)

        simulator.step((target,), 0.02, gravity=0.0, dampening=0.5, max_link_iterations=0)

        self.assertAlmostEqual(simulator.positions[0][0], 0.0)
        self.assertAlmostEqual(simulator.velocities[0][0], -25.0)

    def test_gravity_is_per_evaluation_velocity_impulse(self):
        target = transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.0, 0.0, 0.0))
        data = DyngResourceData(
            "",
            "gravity-impulse",
            (dyng.DyngNode("point", "", 2.0, 0.0, 100.0, target),),
            (),
            (),
            (),
        )
        short_step = DyngSimulator(data, (target,))
        long_step = DyngSimulator(data, (target,))

        short_step.step((target,), 0.01, dampening=1.0, max_link_iterations=0)
        long_step.step((target,), 0.02, dampening=1.0, max_link_iterations=0)

        self.assertAlmostEqual(short_step.velocities[0][2], -0.654)
        self.assertAlmostEqual(long_step.velocities[0][2], -0.654)
        self.assertAlmostEqual(long_step.positions[0][2], short_step.positions[0][2] * 2.0)

    def test_relaxed_mode_runs_thirty_full_evaluations(self):
        target = transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.0, 0.0, 0.0))
        data = DyngResourceData(
            "",
            "relaxed",
            (dyng.DyngNode("point", "", 1.0, 0.0, 100.0, target),),
            (),
            (),
            (),
        )
        simulator = DyngSimulator(data, (target,))

        simulator.step((target,), 0.01, dampening=1.0 / 0.9, max_link_iterations=0, relaxed=True)

        self.assertAlmostEqual(simulator.velocities[0][2], -9.81)

    def test_body_collision_pushes_dynamic_node_out_of_static_collider(self):
        target = transform_from_axes(
            (1, 0, 0),
            (0, 1, 0),
            (0, 0, 1),
            (1.0, 0.0, 0.0),
        )
        data = DyngResourceData(
            "",
            "body-collision",
            (
                dyng.DyngNode("root", "", 0.0, 0.0, 0.0, IDENTITY_MATRIX),
                dyng.DyngNode("dyng_child", "root", 1.0, 0.0, 2.0, target),
            ),
            (),
            (),
            (dyng.DyngCollision("root", 0.0, 0.0, IDENTITY_MATRIX),),
        )
        targets = [node.transform for node in data.nodes]
        simulator = DyngSimulator(data, targets)
        simulator.positions[1] = (0.1, 0.0, 0.0)

        simulator.step(
            targets,
            1.0 / 60.0,
            gravity=0.0,
            body_collision=True,
            body_collision_radius=0.5,
            body_collision_strength=1.0,
        )

        distance = dyng._v_len(dyng._v_sub(simulator.positions[1], simulator.positions[0]))
        self.assertGreaterEqual(distance, 0.5)

    def test_use_offsets_anchors_zero_distance_to_collision_transform(self):
        chunk = fake_dyng_chunk()
        offset = transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.0, 0.25, 0.0))
        for prop in chunk.PROPS:
            if prop.theName == "nodeDistances":
                prop.value = [0.0, 0.0]
            elif prop.theName == "collisionTransforms":
                prop.More = [matrix_element(IDENTITY_MATRIX), matrix_element(offset)]
        data = parse_dyng_chunk(chunk, source_path="offset.w3dyng")
        targets = [node.transform for node in data.nodes]
        simulator = DyngSimulator(data, targets)

        simulator.step(targets, 1.0 / 60.0, use_offsets=True)

        self.assertAlmostEqual(simulator.positions[1][0], 1.0)
        self.assertAlmostEqual(simulator.positions[1][1], 0.25)
        self.assertAlmostEqual(simulator.positions[1][2], 0.0)

    def test_use_offsets_composes_collision_transform_in_bone_space(self):
        target = transform_from_axes((0, 1, 0), (-1, 0, 0), (0, 0, 1), (1.0, 2.0, 3.0))
        offset = transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0.0, 0.25, 0.0))
        data = DyngResourceData(
            "",
            "rotated-offset",
            (dyng.DyngNode("anchor", "", 0.0, 0.0, 0.0, target),),
            (),
            (),
            (dyng.DyngCollision("anchor", 0.0, 0.0, offset),),
        )
        simulator = DyngSimulator(data, (target,))

        simulator.step((target,), 1.0 / 60.0, gravity=0.0, use_offsets=True, max_link_iterations=0)

        assert_vector_almost_equal(self, simulator.positions[0], (0.75, 2.0, 3.0))

    def test_offsets_plane_collision_uses_base_transform_axis(self):
        target = transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (1.0, 0.0, 0.0))
        collision_offset = transform_from_axes((1, 0, 0), (0, 0, 1), (0, -1, 0), (0.0, 0.0, 0.0))
        data = DyngResourceData(
            "",
            "offset-plane",
            (
                dyng.DyngNode("root", "", 0.0, 0.0, 0.0, IDENTITY_MATRIX),
                dyng.DyngNode("dyng_child", "root", 0.0, 0.0, 1.0, target),
            ),
            (),
            (),
            (
                dyng.DyngCollision("root", 0.0, 0.0, IDENTITY_MATRIX),
                dyng.DyngCollision("dyng_child", 0.0, 0.0, collision_offset),
            ),
        )
        simulator = DyngSimulator(data, [node.transform for node in data.nodes])
        simulator.positions[1] = (1.0, -0.5, 0.0)
        simulator.dt = 1.0 / 60.0

        simulator.step(
            [node.transform for node in data.nodes],
            1.0 / 60.0,
            gravity=0.0,
            use_offsets=True,
            plane_collision=True,
            max_link_iterations=0,
        )

        # The plane correction updates velocity before integration, so a
        # 0.5-unit correction advances another 0.5.
        self.assertAlmostEqual(simulator.positions[1][1], 0.5)

    def test_lookat_rotation_rotates_existing_basis(self):
        matrix = transform_from_axes((1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 0, 0))

        rotated = dyng._orient_x_axis_to(matrix, (0, 1, 0), (2, 3, 4))

        assert_vector_almost_equal(self, rotated[0][:3], (0, 1, 0))
        assert_vector_almost_equal(self, rotated[1][:3], (-1, 0, 0))
        assert_vector_almost_equal(self, rotated[2][:3], (0, 0, 1))
        assert_vector_almost_equal(self, rotated[3][:3], (2, 3, 4))

    def test_shake_velocity_impulse_is_scaled_by_dt(self):
        chunk = fake_dyng_chunk()
        for prop in chunk.PROPS:
            if prop.theName == "collisionRadiuses":
                prop.value = [0.0, 1.0]
        data = parse_dyng_chunk(chunk, source_path="shake.w3dyng")
        targets = [node.transform for node in data.nodes]
        initial = targets[1][3][:3]

        large_dt = DyngSimulator(data, targets)
        small_dt = DyngSimulator(data, targets)
        large_dt.step(targets, 0.02, gravity=0.0, shake=1.0)
        small_dt.step(targets, 0.01, gravity=0.0, shake=1.0)

        large_delta = dyng._v_len(dyng._v_sub(large_dt.positions[1], initial))
        small_delta = dyng._v_len(dyng._v_sub(small_dt.positions[1], initial))
        self.assertGreater(large_delta, small_delta * 3.5)


class PhysicsPresetStoreTests(unittest.TestCase):
    def test_extension_package_name_strips_submodule(self):
        self.assertEqual(
            physics_presets._extension_package_name("bl_ext.vscode_development.witcher3_tools.physics"),
            "bl_ext.vscode_development.witcher3_tools",
        )
        self.assertIsNone(physics_presets._extension_package_name("witcher3_tools.physics"))

    def test_save_load_delete_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "presets.json"
            original_store_path = physics_presets._store_path
            physics_presets._store_path = lambda: str(path)
            try:
                saved = physics_presets.save_preset(
                    "breast",
                    "  My   Preset  ",
                    {"simTime": 0.25, "ellipse": (0.0, 0.0, 0.1, 0.2)},
                )

                self.assertEqual(saved, "My Preset")
                self.assertEqual(physics_presets.saved_preset_names("breast"), ("My Preset",))
                self.assertEqual(
                    physics_presets.get_preset("breast", "My Preset"),
                    {"simTime": 0.25, "ellipse": [0.0, 0.0, 0.1, 0.2]},
                )
                self.assertTrue(physics_presets.delete_preset("breast", "My Preset"))
                self.assertEqual(physics_presets.saved_preset_names("breast"), ())
            finally:
                physics_presets._store_path = original_store_path

    def test_extension_seed_file_populates_empty_store_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "presets.json"
            seed_path = pathlib.Path(temp_dir) / "extension" / "physics_user_presets.json"
            seed_path.parent.mkdir()
            seed_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "breast": {
                            "Example Preset": {
                                "preset": "Example Preset",
                                "simTime": 0.2,
                            }
                        },
                        "dyng": {},
                    }
                ),
                encoding="utf-8",
            )
            original_store_path = physics_presets._store_path
            original_seed_path = physics_presets._package_seed_path
            physics_presets._store_path = lambda: str(path)
            physics_presets._package_seed_path = lambda: str(seed_path)
            try:
                self.assertEqual(physics_presets.saved_preset_names("breast"), ("Example Preset",))
                self.assertEqual(
                    physics_presets.get_preset("breast", "Example Preset"),
                    {"preset": "Example Preset", "simTime": 0.2},
                )

                self.assertTrue(physics_presets.delete_preset("breast", "Example Preset"))
                self.assertEqual(physics_presets.saved_preset_names("breast"), ())
            finally:
                physics_presets._store_path = original_store_path
                physics_presets._package_seed_path = original_seed_path


if __name__ == "__main__":
    unittest.main()
