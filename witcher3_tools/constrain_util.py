import logging
import bpy
import sys
import os
from . import file_helpers

log = logging.getLogger(__name__)

#add copytransforms on def bones
def CreateConstraints(arm_parent, arm_child):
    #switch to pose mode and find pose bones    
    bpy.ops.object.mode_set(mode='POSE', toggle=False)

    for tgt_parent_bone in arm_parent.pose.bones:
        tgt_child_bone = False
        p_bone_name = file_helpers.rm_ns(tgt_parent_bone.name)
        log.debug("Checking bone: %s", p_bone_name)

        for cBone in arm_child.pose.bones:
            c_bone_name = file_helpers.rm_ns(cBone.name)
            if c_bone_name == p_bone_name:
                tgt_child_bone = cBone
        if tgt_child_bone:
            log.debug("  Matched: %s -> %s", tgt_child_bone, tgt_parent_bone)


            # for cons in tgt_child_bone.constraints:
            #     tgt_child_bone.constraints.remove(cons)
            child_of = tgt_child_bone.constraints.new('CHILD_OF')
            child_of.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
            child_of.target = arm_parent
            child_of.subtarget = tgt_parent_bone.name
            arm_child.data.bones.active = arm_child.data.bones[tgt_child_bone.name]


            bpy.ops.object.mode_set(mode='EDIT', toggle=False)
            #bone_to_edit = arm_child.data.bones[tgt_child_bone.name]
            #bone_to_edit.parent_clear(type='CLEAR')
            #bpy.context.active_bone.use_local_location = False
            #bpy.context.active_bone.use_inherit_rotation = False
            bpy.context.active_bone.parent = None
            bpy.ops.object.mode_set(mode='POSE', toggle=False)

            bpy.ops.constraint.childof_set_inverse(constraint=tgt_parent_bone.name + " to " + tgt_child_bone.name, owner='BONE')
            #bpy.ops.object.mode_set(mode='POSE', toggle=False)
            # context_py = bpy.context.copy()
            # context_py["constraint"] = child_of
            # arm_child.data.bones.active = tgt_child_bone.bone
            # bpy.ops.constraint.childof_set_inverse(context_py, constraint="Child Of", owner='BONE')

    bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
    return

def constrain_w3_rig(arm_parent, arm_child, mo=False):
    log.info("Creating constraints...")
    CreateConstraints(arm_parent, arm_child)   

