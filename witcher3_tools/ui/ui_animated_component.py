"""Entity Builder: author and export .w2ent templates (cutscene trajectory props, rigged, static)."""

import json
import logging
import os
from math import degrees

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from ..CR2W import animated_component as ac
from .. import get_uncook_path
from ..rigging.attachment import coerce_attachment_flags, normalize_engine_transform
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
# Builder-only markers; export is limited to entities authored here (imported
# entities would lose every component the builder doesn't model).
P_BUILDER = "witcher_entity_builder"
P_KIND = "witcher_entity_kind"

T_ANIMATED_COMPONENT = "CAnimatedComponent"
T_MESH_COMPONENT = "CMeshComponent"
T_STATIC_MESH_COMPONENT = "CStaticMeshComponent"
P_ATTACHMENT_FLAGS = "witcher_attachment_flags"
P_ATTACHMENT_RELATIVE = "witcher_hard_attachment_relative_transform"

KIND_TRAJECTORY, KIND_ANIMATED, KIND_STATIC = "TRAJECTORY", "ANIMATED", "STATIC"
_KIND_LABELS = {KIND_TRAJECTORY: "Cutscene Props", KIND_ANIMATED: "Rigged", KIND_STATIC: "Static"}
DEFAULT_TRAJECTORY_ENTITY_PATH = r"animations\cutscenes\blender_tools\trajectory_props.w2ent"
DEFAULT_CUSTOM_ENTITY_PATH = r"blender_tools\entities\custom_entity.w2ent"

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


def is_animated_component(obj):
    return (getattr(obj, "type", None) == 'ARMATURE'
            and str(obj.get(P_TYPE, "")) == T_ANIMATED_COMPONENT)


def is_builder_root(obj):
    return obj is not None and bool(obj.get(P_ENTITY_ROOT)) and bool(obj.get(P_BUILDER))


def _entity_root_of(obj):
    while obj is not None:
        if obj.get(P_ENTITY_ROOT):
            return obj
        obj = obj.parent
    return None


def builder_root_of(obj):
    root = _entity_root_of(obj)
    return root if is_builder_root(root) else None


def active_builder_root(context):
    candidates = [getattr(context, "active_object", None)]
    candidates += list(getattr(context, "selected_objects", None) or [])
    for obj in candidates:
        root = builder_root_of(obj)
        if root is not None:
            return root
    return None


def root_armature(root):
    if root is None:
        return None
    return next((c for c in root.children if is_animated_component(c)), None)


def entity_kind(root):
    kind = str(root.get(P_KIND, "") or "")
    return kind or (KIND_ANIMATED if root_armature(root) else KIND_STATIC)


def active_animated_component(context):
    """Return the selected builder-owned CAnimatedComponent."""
    candidates = [getattr(context, "active_object", None)]
    candidates += list(getattr(context, "selected_objects", None) or [])
    for obj in candidates:
        if is_animated_component(obj) and builder_root_of(obj) is not None:
            return obj
    return root_armature(active_builder_root(context))


def _bone_index(arm_obj, bone_name):
    """CSkeleton order as recorded by the rig importer; Blender's data.bones is tree-ordered."""
    settings = getattr(arm_obj.data, "witcherui_RigSettings", None)
    names = [b.name for b in settings.bone_order_list] if settings is not None else []
    if not names:
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


def iter_static_meshes(root):
    return [c for c in root.children
            if getattr(c, "type", None) == 'MESH'
            and str(c.get(P_TYPE, "") or "") == T_STATIC_MESH_COMPONENT]


def resolve_mesh_depot(mesh_obj):
    """Resolve a .w2mesh depot path from importer properties."""
    candidates = [str(mesh_obj.get(key, "") or "") for key in _MESH_PATH_PROPS]
    settings = getattr(mesh_obj, "witcherui_MeshSettings", None)
    if settings is not None:
        candidates.append(str(getattr(settings, "item_repo_path", "") or ""))
    for value in candidates:
        path = _norm_repo_path(value)
        if path.lower().endswith(".w2mesh"):
            return path
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
        # The mesh basis under its anchor is the live component transform (the importer places it there too).
        component_transform = matrix_to_engine_transform(mesh.matrix_basis)
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


