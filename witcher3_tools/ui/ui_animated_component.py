"""Author and export skeleton-bound CAnimatedComponent entities."""

import json
import logging
import os

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty

from ..CR2W import animated_component as ac
from .. import get_wolvenkit
from .. import get_uncook_path
from ..attachment_math import coerce_attachment_flags, normalize_engine_transform
from ..ui.ui_utils import WITCH_PT_Base
from ..ui.armature_context import set_main_armature

log = logging.getLogger(__name__)

# Custom properties shared with the entity importer.
P_ENTITY_ROOT = "witcher_entity_root"
P_TYPE = "witcher_type"
P_NAME = "witcher_name"
P_PATH = "witcher_path"
P_ENTITY_PATH = "witcher_entity_path"
P_BEHAVIOR = "witcher_behavior_path"

T_ANIMATED_COMPONENT = "CAnimatedComponent"
T_MESH_COMPONENT = "CMeshComponent"
P_ATTACHMENT_FLAGS = "witcher_attachment_flags"
P_ATTACHMENT_RELATIVE = "witcher_hard_attachment_relative_transform"

_MESH_PATH_PROPS = (P_PATH, "repo_path", "witcher_redkit_mesh_path", "w3_source_mesh_path", "_depot_path")


# Structure helpers

def _active_collection(context=None):
    context = context or bpy.context
    collection = getattr(context, "collection", None)
    if collection is not None:
        return collection
    active_layer = getattr(getattr(context, "view_layer", None), "active_layer_collection", None)
    collection = getattr(active_layer, "collection", None)
    if collection is not None:
        return collection
    return bpy.context.scene.collection


def _object_collection(obj, fallback=None):
    collections = getattr(obj, "users_collection", None) or ()
    if collections:
        return collections[0]
    return fallback or _active_collection()


def is_animated_component(obj):
    return (getattr(obj, "type", None) == 'ARMATURE'
            and str(obj.get(P_TYPE, "")) == T_ANIMATED_COMPONENT)


def active_animated_component(context):
    obj = getattr(context, "active_object", None)
    if is_animated_component(obj):
        return obj
    for sel in getattr(context, "selected_objects", []) or []:
        if is_animated_component(sel):
            return sel
    return None


def _bone_index(arm_obj, bone_name):
    names = [b.name for b in arm_obj.data.bones]
    return names.index(bone_name) if bone_name in names else 0


_SLOT_CONSTRAINT_TYPES = {'COPY_TRANSFORMS', 'COPY_LOCATION', 'COPY_ROTATION', 'COPY_SCALE', 'CHILD_OF'}


def _is_hard_attachment_empty(obj):
    if getattr(obj, "type", None) != 'EMPTY':
        return False
    if str(obj.get(P_TYPE, "") or "") == "CHardAttachment":
        return True
    return str(getattr(obj, "name", "") or "").startswith("CHardAttachment")


def _attachment_slot(arm_obj, empty):
    """Return the attachment empty's constrained or parented slot bone."""
    for con in getattr(empty, "constraints", []) or []:
        if (con.type in _SLOT_CONSTRAINT_TYPES and con.target is arm_obj
                and getattr(con, "subtarget", "")):
            return con.subtarget
    if empty.parent is arm_obj and empty.parent_type == 'BONE' and empty.parent_bone:
        return empty.parent_bone
    return ""


def iter_hard_attachments(arm_obj):
    """Yield hard attachments as (empty, mesh, slot) tuples in bone order."""
    out = []
    seen = set()
    for child in getattr(bpy.data, "objects", []) or []:
        if not _is_hard_attachment_empty(child) or id(child) in seen:
            continue
        slot = _attachment_slot(arm_obj, child)
        if not slot:
            continue
        if child.parent is not arm_obj and not any(
                con.type in _SLOT_CONSTRAINT_TYPES and con.target is arm_obj
                for con in getattr(child, "constraints", []) or []):
            continue
        mesh = next((m for m in child.children if getattr(m, "type", None) == 'MESH'), None)
        out.append((child, mesh, slot))
        seen.add(id(child))
    out.sort(key=lambda t: _bone_index(arm_obj, t[2]))
    return out


