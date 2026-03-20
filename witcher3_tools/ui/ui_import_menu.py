import bpy

from bpy.props import (StringProperty,
                       BoolProperty,
                       CollectionProperty,
                       IntProperty,
                       FloatProperty,
                       EnumProperty,
                       PointerProperty
                       )


from ..ui.ui_anims import (ButtonOperatorImportW2Anims, WITCH_OT_import_w3_fbx, WITCH_OT_ImportW2Rig, WITCH_OT_ExportW2AnimJson, WITCH_OT_ExportW2RigJson)
from ..ui.ui_mesh import (WITCH_OT_w2mesh, WITCH_OT_w2mesh_export, WITCH_OT_apx, WITCH_OT_nxs,
                          WITCH_OT_export_goto_project_path, WITCH_OT_set_repo_path_from_browser,
                          WITCH_OT_generate_lods,
                          WITCH_OT_create_box_collider, WITCH_OT_create_sphere_collider,
                          WITCH_OT_create_capsule_collider, WITCH_OT_create_convex_collider,
                          WITCH_OT_create_trimesh_collider,
                          WITCH_OT_create_sound_info, WITCH_OT_remove_sound_info,
                          PHYSICAL_MATERIAL_ENUM_ITEMS, DEFAULT_PHYSICAL_MATERIAL)
from ..ui.ui_entity import WITCH_OT_w2ent, WITCH_OT_flyr, WITCH_OT_ENTITY_w2ent_chara, WITCH_OT_ENTITY_import_inventory
from ..ui.ui_scene import ButtonOperatorImportW2scene, ButtonOperatorImportW2cutscene
from ..ui.ui_speech import ButtonOperatorImportVoice, ImportWEM
from ..ui.ui_material import WITCH_OT_w2mg, WITCH_OT_w2mi, WITCH_OT_xbm, WITCH_OT_w2cube
from ..ui.ui_texture_export import WITCH_OT_xbm_export, WITCH_OT_dds_convert
from ..ui.ui_map import (WITCH_OT_w2L,
                                     WITCH_OT_w2w,
                                    #  WITCH_OT_load_layer,
                                    #  WITCH_OT_load_layer_group,
                                    #  WITCH_OT_radish_w2L
                                     )
from ..ui.ui_custom_icons import custom_icons
from ..external_addon_tools import get_srt_addon_status
from bpy_extras.io_utils import ImportHelper
import os
import logging

log = logging.getLogger(__name__)


def _witcher_menu_icon_kwargs():
    try:
        return {"icon_value": custom_icons["main"]["witcher_icon"].icon_id}
    except Exception:
        return {"icon": 'PLUGIN'}


def _safe_menu_remove(menu_type, callback):
    try:
        menu_type.remove(callback)
    except (ValueError, RuntimeError, AttributeError):
        pass

class WITCH_OT_srt(bpy.types.Operator, ImportHelper):
    """Import SpeedTree (.srt) file using io_mesh_srt addon"""
    bl_idname = "witcher.import_srt"
    bl_label = "Import SpeedTree (.srt)"
    filename_ext = ".srt"

    filter_glob: StringProperty(default='*.srt', options={'HIDDEN'})

    def execute(self, context):
        from .. import get_all_addon_prefs, get_uncook_path
        srt_status = get_srt_addon_status()
        if not srt_status["enabled"]:
            self.report({'ERROR'}, "io_mesh_srt addon is required. Install from: " + srt_status.get("url", ""))
            return {'CANCELLED'}

        fdir = self.filepath
        if not os.path.isfile(fdir):
            self.report({'ERROR'}, "File not found.")
            return {'CANCELLED'}

        from ..ui.ui_file_browser import (
            _export_srt_textures_for_import,
            _prepare_srt_lod0_json,
            _snapshot_srt_import_state,
            _flatten_srt_import_collections,
        )
        prefs = get_all_addon_prefs(context)
        use_custom_grouping = bool(getattr(prefs, "ab_srt_custom_grouping", True))
        lod0_only = bool(getattr(prefs, "ab_srt_lod0_only", True))

        srt_snapshot = _snapshot_srt_import_state(context) if use_custom_grouping else {}
        tex_stats = _export_srt_textures_for_import(context, fdir, fdir, loadmods=False)
        import_path = tex_stats.get("import_path") or fdir
        if lod0_only:
            import_path = _prepare_srt_lod0_json(import_path)
        result = getattr(bpy.ops, "import").srt_json(filepath=import_path)
        if 'FINISHED' not in result:
            self.report({'ERROR'}, f"SRT import failed: {os.path.basename(fdir)}")
            return {'CANCELLED'}
        if use_custom_grouping:
            _flatten_srt_import_collections(context, import_path, srt_snapshot)
        return {'FINISHED'}

    def invoke(self, context, event):
        from .. import get_uncook_path
        UNCOOK_PATH = get_uncook_path(context)
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)


