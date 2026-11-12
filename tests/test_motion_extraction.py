import glob
import math
import os
import struct
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
    _pkg.get_addon_name = lambda: "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.animation import motion_extraction as mx
from witcher3_tools.CR2W import w2anims_inject as wi

REDKIT_ANIMS = Path(os.environ.get("W3_REDKIT_DEPOT", r"C:\w3.modding\The Witcher 3 REDkit\r4data")) / "animations"


def _yaw_quat(yaw):
    return (math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2))


def _floats(raw, idx, prop):
    v = raw.top_props(idx)[prop][2]
    return list(struct.unpack_from(f"<{struct.unpack_from('<I', v)[0]}f", v, 4))


def _vectors(raw, idx):
    v = raw.top_props(idx)["frames"][2]
    out, o = [], 4
    for _ in range(struct.unpack_from("<I", v)[0]):
        vals, o = {}, o + 1
        while True:
            n = struct.unpack_from("<H", v, o)[0]
            if n == 0:
                o += 2
                break
            _, size = struct.unpack_from("<HI", v, o + 2)
            vals[raw.name(n)] = struct.unpack_from("<f", v, o + 8)[0]
            o += 4 + size
        out.append(tuple(vals.get(k, 0.0) for k in "XYZW"))
    return out


class SyntheticTests(unittest.TestCase):
    def test_straight_walk_is_two_keys(self):
        frames = [(0.0, 0.065 * i, 0.0, 0.0) for i in range(31)]
        me = mx.compress(frames, 1.0)
        self.assertEqual(me["flags"], mx.FLAG_Y)
        self.assertEqual(me["delta_times"], [30])
        self.assertEqual(me["frames"], [0.0, frames[-1][1]])
        self.assertAlmostEqual(mx.sample(me, 0.5)[1], frames[15][1], places=6)

    def test_no_motion_writes_nothing(self):
        self.assertIsNone(mx.compress([(0.005, 0.0, 0.0, 0.3)] * 20, 1.0))
        self.assertIsNone(mx.compress([], 1.0))

    def test_yaw_threshold_is_19_degrees(self):
        turn = [(0.0, 0.0, 0.0, 0.32 * i / 20) for i in range(21)]
        self.assertIsNone(mx.compress(turn, 1.0))
        turn = [(0.0, 0.0, 0.0, 0.34 * i / 20) for i in range(21)]
        self.assertEqual(mx.compress(turn, 1.0)["flags"], mx.FLAG_YAW)

    def test_forced_axes(self):
        frames = [(0.0, 0.1 * i, 0.0, 0.0) for i in range(10)]
        me = mx.compress(frames, 0.3, flags=mx.FLAG_X | mx.FLAG_Y)
        self.assertEqual(me["flags"], mx.FLAG_X | mx.FLAG_Y)
        self.assertEqual(len(me["frames"]), 2 * (len(me["delta_times"]) + 1))

    def test_key_gap_capped_at_255(self):
        frames = [(0.0, 0.01 * i, 0.0, 0.0) for i in range(600)]
        me = mx.compress(frames, 20.0)
        self.assertTrue(all(0 < d <= 255 for d in me["delta_times"]))
        self.assertEqual(sum(me["delta_times"]), 599)

    def test_extract_is_relative_to_first_frame(self):
        # At 90 degrees yaw, world -X becomes local +Y.
        positions = [(2.0 - 0.1 * i, 3.0, 0.0) for i in range(10)]
        rotations = [_yaw_quat(math.pi / 2)] * 10
        frames = mx.extract(positions, rotations)
        self.assertAlmostEqual(frames[-1][0], 0.0, places=6)
        self.assertAlmostEqual(frames[-1][1], 0.9, places=6)
        self.assertAlmostEqual(frames[-1][3], 0.0, places=6)

    def test_extract_unwraps_full_turns(self):
        yaws = [i * 0.3 for i in range(30)]  # 8.7 rad, more than a full turn
        frames = mx.extract([(0.0, 0.0, 0.0)] * 30, [_yaw_quat(y) for y in yaws])
        for f, y in zip(frames, yaws):
            self.assertAlmostEqual(f[3], y, places=5)

    def test_summary(self):
        frames = [(0.0, 0.065 * i, 0.0, 0.0) for i in range(31)]
        info = mx.summary(frames, 1.0)
        self.assertAlmostEqual(info["distance"], 1.95, places=6)
        self.assertAlmostEqual(info["speed"], 1.95, places=6)

    def test_round_trip_path_has_nonzero_travel(self):
        frames = [(0, 0, 0, 0), (1, 0, 0, 0), (1, 1, 0, 0), (0, 1, 0, 0), (0, 0, 0, 0)]
        info = mx.summary(frames, 4.0)
        self.assertEqual(info["distance"], 4.0)
        self.assertEqual(info["speed"], 1.0)


@unittest.skipUnless(REDKIT_ANIMS.is_dir(), "REDkit depot not available")
class CdprOracleTests(unittest.TestCase):
    """Compare compression with REDkit's stored raw and compressed curves."""

    def test_matches_cdpr_compression(self):
        checked = exact = 0
        mismatches = []
        for path in glob.glob(str(REDKIT_ANIMS / "man" / "**" / "*.w2anims"), recursive=True):
            try:
                raw = wi.RawCR2W(Path(path).read_bytes())
                clips = wi.describe_set(raw)
            except wi.InjectError:
                continue
            for c in clips:
                if c["ume"] is None or c["me"] is None or raw.chunk_class(c["me"]) != "CLineMotionExtraction2":
                    continue
                ume = raw.top_props(c["ume"])
                duration = struct.unpack("<f", ume["duration"][2][:4])[0] if "duration" in ume else c["duration"]
                ours = mx.compress(_vectors(raw, c["ume"]), duration)
                me = raw.top_props(c["me"])
                dv = me["deltaTimes"][2]
                theirs = (me["flags"][2][0], list(dv[4:4 + struct.unpack_from("<I", dv)[0]]), _floats(raw, c["me"], "frames"))
                checked += 1
                if (ours and (ours["flags"], ours["delta_times"]) == theirs[:2]
                        and all(abs(a - b) < 1e-6 for a, b in zip(ours["frames"], theirs[2]))):
                    exact += 1
                else:
                    mismatches.append((Path(path).name, c["name"]))
        self.assertGreater(checked, 3000)
        self.assertEqual(mismatches, [])


if __name__ == "__main__":
    unittest.main()
