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

from io_import_w2l.ui.ui_anims import (WITCH_OT_import_w3_fbx)
from io_import_w2l.ui.ui_mesh import WITCH_OT_w2mesh, WITCH_OT_w2mesh_export
from io_import_w2l.ui.ui_mesh import WITCH_OT_apx
from io_import_w2l import get_uncook_path

class WITCH_MT_Menu(bpy.types.Menu):
    bl_label = "Witcher 3 Assets"
    bl_idname = "IMPORT_MT_witcherio"

    def draw(self, context):
        layout = self.layout
        op = layout.operator(WITCH_OT_w2mesh.bl_idname, text="Mesh (.w2mesh)", icon='MESH_DATA')
        op.filepath = get_uncook_path(context)
        layout.operator(WITCH_OT_import_w3_fbx.bl_idname, text="Witcher 3 FBX (.fbx)", icon='MESH_DATA')
        layout.separator()
        layout.operator(WITCH_OT_import_w3_fbx.bl_idname, text="Entity (.w2ent)", icon='MESH_DATA')
        layout.separator()
        layout.operator(WITCH_OT_import_w3_fbx.bl_idname, text="Mesh (.w2l)", icon='MESH_DATA')
        layout.operator(WITCH_OT_import_w3_fbx.bl_idname, text="Mesh (.w2w)", icon='MESH_DATA')

def menu_import(self, context):
    witcher_icon = custom_icons["main"]["witcher_icon"]
    self.layout.menu(WITCH_MT_Menu.bl_idname, icon_value=witcher_icon.icon_id)

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
)

register_, unregister_ = bpy.utils.register_classes_factory(classes)

def register():
    register_custom_icon()
    register_()
    bpy.types.TOPBAR_MT_file_import.append(menu_import)

def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)
    unregister_custom_icon()
    unregister_()