def CreateConstraints2(arm_parent: bpy.types.Object, arm_child:bpy.types.Object):
    if not isinstance(arm_parent, bpy.types.Object) or arm_parent.type != 'ARMATURE':
        raise TypeError("arm_parent must be a Blender armature object")
    if not isinstance(arm_child, bpy.types.Object) or arm_child.type != 'ARMATURE':
        raise TypeError("arm_child must be a Blender armature object")

    bpy.ops.object.select_all(action='DESELECT')
    arm_parent.select_set(True)
    arm_child.select_set(True)
    bpy.context.view_layer.objects.active = arm_parent
    objs = bpy.context.selected_objects[:]
    obj = objs[0]
    try:
        bpy.ops.object.mode_set(mode='POSE', toggle=False)
    except Exception as e:
        raise e
    
    #flat_child_hierarchy = all(bone.parent is None for bone in arm_child.data.edit_bones)
    is_dyng = (sum(bone.name.startswith("dyng_") for bone in arm_child.data.bones) > len(arm_child.data.bones) / 2) or 'dyng' in arm_child.name.lower()
    
    for tgt_parent_bone in arm_parent.pose.bones:
        tgt_child_bone = False
        p_bone_name = file_helpers.rm_ns(tgt_parent_bone.name)
        #print(p_bone_name)

        for cBone in arm_child.pose.bones:
            c_bone_name = file_helpers.rm_ns(cBone.name)
            if c_bone_name == p_bone_name:
                tgt_child_bone = cBone
        #some positions of the face rig of a character don't match
        CHILD_OF_list = ['ears', 'jaw', 'tongue1', 'tongue2', 'tongue_right_side', 'tongue_left_side','left_eye', 'right_eye'
                        ,'right_chick1','left_chick1',
                        
                        
                        ]
        #if tgt_child_bone and "ears" not in tgt_child_bone.name and not "eye" == tgt_child_bone.name and not "jaw" == tgt_child_bone.name:

        # if tgt_child_bone and tgt_child_bone.name == "head":
        #     if tgt_child_bone.parent == None:
        #         CHILD_OF_list.remove("head")
        # if tgt_child_bone and not tgt_child_bone.parent:
        #     CHILD_OF_list.append(tgt_child_bone.name)

        if tgt_child_bone and tgt_child_bone.name not in CHILD_OF_list:
            for cons in tgt_child_bone.constraints:
                tgt_child_bone.constraints.remove(cons)
            if tgt_child_bone.parent is None and not is_dyng: #and tgt_parent_bone.parent is None and is_dyng: # check for root bone that needs moving into position
                child_of = tgt_child_bone.constraints.new('CHILD_OF')
                child_of.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
                child_of.target = arm_parent
                child_of.subtarget = tgt_parent_bone.name
            else:
                copyTransform = tgt_child_bone.constraints.new('COPY_TRANSFORMS')
                copyTransform.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
                copyTransform.target = arm_parent
                copyTransform.subtarget = tgt_parent_bone.name
                # # Create and configure the Copy Location constraint
                # copyLocation = tgt_child_bone.constraints.new('COPY_LOCATION')
                # copyLocation.name = "Copy Location: " + tgt_parent_bone.name + " to " + tgt_child_bone.name
                # copyLocation.target = arm_parent
                # copyLocation.subtarget = tgt_parent_bone.name
                # copyLocation.target_space = 'LOCAL'
                # copyLocation.owner_space = 'LOCAL_WITH_PARENT'
                # copyLocation.use_offset = True  # Maintain offset

                # # Create and configure the Copy Rotation constraint
                # copyRotation = tgt_child_bone.constraints.new('COPY_ROTATION')
                # copyRotation.name = "Copy Rotation: " + tgt_parent_bone.name + " to " + tgt_child_bone.name
                # copyRotation.target = arm_parent
                # copyRotation.subtarget = tgt_parent_bone.name
                # copyRotation.target_space = 'LOCAL'
                # copyRotation.owner_space = 'LOCAL_WITH_PARENT'
            
            #! TEMP STUFF FOR ADDING IK
            # copyTransform.target_space = "WORLD"
            # copyTransform.owner_space = "WORLD"
            # copyTransform.target_space = "LOCAL_WITH_PARENT"
            # copyTransform.owner_space = "LOCAL_WITH_PARENT"
            
            # copyRotation = tgt_child_bone.constraints.new('COPY_ROTATION')
            # copyRotation.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
            # copyRotation.target = arm_parent
            # copyRotation.subtarget = tgt_parent_bone.name
            # copyRotation.mix_mode = "REPLACE"
            # copyRotation.target_space = "LOCAL_OWNER_ORIENT"
            # copyRotation.owner_space = "LOCAL"
            
            
            # copyLocation = tgt_child_bone.constraints.new('COPY_LOCATION')
            # copyLocation.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
            # copyLocation.target = arm_parent
            # copyLocation.subtarget = tgt_parent_bone.name
            #! TEMP STUFF END
            
        elif tgt_child_bone:
            child_of = tgt_child_bone.constraints.new('CHILD_OF')
            child_of.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
            child_of.target = arm_parent
            child_of.subtarget = tgt_parent_bone.name
            # if (tgt_child_bone.name == "torso3"
            #     or tgt_child_bone.name == "l_shoulder"
            #     or tgt_child_bone.name == "r_shoulder"
            #     or tgt_child_bone.name == "neck"
            #     or tgt_child_bone.name == "placer_thyroid"):
            #     child_of.inverse_matrix = Matrix()
            # else:
            #     child_of.inverse_matrix = Matrix() @ tgt_child_bone.matrix.inverted()

            # if tgt_child_bone.name == "torso3":
            #     child_of.inverse_matrix = Matrix()
                #bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
                #return
                #ca ="adwa"
                #tgt_child_bone.matrix = arm_parent.matrix_parent_inverse @ tgt_parent_bone.matrix @ tgt_child_bone.matrix.inverted()
                #child_of.inverse_matrix = arm_parent.matrix_parent_inverse @ tgt_child_bone.matrix#.inverted()
                # for c in arm_child.constraints:
                #     print(f"{c.name}: {c.type}")
                #bpy.ops.constraint.childof_clear_inverse(bpy.context.copy(), constraint=tgt_parent_bone.name + " to " + tgt_child_bone.name, owner='BONE')
                #bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
                #return
                #bpy.ops.constraint.childof_set_inverse(constraint=tgt_parent_bone.name + " to " + tgt_child_bone.name, owner='BONE')
            # arm_child.data.bones.active = arm_child.data.bones[tgt_child_bone.name]
            # bpy.ops.object.mode_set(mode='EDIT', toggle=False)
            # bpy.context.active_bone.parent = None
            # bpy.ops.object.mode_set(mode='POSE', toggle=False)
    bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
    return {'FINISHED'}


