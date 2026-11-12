import glob
import hashlib
import math
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if "witcher3_tools" not in sys.modules:
    _pkg = types.ModuleType("witcher3_tools")
    _pkg.__path__ = [str(REPO_ROOT / "witcher3_tools")]
    _pkg.__package__ = "witcher3_tools"
    _pkg.get_addon_name = lambda: "witcher3_tools"
    sys.modules["witcher3_tools"] = _pkg

from witcher3_tools.CR2W import anims_builder, cr2w_writer, dc_anims
from witcher3_tools.CR2W import w2anims_inject as wi

UNCOOK_ROOTS = (
    os.environ.get("W3_UNCOOK", ""),
    r"C:\Users\david\AppData\Roaming\Blender Foundation\Blender\5.0\extensions\.user\vscode_development\witcher3_tools\witcher_uncook",
    r"E:\w3.modding\modkit\r4data",
)
REDKIT_DEPOT = Path(os.environ.get("W3_REDKIT_DEPOT", r"C:\w3.modding\The Witcher 3 REDkit\r4data"))
BEAR_SET = "animations/animals/bear/bear_animation.w2anims"
BEAR_RIG = "characters/base_entities/bear_berserker_base/bear_berserker_base.w2rig"


def _uncook_root():
    for root in UNCOOK_ROOTS:
        if root and (Path(root) / BEAR_SET).is_file():
            return Path(root)
    return None


def _spec(name, num_bones, num_frames=10, replace=None):
    bones = []
    for b in range(num_bones):
        angle = 0.05 * (b % 3)
        bones.append({
            "name": f"b{b}",
            "pos_frames": [(0.01 * b, 0.1 * f, 0.0) for f in range(num_frames)],
            "rot_frames": [(0.0, 0.0, math.sin(angle * f), math.cos(angle * f)) for f in range(num_frames)],
            "scale_frames": [(1.0, 1.0, 1.0)] * num_frames,
        })
    spec = {"action_name": name, "bones": bones, "num_frames": num_frames, "dt": 1 / 30, "fps": 30.0}
    if replace:
        spec["replace"] = replace
    return spec


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _real_common_blender():
    # Discard stubs left by other tests before the reader imports this module.
    mod = sys.modules.get("witcher3_tools.CR2W.common_blender")
    if mod is not None and not hasattr(mod, "extract_missing_buffers"):
        del sys.modules["witcher3_tools.CR2W.common_blender"]
        import importlib
        importlib.import_module("witcher3_tools.CR2W.common_blender")


def _clip(path, name, rig):
    _real_common_blender()
    anim_set = dc_anims.load_bin_anims_single(str(path), name, rigPath=str(rig))
    return next(e.animation for e in anim_set.animations if e.animation.name == name)


def _frames(anim):
    return [(b.BoneName, b.positionFrames, [(q.X, q.Y, q.Z, q.W) for q in b.rotationFramesQuat], b.scaleFrames)
            for b in anim.animBuffer.bones]


def _check_synthetic(test, anim, frames=10):
    test.assertAlmostEqual(anim.duration, (frames - 1) / 30, places=5)
    bone = anim.animBuffer.bones[7]
    for f, pos in enumerate(bone.positionFrames):
        test.assertAlmostEqual(pos[0], 0.07, places=2)
        test.assertAlmostEqual(pos[1], 0.1 * f, places=2)
    test.assertAlmostEqual(bone.rotationFramesQuat[3].Z, math.sin(0.15), places=3)


class NameHashTests(unittest.TestCase):
    def test_engine_hash(self):
        self.assertEqual(wi.name_hash(""), 0)
        self.assertEqual(wi.name_hash("CSkeletalAnimationSet"), cr2w_writer._fnv1a32(b"CSkeletalAnimationSet\0"))


