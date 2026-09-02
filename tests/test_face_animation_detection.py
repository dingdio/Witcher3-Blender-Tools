import unittest
from pathlib import Path

from _helpers import exec_functions


SOURCE = Path(__file__).resolve().parents[1] / "witcher3_tools" / "ui" / "ui_anims_list.py"


class FaceAnimationDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.is_face_animation = staticmethod(exec_functions(SOURCE, {"is_face_animation"})["is_face_animation"])

    def test_markers_in_folder_names_do_not_make_body_clips_face_clips(self):
        workspace = r"C:\w3.modding\projects\lipsync_test\workspace\animations\cutscenes\cs.w2cutscene"
        self.assertFalse(self.is_face_animation("cirilla:Root:cs_cirilla", workspace))
        self.assertFalse(self.is_face_animation("geralt:Root:cs", r"D:\my_mimic_project\cs.w2cutscene"))

    def test_face_markers_in_name_file_or_mimics_folder(self):
        self.assertTrue(self.is_face_animation("cirilla:face:cs_cirilla_face", r"C:\x\cs.w2cutscene"))
        self.assertTrue(self.is_face_animation("lipsync", r"C:\x\cs.w2cutscene"))
        self.assertTrue(self.is_face_animation("anim", r"C:\x\geralt_lipsync_01.w2anims"))
        self.assertTrue(self.is_face_animation("anim", r"animations\mimics\anna_henrietta_mimic_animation.w2anims"))
        self.assertTrue(self.is_face_animation("anim", "animations/mimics/ciri_face.w2anims"))


if __name__ == "__main__":
    unittest.main()