def CreateConstraints_IK_rig(arm_parent, arm_child):
    bpy.ops.object.mode_set(mode='POSE', toggle=False)
    for tgt_parent_bone in arm_parent.pose.bones:
        tgt_child_bone = False
        p_bone_name = file_helpers.rm_ns(tgt_parent_bone.name)
        log.debug("Checking bone: %s", p_bone_name)
        for cBone in arm_child.pose.bones:
            c_bone_name = file_helpers.rm_ns(cBone.name)
            if c_bone_name == p_bone_name:
                tgt_child_bone = cBone
        CHILD_OF_list = []
        if tgt_child_bone and tgt_child_bone.name not in CHILD_OF_list:
            for cons in tgt_child_bone.constraints:
                tgt_child_bone.constraints.remove(cons)
            copyTransform = tgt_child_bone.constraints.new('COPY_TRANSFORMS')
            copyTransform.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
            copyTransform.target = arm_parent
            copyTransform.subtarget = tgt_parent_bone.name
            
            #! TEMP STUFF FOR ADDING IK
            copyTransform.target_space = "WORLD"
            copyTransform.owner_space = "WORLD"
            copyTransform.target_space = "LOCAL_WITH_PARENT"
            copyTransform.owner_space = "LOCAL_WITH_PARENT"
            
            copyRotation = tgt_child_bone.constraints.new('COPY_ROTATION')
            copyRotation.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
            copyRotation.target = arm_parent
            copyRotation.subtarget = tgt_parent_bone.name
            copyRotation.mix_mode = "REPLACE"
            copyRotation.target_space = "LOCAL_OWNER_ORIENT"
            copyRotation.owner_space = "LOCAL"
            
            copyLocation = tgt_child_bone.constraints.new('COPY_LOCATION')
            copyLocation.name = tgt_parent_bone.name + " to " + tgt_child_bone.name
            copyLocation.target = arm_parent
            copyLocation.subtarget = tgt_parent_bone.name
            #! TEMP STUFF END
    bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
    return {'FINISHED'}


def _object_parent_depth(obj):
    depth = 0
    current = getattr(obj, "parent", None)
    while current is not None:
        depth += 1
        current = current.parent
        if depth > 1024:
            break
    return depth


def _is_ancestor_object(ancestor, child):
    current = getattr(child, "parent", None)
    while current is not None:
        if current == ancestor:
            return True
        current = current.parent
    return False


def _pick_top_level_armature(selected_objects, active_object):
    armatures = [obj for obj in selected_objects if obj.type == 'ARMATURE']
    if not armatures:
        return None

    top_level = [
        arm
        for arm in armatures
        if not any(other != arm and _is_ancestor_object(other, arm) for other in armatures)
    ]
    candidates = top_level or armatures
    min_depth = min(_object_parent_depth(obj) for obj in candidates)
    depth_candidates = [obj for obj in candidates if _object_parent_depth(obj) == min_depth]

    if active_object in depth_candidates:
        return active_object
    return depth_candidates[0]


def _pick_weapon_child(selected_objects, parent_armature, active_object):
    candidates = [obj for obj in selected_objects if obj != parent_armature]
    non_armatures = [obj for obj in candidates if obj.type != 'ARMATURE']
    if active_object in non_armatures:
        return active_object
    if non_armatures:
        return non_armatures[0]

    armatures = [obj for obj in candidates if obj.type == 'ARMATURE']
    descendants = [obj for obj in armatures if _is_ancestor_object(parent_armature, obj)]
    if active_object in descendants:
        return active_object
    if descendants:
        return max(descendants, key=_object_parent_depth)

    if active_object in armatures:
        return active_object
    if armatures:
        return armatures[0]
    return None


def _find_pose_bone_name(armature_obj, bone_name):
    if not armature_obj or armature_obj.type != 'ARMATURE' or not armature_obj.pose:
        return None
    if armature_obj.pose.bones.get(bone_name):
        return bone_name
    for pose_bone in armature_obj.pose.bones:
        if file_helpers.rm_ns(pose_bone.name) == bone_name:
            return pose_bone.name
    return None


def _get_root_pose_bone(armature_obj):
    if not armature_obj or armature_obj.type != 'ARMATURE' or not armature_obj.pose:
        return None
    for pose_bone in armature_obj.pose.bones:
        if pose_bone.parent is None:
            return pose_bone
    if armature_obj.pose.bones:
        return armature_obj.pose.bones[0]
    return None


def _mute_constraints(constraints):
    muted = 0
    for cons in constraints:
        try:
            cons.mute = True
            cons.show_expanded = False
            muted += 1
        except Exception:
            continue
    return muted


def _remove_existing_weapon_attach_constraints(obj):
    removed = 0
    for cons in list(obj.constraints):
        if cons.name.startswith("W3_ATTACH_"):
            obj.constraints.remove(cons)
            removed += 1
    return removed


def _set_copy_transforms_mix_mode(constraint, preferred_modes):
    for mode in preferred_modes:
        try:
            constraint.mix_mode = mode
            return mode
        except Exception:
            continue
    return ""


