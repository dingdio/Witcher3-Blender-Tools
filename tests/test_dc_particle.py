import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

pkg = sys.modules.get("witcher3_tools")
if pkg is None or not getattr(pkg, "__path__", None):
    pkg = types.ModuleType("witcher3_tools")
    pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    pkg.__package__ = "witcher3_tools"
    sys.modules["witcher3_tools"] = pkg

from witcher3_tools.CR2W.dc_particle import (  # noqa: E402
    _decode_bursts,
    _decode_evaluator,
    _pointer_index,
    load_bin_particle,
)


class Node:
    def __init__(
        self,
        name="",
        the_type="",
        value=None,
        children=None,
        index=None,
        handles=None,
    ):
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
        return next(
            (item for item in getattr(self, "More", ()) if item.theName == name),
            None,
        )


def chunk(type_name, props=()):
    return types.SimpleNamespace(Type=type_name, name=type_name, PROPS=list(props))


def pointer(name, target_index, the_type="ptr:IEvaluatorFloat"):
    return Node(name, the_type, target_index + 1)


def curve_bytes(keys):
    payload = bytearray(b"\0\0\0")
    payload.extend(struct.pack("<I", len(keys)))
    for key in keys:
        payload.extend(struct.pack("<ff4f4fii", *key))
    return bytes(payload)


def synthetic_particle():
    vector_curve = curve_bytes(
        (
            (0.0, 0.25, -0.1, 0.0, 0.1, 0.0, 0.2, 0.0, 0.3, 0.0, 1, 2),
            (1.0, 1.5, -0.4, 0.0, 0.4, 0.0, 0.5, 0.0, 0.6, 0.0, 3, 4),
        )
    )
    alpha_curve = curve_bytes(
        ((0.0, 1.0, -0.1, 0.0, 0.1, 0.0, 0.2, 0.0, 0.2, 0.0, 1, 1),)
    )
    raw_data = vector_curve + alpha_curve

    lod = Node(
        "lods",
        "array:1,0,SParticleEmitterLODLevel",
        children=[
            Node(
                "emitterDurationSettings",
                "EmitterDurationSettings",
                children=[Node("emitterDuration", "Float", 3.0)],
            ),
            Node("sortBackToFront", "Bool", 1),
        ],
    )
    lod.Count = 1
    embedded_material = Node(
        "material",
        "handle:IMaterial",
        handles=[types.SimpleNamespace(Reference=11, ChunkHandle=True, val=12)],
    )

    curve_color = Node(
        "color",
        "Color",
        children=[
            Node("Red", "Uint8", 10),
            Node("Green", "Uint8", 20),
            Node("Blue", "Uint8", 30),
            Node("Alpha", "Uint8", 40),
        ],
    )
    curve_color.dataEnd = 1
    chunks = [
        chunk("CParticleSystem"),
        chunk(
            "CParticleEmitter",
            (
                Node("editorName", "String", "splash"),
                embedded_material,
                pointer("particleDrawer", 12, "ptr:IParticleDrawer"),
                Node("keepSimulationLocal", "Bool", 1),
                lod,
            ),
        ),
        chunk("CParticleModificatorSizeOverLife", (pointer("size", 3, "ptr:IEvaluatorVector"),)),
        chunk("CEvaluatorVectorCurve", (Node("freeAxes", "EFreeVectorAxes", index="FVA_One"),)),
        chunk("CCurve", (curve_color,)),
        chunk("CParticleModificatorAlphaOverLife", (pointer("alpha", 6),)),
        chunk("CEvaluatorFloatCurve"),
        chunk("CCurve"),
        chunk(
            "CParticleInitializerSpawnCircle",
            (pointer("innerRadius", 9), pointer("outerRadius", 10)),
        ),
        chunk("CEvaluatorFloatConst"),
        chunk("CEvaluatorFloatConst", (Node("value", "Float", 0.65),)),
        chunk(
            "CMaterialInstance",
            (
                Node(
                    "baseMaterial",
                    "handle:IMaterial",
                    handles=[types.SimpleNamespace(DepotPath="fx/shaders/water.w2mg")],
                ),
            ),
        ),
        chunk("CParticleDrawerEmitterOrientation"),
        chunk("CEvaluatorFloatConst", (Node("value", "Float", 10.0),)),
    ]
    chunks[11].CMaterialInstance = types.SimpleNamespace(
        InstanceParameters=types.SimpleNamespace(
            elements=[
                types.SimpleNamespace(PROP=Node("reflection_multiplier", "Float", 20.0)),
                types.SimpleNamespace(
                    PROP=Node(
                        "normal_and_splash",
                        "handle:ITexture",
                        handles=[types.SimpleNamespace(DepotPath="fx/textures/ring.xbm")],
                    )
                ),
            ]
        )
    )

    parents = (0, 1, 2, 3, 4, 2, 6, 7, 2, 9, 9, 1, 2, 2)
    exports = [
        types.SimpleNamespace(parentID=parent, dataOffset=0, dataSize=0)
        for parent in parents
    ]
    exports[4].dataOffset = 0
    exports[4].dataSize = len(vector_curve)
    exports[7].dataOffset = len(vector_curve)
    exports[7].dataSize = len(alpha_curve)
    cr2w = types.SimpleNamespace(
        start=0,
        CHUNKS=types.SimpleNamespace(CHUNKS=chunks),
        CR2WExport=exports,
    )
    return cr2w, raw_data


