import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _install_namespace_stub(qualified_name: str, package_path: Path) -> None:
    if qualified_name in sys.modules:
        return
    module = types.ModuleType(qualified_name)
    module.__path__ = [str(package_path)]
    module.__package__ = qualified_name
    sys.modules[qualified_name] = module


_install_namespace_stub("witcher3_tools", REPO_ROOT / "witcher3_tools")
_install_namespace_stub("witcher3_tools.CR2W", REPO_ROOT / "witcher3_tools" / "CR2W")

from witcher3_tools.CR2W import retarget_anims, w3_types  # noqa: E402


IDENTITY = w3_types.Quaternion(0.0, 0.0, 0.0, 1.0)
YAW_180 = w3_types.Quaternion(0.0, 0.0, 1.0, 0.0)


def _bone(idx, name, parent_id, co, ro_quat=IDENTITY):
    return w3_types.W3Bone(
        idx,
        name,
        list(co),
        parent_id,
        ro_quat=ro_quat,
        sc=[1.0, 1.0, 1.0],
    )


def _anim_bone(idx, name, pos_frames=None, rot_frames=None):
    pos_frames = pos_frames if pos_frames is not None else [[0.0, 0.0, 0.0]]
    rot_frames = rot_frames if rot_frames is not None else [IDENTITY]
    return w3_types.w2AnimsFrames(
        idx,
        BoneName=name,
        position_dt=1.0 / 30.0,
        position_numFrames=len(pos_frames),
        positionFrames=pos_frames,
        rotation_dt=1.0 / 30.0,
        rotation_numFrames=len(rot_frames),
        rotationFrames=rot_frames,
        scale_dt=1.0 / 30.0,
        scale_numFrames=1,
        scaleFrames=[[1.0, 1.0, 1.0]],
        rotationFramesQuat=rot_frames,
    )


def _entry_with_weapon_animation():
    buffer = w3_types.CAnimationBufferBitwiseCompressed(
        bones=[
            _anim_bone(
                6,
                "r_weapon",
                pos_frames=[
                    [0.08, 0.03, 0.0],
                    [0.10, 0.04, 0.0],
                ],
            ),
        ],
        tracks=[],
        duration=1.0 / 30.0,
        numFrames=2,
        dt=1.0 / 30.0,
    )
    anim = w3_types.CSkeletalAnimation(
        name="synthetic_weapon_retarget",
        duration=1.0 / 30.0,
        framesPerSecond=30.0,
        animBuffer=buffer,
    )
    return w3_types.CSkeletalAnimationSetEntry(animation=anim)


def _entry_with_root_motion(root_rot, trajectory_positions, pelvis_positions):
    num_frames = len(trajectory_positions)
    buffer = w3_types.CAnimationBufferBitwiseCompressed(
        bones=[
            _anim_bone(
                0,
                "Root",
                pos_frames=[[0.0, 0.0, 0.0] for _ in range(num_frames)],
                rot_frames=[root_rot for _ in range(num_frames)],
            ),
            _anim_bone(
                1,
                "Trajectory",
                pos_frames=trajectory_positions,
                rot_frames=[IDENTITY for _ in range(num_frames)],
            ),
            _anim_bone(
                2,
                "pelvis",
                pos_frames=pelvis_positions,
                rot_frames=[IDENTITY for _ in range(num_frames)],
            ),
        ],
        tracks=[],
        duration=(num_frames - 1) / 30.0,
        numFrames=num_frames,
        dt=1.0 / 30.0,
    )
    anim = w3_types.CSkeletalAnimation(
        name="synthetic_root_motion_retarget",
        duration=(num_frames - 1) / 30.0,
        framesPerSecond=30.0,
        animBuffer=buffer,
    )
    return w3_types.CSkeletalAnimationSetEntry(animation=anim)


def _source_skeleton():
    return w3_types.CSkeleton([
        _bone(0, "Root", -1, (0.0, 0.0, 0.0)),
        _bone(1, "Trajectory", 0, (0.0, 0.0, 0.0), ro_quat=YAW_180),
        _bone(2, "pelvis", 1, (0.0, 0.0, 0.0)),
        _bone(3, "l_hand", 2, (-0.5, 0.0, 0.0)),
        _bone(4, "r_hand", 2, (0.5, 0.0, 0.0)),
        _bone(5, "l_weapon", 3, (0.06, 0.02, 0.0)),
        _bone(6, "r_weapon", 4, (0.08, 0.03, 0.0)),
    ])


def _target_skeleton():
    return w3_types.CSkeleton([
        _bone(0, "Root", -1, (0.0, 0.0, 0.0)),
        _bone(1, "Trajectory", 0, (0.0, 0.0, 0.0)),
        _bone(2, "pelvis", 1, (0.0, 0.0, 0.0)),
        _bone(3, "l_hand", 2, (-0.4, 0.0, 0.0)),
        _bone(4, "r_hand", 2, (0.5, 0.0, 0.0)),
        _bone(5, "l_weapon", 3, (0.0, 0.0, 0.0)),
        _bone(6, "r_weapon", 4, (0.0, 0.0, 0.0)),
    ])


def _source_root_motion_skeleton():
    return w3_types.CSkeleton([
        _bone(0, "Root", -1, (0.0, 0.0, 0.0)),
        _bone(1, "Trajectory", 0, (0.0, 0.0, 0.0), ro_quat=YAW_180),
        _bone(2, "pelvis", 0, (0.0, 0.0, 1.0)),
    ])


