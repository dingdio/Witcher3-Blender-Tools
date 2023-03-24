
import os
import time
import bpy

from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

from io_import_w2l.importers import import_entity
from io_import_w2l.importers import import_anims
from io_import_w2l.ui.ui_utils import WITCH_PT_Base
from bpy.types import Panel, Operator, UIList, PropertyGroup
from bpy.props import IntProperty, StringProperty, CollectionProperty, BoolProperty
from io_import_w2l.importers.import_entity import test_load_entity

from io_import_w2l import get_uncook_path

from bpy_extras.io_utils import (
        ImportHelper
        )

class WITCH_UL_ENTITY_List(UIList):
    """Demo UIList."""
    bl_idname = "WITCH_UL_ENTITY_List"
    layout_type = "DEFAULT" # could be "COMPACT" or "GRID"

    def draw_item(self, context,
                    layout, # Layout to draw the item
                    data, # Data from which to take Collection property
                    item, # Item of the collection property
                    icon, # Icon of the item in the collection
                    active_data, # Data from which to take property for the active element
                    active_propname, # Identifier of property in active_data, for the active element
                    index, # Index of the item in the collection - default 0
                    flt_flag # The filter-flag result for this item - default 0
            ):
        #Make sure your code supports all 3 layout types
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.name)

        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")

class WITCH_OT_ENTITY_w2ent_chara(bpy.types.Operator, ImportHelper):
    """Load a Witcher 3 character (.w2ent) file"""
    bl_idname = "object.import_w2ent_chara_btn"
    bl_label = "Import Character"
    filename_ext = ".w2ent"
    bl_options = {'REGISTER', 'UNDO'}
    
    filter_glob: StringProperty(default='*.w2ent;*.w2ent.json', options={'HIDDEN'})
    import_apperance: IntProperty(
        name="Select Apperance",
        default=0,
        description="Select index of apperance. 0 will only import character base"
    )
    def draw(self, context):
        filepath = self.filepath
        layout = self.layout
        
        # check if the file is a file and has the .w2ent extension
        if os.path.isfile(self.filepath) and self.filepath.endswith('.w2ent'):
            pass

        else:
            layout.label(text="Selected file is not a .w2ent file.")
            
        row = layout.row()
        row.prop(self, "import_apperance")
        if False:
            pass
            sections = ["Settings"]
            section_options = {
                "Settings" : [
                            # "do_import_mats",
                            # "do_import_armature",
                            # "keep_lod_meshes",
                            # "do_merge_normals",
                            # "rotate_180"
                            ]
            }
            for section in sections:
                row = layout.row()
                box = row.box()
                box.label(text=section)
                for prop in section_options[section]:
                    box.prop(self, prop)

    def execute(self, context):
        print("importing character now!")
        fdir = self.filepath
        s = time.time()
        if fdir.endswith(".w2ent") or fdir.endswith(".json"):
            import_entity.import_ent_template(fdir, False, self.import_apperance)
        else:
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        message = f'Read character file in {time.time() - s} seconds.'
        log.info(message)
        self.report({'INFO'}, message)
        return {'FINISHED'}