def resolve_mesh_depot(mesh_obj):
    for key in _MESH_PATH_PROPS:
        v = str(mesh_obj.get(key, "") or "").strip()
        if v.lower().endswith(".w2mesh"):
            return v.replace("/", "\\")
    return ""


def collect_hard_attachment_export_data(arm_obj):
    """Return serializable CHardAttachment data and skipped object names."""
    attachments = []
    skipped = []
    for empty, mesh, slot in iter_hard_attachments(arm_obj):
        if mesh is None:
            skipped.append(getattr(empty, "name", "CHardAttachment"))
            continue
        depot = resolve_mesh_depot(mesh)
        if not depot:
            skipped.append(getattr(mesh, "name", "mesh"))
            continue
        relative_transform = normalize_engine_transform(None)
        relative_transform_raw = str(empty.get(P_ATTACHMENT_RELATIVE, "") or "").strip()
        if relative_transform_raw:
            try:
                relative_transform = normalize_engine_transform(json.loads(relative_transform_raw))
            except Exception:
                skipped.append(getattr(empty, "name", "CHardAttachment"))
                continue
        component_transform = None
        component_transform_raw = str(mesh.get("witcher_component_transform", "") or "").strip()
        if component_transform_raw:
            try:
                component_transform = json.loads(component_transform_raw)
            except Exception:
                skipped.append(getattr(mesh, "name", "mesh"))
                continue
        attachments.append({
            "mesh": depot,
            "slot": slot,
            "bone_index": _bone_index(arm_obj, slot),
            "name": str(mesh.get(P_NAME, "") or "") or None,
            "attachment_flags": coerce_attachment_flags(
                empty.get(P_ATTACHMENT_FLAGS, 0)
            ),
            "relative_transform": relative_transform,
            "component_transform": component_transform,
        })
    return attachments, skipped


def create_animated_component(entity_name, skeleton_path, bone_names, entity_path,
                              behavior_path="", component_name=ac.DEFAULT_COMPONENT_NAME,
                              chunk_index=2, target_collection=None):
    """Create an entity root and return its CAnimatedComponent armature."""
    target_collection = target_collection or _active_collection()
    root = bpy.data.objects.new(entity_name, None)
    root.empty_display_type = 'PLAIN_AXES'
    root[P_ENTITY_ROOT] = True
    target_collection.objects.link(root)

    arm_name = f"{entity_name}:{T_ANIMATED_COMPONENT}{chunk_index}_ARM"
    arm_data = bpy.data.armatures.new(arm_name)
    arm = bpy.data.objects.new(arm_name, arm_data)
    target_collection.objects.link(arm)
    arm.parent = root

    prev = bpy.context.view_layer.objects.active
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    try:
        # Slot transforms originate at the bone heads. The trajectories rig uses
        # coincident heads and 0.01 m tails along +X.
        ebs = arm_data.edit_bones
        root_name = bone_names[0] if bone_names else "Root"
        root_bone = ebs.new(root_name)
        root_bone.head = (0.0, 0.0, 0.0)
        root_bone.tail = (0.01, 0.0, 0.0)
        for name in bone_names[1:]:
            b = ebs.new(name)
            b.head = (0.0, 0.0, 0.0)
            b.tail = (0.01, 0.0, 0.0)
            b.parent = root_bone
            b.use_connect = False
    finally:
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.context.view_layer.objects.active = prev

    arm[P_TYPE] = T_ANIMATED_COMPONENT
    arm[P_NAME] = component_name
    arm[P_PATH] = skeleton_path
    arm[P_ENTITY_PATH] = entity_path
    arm[P_BEHAVIOR] = behavior_path
    return arm