def _reparent_keep_world(child, parent_obj=None):
    if not child:
        return False
    had_parent = child.parent is not None
    world_matrix = child.matrix_world.copy()
    child.parent = parent_obj
    if parent_obj is None:
        child.parent_type = 'OBJECT'
        child.parent_bone = ""
    else:
        child.parent_type = 'OBJECT'
        child.parent_bone = ""
        try:
            child.matrix_parent_inverse = parent_obj.matrix_world.inverted()
        except Exception:
            pass
    child.matrix_world = world_matrix
    return had_parent


def attach_weapon(p_bone_name = "r_weapon"):
    selected_objs = list(bpy.context.selected_objects)
    active_object = bpy.context.active_object
    if not selected_objs:
        log.warning("Attach to %s cancelled: nothing selected.", p_bone_name)
        return {'CANCELLED'}

    arm_parent = _pick_top_level_armature(selected_objs, active_object)
    if arm_parent is None:
        log.warning("Attach to %s cancelled: no armature selected.", p_bone_name)
        return {'CANCELLED'}

    child = _pick_weapon_child(selected_objs, arm_parent, active_object)
    if child is None:
        log.warning("Attach to %s cancelled: no child object found.", p_bone_name)
        return {'CANCELLED'}

    target_bone_name = _find_pose_bone_name(arm_parent, p_bone_name)
    if not target_bone_name:
        log.warning(
            "Attach to %s cancelled: '%s' has no matching pose bone.",
            p_bone_name,
            arm_parent.name,
        )
        return {'CANCELLED'}

    log.info(
        "Attaching '%s' to %s.%s",
        child.name,
        arm_parent.name,
        target_bone_name,
    )

    muted_count = _mute_constraints(list(child.constraints))
    if child.type == 'ARMATURE':
        root_pose_bone = _get_root_pose_bone(child)
        if root_pose_bone is not None:
            muted_count += _mute_constraints(list(root_pose_bone.constraints))

    _remove_existing_weapon_attach_constraints(child)
    was_parented = _reparent_keep_world(child, arm_parent)

    rig_settings = getattr(getattr(arm_parent, "data", None), "witcherui_RigSettings", None)
    rot90_active = False
    if rig_settings is not None:
        if hasattr(rig_settings, "rot90_imported"):
            rot90_active = bool(getattr(rig_settings, "rot90_imported", False))
        elif hasattr(rig_settings, "rot90_compensate"):
            rot90_active = bool(getattr(rig_settings, "rot90_compensate", False))
    use_world_replace = not rot90_active

    copy_transform = child.constraints.new('COPY_TRANSFORMS')
    copy_transform.name = f"W3_ATTACH_{p_bone_name}"
    copy_transform.target = arm_parent
    copy_transform.subtarget = target_bone_name
    try:
        if use_world_replace:
            copy_transform.owner_space = 'WORLD'
            copy_transform.target_space = 'WORLD'
        else:
            # Rot90 display-fix rigs should use local/pose with BEFORE-style mixing.
            copy_transform.owner_space = 'LOCAL'
            copy_transform.target_space = 'POSE'
    except Exception:
        pass
    if use_world_replace:
        applied_mix = _set_copy_transforms_mix_mode(copy_transform, ('REPLACE',))
    else:
        applied_mix = _set_copy_transforms_mix_mode(copy_transform, ('BEFORE', 'BEFORE_FULL'))

    try:
        bpy.context.view_layer.update()
    except Exception:
        pass

    log.info(
        "Attached '%s' to %s.%s via COPY_TRANSFORMS (owner_space=%s, target_space=%s, mix=%s, muted=%d, reparented=%s).",
        child.name,
        arm_parent.name,
        target_bone_name,
        getattr(copy_transform, "owner_space", ""),
        getattr(copy_transform, "target_space", ""),
        applied_mix or getattr(copy_transform, "mix_mode", ""),
        muted_count,
        was_parented,
    )
    return {'FINISHED'}

def do_it(type = 1):
    #bpy.data.objects['CMeshComponent14:Armature']
    arm_parent = False
    arm_child = bpy.context.active_object

    selected_objs = [obj for obj in bpy.context.selected_objects if obj != bpy.context.active_object]

    for obj in selected_objs:
        if obj.type != 'ARMATURE':
            continue
        if not arm_parent:
            arm_parent = obj
            continue

    # arm_parent = bpy.context.object
    # objects = bpy.context.selected_objects
    # arm = arm_parent.data

    # if bpy.context.object.type != 'ARMATURE':
    #     print("No Armature selected! Exiting script.")
    #     return {"ERROR"}
    log.info("Creating constraints...")
    if type == 1:
        result = CreateConstraints2(arm_parent, arm_child)
    elif type == 2:
        CreateConstraints_IK_rig(arm_parent, arm_child)
    log.info("Constraints script finished")
    return {'FINISHED'}
