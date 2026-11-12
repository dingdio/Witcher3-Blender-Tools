"""Clip injection UI. REDkit edits use workspace copies and project backups."""
import os
import time

import bpy
from bpy.props import BoolProperty, CollectionProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from bpy_extras.io_utils import ExportHelper

from ..CR2W import w2anims_inject as wi

_SEARCH_CACHE = {}  # root -> (scanned_at, [(clip, duration, has_me, path)])


def settings(context):
    return context.scene.witcher_inject


def _prefs(context):
    from .. import get_all_addon_prefs
    return get_all_addon_prefs(context)


def _norm(path):
    return os.path.normcase(os.path.realpath(bpy.path.abspath(path))) if path else ""


def _under(path, root):
    path, root = _norm(path), _norm(root)
    if not path or not root:
        return False
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def _project_items(context):
    items = getattr(_prefs(context), "redkit_projects", None)
    return [] if items is None or isinstance(items, str) else list(items)


def redkit_projects(context):
    return [os.path.normpath(bpy.path.abspath(p.path)) for p in _project_items(context) if getattr(p, "path", "")]


def active_project(context):
    projects = _project_items(context)
    index = getattr(_prefs(context), "redkit_projects_index", 0)
    if 0 <= index < len(projects) and getattr(projects[index], "path", ""):
        return os.path.normpath(bpy.path.abspath(projects[index].path))
    return None


def depot_roots(context):
    prefs = _prefs(context)
    roots = [getattr(prefs, a, "") for a in ("redkit_depot_path", "redkit_uncooked_path")]
    return [os.path.normpath(bpy.path.abspath(r)) for r in roots if r]


def game_roots(context):
    prefs = _prefs(context)
    return [os.path.normpath(bpy.path.abspath(r)) for r in (getattr(prefs, "uncook_path", ""),) if r]


def classify(context, path):
    """Return (kind, containing root, relative path)."""
    for root in depot_roots(context):
        if _under(path, root):
            return "DEPOT", root, os.path.relpath(_norm(path), _norm(root))
    for project in redkit_projects(context):
        ws = os.path.join(project, "workspace")
        if _under(path, ws):
            return "WORKSPACE", project, os.path.relpath(_norm(path), _norm(ws))
    for root in game_roots(context):
        if _under(path, root):
            return "GAME", root, os.path.relpath(_norm(path), _norm(root))
    return "OTHER", None, None


def depot_twin(context, path):
    """Find the same relative path in the REDkit depots."""
    kind, _, rel = classify(context, path)
    if not rel:
        return None
    for root in depot_roots(context):
        candidate = os.path.join(root, rel)
        if os.path.isfile(candidate) and _norm(candidate) != _norm(path):
            return candidate
    return None


class WitcherInjectClip(bpy.types.PropertyGroup):
    name: StringProperty()
    duration: FloatProperty()
    events: IntProperty()
    has_motion: BoolProperty()


class WitcherInjectHit(bpy.types.PropertyGroup):
    name: StringProperty()
    path: StringProperty(name="Set", subtype='FILE_PATH')
    duration: FloatProperty()
    has_motion: BoolProperty()


def _load_target(self, context):
    self.clips.clear()
    self.error = ""
    self.info = ""
    self.external_buffers = 0
    path = bpy.path.abspath(self.target)
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, "rb") as f:
            raw = wi.RawCR2W(f.read())
        clips = wi.describe_set(raw)
    except (OSError, wi.InjectError) as exc:
        self.error = str(exc)
        return
    for c in clips:
        item = self.clips.add()
        item.name, item.duration, item.events, item.has_motion = c["name"], c["duration"], c["events"], c["me"] is not None
    cooked = not wi.set_is_uncooked(raw, clips)
    self.external_buffers = sum(1 for b in raw.buffers if not b[2])
    self.info = (f"v{raw.version}, " + ("cooked, buffers in .N.buffer files" if cooked
                                            else "uncooked (REDkit), buffers embedded"))


class WitcherInjectSettings(bpy.types.PropertyGroup):
    target: StringProperty(name="Target", subtype='FILE_PATH', update=_load_target,
                           description="The .w2anims to add clips to or replace clips in")
    clips: CollectionProperty(type=WitcherInjectClip)
    clip_index: IntProperty()
    error: StringProperty()
    info: StringProperty()
    last_result: StringProperty(name="Last Operation")
    external_buffers: IntProperty()
    searched: StringProperty()
    query: StringProperty(name="Find Clip", description="Words that must all appear in the clip name, e.g. 'walk cycle'")
    hits: CollectionProperty(type=WitcherInjectHit)
    hit_index: IntProperty()