def add_hard_attachment(arm_obj, mesh_obj, slot_bone, mesh_depot):
    """Attach a mesh through a CHardAttachment at the slot bone head."""
    from mathutils import Matrix
    from ..importers import import_entity

    empty = import_entity._link_hard_attachment_anchor(
        arm_obj,
        mesh_obj,
        slot_bone,
        None,
        0,
    )
    # Authored components start at the slot origin.
    mesh_obj.matrix_parent_inverse = Matrix.Identity(4)
    mesh_obj.matrix_basis = Matrix.Identity(4)

    mesh_obj[P_TYPE] = T_MESH_COMPONENT
    if not str(mesh_obj.get(P_NAME, "") or "").strip():
        mesh_obj[P_NAME] = ac._mesh_stem(mesh_depot)
    mesh_obj[P_PATH] = mesh_depot
    return empty


# Scene properties

_slot_items_cache = [("", "", "")]


def _slot_enum_items(self, context):
    global _slot_items_cache
    arm = active_animated_component(context)
    if arm is None:
        _slot_items_cache = [("", "<no component>", "")]
        return _slot_items_cache
    items = [(b.name, b.name, f"parentSlotName {b.name}")
             for b in arm.data.bones if b.name != (arm.data.bones[0].name if arm.data.bones else "Root")]
    _slot_items_cache = items or [("", "<no bones>", "")]
    return _slot_items_cache


# Operators

class WITCH_OT_CreateAnimatedComponent(bpy.types.Operator):
    """Create a skeleton-bound CAnimatedComponent using the selected preset."""
    bl_idname = "witcher.create_animated_component"
    bl_label = "Create CAnimatedComponent"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        skeleton = str(getattr(scene, "witcher_ac_skeleton_path", "") or "").strip().replace("/", "\\")
        if not skeleton:
            skeleton = ac.TRAJECTORY_RIG_PATH
        bone_count = int(getattr(scene, "witcher_ac_bone_count", ac.TRAJECTORY_BONE_COUNT) or ac.TRAJECTORY_BONE_COUNT)
        bones = ac.trajectory_bone_names(bone_count)

        entity_path = str(getattr(scene, "witcher_ac_entity_path", "") or "").strip().replace("/", "\\")
        if not entity_path:
            entity_path = "animations\\cutscenes\\blender_tools\\trajectory_props.w2ent"
        if not entity_path.lower().endswith(".w2ent"):
            entity_path += ".w2ent"

        entity_name = str(getattr(scene, "witcher_ac_entity_name", "") or "").strip()
        if not entity_name:
            entity_name = os.path.splitext(os.path.basename(entity_path.replace("\\", "/")))[0] or "entity"

        behavior = ""
        if bool(getattr(scene, "witcher_ac_use_behavior", True)):
            behavior = str(getattr(scene, "witcher_ac_behavior_path", "") or "").strip().replace("/", "\\")

        arm = create_animated_component(
            entity_name,
            skeleton,
            bones,
            entity_path,
            behavior,
            target_collection=_active_collection(context),
        )

        for o in bpy.data.objects:
            o.select_set(False)
        arm.select_set(True)
        context.view_layer.objects.active = arm
        set_main_armature(scene, arm)
        scene.witcher_ac_entity_path = entity_path

        self.report({'INFO'}, f"Created CAnimatedComponent '{arm.name}' ({len(bones)} bones).")
        return {'FINISHED'}