def matrix_to_engine_transform(matrix):
    """Blender local matrix -> EngineTransform dict.

    EulerAngles::ToMatrix composes Y(roll)·X(pitch)·Z(yaw), i.e. Blender 'YXZ'
    with x=pitch, y=roll, z=yaw.
    """
    loc, rot, scale = matrix.decompose()
    e = rot.to_euler('YXZ')
    values = {"X": loc.x, "Y": loc.y, "Z": loc.z,
              "Pitch": degrees(e.x), "Roll": degrees(e.y), "Yaw": degrees(e.z),
              "Scale_x": scale.x, "Scale_y": scale.y, "Scale_z": scale.z}
    return normalize_engine_transform({k: round(v, 6) + 0.0 for k, v in values.items()})


def collect_static_mesh_export_data(root):
    """Return serializable CStaticMeshComponent data and skipped object names."""
    items = []
    skipped = []
    for mesh in iter_static_meshes(root):
        depot = resolve_mesh_depot(mesh)
        if not depot:
            skipped.append(mesh.name)
            continue
        items.append({
            "mesh": depot,
            "name": str(mesh.get(P_NAME, "") or "") or None,
            "transform": matrix_to_engine_transform(mesh.matrix_local),
        })
    return items, skipped


# Creation

def create_entity_root(entity_name, entity_path, kind, target_collection=None):
    root = bpy.data.objects.new(entity_name, None)
    root.empty_display_type = 'PLAIN_AXES'
    root[P_ENTITY_ROOT] = True
    root[P_BUILDER] = True
    root[P_KIND] = kind
    root[P_ENTITY_PATH] = entity_path
    (target_collection or _active_collection()).objects.link(root)
    return root


def _synthesize_armature(arm_name, bone_names, target_collection):
    arm_data = bpy.data.armatures.new(arm_name)
    arm = bpy.data.objects.new(arm_name, arm_data)
    target_collection.objects.link(arm)

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
    return arm


def _armature_from_rig_file(rig_file, arm_ns, target_collection):
    """Load a real .w2rig through the rig importer so bones match imported entities."""
    from ..CR2W.dc_skeleton import load_bin_skeleton
    from ..importers import import_rig

    arm = import_rig.create_armature_from_skeleton_data(
        load_bin_skeleton(rig_file), fileName=rig_file, ns=arm_ns, context=bpy.context)
    for coll in list(arm.users_collection):
        if coll is not target_collection:
            coll.objects.unlink(arm)
    if arm.name not in target_collection.objects:
        target_collection.objects.link(arm)
    return arm


def create_animated_component(entity_name, skeleton_path, bone_names, entity_path,
                              behavior_path="", component_name=ac.DEFAULT_COMPONENT_NAME,
                              chunk_index=2, target_collection=None, rig_file=None, kind=None):
    """Create an entity root and return its CAnimatedComponent armature.

    rig_file: absolute .w2rig to load real bones from; otherwise bone_names are
    synthesized with coincident heads (trajectory convention).
    """
    target_collection = target_collection or _active_collection()
    if kind is None:
        kind = KIND_TRAJECTORY if skeleton_path == ac.TRAJECTORY_RIG_PATH else KIND_ANIMATED

    arm_ns = f"{entity_name}:{T_ANIMATED_COMPONENT}{chunk_index}"
    if rig_file:
        arm = _armature_from_rig_file(rig_file, arm_ns, target_collection)
    else:
        arm = _synthesize_armature(f"{arm_ns}_ARM", bone_names, target_collection)
    root = create_entity_root(entity_name, entity_path, kind, target_collection)
    arm.parent = root
    arm[P_TYPE] = T_ANIMATED_COMPONENT
    arm[P_NAME] = component_name
    arm[P_PATH] = skeleton_path
    arm[P_ENTITY_PATH] = entity_path
    arm[P_BEHAVIOR] = behavior_path
    arm[P_BUILDER] = True
    return arm


