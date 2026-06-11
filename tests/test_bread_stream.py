"""Tests for bReadStream and the cr2w_buf fast paths in bin_helpers.

The peek helpers (FileSize, readU32Check, ...) have two code paths: a
seek/read/seek fallback for plain file objects and a struct.unpack_from fast
path for cr2w_buf-backed streams. Both must return identical values and leave
the stream position untouched.
"""

import io
import os
import struct
import sys
import tempfile
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

from witcher3_tools.CR2W.bStream import bReadStream, open_cr2w_read_stream
from witcher3_tools.CR2W import bin_helpers

for _name in [n for n in list(sys.modules) if n == "witcher3_tools" or n.startswith("witcher3_tools.")]:
    sys.modules.pop(_name, None)


DATA = struct.pack("<IHBf", 0xDEADBEEF, 0x1234, 0x7F, 3.5) + b"hello\x00world\x00" + bytes(range(32))


class BReadStreamTests(unittest.TestCase):
    def test_basic_stream_api(self):
        s = bReadStream(DATA, name="test.bin")
        self.assertEqual(s.name, "test.bin")
        self.assertEqual(s.cr2w_buf, DATA)
        self.assertEqual(s.read(4), DATA[:4])
        self.assertEqual(s.tell(), 4)
        s.seek(2)
        self.assertEqual(s.tell(), 2)
        s.seek(0, os.SEEK_END)
        self.assertEqual(s.tell(), len(DATA))

    def test_accepts_bytearray_and_memoryview(self):
        for raw in (bytearray(DATA), memoryview(DATA)):
            s = bReadStream(raw)
            self.assertIsInstance(s.cr2w_buf, bytes)
            self.assertEqual(s.read(), DATA)

    def test_write_is_blocked(self):
        s = bReadStream(DATA)
        with self.assertRaises(io.UnsupportedOperation):
            s.write(b"xx")
        with self.assertRaises(io.UnsupportedOperation):
            s.truncate(0)

    def test_open_cr2w_read_stream(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as fh:
            fh.write(DATA)
            path = fh.name
        try:
            s = open_cr2w_read_stream(path)
            self.assertEqual(s.cr2w_buf, DATA)
            self.assertEqual(s.name, path)
        finally:
            os.unlink(path)


class FastPathEquivalenceTests(unittest.TestCase):
    """Fast path (bReadStream) and fallback (BytesIO) must agree exactly."""

    def _pair(self, pos=0):
        fast = bReadStream(DATA)
        slow = io.BytesIO(DATA)
        fast.seek(pos)
        slow.seek(pos)
        return fast, slow

    def _check_helper(self, fn, *args):
        for start_pos in (0, 3, 9):
            fast, slow = self._pair(start_pos)
            self.assertEqual(fn(fast, *args), fn(slow, *args))
            self.assertEqual(fast.tell(), start_pos, "fast path moved the stream")
            self.assertEqual(slow.tell(), start_pos, "fallback moved the stream")

    def test_filesize(self):
        fast, slow = self._pair(5)
        self.assertEqual(bin_helpers.FileSize(fast), len(DATA))
        self.assertEqual(bin_helpers.FileSize(slow), len(DATA))
        self.assertEqual(fast.tell(), 5)
        self.assertEqual(slow.tell(), 5)

    def test_peek_helpers(self):
        self._check_helper(bin_helpers.readU32Check, 0)
        self._check_helper(bin_helpers.readU32Check, 7)
        self._check_helper(bin_helpers.readUShortCheck, 4)
        self._check_helper(bin_helpers.readUByteCheck, 6)
        self._check_helper(bin_helpers.readFloatCheck, 7)
        self._check_helper(bin_helpers.detectedFloat, 7)
        self._check_helper(bin_helpers.detectedFloat, 0)

    def test_peek_past_end_raises_struct_error_on_both(self):
        for fn, pos in ((bin_helpers.readU32Check, len(DATA) - 2),
                        (bin_helpers.readUShortCheck, len(DATA) - 1),
                        (bin_helpers.readUByteCheck, len(DATA))):
            fast, slow = self._pair()
            with self.assertRaises(struct.error):
                fn(fast, pos)
            with self.assertRaises(struct.error):
                fn(slow, pos)

    def test_getstring(self):
        start = struct.calcsize("<IHBf")
        for stream in (bReadStream(DATA), io.BytesIO(DATA)):
            stream.seek(start)
            self.assertEqual(bin_helpers.getString(stream), "hello")
            self.assertEqual(bin_helpers.getString(stream), "world")
            self.assertEqual(stream.tell(), start + len(b"hello\x00world\x00"))

    def test_getstring_unterminated_reads_to_end(self):
        raw = b"abc"
        for stream in (bReadStream(raw), io.BytesIO(raw)):
            self.assertEqual(bin_helpers.getString(stream), "abc")
            self.assertEqual(stream.tell(), len(raw))


if __name__ == "__main__":
    unittest.main()