class WITCH_MT_Menu(bpy.types.Menu):
    bl_label = "Witcher 3 Assets"
    bl_idname = "IMPORT_MT_witcherio"

    def draw(self, context):
        layout = self.layout
        layout.operator(WITCH_OT_w2mesh.bl_idname, text="Mesh (.w2mesh)", icon='MESH_DATA')
        layout.operator(WITCH_OT_apx.bl_idname, text="Redcloth (.redcloth)", icon='MESH_DATA')
        layout.operator(WITCH_OT_nxs.bl_idname, text="Collision (.nxs)", icon='MESH_DATA')
        layout.operator(WITCH_OT_import_w3_fbx.bl_idname, text="Witcher 3 FBX (.fbx)", icon='MESH_DATA')
        layout.separator()
        layout.operator(WITCH_OT_w2ent.bl_idname, text="Item Entity (.w2ent)", icon='MESH_DATA')
        layout.operator(WITCH_OT_ENTITY_w2ent_chara.bl_idname, text="Character Entity (.w2ent)", icon='MESH_DATA')
        layout.operator(WITCH_OT_ENTITY_import_inventory.bl_idname, text="Inventory (.w2ent)", icon='MESH_DATA')
        layout.separator()
        layout.operator(WITCH_OT_w2mi.bl_idname, text="Instance (.w2mi)", icon='MESH_DATA')
        layout.operator(WITCH_OT_w2mg.bl_idname, text="Shader (.w2mg)", icon='MESH_DATA')
        layout.operator(WITCH_OT_xbm.bl_idname, text="Texture (.xbm)", icon='SPHERE')
        layout.operator(WITCH_OT_w2cube.bl_idname, text="Cubemap (.w2cube)", icon='MESH_CUBE')
        layout.separator()
        layout.operator(WITCH_OT_ImportW2Rig.bl_idname, text="Rig (.w2rig)", icon='ARMATURE_DATA')
        layout.operator(ButtonOperatorImportW2Anims.bl_idname, text="Animation (.w2anims)", icon='ARMATURE_DATA')
        layout.separator()
        layout.operator(ButtonOperatorImportVoice.bl_idname, text="Voiceline Pair (.cr2w)", icon='SPEAKER')
        layout.operator(ImportWEM.bl_idname, text="Audio (.wem)", icon='SOUND')
        layout.separator()
        layout.operator(WITCH_OT_w2L.bl_idname, text="Layer (.w2l)", icon='SPHERE')
        layout.operator(WITCH_OT_w2w.bl_idname, text="World (.w2w)", icon='WORLD_DATA')
        layout.operator(WITCH_OT_flyr.bl_idname, text="Foliage (.flyr)", icon='FORCE_WIND')
        layout.operator(WITCH_OT_srt.bl_idname, text="SpeedTree (.srt)", icon='OUTLINER_COLLECTION')
        layout.separator()
        layout.operator(ButtonOperatorImportW2scene.bl_idname, text="Scene (.w2scene)", icon='SCENE_DATA')
        layout.operator(ButtonOperatorImportW2cutscene.bl_idname, text="Cutscene (.w2cutscene)", icon='SCENE_DATA')
        layout.separator()
        layout.operator("witcher.open_external_collision_cache", text="Open collision.cache", icon="MESH_CUBE")
        layout.operator("witcher.open_external_bundle", text="Open .bundle", icon="PACKAGE")