def _target_root_motion_skeleton():
    return w3_types.CSkeleton([
        _bone(0, "Root", -1, (0.0, 0.0, 0.0)),
        _bone(1, "Trajectory", 0, (0.0, 0.0, 0.0)),
        _bone(2, "pelvis", 0, (0.0, 0.0, 1.0)),
    ])


def _output_bone(entry, name):
    for bone in entry.animation.animBuffer.bones:
        if bone.BoneName.lower() == name.lower():
            return bone
    raise AssertionError(f"Missing output bone {name}")


def _assert_vec_close(testcase, actual, expected, places=6):
    testcase.assertEqual(len(actual), len(expected))
    for actual_value, expected_value in zip(actual, expected):
        testcase.assertAlmostEqual(actual_value, expected_value, places=places)


def _world_positions_at(entry, skeleton, frame_idx):
    buffer = entry.animation.animBuffer
    bones = retarget_anims._skeleton_bones(skeleton)
    anim_bones = retarget_anims._source_anim_bone_map(buffer)
    base_dt = retarget_anims._animation_base_dt(
        entry.animation,
        buffer,
        retarget_anims._animation_frame_count(buffer, list(anim_bones.values())),
    )
    rest_pos = [retarget_anims._bone_rest_pos(bone) for bone in bones]
    rest_rot = [retarget_anims._bone_rest_rot(bone) for bone in bones]
    world_pos = []
    world_rot = []
    for idx, bone in enumerate(bones):
        name = retarget_anims._bone_name(bone)
        local_pos, local_rot = retarget_anims._sample_source_local(
            anim_bones.get(name.lower()),
            rest_pos[idx],
            rest_rot[idx],
            frame_idx,
            base_dt,
        )
        parent_idx = retarget_anims._bone_parent_id(bone)
        if 0 <= parent_idx < idx:
            world_pos.append(retarget_anims._vec_add(
                world_pos[parent_idx],
                retarget_anims._quat_rotate_vec(world_rot[parent_idx], local_pos),
            ))
            world_rot.append(retarget_anims._quat_mul(world_rot[parent_idx], local_rot))
        else:
            world_pos.append(local_pos)
            world_rot.append(local_rot)
    return {
        retarget_anims._bone_name(bone).lower(): world_pos[idx]
        for idx, bone in enumerate(bones)
    }


class RetargetFacingBasisTests(unittest.TestCase):
    def test_weapon_offsets_use_same_facing_basis_as_body(self):
        retargeted = retarget_anims.retarget_w2_animation_entry(
            _entry_with_weapon_animation(),
            _source_skeleton(),
            _target_skeleton(),
            hand_fit="off",
        )

        root = _output_bone(retargeted, "Root")
        r_weapon = _output_bone(retargeted, "r_weapon")

        _assert_vec_close(self, root.rotationFramesQuat[0].__json_serializable__(), [0.0, 0.0, 1.0, 0.0])
        _assert_vec_close(self, r_weapon.positionFrames[0], [0.08, 0.03, 0.0])
        _assert_vec_close(self, r_weapon.positionFrames[1], [0.10, 0.04, 0.0])

    def test_hand_fit_uses_same_facing_basis_as_weapon_offsets(self):
        retargeted = retarget_anims.retarget_w2_animation_entry(
            _entry_with_weapon_animation(),
            _source_skeleton(),
            _target_skeleton(),
            hand_fit="weapon_grip",
        )

        l_hand = _output_bone(retargeted, "l_hand")

        _assert_vec_close(self, l_hand.positionFrames[0], [-0.5, 0.0, 0.0])
        self.assertLess(abs(l_hand.positionFrames[0][0]), 0.75)

    def test_root_motion_positions_keep_forward_direction_when_source_root_is_identity(self):
        retargeted = retarget_anims.retarget_w2_animation_entry(
            _entry_with_root_motion(
                IDENTITY,
                [[0.0, 0.0, 0.0], [0.0, 2.0, 0.0]],
                [[0.0, 0.0, 1.0], [0.0, 2.0, 1.0]],
            ),
            _source_root_motion_skeleton(),
            _target_root_motion_skeleton(),
            hand_fit="off",
        )

        first = _world_positions_at(retargeted, _target_root_motion_skeleton(), 0)
        last = _world_positions_at(retargeted, _target_root_motion_skeleton(), 1)

        self.assertGreater(last["trajectory"][1] - first["trajectory"][1], 1.9)
        self.assertGreater(last["pelvis"][1] - first["pelvis"][1], 1.9)

    def test_root_motion_positions_keep_forward_direction_when_source_root_is_flipped(self):
        retargeted = retarget_anims.retarget_w2_animation_entry(
            _entry_with_root_motion(
                YAW_180,
                [[0.0, 0.0, 0.0], [0.0, -3.0, 0.0]],
                [[0.0, 0.0, 1.0], [0.0, -3.0, 1.0]],
            ),
            _source_root_motion_skeleton(),
            _target_root_motion_skeleton(),
            hand_fit="off",
        )

        first = _world_positions_at(retargeted, _target_root_motion_skeleton(), 0)
        last = _world_positions_at(retargeted, _target_root_motion_skeleton(), 1)

        self.assertGreater(last["trajectory"][1] - first["trajectory"][1], 2.9)
        self.assertGreater(last["pelvis"][1] - first["pelvis"][1], 2.9)


if __name__ == "__main__":
    unittest.main()