def add_hard_attachment(arm_obj, mesh_obj, slot_bone, mesh_depot):
    """Attach a mesh through a CHardAttachment at the slot bone head."""
    from mathutils import Matrix
    from ..importers import import_entity

    old_anchor = mesh_obj.parent
    if old_anchor is not None and _is_hard_attachment_empty(old_anchor):
        # Re-attaching to another slot: drop the previous anchor instead of orphaning it.
        mesh_obj.parent = None
        bpy.data.objects.remove(old_anchor, do_unlink=True)

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


def add_static_mesh(root, mesh_obj, mesh_depot):
    """Parent a mesh to the entity root as a CStaticMeshComponent, keeping its world placement."""
    from mathutils import Matrix

    world = mesh_obj.matrix_world.copy()
    mesh_obj.parent = root
    mesh_obj.parent_type = 'OBJECT'
    mesh_obj.matrix_parent_inverse = Matrix.Identity(4)
    mesh_obj.matrix_world = world
    mesh_obj[P_TYPE] = T_STATIC_MESH_COMPONENT
    if not str(mesh_obj.get(P_NAME, "") or "").strip():
        mesh_obj[P_NAME] = ac._mesh_stem(mesh_depot)
    mesh_obj[P_PATH] = mesh_depot


def _add_static_meshes(root, meshes, override):
    added, missing = 0, []
    for mesh in meshes:
        if mesh.parent is root and str(mesh.get(P_TYPE, "") or "") == T_STATIC_MESH_COMPONENT:
            continue
        if _is_hard_attachment_empty(mesh.parent):
            continue
        depot = override or resolve_mesh_depot(mesh)
        if not depot:
            missing.append(mesh.name)
            continue
        add_static_mesh(root, mesh, depot)
        added += 1
    return added, missing


def _select_only(context, obj):
    for o in context.selected_objects:
        o.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj


# Paths

def _norm_repo_path(value):
    path = str(value or "").strip().replace("/", "\\")
    if os.path.splitdrive(path)[0] or path.startswith("\\\\"):
        return ""  # absolute paths are not depot paths
    path = path.strip("\\")
    if any(part in ("", ".", "..") for part in path.split("\\")):
        return ""  # no traversal out of the depot roots
    return path


def _report_absolute_path(operator, raw, label):
    if str(raw or "").strip() and not _norm_repo_path(raw):
        operator.report({'ERROR'}, f"{label} must be game-relative (e.g. dlc\\mymod\\data\\...).")
        return True
    return False


def _entity_path_or_default(value, default):
    path = _norm_repo_path(value) or default
    return path if path.lower().endswith(".w2ent") else path + ".w2ent"


def _entity_name_from_path(entity_path):
    return os.path.splitext(os.path.basename(entity_path.replace("\\", "/")))[0] or "entity"


def _output_roots(context):
    """Writable roots: REDkit project workspace first, then the uncook folder."""
    roots = []
    try:
        from .ui_anims import _anim_get_active_redkit_project
        project = _anim_get_active_redkit_project(context)
    except Exception:
        project = None
    if project:
        roots.append(os.path.join(project, "workspace"))
    try:
        uncook = get_uncook_path(context)
    except Exception:
        uncook = ""
    if uncook:
        roots.append(uncook)
    return roots