class WITCH_OT_AddHardAttachment(bpy.types.Operator):
    """Attach the selected meshes to a slot on the active CAnimatedComponent."""
    bl_idname = "witcher.add_hard_attachment"
    bl_label = "Add CHardAttachment"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_animated_component(context) is not None

    def execute(self, context):
        scene = context.scene
        arm = active_animated_component(context)
        if arm is None:
            self.report({'WARNING'}, "Select a CAnimatedComponent armature first.")
            return {'CANCELLED'}

        slot = str(getattr(scene, "witcher_ac_target_slot", "") or "").strip()
        if not slot or slot not in arm.data.bones:
            self.report({'WARNING'}, "Choose a valid slot bone.")
            return {'CANCELLED'}

        meshes = [o for o in context.selected_objects if getattr(o, "type", None) == 'MESH']
        if not meshes:
            self.report({'WARNING'}, "Select one or more mesh objects to attach.")
            return {'CANCELLED'}

        override = str(getattr(scene, "witcher_ac_mesh_path", "") or "").strip().replace("/", "\\")
        if override and not override.lower().endswith(".w2mesh"):
            self.report({'WARNING'}, "Mesh Path override must end with .w2mesh.")
            return {'CANCELLED'}
        attached, missing = 0, []
        for mesh in meshes:
            depot = override or resolve_mesh_depot(mesh)
            if not depot:
                missing.append(mesh.name)
                continue
            add_hard_attachment(arm, mesh, slot, depot)
            attached += 1

        if missing:
            self.report({'WARNING'}, f"Attached {attached}; no .w2mesh path for: "
                                     f"{', '.join(missing[:4])} (set Mesh Path override).")
        else:
            self.report({'INFO'}, f"Attached {attached} mesh(es) to {slot}.")
        return {'FINISHED'}


class WITCH_OT_RemoveHardAttachment(bpy.types.Operator):
    """Remove an attachment while preserving its mesh's world transform."""
    bl_idname = "witcher.remove_hard_attachment"
    bl_label = "Remove CHardAttachment"
    bl_options = {'REGISTER', 'UNDO'}

    empty_name: StringProperty(default="")

    def execute(self, context):
        empty = bpy.data.objects.get(self.empty_name)
        if empty is None:
            return {'CANCELLED'}
        for mesh in list(empty.children):
            mw = mesh.matrix_world.copy()
            mesh.parent = None
            mesh.matrix_world = mw
        bpy.data.objects.remove(empty, do_unlink=True)
        return {'FINISHED'}


class WITCH_OT_ExportAnimatedComponentEntity(bpy.types.Operator):
    """Export the active CAnimatedComponent as a .w2ent entity template."""
    bl_idname = "witcher.export_animated_component_entity"
    bl_label = "Export CEntityTemplate"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return active_animated_component(context) is not None

    def execute(self, context):
        arm = active_animated_component(context)
        if arm is None:
            self.report({'WARNING'}, "Select a CAnimatedComponent armature first.")
            return {'CANCELLED'}

        entity_path = str(arm.get(P_ENTITY_PATH, "") or "").strip()
        if not entity_path:
            self.report({'WARNING'}, "Component has no witcher_entity_path.")
            return {'CANCELLED'}
        skeleton = str(arm.get(P_PATH, "") or "").strip() or ac.TRAJECTORY_RIG_PATH
        behavior = str(arm.get(P_BEHAVIOR, "") or "").strip()
        component_name = str(arm.get(P_NAME, "") or ac.DEFAULT_COMPONENT_NAME)

        attachments, skipped = collect_hard_attachment_export_data(arm)
        if skipped:
            self.report({'WARNING'}, "Skipped CHardAttachment(s) without mesh path: "
                                     f"{', '.join(skipped[:4])}")
        if not attachments and skipped:
            self.report({'WARNING'}, "No CHardAttachments have a resolvable .w2mesh path.")
            return {'CANCELLED'}

        export_dir = _resolve_export_dir(context)
        if not export_dir:
            self.report({'ERROR'}, "No REDkit project or uncook path configured for output.")
            return {'CANCELLED'}
        try:
            out_path = _safe_repo_output_path(export_dir, entity_path)
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

        entity_name = os.path.splitext(os.path.basename(entity_path.replace("\\", "/")))[0]
        try:
            ac.generate_entity(
                attachments, out_path, get_wolvenkit(context),
                skeleton_path=skeleton, behavior_path=behavior or None,
                entity_name=entity_name, component_name=component_name)
        except Exception as exc:
            log.exception("CAnimatedComponent entity export failed.")
            self.report({'ERROR'}, f"Export failed: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Exported {len(attachments)} CHardAttachment(s) -> {out_path}")
        return {'FINISHED'}