class WITCH_OT_ENTITY_list_loadapp(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "tool.list_loadapp"
    bl_label = "Load"
    bl_description = "Load the selected apperance for this character"

    action: StringProperty(default="default")
    @classmethod
    def poll(cls, context):
        return context.scene

    def execute(self, context):
        ob = context.object
        if ob and ob.type == "ARMATURE" and "CMovingPhysicalAgentComponent" in ob.name:
            main_arm_obj:bpy.types.Object = ob
            
            #main_arm_obj = bpy.context.active_object
            rig_settings = main_arm_obj.data.witcherui_RigSettings
            scene = context.scene
            action = self.action

            if "w2anims" == action:
                print("=== load w2anims ====")
                print(rig_settings.main_entity_skeleton)
                
                if rig_settings.animset_list_index >= 0 and rig_settings.animset_list:
                    repoPath = rig_settings.animset_list[rig_settings.animset_list_index]
                    fdir = os.path.join(get_uncook_path(context),repoPath.path)
                    print(fdir)
                    loadFromJson = True
                    if loadFromJson:
                        if (os.path.exists(fdir+'.json')):
                            fdir = fdir + '.json'
                    if "_mimic_" in fdir:
                        import_anims.start_import(context, fdir, rigPath=rig_settings.main_face_skeleton)
                    else:
                        import_anims.start_import(context, fdir, rigPath=rig_settings.main_entity_skeleton)

            if "load" == action:
                print("=== load apperance ====")
                if rig_settings.app_list_index >= 0 and rig_settings.app_list:
                    item = rig_settings.app_list[rig_settings.app_list_index]

                    import_entity.import_from_list_item(context, item, rig_settings.do_import_redcloth)
                    bpy.ops.object.select_all(action='DESELECT')
                    main_arm_obj.select_set(True)
                    bpy.context.view_layer.objects.active = main_arm_obj
                # context.rig_settings.app_list.add()
            elif "clear" == action:
                print("=== Debug Clear ====")
                bpy.context.rig_settings.app_list.clear()

        return {'FINISHED'}


class WITCH_OT_ENTITY_lod_toggle(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "witcher.lod_toggle"
    bl_label = "Toggle"
    bl_description = "Change lod level for all meshes."

    action: StringProperty(default="default")
    @classmethod
    def poll(cls, context):
        return context.scene

    def execute(self, context):
        scene = context.scene
        action = self.action
        ob = context.object
        lods = ['_lod0','_lod1','_lod2']
        if '_collision' in action:
            meshes = set(o for o in scene.objects if o.type == 'MESH')
            hidden_bool = True
            if 'Show' in action:
                hidden_bool = False

            for o in meshes:
                if "_proxy" in o.name:
                    print("hiding _proxy"+o.name)
                    o.hide_set(hidden_bool)
                if "_shadowmesh" in o.name:
                    print("hiding _shadowmesh"+o.name)
                    o.hide_set(hidden_bool)
                if "_volume" in o.name:
                    print("hiding _volume"+o.name)
                    o.hide_set(hidden_bool)
                if "blockout_box" in o.name:
                    print("hiding blockout_box"+o.name)
                    o.hide_set(hidden_bool)
                if o.name.startswith("capsule_"):
                    print("hiding capsule_"+o.name)
                    o.hide_set(hidden_bool)
                if o.name.startswith("box_"):
                    print("hiding box_"+o.name)
                    o.hide_set(hidden_bool)
            
        elif action in lods:
            lod_idx = int(action[-1:])+1
            lod_meshes = []
            for mesh in scene.objects:
                # only for meshes
                if mesh.type == 'MESH':
                    if 'lod_level' in mesh.witcherui_MeshSettings:
                        if mesh.witcherui_MeshSettings['lod_level'] == lod_idx:
                            mesh.hide_set(False)
                        else:
                            mesh.hide_set(True)
            #         if mesh.name[-5:-1] == "_lod":
            #             mesh.hide_viewport = True
            #             mesh.hide_render = True
            #             if mesh.name[:-5] not in lod_meshes:
            #                 lod_meshes.append(mesh.name[:-5])
            # for lod_mesh in lod_meshes:
            #     mesh_bl = scene.objects.get(lod_mesh+action)
            #     if mesh_bl:
            #         mesh_bl.hide_viewport = False
            #         mesh_bl.hide_render = False
            #     else:
            #         mesh_bl = scene.objects.get(lod_mesh+"_lod"+str(lod_idx-1))
            #         if mesh_bl:
            #             mesh_bl.hide_viewport = False
            #             mesh_bl.hide_render = False
            #         else:
            #             mesh_bl = scene.objects.get(lod_mesh+"_lod"+str(lod_idx-1))
            #             if mesh_bl:
            #                 mesh_bl.hide_viewport = False
            #                 mesh_bl.hide_render = False
            #             else:
            #                 log.debug("LOD ERROR")

            # if mesh.name[-5:] == action:
            #     mesh.hide_viewport = False
            #     mesh.hide_render = False
        return {'FINISHED'}

##############################
#       Animset List         #
##############################

class WITCH_PT_ENTITY_ANIMSET_UL_List(UIList):
    """List for the Animsets"""
    bl_idname = "WITCH_PT_ENTITY_ANIMSET_UL_List"
    layout_type = "DEFAULT" # could be "COMPACT" or "GRID"

    def draw_item(self, context,
                    layout, # Layout to draw the item
                    data, # Data from which to take Collection property
                    item, # Item of the collection property
                    icon, # Icon of the item in the collection
                    active_data, # Data from which to take property for the active element
                    active_propname, # Identifier of property in active_data, for the active element
                    index, # Index of the item in the collection - default 0
                    flt_flag # The filter-flag result for this item - default 0
            ):
        #Make sure your code supports all 3 layout types
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.path)

        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")

class WITCH_PT_ENTITY_Panel(WITCH_PT_Base, Panel):
    bl_idname = "WITCH_PT_ENTITY_Panel"
    bl_label = "Character Appearances"
    bl_description = ""
    #bl_options = {'HEADER_LAYOUT_EXPAND'}
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        row = layout.row().box()
        row = row.column(align=True)
        row.label(text='Character')
        op = row.operator(WITCH_OT_ENTITY_w2ent_chara.bl_idname, text="Import Character", icon='SPHERE')
        op.filepath = os.path.join(get_uncook_path(context),"characters\\")

        
        ob = context.object
        if ob != None:
            if ob.type == "ARMATURE" and "CMovingPhysicalAgentComponent" in ob.name or "CAnimatedComponent" in ob.name:
                main_arm_obj = ob
                
                #main_arm_obj = bpy.context.active_object
                rig_settings = main_arm_obj.data.witcherui_RigSettings
                object = rig_settings #context.scene
                if object == None:
                    return
                
                col = row.column(align=True)
                col.template_list("WITCH_UL_ENTITY_List", "The_List", object,
                                    "app_list", object, "app_list_index")

                grid = row.grid_flow( columns = 2 )
                grid.operator(WITCH_OT_ENTITY_list_loadapp.bl_idname, text="Load").action = "load"
                #grid.operator(WITCH_OT_ENTITY_list_loadapp.bl_idname, text="Clear List").action = "clear"
                sections = ["Import Settings"]
                section_options = {
                    "Import Settings" :["do_import_redcloth",
                                        "do_import_lods"]
                }
                for section in sections:
                    row = layout.row()
                    box = row.box()
                    box.label(text=section)
                    for prop in section_options[section]:
                        box.prop(rig_settings, prop)

                self.layout.label(text = "Character Info:")
                if object.app_list_index >= 0 and object.app_list:
                    item = object.app_list[object.app_list_index]

                    row = self.layout.row()
                    row.prop(item, "name")
                    
                row = self.layout.row()
                row.prop(rig_settings, "main_entity_skeleton")
                row = self.layout.row()
                row.prop(rig_settings, "main_face_skeleton")
                row = self.layout.row()
                row.prop(rig_settings, "repo_path")
                
                row = layout.row().box()
                row = row.column(align=True)
                row.label(text='Animation')
                col = row.column(align=True)
                col.template_list("WITCH_PT_ENTITY_ANIMSET_UL_List", "The_List_2", object,
                                    "animset_list", object, "animset_list_index")
                row.operator(WITCH_OT_ENTITY_list_loadapp.bl_idname, text="Load .w2anims").action = "w2anims"


                if rig_settings.witcher_tracks_list:
                    box = layout.box()
                    row = box.row(align=False)
                    row.prop(rig_settings, "witcher_tracks_collapse", icon="TRIA_DOWN" if not rig_settings.witcher_tracks_collapse else "TRIA_RIGHT", icon_only=True, emboss=False)
                    track_items = [x for x in rig_settings.witcher_tracks_list if x.type == 0]
                    row.label(text="Tracks (" + str(len(track_items)) + ")")
                    
                    if not rig_settings.witcher_tracks_collapse:
                        
                        for track in track_items:
                            if 'hctFOV' in track.name:
                                the_data = main_arm_obj.pose.bones["Camera_Node"]
                                #box.prop(bpy.data.cameras["Camera"], 'lens')#text = track.name)
                                if hasattr(the_data,'[\"' + track.path + '\"]'):
                                    box.prop(the_data, '[\"' + track.path + '\"]', text = track.name)
                                else:
                                    pass
                    
classes = [
    #properties
    #ListItemApp,
    #ListItemAnimset,
    
    #operators
    WITCH_OT_ENTITY_w2ent_chara,
    WITCH_OT_ENTITY_list_loadapp,
    WITCH_OT_ENTITY_lod_toggle,
    
    #lists
    WITCH_UL_ENTITY_List,
    
    #panels
    WITCH_PT_ENTITY_Panel,
    WITCH_PT_ENTITY_ANIMSET_UL_List,
]


def register():
    for c in classes:
        bpy.utils.register_class(c)


    


def unregister():
    # del bpy.types.rig_settings.app_list
    # del bpy.types.rig_settings.app_list_index

    # del bpy.types.rig_settings.main_entity_skeleton
    # del bpy.types.rig_settings.main_face_skeleton

    # del bpy.types.rig_settings.animset_list
    # del bpy.types.rig_settings.animset_list_index
    for c in classes:
        bpy.utils.unregister_class(c)

if __name__ == '__main__':
    register()