def _repo_roots(context):
    """Read roots: output roots plus the REDkit dual depot (r4data, then uncooked) ahead of uncook."""
    from ..CR2W.common_blender import _get_redkit_depot_roots

    roots = _output_roots(context)
    roots[1 if roots and roots[0].lower().endswith("workspace") else 0:0] = _get_redkit_depot_roots()
    seen = set()
    return [r for r in roots if not (os.path.normcase(r) in seen or seen.add(os.path.normcase(r)))]


def _resolve_repo_file(context, repo_path):
    rel = _norm_repo_path(repo_path)
    for root in _repo_roots(context):
        candidate = os.path.join(root, rel)
        if os.path.isfile(candidate):
            return candidate
    return ""


def _resolve_export_dir(context):
    roots = _output_roots(context)
    return roots[0] if roots else ""


def _safe_repo_output_path(export_dir, repo_path, suffix=".w2ent"):
    root = os.path.abspath(os.path.normpath(str(export_dir or "")))
    raw = str(repo_path or "").strip().replace("/", os.sep).replace("\\", os.sep)
    drive, _tail = os.path.splitdrive(raw)
    if not root or not raw or drive or os.path.isabs(raw) or not raw.lower().endswith(suffix):
        raise ValueError(f"Path must be a game-relative {suffix} path.")
    candidate = os.path.abspath(os.path.normpath(os.path.join(root, raw.lstrip(os.sep))))
    try:
        inside = os.path.normcase(os.path.commonpath((root, candidate))) == os.path.normcase(root)
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("Path must stay inside the configured export root.")
    return candidate


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

class WITCH_OT_CreateBuilderEntity(bpy.types.Operator):
    """Create a cutscene-prop, rigged, or static entity."""
    bl_idname = "witcher.create_builder_entity"
    bl_label = "Create Entity"
    bl_options = {'REGISTER', 'UNDO'}

    kind: EnumProperty(
        name="Kind",
        items=[(KIND_TRAJECTORY, "Cutscene Props (trajectories_24)", "24 animatable prop slots"),
               (KIND_ANIMATED, "Rigged", "CAnimatedComponent bound to a .w2rig"),
               (KIND_STATIC, "Static", "CStaticMeshComponents only")],
        default=KIND_TRAJECTORY)

    def execute(self, context):
        scene = context.scene
        collection = _active_collection(context)
        selected_meshes = [o for o in context.selected_objects if getattr(o, "type", None) == 'MESH']
        raw_entity = scene.witcher_ac_entity_path if self.kind == KIND_TRAJECTORY else scene.witcher_ac_custom_entity_path
        if _report_absolute_path(self, raw_entity, "Entity path"):
            return {'CANCELLED'}

        if self.kind == KIND_TRAJECTORY:
            entity_path = _entity_path_or_default(scene.witcher_ac_entity_path, DEFAULT_TRAJECTORY_ENTITY_PATH)
            scene.witcher_ac_entity_path = entity_path
            arm = create_animated_component(
                _entity_name_from_path(entity_path), ac.TRAJECTORY_RIG_PATH,
                ac.trajectory_bone_names(), entity_path, ac.CUTSCENE_BEHAVIOR_PATH,
                target_collection=collection, kind=KIND_TRAJECTORY)
            root = arm.parent
            message = f"Created '{root.name}' with {ac.TRAJECTORY_BONE_COUNT} trajectory slots."
        else:
            entity_path = _entity_path_or_default(scene.witcher_ac_custom_entity_path, DEFAULT_CUSTOM_ENTITY_PATH)
            scene.witcher_ac_custom_entity_path = entity_path
            name = _entity_name_from_path(entity_path)
            if self.kind == KIND_ANIMATED:
                skeleton = _norm_repo_path(scene.witcher_ac_skeleton_path)
                if not skeleton.lower().endswith(".w2rig"):
                    self.report({'ERROR'}, "Set a game-relative .w2rig skeleton path.")
                    return {'CANCELLED'}
                rig_file = _resolve_repo_file(context, skeleton)
                if not rig_file:
                    self.report({'ERROR'}, f"Skeleton not found under the project/uncook paths: {skeleton}")
                    return {'CANCELLED'}
                behavior = _norm_repo_path(scene.witcher_ac_behavior_path) if scene.witcher_ac_use_behavior else ""
                try:
                    arm = create_animated_component(
                        name, skeleton, [], entity_path, behavior,
                        target_collection=collection, rig_file=rig_file, kind=KIND_ANIMATED)
                except Exception as exc:
                    log.exception("Failed to load skeleton %s", rig_file)
                    self.report({'ERROR'}, f"Failed to load skeleton: {exc}")
                    return {'CANCELLED'}
                root = arm.parent
                message = f"Created '{root.name}' from {os.path.basename(skeleton)} ({len(arm.data.bones)} bones)."
            else:
                arm = None
                root = create_entity_root(name, entity_path, KIND_STATIC, collection)
                added, missing = _add_static_meshes(root, selected_meshes, "")
                message = f"Created '{root.name}'" + (f" with {added} static mesh(es)." if added else ".")
                if missing:
                    self.report({'WARNING'}, f"No .w2mesh path for: {', '.join(missing[:4])} "
                                             "(set Mesh Path, then Add Selected Meshes).")

        _select_only(context, arm or root)
        if arm is not None:
            set_main_armature(scene, arm)
        self.report({'INFO'}, message)
        return {'FINISHED'}