class SyntheticSetTests(unittest.TestCase):
    """Exporter fixtures use uncooked data and embedded buffers."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "src" / "set.w2anims"
        cr2w_writer.write_w2anims(anims_builder.build_w2anims([_spec("a", 4), _spec("b", 4)]), str(self.src))

    def test_zero_splice_identical(self):
        data = self.src.read_bytes()
        raw = wi.RawCR2W(data)
        self.assertEqual(wi._splice(raw, wi.describe_set(raw), [])[0], data)

    def test_add_to_uncooked_set_embeds(self):
        out = self.tmp / "out" / "set.w2anims"
        report = wi.inject_file(self.src, out, [_spec("c", 4)])
        self.assertEqual(report["added"], ["c"])
        self.assertTrue(report["uncooked"] and report["embedded"])
        self.assertEqual(report["new_buffer_files"], [])
        self.assertEqual(list(out.parent.glob("*.buffer")), [])
        raw = wi.RawCR2W(out.read_bytes())
        self.assertTrue(all(b[2] for b in raw.buffers))
        names = [e.animation.name for e in dc_anims.load_bin_anims_info(str(out)).animations]
        self.assertEqual(names, ["a", "b", "c"])

    def test_replace_in_uncooked_set(self):
        out = self.tmp / "out" / "set.w2anims"
        src_raw = wi.RawCR2W(self.src.read_bytes())
        before = wi.describe_set(src_raw)
        report = wi.inject_file(self.src, out, [_spec("whatever", 4, num_frames=5, replace="B")])
        self.assertEqual(report["replaced"], ["b"])
        out_raw = wi.RawCR2W(out.read_bytes())
        after = wi.describe_set(out_raw)
        self.assertEqual([c["name"] for c in after], ["a", "b"])
        for key in ("entry", "anim", "buffer", "buffer_index"):
            self.assertEqual(after[1][key], before[1][key])
        self.assertAlmostEqual(after[1]["duration"], 4 / 30, places=5)
        self.assertEqual(out_raw.chunk(before[0]["anim"]), src_raw.chunk(before[0]["anim"]))

    def test_in_place_needs_backup(self):
        before = _sha(self.src)
        with self.assertRaisesRegex(wi.InjectError, "different file"):
            wi.inject_file(self.src, self.src, [_spec("c", 4)])
        self.assertEqual(_sha(self.src), before)
        report = wi.inject_file(self.src, self.src, [_spec("c", 4)], backup_dir=str(self.tmp / "backups"))
        self.assertEqual(_sha(Path(report["backup"]) / "set.w2anims"), before)
        self.assertNotEqual(_sha(self.src), before)

    def test_refusals(self):
        before = _sha(self.src)
        with self.assertRaisesRegex(wi.InjectError, "already exists"):
            wi.inject_file(self.src, self.tmp / "o1.w2anims", [_spec("A", 4)])
        with self.assertRaisesRegex(wi.InjectError, "targeted twice"):
            wi.inject_file(self.src, self.tmp / "o2.w2anims", [_spec("c", 4), _spec("C", 4)])
        with self.assertRaisesRegex(wi.InjectError, "targeted twice"):
            wi.inject_file(self.src, self.tmp / "o2.w2anims", [_spec("x", 4, replace="a"), _spec("y", 4, replace="A")])
        with self.assertRaisesRegex(wi.InjectError, "bones"):
            wi.inject_file(self.src, self.tmp / "o3.w2anims", [_spec("c", 5)])
        with self.assertRaisesRegex(wi.InjectError, "bones"):
            wi.inject_file(self.src, self.tmp / "o3.w2anims", [_spec("c", 5, replace="a")])
        with self.assertRaisesRegex(wi.InjectError, "not in the set"):
            wi.inject_file(self.src, self.tmp / "o4.w2anims", [_spec("c", 4, replace="nope")])
        self.assertEqual(_sha(self.src), before)
        self.assertEqual(sorted(p.name for p in self.tmp.glob("o*")), [])

    def test_backups_do_not_overwrite_each_other(self):
        before = self.src.read_bytes()
        with mock.patch.object(wi.datetime, 'datetime') as clock:
            clock.now.return_value.strftime.return_value = 'same_second'
            first = wi.backup_file(self.src, self.tmp / 'backups')
            self.src.write_bytes(b'changed')
            second = wi.backup_file(self.src, self.tmp / 'backups')
        self.assertNotEqual(first, second)
        self.assertEqual((Path(first) / self.src.name).read_bytes(), before)
        self.assertEqual((Path(second) / self.src.name).read_bytes(), b'changed')

    def test_rebuild_validates_before_replacing_workspace(self):
        before = self.src.read_bytes()
        broken = self.tmp / 'broken.w2anims'
        broken.write_bytes(b'corrupt')
        with self.assertRaises(wi.InjectError):
            wi.rebuild_workspace(broken, self.src, self.tmp / 'backups')
        self.assertEqual(self.src.read_bytes(), before)
        self.assertFalse((self.tmp / 'backups').exists())

    def test_corruption(self):
        good = self.src.read_bytes()
        raw = wi.RawCR2W(good)
        data = bytearray(good)
        data[raw.tables[1][0] + 3] ^= 0xFF
        with self.assertRaisesRegex(wi.InjectError, "CRC"):
            wi.inject_bytes(bytes(data), [_spec("c", 4)])
        with self.assertRaisesRegex(wi.InjectError, "trailing"):
            wi.inject_bytes(good + b"\0", [_spec("c", 4)])
        data = bytearray(good)
        data[raw.exports[1][4] + 3] ^= 0xFF
        with self.assertRaisesRegex(wi.InjectError, "could not be read"):
            wi.inject_bytes(bytes(data), [_spec("c", 4)])
        data = bytearray(good)
        last = raw.exports[2][4] + raw.exports[2][3] - 1
        data[last] ^= 0xFF  # Preserve stale object CRCs.
        out, _, report = wi.inject_bytes(bytes(data), [_spec("c", 4)])
        self.assertEqual(report["stale_crcs_kept"], [2])
        self.assertEqual(wi.RawCR2W(out).stale_crcs, [2])


@unittest.skipUnless(_uncook_root(), "vanilla uncook not available")
class VanillaTests(unittest.TestCase):
    def setUp(self):
        self.root = _uncook_root()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.root / BEAR_SET
        self.rig = self.root / BEAR_RIG
        self.src_hashes = {p: _sha(p) for p in self.src.parent.glob("bear_animation.w2anims*")}
        self.clips = wi.describe_set(wi.RawCR2W(self.src.read_bytes()))

    def tearDown(self):
        self.assertEqual({p: _sha(p) for p in self.src.parent.glob("bear_animation.w2anims*")}, self.src_hashes)

    def _same_frames(self, a, b, name):
        self.assertEqual(_frames(_clip(a, name, self.rig)), _frames(_clip(b, name, self.rig)))

    def test_zero_splice_identical_for_every_set(self):
        checked = 0
        for root in [self.root] + ([REDKIT_DEPOT] if REDKIT_DEPOT.is_dir() else []):
            for path in glob.glob(str(root / "**" / "*.w2anims"), recursive=True):
                data = Path(path).read_bytes()
                try:
                    raw = wi.RawCR2W(data)
                    clips = wi.describe_set(raw)
                except wi.InjectError:
                    continue
                for i in range(1, len(raw.names)):
                    self.assertEqual(raw.names[i][1], wi.name_hash(raw.name(i)), path)
                self.assertEqual(wi._splice(raw, clips, [])[0], data, path)
                checked += 1
        self.assertGreater(checked, 400)

    def test_add_to_cooked_set(self):
        out = self.tmp / BEAR_SET
        bones = self.clips[0]["bones"]
        report = wi.inject_file(self.src, out, [_spec("inject_test_a", bones), _spec("inject_test_b", bones)])
        self.assertFalse(report["uncooked"] or report["embedded"])
        self.assertEqual(report["missing_buffers"], [])
        self.assertEqual(report["copied_buffers"], len(self.src_hashes) - 1)
        for p, h in self.src_hashes.items():
            if p != self.src:
                self.assertEqual(_sha(out.parent / p.name), h)
        names = [e.animation.name for e in dc_anims.load_bin_anims_info(str(out)).animations]
        self.assertEqual(names, [c["name"] for c in self.clips] + ["inject_test_a", "inject_test_b"])
        self._same_frames(out, self.src, self.clips[0]["name"])
        self._same_frames(out, self.src, self.clips[-1]["name"])
        _check_synthetic(self, _clip(out, "inject_test_b", self.rig))

    def test_replace_in_cooked_set(self):
        target = self.clips[2]
        out = self.tmp / BEAR_SET
        report = wi.inject_file(self.src, out, [_spec("strut", target["bones"], replace=target["name"])])
        self.assertEqual(report["replaced"], [target["name"]])
        self.assertEqual(report["new_buffer_files"], [target["buffer_index"]])
        src_raw, out_raw = wi.RawCR2W(self.src.read_bytes()), wi.RawCR2W(out.read_bytes())
        after = wi.describe_set(out_raw)
        self.assertEqual([c["name"] for c in after], [c["name"] for c in self.clips])
        self.assertEqual(after[2]["events"], target["events"])
        self.assertEqual(out_raw.chunk(target["entry"]), src_raw.chunk(target["entry"]))
        replaced_buffer = wi.buffer_path(str(self.src), target["buffer_index"])
        self.assertNotEqual(_sha(wi.buffer_path(str(out), target["buffer_index"])), self.src_hashes[Path(replaced_buffer)])
        _check_synthetic(self, _clip(out, target["name"], self.rig))
        self._same_frames(out, self.src, self.clips[1]["name"])
        self._same_frames(out, self.src, self.clips[3]["name"])

    def test_copy_into_workspace_embeds(self):
        dst = self.tmp / "workspace" / BEAR_SET
        wi.copy_into_workspace(str(self.src), str(dst))
        raw = wi.RawCR2W(dst.read_bytes())
        self.assertTrue(all(b[2] for b in raw.buffers))
        self.assertEqual(list(dst.parent.glob("*.buffer")), [])
        self._same_frames(dst, self.src, self.clips[0]["name"])
        self._same_frames(dst, self.src, self.clips[-1]["name"])
        with self.assertRaisesRegex(wi.InjectError, "already exists"):
            wi.copy_into_workspace(str(self.src), str(dst))
        report = wi.inject_file(dst, dst, [_spec("x", 98, replace=self.clips[0]["name"])],
                                embed_buffers=True, backup_dir=str(self.tmp / "bk"))
        self.assertTrue(report["embedded"])
        self.assertEqual(list(dst.parent.glob("*.buffer")), [])
        _check_synthetic(self, _clip(dst, self.clips[0]["name"], self.rig))
        self._same_frames(dst, self.src, self.clips[1]["name"])

    def test_stale_companion_buffer_refused(self):
        src_dir = self.tmp / "src"
        src_dir.mkdir()
        for p in self.src.parent.glob("bear_animation.w2anims*"):
            shutil.copyfile(p, src_dir / p.name)
        src = src_dir / "bear_animation.w2anims"
        stale = Path(wi.buffer_path(str(src), 5))
        stale.write_bytes(stale.read_bytes()[:-1])
        out = self.tmp / "out" / "bear_animation.w2anims"
        with self.assertRaisesRegex(wi.InjectError, "stale"):
            wi.inject_file(src, out, [_spec("x", 98)])
        self.assertFalse(out.parent.exists() and any(out.parent.iterdir()))

    def test_failed_multi_file_write_restores_copy(self):
        out = self.tmp / BEAR_SET
        bones = self.clips[0]["bones"]
        wi.inject_file(self.src, out, [_spec("first", bones)])
        before = {p.name: _sha(p) for p in out.parent.iterdir()}
        replace = os.replace
        failed = False

        def locked_buffer(src, dst):
            nonlocal failed
            if str(dst).endswith('.1.buffer') and not failed:
                failed = True
                raise PermissionError("buffer locked")
            return replace(src, dst)

        with mock.patch.object(wi.os, 'replace', side_effect=locked_buffer):
            with self.assertRaisesRegex((OSError, wi.InjectError), 'buffer locked'):
                wi.inject_file(self.src, out, [_spec("second", bones, num_frames=5)])
        self.assertEqual({p.name: _sha(p) for p in out.parent.iterdir()}, before)


@unittest.skipUnless((REDKIT_DEPOT / BEAR_SET).is_file() and _uncook_root(), "REDkit depot not available")
class RedkitDepotTests(unittest.TestCase):
    def test_replace_and_add_uncooked(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        src = REDKIT_DEPOT / BEAR_SET
        rig = _uncook_root() / BEAR_RIG
        before = _sha(src)
        clips = wi.describe_set(wi.RawCR2W(src.read_bytes()))
        dst = tmp / BEAR_SET
        dst.parent.mkdir(parents=True)
        shutil.copyfile(src, dst)
        bones = clips[0]["bones"]
        target = next(c for c in clips if c["me"] is not None and c["ume"] is not None)
        replacement = _spec("x", bones, replace=target["name"])
        replacement["motion_extraction"] = {
            "duration": 0.3, "frames": [0.0, 0.9], "delta_times": [9], "flags": 2,
            "uncompressed_frames": [(0.0, 0.1 * f, 0.0, 0.0) for f in range(10)],
        }
        report = wi.inject_file(dst, dst, [replacement, _spec("brand_new", bones)], backup_dir=str(tmp / "bk"))
        self.assertEqual(_sha(src), before)
        self.assertTrue(report["uncooked"] and report["embedded"])
        self.assertTrue(report["stale_crcs_kept"])
        raw = wi.RawCR2W(dst.read_bytes())
        after = wi.describe_set(raw)
        self.assertEqual([c["name"] for c in after], [c["name"] for c in clips] + ["brand_new"])
        new = next(c for c in after if c["name"] == target["name"])
        self.assertEqual((new["me"], new["ume"]), (target["me"], target["ume"]))
        self.assertEqual(raw.chunk_class(new["ume"]), "CUncompressedMotionExtraction")
        self.assertEqual(report["orphaned_chunks"], [])
        _check_synthetic(self, _clip(dst, target["name"], rig))
        self.assertEqual(_frames(_clip(dst, clips[0]["name"], rig)), _frames(_clip(src, clips[0]["name"], rig)))


if __name__ == "__main__":
    unittest.main()
