import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "witcher3_tools" / "read_game_bin.py"
SPEC = importlib.util.spec_from_file_location("read_game_bin_for_test", MODULE_PATH)
read_game_bin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(read_game_bin)


class WitcherGamePathTests(unittest.TestCase):
    def test_accepts_dx11_dx12_and_direct_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            game_root = Path(temp_dir)
            dx11_exe = game_root / "bin" / "x64" / "witcher3.exe"
            dx12_exe = game_root / "bin" / "x64_dx12" / "witcher3.exe"
            dx11_exe.parent.mkdir(parents=True)
            dx12_exe.parent.mkdir(parents=True)
            dx11_exe.touch()
            dx12_exe.touch()

            self.assertEqual(read_game_bin.get_witcher3_exe_path(game_root), os.path.normpath(dx12_exe))
            self.assertTrue(read_game_bin.is_valid_witcher3_game_path(game_root))

            dx12_exe.unlink()
            self.assertEqual(read_game_bin.get_witcher3_exe_path(game_root), os.path.normpath(dx11_exe))
            self.assertTrue(read_game_bin.is_valid_witcher3_game_path(game_root))
            dx12_exe.touch()
            self.assertTrue(read_game_bin.is_valid_witcher3_game_path(dx12_exe))
            self.assertEqual(read_game_bin.get_witcher3_game_root(dx12_exe), os.path.normpath(game_root))

            desktop_exe = game_root / "bin" / "Gaming.Desktop.x64" / "witcher3.exe"
            desktop_exe.parent.mkdir(parents=True)
            desktop_exe.touch()
            dx11_exe.unlink()
            dx12_exe.unlink()
            self.assertEqual(read_game_bin.get_witcher3_exe_path(game_root), os.path.normpath(desktop_exe))
            self.assertEqual(read_game_bin.get_witcher3_game_root(desktop_exe), os.path.normpath(game_root))


if __name__ == "__main__":
    unittest.main()