class WITCH_OT_AddHardAttachment(bpy.types.Operator):
    """Attach selected meshes to a bone slot."""
    bl_idname = "witcher.add_hard_attachment"
    bl_label = "Attach Selected Meshes"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_animated_component(context) is not None

    def execute(self, context):
        scene = context.scene
        arm = active_animated_component(context)
        if arm is None:
            self.report({'WARNING'}, "Select an entity with a CAnimatedComponent first.")
            return {'CANCELLED'}

        slot = str(getattr(scene, "witcher_ac_target_slot", "") or "").strip()
        if not slot or slot not in arm.data.bones:
            self.report({'WARNING'}, "Choose a valid slot bone.")
            return {'CANCELLED'}

        meshes = [o for o in context.selected_objects if getattr(o, "type", None) == 'MESH']
        if not meshes:
            self.report({'WARNING'}, "Select one or more mesh objects to attach.")
            return {'CANCELLED'}

        if _report_absolute_path(self, scene.witcher_ac_mesh_path, "Mesh Path"):
            return {'CANCELLED'}
        override = _norm_repo_path(scene.witcher_ac_mesh_path)
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
    """Remove an attachment without moving its mesh."""
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
            if P_TYPE in mesh:
                del mesh[P_TYPE]
        bpy.data.objects.remove(empty, do_unlink=True)
        return {'FINISHED'}


class WITCH_OT_AddStaticMeshComponent(bpy.types.Operator):
    """Add selected meshes as static components without moving them."""
    bl_idname = "witcher.add_static_mesh_component"
    bl_label = "Add Selected Meshes"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return active_builder_root(context) is not None

    def execute(self, context):
        root = active_builder_root(context)
        meshes = [o for o in context.selected_objects if getattr(o, "type", None) == 'MESH']
        if not meshes:
            self.report({'WARNING'}, "Select one or more mesh objects to add.")
            return {'CANCELLED'}
        if _report_absolute_path(self, context.scene.witcher_ac_mesh_path, "Mesh Path"):
            return {'CANCELLED'}
        override = _norm_repo_path(context.scene.witcher_ac_mesh_path)
        if override and not override.lower().endswith(".w2mesh"):
            self.report({'WARNING'}, "Mesh Path override must end with .w2mesh.")
            return {'CANCELLED'}
        added, missing = _add_static_meshes(root, meshes, override)
        if missing:
            self.report({'WARNING'}, f"Added {added}; no .w2mesh path for: "
                                     f"{', '.join(missing[:4])} (set Mesh Path override).")
        else:
            self.report({'INFO'}, f"Added {added} static mesh(es) to '{root.name}'.")
        return {'FINISHED'}