def _resolve_export_dir(context):
    try:
        from .ui_anims import _anim_get_active_redkit_project
        project = _anim_get_active_redkit_project(context)
    except Exception:
        project = None
    if project:
        return os.path.join(project, "workspace")
    try:
        return get_uncook_path(context)
    except Exception:
        return ""


def _safe_repo_output_path(export_dir, repo_path):
    root = os.path.abspath(os.path.normpath(str(export_dir or "")))
    raw = str(repo_path or "").strip().replace("/", os.sep).replace("\\", os.sep)
    drive, _tail = os.path.splitdrive(raw)
    if not root or not raw or drive or os.path.isabs(raw) or not raw.lower().endswith(".w2ent"):
        raise ValueError("Entity path must be a game-relative .w2ent path.")
    candidate = os.path.abspath(os.path.normpath(os.path.join(root, raw.lstrip(os.sep))))
    try:
        if os.path.normcase(os.path.commonpath((root, candidate))) != os.path.normcase(root):
            raise ValueError("Entity path must stay inside the configured export root.")
    except ValueError:
        raise ValueError("Entity path must stay inside the configured export root.")
    return candidate


# Panel

class WITCHER_PT_animated_component(WITCH_PT_Base, bpy.types.Panel):
    bl_idname = "WITCHER_PT_animated_component"
    bl_label = "CAnimatedComponent Authoring"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='CON_ARMATURE')

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        scene = context.scene
        arm = active_animated_component(context)

        if arm is None:
            self._draw_create(layout, scene)
        else:
            self._draw_edit(layout, scene, arm)

    def _draw_create(self, layout, scene):
        box = layout.box()
        box.label(text="New Component", icon='ADD')
        box.label(text="Skeleton-bound armature + hard-attached meshes.", icon='INFO')
        box.prop(scene, "witcher_ac_preset", text="Preset")
        col = box.column(align=True)
        col.prop(scene, "witcher_ac_skeleton_path", text="Skeleton (.w2rig)")
        col.prop(scene, "witcher_ac_bone_count", text="Trajectory Bones")
        beh = box.row(align=True)
        beh.prop(scene, "witcher_ac_use_behavior", text="")
        sub = beh.row(align=True)
        sub.enabled = bool(getattr(scene, "witcher_ac_use_behavior", True))
        sub.prop(scene, "witcher_ac_behavior_path", text="Behavior (.w2beh)")
        box.prop(scene, "witcher_ac_entity_name", text="Entity Name")
        box.prop(scene, "witcher_ac_entity_path", text="Entity (.w2ent)")
        box.operator(WITCH_OT_CreateAnimatedComponent.bl_idname, icon='ARMATURE_DATA')
        layout.label(text="Or select an existing CAnimatedComponent to edit.", icon='INFO')

    def _draw_edit(self, layout, scene, arm):
        info = layout.box()
        info.label(text=str(arm.get(P_NAME, arm.name)), icon='CON_ARMATURE')
        col = info.column(align=True)
        if P_PATH in arm:
            col.prop(arm, f'["{P_PATH}"]', text="Skeleton")
        else:
            col.label(text="Skeleton: not set", icon='ERROR')
        col.label(text=f"Bones: {len(arm.data.bones)}")
        if P_ENTITY_PATH in arm:
            col.prop(arm, f'["{P_ENTITY_PATH}"]', text="Entity")
        else:
            col.label(text="Entity path: not set", icon='ERROR')

        attachments = iter_hard_attachments(arm)
        att_box = layout.box()
        att_box.label(text=f"CHardAttachments ({len(attachments)})", icon='LINKED')
        if attachments:
            for empty, mesh, slot in attachments:
                row = att_box.row(align=True)
                mesh_name = str((mesh.get(P_NAME, "") if mesh else "") or (mesh.name if mesh else "<missing>"))
                row.label(text=slot, icon='BONE_DATA')
                row.label(text=mesh_name, icon='MESH_DATA' if mesh else 'ERROR')
                op = row.operator(WITCH_OT_RemoveHardAttachment.bl_idname, text="", icon='X')
                op.empty_name = empty.name
        else:
            att_box.label(text="No meshes attached yet.", icon='INFO')

        add = layout.box()
        add.label(text="Attach Selected Meshes", icon='ADD')
        add.prop(scene, "witcher_ac_target_slot", text="Slot")
        add.prop(scene, "witcher_ac_mesh_path", text="Mesh Path")
        add.operator(WITCH_OT_AddHardAttachment.bl_idname, icon='LINKED')

        layout.separator(factor=0.5)
        layout.operator(WITCH_OT_ExportAnimatedComponentEntity.bl_idname, icon='EXPORT')


