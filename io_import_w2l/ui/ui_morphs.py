
from io_import_w2l.CR2W.CR2W_types import dotdict
import bpy


class WITCH_PT_Base:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Witcher'
    bl_context = ''#'objectmode'

class witcherui_redmorph(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name = "Name")
    path: bpy.props.StringProperty(name = "Path")
    type: bpy.props.IntProperty(name = "Type")

bpy.utils.register_class(witcherui_redmorph)

class witcherui_RigSettings(bpy.types.PropertyGroup):

    model_name: bpy.props.StringProperty(default = "",
                        name = "Model name",
                        description = "Model name")
    def poll_mesh(self, object):
        return object.type == 'MESH'
    model_body: bpy.props.PointerProperty(name = "Model Body",
                        description = "",
                        type = bpy.types.Object,
                        poll = poll_mesh)
    def poll_armature(self, object):
        if object.type == 'ARMATURE':
            return object.data == self.id_data
        else:
            return False
    model_armature_object: bpy.props.PointerProperty(name = "Model Armature Object",
                        description = "",
                        type = bpy.types.Object,
                        poll = poll_armature)

    witcher_morphs_list: bpy.props.CollectionProperty(name = "Witcher Morphs List",
                        type=witcherui_redmorph)

    witcher_morphs_number: bpy.props.IntProperty(default = 0,
                        name = "")
    witcher_body_morphs: bpy.props.BoolProperty(default = True,
                        name = "Body Morphs Morphs",
                        description = "Search for witcher Body morphs")
    witcher_morphs_collapse: bpy.props.BoolProperty(default = True)

bpy.utils.register_class(witcherui_RigSettings)
bpy.types.Armature.witcherui_RigSettings = bpy.props.PointerProperty(type = witcherui_RigSettings)


class PANEL_PT_WitcherMorphs(WITCH_PT_Base, bpy.types.Panel):
    bl_idname = "PANEL_PT_WitcherMorphs"
    bl_label = "Morphs"
    #bl_options = {"DEFAULT_CLOSED"}

    # def draw_header(self,context):
    #     pass
    def draw(self, context):


        return
        ob = context.object
        coll = context.collection
        scn = context.scene
        layout = self.layout
        box = layout.box()
        if ob:
            box.label(text = "Active Object: %s" % ob.entity_type)
            box.prop(ob, "name")
            if ob.template:
                box.prop(ob, "template")
            if ob.entity_type:
                box.prop(ob, "entity_type")
        else:
            box.label(text = "No active object")

        main_arm_obj = bpy.context.scene.objects["shani:CMimicComponent12_ARM"]
        
        main_arm_obj = bpy.context.active_object
        rig_settings = main_arm_obj.data.witcherui_RigSettings

        #cake = settings.witcher_morphs_list.add()


        layout = self.layout

        box = layout.box()
        # rig_settings = dotdict({'model_armature_object':main_arm_obj})
        # body_morphs = [dotdict({'name':"jaw_open_o",'path':"jaw_open_o"})]
        # settings = dotdict({'morphs_error':"morphs_error"})
        row = box.row(align=False)
        row.label(text = "Face Morphs")

        if rig_settings.witcher_body_morphs:
            box = layout.box()
            row = box.row(align=False)
            row.prop(rig_settings, "witcher_morphs_collapse", icon="TRIA_DOWN" if not rig_settings.witcher_morphs_collapse else "TRIA_RIGHT", icon_only=True, emboss=False)
            body_morphs = [x for x in rig_settings.witcher_morphs_list if x.type == 4] #and self.morph_filter(x, rig_settings)]
            row.label(text="Body (" + str(len(body_morphs)) + ")")

            if not rig_settings.witcher_morphs_collapse:

                for morph in body_morphs:
                    if hasattr(rig_settings.model_armature_object,'[\"' + morph.path + '\"]'):
                        box.prop(rig_settings.model_armature_object, '[\"' + morph.path + '\"]', text = morph.name)
                    else:
                        pass
                        # row = box.row(align=False)
                        # row.label(text = morph.name)
                        # row.prop(settings, 'morphs_error', text = "", icon = "ERROR", emboss=False, icon_only = True)


from bpy.utils import (register_class, unregister_class)

_classes = [
    PANEL_PT_WitcherMorphs,
]


def register():
    for cls in _classes:
        register_class(cls)

def unregister():
    for cls in _classes:
        unregister_class(cls)