class WITCH_OT_RemoveStaticMeshComponent(bpy.types.Operator):
    """Detach a static component without moving its mesh."""
    bl_idname = "witcher.remove_static_mesh_component"
    bl_label = "Remove Static Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    mesh_name: StringProperty(default="")

    def execute(self, context):
        mesh = bpy.data.objects.get(self.mesh_name)
        if mesh is None:
            return {'CANCELLED'}
        mw = mesh.matrix_world.copy()
        mesh.parent = None
        mesh.matrix_world = mw
        if P_TYPE in mesh:
            del mesh[P_TYPE]
        return {'FINISHED'}


class WITCH_OT_BuilderPathDetails(bpy.types.Operator):
    """Show and copy the resolved depot path."""
    bl_idname = "witcher.entity_builder_path_details"
    bl_label = "Depot Path"
    bl_options = {'INTERNAL'}

    text: StringProperty(name="Depot Path", default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        self.layout.prop(self, "text", text="")

    def execute(self, context):
        context.window_manager.clipboard = self.text
        self.report({'INFO'}, "Depot path copied to clipboard")
        return {'FINISHED'}


class WITCH_OT_ExportBuilderEntity(bpy.types.Operator):
    """Export the active entity to the configured writable depot."""
    bl_idname = "witcher.export_builder_entity"
    bl_label = "Export .w2ent"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return active_builder_root(context) is not None

    def execute(self, context):
        root = active_builder_root(context)
        arm = root_armature(root)

        entity_path = _norm_repo_path(root.get(P_ENTITY_PATH, "") or (arm.get(P_ENTITY_PATH, "") if arm else ""))
        if not entity_path:
            self.report({'ERROR'}, "Entity has no .w2ent path.")
            return {'CANCELLED'}

        attachments, skipped = collect_hard_attachment_export_data(arm) if arm else ([], [])
        statics, skipped_static = collect_static_mesh_export_data(root)
        skipped += skipped_static
        if skipped:
            self.report({'WARNING'}, "Skipped mesh(es) without a .w2mesh path: "
                                     f"{', '.join(skipped[:4])}")
        if arm is None and not statics:
            self.report({'ERROR'}, "Add at least one static mesh with a .w2mesh path before exporting.")
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

        # Custom props are user-editable; re-validate them like typed input.
        if arm is not None and (_report_absolute_path(self, arm.get(P_PATH, ""), "Skeleton path")
                                or _report_absolute_path(self, arm.get(P_BEHAVIOR, ""), "Behavior path")):
            return {'CANCELLED'}
        skeleton = (_norm_repo_path(arm.get(P_PATH, "")) or ac.TRAJECTORY_RIG_PATH) if arm else None
        behavior = _norm_repo_path(arm.get(P_BEHAVIOR, "")) if arm else ""
        component_name = str(arm.get(P_NAME, "") or ac.DEFAULT_COMPONENT_NAME) if arm else ac.DEFAULT_COMPONENT_NAME
        try:
            ac.generate_entity(
                attachments, out_path,
                skeleton_path=skeleton, behavior_path=behavior or None,
                entity_name=_entity_name_from_path(entity_path), component_name=component_name,
                static_meshes=statics)
        except Exception as exc:
            log.exception("Entity export failed.")
            self.report({'ERROR'}, f"Export failed: {exc}")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Exported '{root.name}': {len(attachments)} attachment(s), "
                              f"{len(statics)} static mesh(es) -> {out_path}")
        return {'FINISHED'}


# Panels

def _draw_component_row(row, name, depot):
    row.label(text=name if depot else f"{name} (no .w2mesh path)", icon='MESH_DATA' if depot else 'ERROR')
    sub = row.row(align=True)
    sub.enabled = bool(depot)
    sub.operator(WITCH_OT_BuilderPathDetails.bl_idname, text="", icon='INFO').text = depot


