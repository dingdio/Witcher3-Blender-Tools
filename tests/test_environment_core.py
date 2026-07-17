import io
import math
import struct
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

pkg = sys.modules.get("witcher3_tools")
if pkg is None or not getattr(pkg, "__path__", None):
    pkg = types.ModuleType("witcher3_tools")
    pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    pkg.__package__ = "witcher3_tools"
    sys.modules["witcher3_tools"] = pkg

from witcher3_tools.CR2W.dc_environment import (  # noqa: E402
    CurvePoint,
    GlobalLightingTrajectory,
    SimpleCurve,
    curve_is_placeholder,
    decode_world_environment,
    describe_environment_fields,
    gamma_to_linear_color,
    load_weather_table,
    normalize_curve_entry_name,
    parse_simple_curve,
)
from witcher3_tools.CR2W.CR2W_types import PROPERTY  # noqa: E402


class Node:
    def __init__(self, name="", the_type="", value=None, children=None, index=None, handles=None):
        self.theName = name
        self.theType = the_type
        if value is not None:
            self.Value = value
        if children is not None:
            self.More = children
        if index is not None:
            self.Index = types.SimpleNamespace(String=index, strings=[index])
        if handles is not None:
            self.Handles = handles

    def GetVariableByName(self, name):
        return next((item for item in getattr(self, "More", ()) if item.theName == name), None)


def scalar_curve_node(name, points, base_type="CT_Linear"):
    entries = []
    for time, value in points:
        entries.append(
            Node(
                children=[
                    Node("me", "Float", time),
                    Node(
                        "ntrolPoint",
                        "Vector",
                        children=[
                            Node("X", "Float", -0.1),
                            Node("Y", "Float", 0.0),
                            Node("Z", "Float", 0.1),
                            Node("W", "Float", 0.0),
                        ],
                    ),
                    Node("lue", "Float", value),
                    Node("rveTypeL", "Uint16", 1),
                    Node("rveTypeR", "Uint16", 1),
                ]
            )
        )
    return Node(
        name,
        "SSimpleCurve",
        children=[
            Node("dataCurveValues", "array:147,0,SCurveDataEntry", children=entries),
            Node("dataBaseType", "ECurveBaseType", index=base_type),
        ],
    )


class TestEnvironmentCurves(unittest.TestCase):
    def test_uncooked_curve_entry_aliases_are_normalized(self):
        self.assertEqual(normalize_curve_entry_name("me"), "time")
        self.assertEqual(normalize_curve_entry_name("ntrolPoint"), "controlPoint")
        self.assertEqual(normalize_curve_entry_name("lue"), "value")
        self.assertEqual(normalize_curve_entry_name("rveTypeL"), "curveTypeL")
        self.assertEqual(normalize_curve_entry_name("rveTypeR"), "curveTypeR")

    def test_scalar_curve_decodes_and_interpolates(self):
        curve = parse_simple_curve(scalar_curve_node("height", ((0.25, 0.0), (0.75, 1.0))))
        self.assertEqual([point.time for point in curve.points], [0.25, 0.75])
        self.assertAlmostEqual(curve.evaluate_scalar(0.5), 0.5)
        self.assertAlmostEqual(curve.evaluate_scalar_seconds(12 * 60 * 60), 0.5)

    def test_single_struct_array_entry_is_not_split_into_field_points(self):
        source = scalar_curve_node("moonSize", ((0.447071582, 2.22543335),))
        data = source.More[0]
        data.Count = 1
        data.More = data.More[0].More

        curve = parse_simple_curve(source)

        self.assertEqual(len(curve.points), 1)
        self.assertAlmostEqual(curve.points[0].time, 0.447071582)
        self.assertAlmostEqual(curve.points[0].value, 2.22543335)
        self.assertFalse(curve_is_placeholder(curve))

    def test_split_scalar_curve_decodes_parallel_arrays(self):
        times = Node("dataTimes", "array:2,0,Float")
        times.Count = 2
        times.value = [0.25, 0.75]
        values = Node("dataValues", "array:2,0,Float")
        values.Count = 2
        values.value = [2.0, 6.0]
        types_l = Node("dataCurveType0", "array:2,0,Uint8")
        types_l.Count = 2
        types_l.value = [1, 1]
        types_r = Node("dataCurveType1", "array:2,0,Uint8")
        types_r.Count = 2
        types_r.value = [1, 1]
        controls = Node(
            "dataControlPoints",
            "array:2,0,Vector",
            children=[
                Node(children=[Node("X", "Float", -0.1), Node("Z", "Float", 0.1)]),
                Node(children=[Node("X", "Float", -0.1), Node("Z", "Float", 0.1)]),
            ],
        )
        controls.Count = 2
        source = Node(
            "radius",
            "SSimpleCurve",
            children=[
                times,
                values,
                types_l,
                types_r,
                controls,
                Node("dataBaseType", index="CT_Linear"),
            ],
        )

        curve = parse_simple_curve(source)

        self.assertEqual([point.time for point in curve.points], [0.25, 0.75])
        self.assertEqual([point.value for point in curve.points], [2.0, 6.0])
        self.assertAlmostEqual(curve.evaluate_scalar(0.5), 4.0)

    def test_split_single_color_uses_flattened_control_point(self):
        times = Node("dataTimes", "array:1,0,Float", 0.375)
        times.Count = 1
        controls = Node(
            "dataControlPoints",
            "array:1,0,Vector",
            children=[
                Node("X", "Float", 201.0),
                Node("Y", "Float", 240.0),
                Node("Z", "Float", 248.0),
                Node("W", "Float", 50.0),
            ],
        )
        controls.Count = 1
        source = Node(
            "color",
            "SSimpleCurve",
            children=[Node("CurveType", index="SCT_ColorScaled"), times, controls],
        )

        curve = parse_simple_curve(source)

        self.assertEqual(len(curve.points), 1)
        self.assertEqual(curve.points[0].time, 0.375)
        self.assertEqual(curve.points[0].control_point, (201.0, 240.0, 248.0, 50.0))

    def test_color_scaled_gamma_conversion(self):
        converted = gamma_to_linear_color((255.0, 127.5, 0.0, 2.0))
        self.assertEqual(converted[0], 2.0)
        self.assertAlmostEqual(converted[1], 2.0 * math.pow(0.5, 2.2))
        self.assertEqual(converted[2:], (0.0, 1.0))

    def test_smooth_curve_interpolates_between_neighbouring_points(self):
        curve = SimpleCurve(
            base_type="CT_Smooth",
            points=(
                CurvePoint(0.0, value=0.0),
                CurvePoint(0.25, value=1.0),
                CurvePoint(0.75, value=3.0),
                CurvePoint(1.0, value=4.0),
            ),
        )

        self.assertAlmostEqual(curve.evaluate_scalar(0.5), 2.0)