class WITCH_MT_Menu_witcher_2(bpy.types.Menu):
    bl_label = "Witcher 2 Assets"
    bl_idname = "IMPORT_MT_witcher2io"

    def draw(self, context):
        layout = self.layout
        layout.operator(WITCH_OT_w2mesh.bl_idname, text="Mesh (.w2mesh)", icon='MESH_DATA')
        layout.separator()
        layout.operator(WITCH_OT_w2ent.bl_idname, text="Entity (.w2ent)", icon='MESH_DATA')
        layout.operator(WITCH_OT_ENTITY_w2ent_chara.bl_idname, text="Character Entity (.w2ent)", icon='MESH_DATA')
        layout.separator()
        layout.operator(WITCH_OT_ImportW2Rig.bl_idname, text="Rig (.w2rig)", icon='ARMATURE_DATA')
        layout.operator(ButtonOperatorImportW2Anims.bl_idname, text="Animations (.w2anims)", icon='ACTION')
        layout.separator()
        layout.operator(WITCH_OT_w2L.bl_idname, text="Layer (.w2l)", icon='SPHERE')
        layout.operator(WITCH_OT_w2w.bl_idname, text="World (.w2w)", icon='WORLD_DATA')

def menu_import_witcher_2(self, context):
    self.layout.menu(WITCH_MT_Menu.bl_idname, **_witcher_menu_icon_kwargs())

def menu_import(self, context):
    self.layout.menu(WITCH_MT_Menu_witcher_2.bl_idname, **_witcher_menu_icon_kwargs())

class WITCH_MT_MenuExport(bpy.types.Menu):
    bl_label = "Witcher 3 Assets"
    bl_idname = "EXPORT_MT_witcherio"

    def draw(self, context):
        layout = self.layout
        layout.operator(WITCH_OT_w2mesh_export.bl_idname, text="Mesh (.w2mesh)", icon='MESH_DATA')
        layout.separator()
        layout.operator(WITCH_OT_ExportW2RigJson.bl_idname, text="Rig (.w2rig)", icon='ARMATURE_DATA')
        layout.operator(WITCH_OT_ExportW2AnimJson.bl_idname, text="Animation (.w2anims)", icon='ARMATURE_DATA')
        layout.separator()
        layout.operator(WITCH_OT_xbm_export.bl_idname, text="Texture (.xbm)", icon='IMAGE_DATA')
        layout.operator(WITCH_OT_dds_convert.bl_idname, text="Convert DDS to Editable", icon='FILE_REFRESH')

def menu_export(self, context):
    self.layout.menu(WITCH_MT_MenuExport.bl_idname, **_witcher_menu_icon_kwargs())


classes = (
    WITCH_OT_import_w3_fbx,
    WITCH_OT_w2mesh,
    WITCH_OT_w2mesh_export,
    WITCH_OT_export_goto_project_path,
    WITCH_OT_set_repo_path_from_browser,
    WITCH_OT_generate_lods,
    WITCH_OT_create_box_collider,
    WITCH_OT_create_sphere_collider,
    WITCH_OT_create_capsule_collider,
    WITCH_OT_create_convex_collider,
    WITCH_OT_create_trimesh_collider,
    WITCH_OT_create_sound_info,
    WITCH_OT_remove_sound_info,
    WITCH_OT_apx,
    WITCH_OT_nxs,
    WITCH_OT_srt,
    WITCH_OT_xbm_export,
    WITCH_OT_dds_convert,
    WITCH_MT_Menu,
    WITCH_MT_MenuExport,
    WITCH_MT_Menu_witcher_2
)

register_, unregister_ = bpy.utils.register_classes_factory(classes)

def register():
    register_()
    if hasattr(bpy.types.Scene, "witcher_collision_physical_material"):
        del bpy.types.Scene.witcher_collision_physical_material
    bpy.types.Scene.witcher_collision_physical_material = EnumProperty(
        name="Physical Material",
        description="Physical material for collision meshes",
        items=PHYSICAL_MATERIAL_ENUM_ITEMS,
        default=DEFAULT_PHYSICAL_MATERIAL,
    )
    _safe_menu_remove(bpy.types.TOPBAR_MT_file_import, menu_import)
    _safe_menu_remove(bpy.types.TOPBAR_MT_file_import, menu_import_witcher_2)
    _safe_menu_remove(bpy.types.TOPBAR_MT_file_export, menu_export)
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_import.append(menu_import_witcher_2)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)

def unregister():
    _safe_menu_remove(bpy.types.TOPBAR_MT_file_export, menu_export)
    _safe_menu_remove(bpy.types.TOPBAR_MT_file_import, menu_import)
    _safe_menu_remove(bpy.types.TOPBAR_MT_file_import, menu_import_witcher_2)
    if hasattr(bpy.types.Scene, "witcher_collision_physical_material"):
        del bpy.types.Scene.witcher_collision_physical_material
    unregister_()