class WITCHER_PT_animated_component(WITCH_PT_Base, bpy.types.Panel):
    bl_idname = "WITCHER_PT_animated_component"
    bl_label = "Entity Builder"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='MOD_BUILD')

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        root = active_builder_root(context)
        if root is None:
            self._draw_create(layout, context)
        else:
            self._draw_edit(layout, context.scene, root)

    def _draw_create(self, layout, context):
        scene = context.scene
        box = layout.box()
        box.label(text="Trajectories Entity (Advanced)", icon='OUTLINER_OB_ARMATURE')
        col = box.column(align=True)
        col.label(text="trajectories_24: 24 animatable prop slots.", icon='INFO')
        col.label(text="Cutscene props: Cutscene → Actors → Props.", icon='BLANK1')
        box.prop(scene, "witcher_ac_entity_path", text="Entity (.w2ent)")
        row = box.row()
        row.scale_y = 1.5
        op = row.operator(WITCH_OT_CreateBuilderEntity.bl_idname, text="Create trajectories_24 Entity", icon='ADD')
        op.kind = KIND_TRAJECTORY

        active = getattr(context, "active_object", None)
        if active is not None and (_entity_root_of(active) is not None or is_animated_component(active)):
            layout.label(text="Imported entities can't be edited or exported here.", icon='INFO')
        else:
            layout.label(text="Select an entity created here to edit it.", icon='INFO')

    def _draw_edit(self, layout, scene, root):
        arm = root_armature(root)
        kind = entity_kind(root)

        info = layout.box()
        row = info.row(align=True)
        row.label(text=root.name, icon='OUTLINER_OB_ARMATURE' if arm else 'OUTLINER_OB_MESH')
        row.label(text=_KIND_LABELS.get(kind, kind))
        col = info.column(align=True)
        path_owner = root if P_ENTITY_PATH in root else arm
        if path_owner is not None and P_ENTITY_PATH in path_owner:
            col.prop(path_owner, f'["{P_ENTITY_PATH}"]', text="Entity")
        else:
            col.label(text="Entity path: not set", icon='ERROR')
        if arm is not None:
            if P_PATH in arm:
                col.prop(arm, f'["{P_PATH}"]', text="Skeleton")
            else:
                col.label(text="Skeleton: not set", icon='ERROR')
            col.label(text=f"Bones: {len(arm.data.bones)}")

        if arm is not None:
            attachments = iter_hard_attachments(arm)
            att_box = layout.box()
            att_box.label(text=f"Attached Meshes ({len(attachments)})", icon='LINKED')
            if attachments:
                for empty, mesh, slot in attachments:
                    row = att_box.row(align=True)
                    mesh_name = str((mesh.get(P_NAME, "") if mesh else "") or (mesh.name if mesh else "<missing>"))
                    row.label(text=slot, icon='BONE_DATA')
                    _draw_component_row(row, mesh_name, resolve_mesh_depot(mesh) if mesh else "")
                    op = row.operator(WITCH_OT_RemoveHardAttachment.bl_idname, text="", icon='X')
                    op.empty_name = empty.name
            else:
                att_box.label(text="No meshes attached yet.", icon='INFO')
            add = att_box.column(align=True)
            add.prop(scene, "witcher_ac_target_slot", text="Slot")
            add.prop(scene, "witcher_ac_mesh_path", text="Mesh Path")
            add.operator(WITCH_OT_AddHardAttachment.bl_idname, icon='LINKED')

        statics = iter_static_meshes(root)
        if kind != KIND_TRAJECTORY or statics:
            st_box = layout.box()
            st_box.label(text=f"Static Meshes ({len(statics)})", icon='MESH_DATA')
            for mesh in statics:
                depot = resolve_mesh_depot(mesh)
                row = st_box.row(align=True)
                _draw_component_row(row, str(mesh.get(P_NAME, "") or mesh.name), depot)
                op = row.operator(WITCH_OT_RemoveStaticMeshComponent.bl_idname, text="", icon='X')
                op.mesh_name = mesh.name
            add = st_box.column(align=True)
            if arm is None:
                add.prop(scene, "witcher_ac_mesh_path", text="Mesh Path")
            add.operator(WITCH_OT_AddStaticMeshComponent.bl_idname, icon='ADD')

        layout.separator(factor=0.5)
        row = layout.row()
        row.scale_y = 1.3
        row.operator(WITCH_OT_ExportBuilderEntity.bl_idname, icon='EXPORT')