class TestCR2WPrimitiveArrays(unittest.TestCase):
    def test_variant_prefixed_two_float_array_keeps_all_values(self):
        payload = struct.pack("<Iff", 2, 0.1, 20.0)
        type_info = types.SimpleNamespace(
            type="array:138,0,Float",
            name="dataValues",
            size=len(payload) + 4,
        )
        cr2w = types.SimpleNamespace(
            HEADER=types.SimpleNamespace(version=200),
            CNAMES=[],
        )
        parent = types.SimpleNamespace(classEnd=None)

        prop = PROPERTY(
            io.BytesIO(payload),
            cr2w,
            parent,
            custom_propstart=type_info,
        )

        self.assertEqual(prop.Count, 2)
        self.assertEqual(len(prop.value), 2)
        self.assertAlmostEqual(prop.value[0], 0.1)
        self.assertAlmostEqual(prop.value[1], 20.0)


class TestGlobalLightingTrajectory(unittest.TestCase):
    def test_direction_uses_engine_pitch_and_yaw_axes(self):
        flat = SimpleCurve(points=(CurvePoint(time=0.0, value=0.0),))
        trajectory = GlobalLightingTrajectory(sun_height=flat)
        direction = trajectory.sun_direction(0.0)
        self.assertAlmostEqual(direction[0], 0.0)
        self.assertAlmostEqual(direction[1], 1.0)
        self.assertAlmostEqual(direction[2], 0.0)

    def test_world_reader_extracts_paths_trajectory_and_embedded_materials(self):
        external_sun = types.SimpleNamespace(DepotPath="environment/skyboxes/sun/sun.w2mesh")
        internal_material = types.SimpleNamespace(Reference=1, ChunkHandle=True, val=2, DepotPath=None)
        skybox = Node(
            "skybox",
            "SWorldSkyboxParameters",
            children=[
                Node("sunMesh", "handle:CMesh", handles=[external_sun]),
                Node("sunMaterial", "handle:CMaterialInstance", handles=[internal_material]),
            ],
        )
        env = Node(
            "environmentParameters",
            "SWorldEnvironmentParameters",
            children=[
                Node("globalLightingTrajectory", "CGlobalLightingTrajectory", children=[]),
                Node(
                    "environmentDefinition",
                    "handle:CEnvironmentDefinition",
                    handles=[types.SimpleNamespace(DepotPath="environment/definitions/test.env")],
                ),
                Node("skybox", "SWorldSkyboxParameters", children=skybox.More),
            ],
        )
        world_chunk = Node(children=[env])
        world_chunk.name = "CGameWorld"
        material_chunk = Node(
            children=[
                Node(
                    "baseMaterial",
                    "handle:IMaterial",
                    handles=[types.SimpleNamespace(DepotPath="environment/skyboxes/sun/sun.w2mg")],
                )
            ]
        )
        material_chunk.name = "CMaterialInstance"
        cr2w = types.SimpleNamespace(
            fileName="levels/test/test.w2w",
            CHUNKS=types.SimpleNamespace(CHUNKS=[world_chunk, material_chunk]),
        )

        decoded = decode_world_environment(cr2w)

        self.assertEqual(decoded.environment_definition, "environment\\definitions\\test.env")
        self.assertEqual(decoded.skybox.sun_mesh, "environment\\skyboxes\\sun\\sun.w2mesh")
        self.assertEqual(decoded.skybox.sun_material, "environment\\skyboxes\\sun\\sun.w2mg")
        self.assertEqual(decoded.skybox.sun_material_ref, 1)