class WITCH_UL_inject_clips(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        replaced = any(e.enabled and e.replace_clip.lower() == item.name.lower()
                       for e in context.scene.witcher_anim_export_set)
        op = row.operator("witcher.inject_details", text="", icon='FILE_REFRESH' if replaced else 'ACTION', emboss=False)
        op.kind, op.index = 'CLIP', index
        if item.has_motion:
            op = row.operator("witcher.inject_details", text="", icon='CON_FOLLOWPATH', emboss=False)
            op.kind, op.index = 'CLIP', index
        row.label(text=item.name)


class WITCH_UL_inject_hits(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        op = row.operator("witcher.inject_details", text="", icon='CON_FOLLOWPATH' if item.has_motion else 'ACTION', emboss=False)
        op.kind, op.index = 'HIT', index
        row.label(text=item.name)


class WITCH_OT_inject_details(bpy.types.Operator):
    bl_idname = "witcher.inject_details"
    bl_label = "Animation Set Details"
    bl_description = "Show copyable clip and set details, including replacement and motion status"

    kind: StringProperty(default='TARGET', options={'HIDDEN', 'SKIP_SAVE'})
    index: IntProperty(default=-1, options={'HIDDEN', 'SKIP_SAVE'})
    clip_name: StringProperty(name="Clip")
    path: StringProperty(name="Set", subtype='FILE_PATH')
    project: StringProperty(name="Project", subtype='DIR_PATH')
    info: StringProperty(name="Details")
    error: StringProperty(name="Error")
    result: StringProperty(name="Last Operation")

    def invoke(self, context, event):
        s = settings(context)
        self.path, self.info, self.error = s.target, s.info, s.error
        self.result = s.last_result
        items = s.hits if self.kind == 'HIT' else s.clips
        if self.kind != 'TARGET' and 0 <= self.index < len(items):
            item = items[self.index]
            self.clip_name = item.name
            self.path = item.path if self.kind == 'HIT' else s.target
            self.info = f"{item.duration:.2f} s; motion extraction: {'yes' if item.has_motion else 'no'}"
            if self.kind == 'CLIP':
                self.info += f"; {item.events} source events"
                replacements = [e.action_name for e in _entries(context) if e.replace_clip.lower() == item.name.lower()]
                if replacements:
                    self.info += "; replaced by " + ", ".join(replacements)
            self.error = ""
        kind, root, _ = classify(context, self.path)
        self.project = root if kind == 'WORKSPACE' else active_project(context) or ""
        return context.window_manager.invoke_props_dialog(self, width=620)

    def draw(self, context):
        col = self.layout.column(align=True)
        if self.clip_name:
            col.prop(self, "clip_name")
        col.prop(self, "path")
        if self.project:
            col.prop(self, "project")
        if self.info:
            col.prop(self, "info")
        if self.error:
            col.prop(self, "error")
        if self.result:
            col.prop(self, "result")

    def execute(self, context):
        return {'FINISHED'}


def _scan_root(root):
    cached = _SEARCH_CACHE.get(root)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    found = []
    anims = os.path.join(root, "animations")
    for base in [anims] if os.path.isdir(anims) else [root]:
        for folder, _, files in os.walk(base):
            for name in files:
                if not name.lower().endswith(".w2anims"):
                    continue
                path = os.path.join(folder, name)
                try:
                    with open(path, "rb") as f:
                        clips = wi.describe_set(wi.RawCR2W(f.read()))
                except (OSError, wi.InjectError):
                    continue
                found += [(c["name"], c["duration"], c["me"] is not None, path) for c in clips]
    _SEARCH_CACHE[root] = (time.time(), found)
    return found


def search_roots(context):
    """Return (roots, label); configured REDkit projects use only REDkit depots."""
    depots = [r for r in depot_roots(context) if os.path.isdir(r)]
    if depots or redkit_projects(context):
        return depots, "REDkit depot"
    return [r for r in game_roots(context) if os.path.isdir(r)], "game uncook folder"


class WITCH_OT_inject_find_clip(bpy.types.Operator):
    """Find animation sets containing a matching clip"""
    bl_idname = "witcher.inject_find_clip"
    bl_label = "Find"

    def execute(self, context):
        s = settings(context)
        words = s.query.lower().split()
        if not words:
            self.report({'WARNING'}, "Type part of a clip name, e.g. 'walk cycle'")
            return {'CANCELLED'}
        roots, label = search_roots(context)
        if not roots:
            self.report({'ERROR'}, "Set REDkit Depot Path (r4data) in the add-on preferences" if redkit_projects(context)
                        else "Set the REDkit depot or the uncook path in the add-on preferences")
            return {'CANCELLED'}
        s.searched = f"Searched the {label}"
        s.hits.clear()
        hits = [h for root in roots for h in _scan_root(root) if all(w in h[0].lower() for w in words)]
        for name, duration, has_me, path in sorted(hits, key=lambda h: (len(h[0]), h[0]))[:200]:
            item = s.hits.add()
            item.name, item.duration, item.has_motion, item.path = name, duration, has_me, path
        self.report({'INFO'}, f"{len(hits)} clip(s) found")
        return {'FINISHED'}


class WITCH_OT_inject_use_hit(bpy.types.Operator):
    """Replace the selected search clip with the selected export action"""
    bl_idname = "witcher.inject_use_hit"
    bl_label = "Use"

    @classmethod
    def poll(cls, context):
        s = settings(context)
        return 0 <= s.hit_index < len(s.hits)

    def execute(self, context):
        s = settings(context)
        hit = s.hits[s.hit_index]
        s.target = hit.path
        export_set = context.scene.witcher_anim_export_set
        idx = context.scene.witcher_anim_export_set_index
        if 0 <= idx < len(export_set):
            export_set[idx].replace_clip = hit.name
        return {'FINISHED'}


class WITCH_OT_inject_copy_to_workspace(bpy.types.Operator):
    """Copy the set to the active workspace with embedded buffers"""
    bl_idname = "witcher.inject_copy_to_workspace"
    bl_label = "Copy to Workspace"

    @classmethod
    def poll(cls, context):
        s = settings(context)
        return bool(s.target) and active_project(context) is not None and \
            classify(context, s.target)[0] in {"DEPOT", "GAME"}

    def execute(self, context):
        s = settings(context)
        kind, _, rel = classify(context, s.target)
        dst = os.path.join(active_project(context), "workspace", rel)
        src = bpy.path.abspath(s.target)
        twin = depot_twin(context, src) if kind == "GAME" else None
        if os.path.exists(dst):
            self.report({'INFO'}, f"Workspace already has {rel}; using it")
        else:
            try:
                wi.copy_into_workspace(twin or src, dst)
            except (OSError, wi.InjectError) as exc:
                s.last_result = str(exc)
                self.report({'ERROR'}, f"Copy cancelled: {exc}")
                return {'CANCELLED'}
            if kind == "GAME" and not twin:
                self.report({'WARNING'}, f"Copied {rel}, but the REDkit depot has no copy of it, so this is the cooked "
                                         "game set (buffers embedded, no editor data)")
            else:
                self.report({'INFO'}, f"Copied REDkit's uncooked {rel} to the workspace")
        s.target = dst
        s.last_result = f"Workspace set: {dst}"
        return {'FINISHED'}


class WITCH_OT_inject_rebuild_from_depot(bpy.types.Operator):
    """Back up the workspace set, then replace it with the REDkit depot copy"""
    bl_idname = "witcher.inject_rebuild_from_depot"
    bl_label = "Rebuild From REDkit Depot"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        s = settings(context)
        return classify(context, s.target)[0] == "WORKSPACE" and s.external_buffers > 0 and \
            depot_twin(context, bpy.path.abspath(s.target)) is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        s = settings(context)
        path = bpy.path.abspath(s.target)
        _, project, rel = classify(context, path)
        twin = depot_twin(context, path)
        try:
            report = wi.rebuild_workspace(twin, path, os.path.join(project, "blender_backups"))
        except (OSError, wi.InjectError) as exc:
            s.last_result = str(exc)
            self.report({'ERROR'}, f"Rebuild failed: {exc}")
            return {'CANCELLED'}
        _load_target(s, context)
        if report["leftover_buffers"]:
            self.report({'WARNING'}, "Rebuilt set; some unused .buffer files could not be removed")
        s.last_result = f"Rebuilt {rel} from the REDkit depot; backup in {report['backup']}"
        self.report({'INFO'}, s.last_result)
        return {'FINISHED'}


def _entries(context):
    return [e for e in context.scene.witcher_anim_export_set if e.enabled]


def _apply(op, context, out_path, backup_dir=None):
    from ..exporters import export_anims
    s = settings(context)
    source = bpy.path.abspath(s.target)
    try:
        report, skipped = export_anims.inject_w3_anim_set(context, source, out_path, _entries(context),
                                                          embed_buffers=backup_dir is not None, backup_dir=backup_dir)
    except (OSError, ValueError, wi.InjectError) as exc:
        s.last_result = str(exc)
        op.report({'ERROR'}, f"Injection failed: {exc}")
        return {'CANCELLED'}
    parts = []
    if report["replaced"]:
        parts.append(f"replaced {', '.join(report['replaced'])}")
    if report["added"]:
        parts.append(f"added {', '.join(report['added'])}")
    msg = f"{'; '.join(parts)} -> {os.path.basename(out_path)}"
    if report.get("backup"):
        msg += f" (backup in {report['backup']})"
    if skipped:
        msg += f"; skipped: {', '.join(skipped)}"
    if report.get("missing_buffers"):
        op.report({'WARNING'}, f"{len(report['missing_buffers'])} .buffer files were not next to the source; the "
                               "game falls back to the original bundle for them")
    s.last_result = msg
    op.report({'INFO'}, msg)
    if _norm(out_path) == _norm(source):
        _load_target(s, context)
    return {'FINISHED'}


def _poll_apply(context):
    from ..exporters import export_anims
    s = settings(context)
    return bool(s.clips) and export_anims.get_selected_armature(context) is not None and bool(_entries(context))


class WITCH_OT_inject_apply(bpy.types.Operator):
    """Back up the workspace set, then apply the export clips"""
    bl_idname = "witcher.inject_apply"
    bl_label = "Apply to Workspace Set"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        s = settings(context)
        return _poll_apply(context) and classify(context, s.target)[0] == "WORKSPACE" and not s.external_buffers

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        s = settings(context)
        _, project, _ = classify(context, s.target)
        path = bpy.path.abspath(s.target)
        return _apply(self, context, path, backup_dir=os.path.join(project, "blender_backups"))


class WITCH_OT_inject_save_copy(bpy.types.Operator, ExportHelper):
    """Save the modified set as a separate file for WolvenKit mods"""
    bl_idname = "witcher.inject_save_copy"
    bl_label = "Save Modified Copy"
    filename_ext = ".w2anims"
    filter_glob: StringProperty(default="*.w2anims", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return _poll_apply(context) and classify(context, settings(context).target)[0] in {"GAME", "OTHER"}

    def invoke(self, context, event):
        # Keep the filename for depot-path lookup.
        self.filepath = os.path.basename(bpy.path.abspath(settings(context).target))
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        out = self.filepath if self.filepath.lower().endswith(".w2anims") else self.filepath + ".w2anims"
        if _norm(out) == _norm(settings(context).target):
            self.report({'ERROR'}, "Choose a different file; the target is never overwritten here")
            return {'CANCELLED'}
        if classify(context, out)[0] in {"WORKSPACE", "DEPOT"}:
            self.report({'ERROR'}, "That's inside REDkit. Use Copy to Workspace instead: it starts from REDkit's "
                                   "uncooked set, which REDkit can load")
            return {'CANCELLED'}
        return _apply(self, context, out)


def draw(layout, context):
    s = settings(context)
    scene = context.scene

    box = layout.box()
    box.label(text="Find a Game Clip", icon='VIEWZOOM')
    row = box.row(align=True)
    row.prop(s, "query", text="")
    row.operator(WITCH_OT_inject_find_clip.bl_idname, icon='VIEWZOOM')
    if s.searched:
        box.label(text=s.searched, icon='FILE_FOLDER')
    if s.hits:
        box.template_list("WITCH_UL_inject_hits", "", s, "hits", s, "hit_index", rows=4)
        if 0 <= s.hit_index < len(s.hits):
            hit = s.hits[s.hit_index]
            col = box.column(align=True)
            col.scale_y = 0.8
            col.prop(hit, "path", text="Set")
            col.label(text=f"{hit.duration:.2f} s", icon='CON_FOLLOWPATH' if hit.has_motion else 'ACTION')
        box.operator(WITCH_OT_inject_use_hit.bl_idname, text="Use Selected Clip",
                     icon='FILE_REFRESH')

    box = layout.box()
    row = box.row(align=True)
    row.label(text="Inject / Replace", icon='FILE_REFRESH')
    row.operator(WITCH_OT_inject_details.bl_idname, text="", icon='INFO')
    box.prop(s, "target", text="")
    if s.error:
        row = box.row()
        row.alert = True
        row.label(text="Cannot read target; see details", icon='ERROR')
        return
    if not s.clips:
        box.label(text="Choose a target animation set", icon='INFO')
        return

    kind, root, rel = classify(context, s.target)
    col = box.column(align=True)
    col.scale_y = 0.8
    col.label(text=f"{len(s.clips)} clips")
    if kind == "WORKSPACE" and s.external_buffers:
        warn = box.column(align=True)
        warn.alert = True
        warn.label(text="Cooked game set: rebuild required", icon='ERROR')
        if depot_twin(context, bpy.path.abspath(s.target)):
            box.operator(WITCH_OT_inject_rebuild_from_depot.bl_idname, icon='FILE_REFRESH')
        else:
            warn.label(text="No uncooked depot copy found")
    elif kind == "WORKSPACE":
        col.label(text="Workspace; backup before apply", icon='CHECKMARK')
    elif kind == "DEPOT":
        warn = box.column(align=True)
        warn.alert = True
        warn.label(text="REDkit depot (read-only)", icon='LOCKED')
    elif kind == "GAME":
        col.label(text="Game set; save a modified copy", icon='INFO')
    else:
        col.label(text="Save a modified copy", icon='INFO')
    if kind in {"DEPOT", "GAME"}:
        project = active_project(context)
        if project:
            box.operator(WITCH_OT_inject_copy_to_workspace.bl_idname,
                         text="Copy to Workspace", icon='DUPLICATE')
        elif kind == "DEPOT":
            box.label(text="Set a REDkit project in Preferences", icon='INFO')

    box.template_list("WITCH_UL_inject_clips", "", s, "clips", s, "clip_index", rows=4)

    entries = _entries(context)
    if entries:
        col = box.column(align=True)
        col.label(text="Export Set clips:")
        moving = {c.name.lower() for c in s.clips if c.has_motion}
        for entry in entries:
            row = col.row(align=True)
            row.prop_search(entry, "action_name", bpy.data, "actions", text="Action", icon='ACTION')
            col.prop_search(entry, "replace_clip", s, "clips", text="Replace", icon='FILE_REFRESH')
            if entry.replace_clip.lower() in moving and not entry.include_motion_extraction:
                note = col.row()
                note.scale_y = 0.8
                note.label(text="Motion extraction required", icon='INFO')
        replaced = {e.replace_clip.lower() for e in entries if e.replace_clip}
        events = [c for c in s.clips if c.name.lower() in replaced and c.events]
        if events:
            info = box.column(align=True)
            info.scale_y = 0.8
            info.label(text="Edited action events replace these", icon='INFO')
    else:
        box.label(text="Enable clips in the Export Set above", icon='INFO')

    row = box.row()
    row.scale_y = 1.2
    if kind == "WORKSPACE":
        row.operator(WITCH_OT_inject_apply.bl_idname, icon='CHECKMARK')
    elif kind != "DEPOT":
        row.operator(WITCH_OT_inject_save_copy.bl_idname, icon='EXPORT')


_classes = (
    WitcherInjectClip,
    WitcherInjectHit,
    WitcherInjectSettings,
    WITCH_OT_inject_details,
    WITCH_UL_inject_clips,
    WITCH_UL_inject_hits,
    WITCH_OT_inject_find_clip,
    WITCH_OT_inject_use_hit,
    WITCH_OT_inject_copy_to_workspace,
    WITCH_OT_inject_rebuild_from_depot,
    WITCH_OT_inject_apply,
    WITCH_OT_inject_save_copy,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.witcher_inject = PointerProperty(type=WitcherInjectSettings)


def unregister():
    del bpy.types.Scene.witcher_inject
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