class WITCHER_PT_entity_builder_custom(WITCH_PT_Base, bpy.types.Panel):
    bl_idname = "WITCHER_PT_entity_builder_custom"
    bl_parent_id = "WITCHER_PT_animated_component"
    bl_label = "Custom Entity"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return active_builder_root(context) is None

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        scene = context.scene
        layout.row().prop(scene, "witcher_ac_kind", expand=True)
        col = layout.column(align=True)
        rigged = scene.witcher_ac_kind == KIND_ANIMATED
        if rigged:
            col.prop(scene, "witcher_ac_skeleton_path", text="Skeleton (.w2rig)")
            beh = col.row(align=True)
            beh.prop(scene, "witcher_ac_use_behavior", text="")
            sub = beh.row(align=True)
            sub.enabled = bool(scene.witcher_ac_use_behavior)
            sub.prop(scene, "witcher_ac_behavior_path", text="Behavior (.w2beh)")
        else:
            col.label(text="Selected meshes become CStaticMeshComponents.", icon='INFO')
        col.prop(scene, "witcher_ac_custom_entity_path", text="Entity (.w2ent)")
        op = layout.operator(WITCH_OT_CreateBuilderEntity.bl_idname,
                             text="Create Rigged Entity" if rigged else "Create Static Entity", icon='ADD')
        op.kind = scene.witcher_ac_kind


classes = [
    WITCH_OT_CreateBuilderEntity,
    WITCH_OT_AddHardAttachment,
    WITCH_OT_RemoveHardAttachment,
    WITCH_OT_AddStaticMeshComponent,
    WITCH_OT_RemoveStaticMeshComponent,
    WITCH_OT_BuilderPathDetails,
    WITCH_OT_ExportBuilderEntity,
    WITCHER_PT_animated_component,
    WITCHER_PT_entity_builder_custom,
]

_scene_props = {
    "witcher_ac_entity_path": StringProperty(
        name="Entity Path", description="Game-relative path for the exported trajectories_24 .w2ent",
        default=DEFAULT_TRAJECTORY_ENTITY_PATH),
    "witcher_ac_kind": EnumProperty(
        name="Kind", description="Custom entity type",
        items=[(KIND_ANIMATED, "Rigged", "CAnimatedComponent bound to a .w2rig; meshes hard-attach to bones"),
               (KIND_STATIC, "Static", "CStaticMeshComponents only, no skeleton")],
        default=KIND_ANIMATED),
    "witcher_ac_skeleton_path": StringProperty(
        name="Skeleton", description="Game-relative CSkeleton (.w2rig) the component binds to",
        default=""),
    "witcher_ac_use_behavior": BoolProperty(
        name="Behavior", description="Bind a CBehaviorGraph (cutscene) to the component",
        default=True),
    "witcher_ac_behavior_path": StringProperty(
        name="Behavior", description="CBehaviorGraph (.w2beh) instance slot",
        default=ac.CUTSCENE_BEHAVIOR_PATH),
    "witcher_ac_custom_entity_path": StringProperty(
        name="Entity Path", description="Game-relative path for the exported .w2ent",
        default=DEFAULT_CUSTOM_ENTITY_PATH),
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