class TestParticleDecoder(unittest.TestCase):
    def test_decodes_hierarchy_curves_defaults_and_material(self):
        cr2w, raw_data = synthetic_particle()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.w2p"
            source.write_bytes(raw_data)
            with mock.patch("witcher3_tools.CR2W.CR2W_file.read_CR2W", return_value=cr2w):
                system = load_bin_particle(source)

        self.assertEqual(system.auto_hide_distance, 100.0)
        self.assertEqual(system.auto_hide_range, 0.0)
        emitter = system.emitter("splash")
        self.assertIsNotNone(emitter)
        self.assertEqual(emitter.max_particles, 55)
        self.assertEqual(emitter.loops, 0)
        self.assertFalse(emitter.use_subframe_emission)
        self.assertTrue(emitter.keep_simulation_local)
        self.assertEqual(emitter.drawer_type, "CParticleDrawerEmitterOrientation")

        self.assertEqual(
            [module.type_name for module in emitter.modules],
            [
                "CParticleModificatorSizeOverLife",
                "CParticleModificatorAlphaOverLife",
                "CParticleInitializerSpawnCircle",
            ],
        )
        size = emitter.module("CParticleModificatorSizeOverLife")
        self.assertTrue(size.property("modulate"))
        evaluator = size.evaluator("size")
        self.assertEqual(evaluator.free_axes, "FVA_One")
        self.assertEqual(len(evaluator.curves), 1)
        self.assertEqual([key.value for key in evaluator.curves[0].keys], [0.25, 1.5])
        self.assertEqual(evaluator.curves[0].color, (10, 20, 30, 40))
        self.assertEqual(evaluator.curves[0].base_type, "CT_Segmented")
        self.assertFalse(evaluator.curves[0].loop)
        for actual, expected in zip(
            evaluator.curves[0].keys[0].tangent_left,
            (-0.1, 0.0, 0.1, 0.0),
        ):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(evaluator.curves[0].keys[1].curve_type_r, 4)

        circle = emitter.module("CParticleInitializerSpawnCircle")
        self.assertTrue(circle.property("worldSpace"))
        self.assertFalse(circle.property("surfaceOnly"))
        self.assertEqual(circle.evaluator("innerRadius").value, 0.0)
        self.assertAlmostEqual(circle.evaluator("outerRadius").value, 0.65)

        self.assertEqual(emitter.lods[0].duration, 3.0)
        self.assertTrue(emitter.lods[0].sort_back_to_front)
        self.assertEqual(emitter.lods[0].birth_rate.value, 10.0)
        self.assertEqual(emitter.material.base_material, "fx\\shaders\\water.w2mg")
        self.assertEqual(emitter.material.parameter("reflection_multiplier"), 20.0)
        self.assertEqual(
            emitter.material.parameter("normal_and_splash"),
            "fx\\textures\\ring.xbm",
        )

    def test_decodes_random_and_start_end_evaluators(self):
        vector_start = Node(
            "start",
            "Vector",
            children=[Node("X", "Float", 0.5), Node("Y", "Float", 1.0)],
        )
        vector_end = Node(
            "end",
            "Vector",
            children=[Node("X", "Float", 2.0), Node("Y", "Float", 1.5)],
        )
        cr2w = types.SimpleNamespace(
            start=0,
            CHUNKS=types.SimpleNamespace(
                CHUNKS=[
                    chunk("CEvaluatorFloatRandomUniform", (Node("max", "Float", 2.0),)),
                    chunk("CEvaluatorVectorStartEnd", (vector_start, vector_end)),
                ]
            ),
            CR2WExport=[
                types.SimpleNamespace(parentID=0, dataOffset=0, dataSize=0),
                types.SimpleNamespace(parentID=0, dataOffset=0, dataSize=0),
            ],
        )

        random = _decode_evaluator(cr2w, 0, b"")
        start_end = _decode_evaluator(cr2w, 1, b"")

        self.assertEqual(random.minimum, 0.0)
        self.assertEqual(random.maximum, 2.0)
        self.assertEqual(start_end.start["X"], 0.5)
        self.assertEqual(start_end.end["Y"], 1.5)
        self.assertEqual(start_end.free_axes, "FVA_Three")
        self.assertEqual(_pointer_index({"chunk_index": 11}), 11)

    def test_decodes_default_burst_stored_as_count_only(self):
        burst_list = Node("burstList", "array:2,0,ParticleBurst", children=[])
        burst_list.Count = 1

        bursts = _decode_bursts(burst_list)

        self.assertEqual(len(bursts), 1)
        self.assertEqual(bursts[0].time, 0.0)
        self.assertEqual(bursts[0].spawn_count, 1)
        self.assertEqual(bursts[0].spawn_time_range, 0.0)
        self.assertEqual(bursts[0].repeat_time, 0.0)

    def test_rejects_a_file_without_a_particle_system(self):
        cr2w = types.SimpleNamespace(
            start=0,
            CHUNKS=types.SimpleNamespace(CHUNKS=[]),
            CR2WExport=[],
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "not_particle.w2p"
            source.write_bytes(b"")
            with mock.patch("witcher3_tools.CR2W.CR2W_file.read_CR2W", return_value=cr2w):
                with self.assertRaisesRegex(ValueError, "CParticleSystem"):
                    load_bin_particle(source)


if __name__ == "__main__":
    unittest.main()
