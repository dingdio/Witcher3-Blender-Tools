from pathlib import Path
import bpy

from bpy.props import (StringProperty,
                       BoolProperty,
                       CollectionProperty,
                       IntProperty,
                       FloatProperty,
                       PointerProperty
                       )

custom_icons = {}

from io_import_w2l.ui.ui_anims import (ButtonOperatorImportW2Anims, WITCH_OT_import_w3_fbx, WITCH_OT_ImportW2Rig, WITCH_OT_ExportW2AnimJson, WITCH_OT_ExportW2RigJson)
from io_import_w2l.ui.ui_mesh import WITCH_OT_w2mesh, WITCH_OT_w2mesh_export
from io_import_w2l.ui.ui_mesh import WITCH_OT_apx
from io_import_w2l.ui.ui_entity import WITCH_OT_w2ent, WITCH_OT_ENTITY_w2ent_chara
from io_import_w2l.ui.ui_material import WITCH_OT_w2mg, WITCH_OT_w2mi, WITCH_OT_xbm
from io_import_w2l.ui.ui_map import (WITCH_OT_w2L,
                                     WITCH_OT_w2w,
                                    #  WITCH_OT_load_layer,
                                    #  WITCH_OT_load_layer_group,
                                    #  WITCH_OT_radish_w2L
                                     )


class WITCH_MT_Menu(bpy.types.Menu):
    bl_label = "Witcher 3 Assets"
    bl_idname = "IMPORT_MT_witcherio"

    def draw(self, context):
        layout = self.layout
        layout.operator(WITCH_OT_w2mesh.bl_idname, text="Mesh (.w2mesh)", icon='MESH_DATA')
        layout.operator(WITCH_OT_import_w3_fbx.bl_idname, text="Witcher 3 FBX (.fbx)", icon='MESH_DATA')
        layout.separator()
        layout.operator(WITCH_OT_w2ent.bl_idname, text="Item Entity (.w2ent)", icon='MESH_DATA')
        layout.operator(WITCH_OT_ENTITY_w2ent_chara.bl_idname, text="Character Entity (.w2ent)", icon='MESH_DATA')
        layout.separator()
        layout.operator(WITCH_OT_w2mi.bl_idname, text="Instance (.w2mi)", icon='MESH_DATA')
        layout.operator(WITCH_OT_w2mg.bl_idname, text="Shader (.w2mg)", icon='MESH_DATA')
        layout.operator(WITCH_OT_xbm.bl_idname, text="Texture (.xbm)", icon='SPHERE')
        layout.separator()
        layout.operator(WITCH_OT_ImportW2Rig.bl_idname, text="Rig (.w2rig)", icon='ARMATURE_DATA')
        layout.operator(ButtonOperatorImportW2Anims.bl_idname, text="Animation (.w2anims)", icon='ARMATURE_DATA')
        layout.separator()
        layout.operator(WITCH_OT_w2L.bl_idname, text="Layer (.w2l)", icon='SPHERE')
        layout.operator(WITCH_OT_w2w.bl_idname, text="World (.w2w)", icon='WORLD_DATA')




def menu_import(self, context):
    witcher_icon = custom_icons["main"]["witcher_icon"]
    self.layout.menu(WITCH_MT_Menu.bl_idname, icon_value=witcher_icon.icon_id)

class WITCH_MT_MenuExport(bpy.types.Menu):
    bl_label = "Witcher 3 Assets"
    bl_idname = "EXPORT_MT_witcherio"

    def draw(self, context):
        layout = self.layout
        layout.operator(WITCH_OT_w2mesh_export.bl_idname, text="Mesh (.w2mesh)", icon='MESH_DATA')
        layout.separator()
        layout.operator(WITCH_OT_ExportW2RigJson.bl_idname, text="Rig (.w2rig)", icon='ARMATURE_DATA')
        layout.operator(WITCH_OT_import_w3_fbx.bl_idname, text="Animation (.w2anims)", icon='ARMATURE_DATA')

def menu_export(self, context):
    witcher_icon = custom_icons["main"]["witcher_icon"]
    self.layout.menu(WITCH_MT_MenuExport.bl_idname, icon_value=witcher_icon.icon_id)

def load_icon(loader, filename, name):
    script_path = Path(__file__).parent
    icon_path = script_path / 'icons' / filename
    loader.load(name, str(icon_path), 'IMAGE')

def register_custom_icon():
    import bpy.utils.previews
    pcoll = bpy.utils.previews.new()
    load_icon(pcoll, 'w_icon.png', "witcher_icon")
    custom_icons["main"] = pcoll

def unregister_custom_icon():
    import bpy.utils.previews
    for pcoll in custom_icons.values():
        bpy.utils.previews.remove(pcoll)
    custom_icons.clear()

classes = (
    WITCH_OT_import_w3_fbx,
    WITCH_OT_w2mesh,
    WITCH_OT_w2mesh_export,
    WITCH_OT_apx,
    WITCH_MT_Menu,
    WITCH_MT_MenuExport,
)

register_, unregister_ = bpy.utils.register_classes_factory(classes)

def register():
    register_custom_icon()
    register_()
    bpy.types.TOPBAR_MT_file_import.append(menu_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_export)

def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    unregister_custom_icon()
    unregister_()