class TestEnvironmentFieldRows(unittest.TestCase):
    def test_placeholder_curves_are_detected(self):
        zero_scalar = SimpleCurve(points=(CurvePoint(time=0.0, value=0.0),))
        real_scalar = SimpleCurve(points=(CurvePoint(time=0.0, value=0.5),))
        zero_color = SimpleCurve(
            curve_type="SCT_Vector",
            points=(CurvePoint(time=0.0, value=0.0, control_point=(-0.1, 0.0, 0.1, 0.0)),),
        )
        real_color = SimpleCurve(
            curve_type="SCT_Vector",
            points=(CurvePoint(time=0.5, value=1.0, control_point=(120.0, 140.0, 200.0, 1.0)),),
        )

        self.assertTrue(curve_is_placeholder(zero_scalar))
        self.assertFalse(curve_is_placeholder(real_scalar))
        self.assertTrue(curve_is_placeholder(zero_color))
        self.assertFalse(curve_is_placeholder(real_color))
        self.assertTrue(curve_is_placeholder(SimpleCurve()))

    def test_describe_orders_groups_and_flags_unset_fields(self):
        constant = SimpleCurve(points=(CurvePoint(time=0.0, value=0.35),))
        varying = SimpleCurve(
            points=(CurvePoint(time=0.0, value=1.0), CurvePoint(time=0.5, value=4.0)),
        )
        placeholder = SimpleCurve(points=(CurvePoint(time=0.0, value=0.0),))
        environment = types.SimpleNamespace(
            params={
                "sunAndMoonParams": {"sunSize": varying},
                "sky": {
                    "activated": True,
                    "globalSkyBrightness": placeholder,
                    "skyColor": constant,
                },
                "customGroup": {"nested": {"inner": 2.0}},
            }
        )

        rows = describe_environment_fields(environment)

        groups = [row.group for row in rows]
        self.assertEqual(
            groups,
            ["sky", "sky", "sky", "sunAndMoonParams", "customGroup"],
        )
        by_field = {(row.group, row.field): row for row in rows}
        self.assertTrue(by_field[("sky", "activated")].is_set)
        self.assertEqual(by_field[("sky", "activated")].value_text, "True")
        self.assertFalse(by_field[("sky", "globalSkyBrightness")].is_set)
        self.assertEqual(by_field[("sky", "globalSkyBrightness")].value_text, "<unset>")
        self.assertEqual(by_field[("sky", "skyColor")].value_text, "= 0.35")
        self.assertEqual(by_field[("sunAndMoonParams", "sunSize")].value_text, "2 keys · 1 … 4")
        nested = by_field[("customGroup", "nested")]
        self.assertEqual(nested.type_text, "struct (1 fields)")
        self.assertEqual(len(nested.children), 1)
        self.assertEqual(nested.children[0].label, "inner")
        self.assertEqual(nested.children[0].value_text, "2")
        self.assertFalse(nested.children[0].has_children)


class TestWeatherTable(unittest.TestCase):
    def test_native_semicolon_schema_and_optional_environment(self):
        source = io.StringIO(
            "name;probability;windScale;blendTime;skybox;fakeShadow;backgroundThunder;"
            "envPath;envBlend;occurenceTime;1_effect;1_priority;1_prob;1_strength;1_type\n"
            "WT_Clear;0.2;0.2;120;1;0;false;;;900;fx/cloud.w2p;80;1;0.5;CLOUDS\n"
            "WT_Rain;0.1;1;60;2;4;true;environment/rain.env;1.5;500;;;;;\n"
        )

        table = load_weather_table(source)

        self.assertEqual(len(table.presets), 2)
        self.assertEqual(table.presets[0].environment_path, "")
        self.assertEqual(table.presets[0].effects[0].effect_type, "CLOUDS")
        self.assertTrue(table.presets[1].background_thunder)
        self.assertEqual(table.presets[1].environment_path, "environment\\rain.env")
        self.assertEqual(table.presets[1].environment_blend, 1.0)


if __name__ == "__main__":
    unittest.main()