def _preset_update(self, context):
    if getattr(self, "witcher_ac_preset", "TRAJECTORY") == "TRAJECTORY":
        self.witcher_ac_skeleton_path = ac.TRAJECTORY_RIG_PATH
        self.witcher_ac_behavior_path = ac.CUTSCENE_BEHAVIOR_PATH
        self.witcher_ac_bone_count = ac.TRAJECTORY_BONE_COUNT
        self.witcher_ac_use_behavior = True


classes = [
    WITCH_OT_CreateAnimatedComponent,
    WITCH_OT_AddHardAttachment,
    WITCH_OT_RemoveHardAttachment,
    WITCH_OT_ExportAnimatedComponentEntity,
    WITCHER_PT_animated_component,
]

_scene_props = {
    "witcher_ac_preset": EnumProperty(
        name="Preset", description="Component preset",
        items=[("TRAJECTORY", "Trajectory (trajectories_24)", "24 trajectory prop slots"),
               ("CUSTOM", "Custom", "Custom skeleton / bone count")],
        default="TRAJECTORY", update=_preset_update),
    "witcher_ac_skeleton_path": StringProperty(
        name="Skeleton", description="CSkeleton (.w2rig) the component binds to",
        default=ac.TRAJECTORY_RIG_PATH),
    "witcher_ac_bone_count": IntProperty(
        name="Trajectory Bones", description="Trajectory01..N slots on the rig",
        default=ac.TRAJECTORY_BONE_COUNT, min=1, max=ac.TRAJECTORY_BONE_COUNT),
    "witcher_ac_use_behavior": BoolProperty(
        name="Behavior", description="Bind a CBehaviorGraph (cutscene) to the component",
        default=True),
    "witcher_ac_behavior_path": StringProperty(
        name="Behavior", description="CBehaviorGraph (.w2beh) instance slot",
        default=ac.CUTSCENE_BEHAVIOR_PATH),
    "witcher_ac_entity_name": StringProperty(
        name="Entity Name", description="Entity name (blank = from the entity path)", default=""),
    "witcher_ac_entity_path": StringProperty(
        name="Entity Path", description="Game-relative path for the exported .w2ent",
        default="animations\\cutscenes\\blender_tools\\trajectory_props.w2ent"),
    "witcher_ac_target_slot": EnumProperty(
        name="Slot", description="Bone (parentSlotName) to attach meshes to",
        items=_slot_enum_items),
    "witcher_ac_mesh_path": StringProperty(
        name="Mesh Path", description="Optional .w2mesh depot override (blank auto-detects)", default=""),
}


def register():
    for c in classes:
        bpy.utils.register_class(c)
    for name, prop in _scene_props.items():
        setattr(bpy.types.Scene, name, prop)


def unregister():
    for name in _scene_props:
        if hasattr(bpy.types.Scene, name):
            delattr(bpy.types.Scene, name)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
