import bpy


class WITCH_PT_materials(bpy.types.Panel):
    bl_label = "Witcher"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Witcher"

    
    
    def draw(self, context):
        layout = self.layout
        mat = context.material
        if mat and mat.witcher_props:
            #layout.operator("material.clear_input_props", text="Reset Parameters")
            layout.prop(mat.witcher_props, "bind_name")
            # if not mat.witcher_props.bind_name:
            #     layout.prop(mat.witcher_props, "name", text="Name")
            row = layout.row()
            row.enabled = not mat.witcher_props.bind_name
            row.prop(mat.witcher_props, "name", text="Name")
            layout.prop(mat.witcher_props, "material_version")
            layout.prop(mat.witcher_props, "local")
            layout.prop(mat.witcher_props, "enableMask")
            #layout.prop(mat.witcher_props, "base")
            #layout.prop(mat.witcher_props, "base")
            #if mat.witcher_props.base == 'custom':
            layout.prop(mat.witcher_props, "base_custom")
            
            #group_inputs = get_group_inputs(mat)
            if mat.witcher_props.local:
                group_inputs = get_group_inputs(mat)
                if group_inputs:
                    for input_socket in group_inputs:
                        if input_socket.is_linked:
                            linked_socket = input_socket.links[0].from_socket
                            
                            row = layout.row()
                            row.prop(linked_socket.node, "witcher_include", text=input_socket.name+":")

                            if linked_socket.node.type == 'TEX_IMAGE':
                                row.prop(linked_socket.node, "image", text="")
                                if linked_socket.node.image != None:
                                    final_path = get_repo_from_abs_path(linked_socket.node.image.filepath)
                                    row.label(text = final_path)
                            elif linked_socket.node.type == 'RGB':
                                row.prop(linked_socket, "default_value", text="")
                            elif linked_socket.node.type == 'VALUE':
                                row.prop(linked_socket, "default_value", text="")
                            elif input_socket.type == 'VECTOR':
                                row.prop(linked_socket.node.inputs[0], "default_value", text="")
                                row.prop(linked_socket.node.inputs[1], "default_value", text="")
                                row.prop(linked_socket.node.inputs[2], "default_value", text="")
                                #row.prop(linked_socket.node.outputs[0], "default_value", text="") #not working??
                            else:
                                row.prop(linked_socket, "default_value", text="")
                if mat.witcher_props.xml_text:
                    layout.prop(mat.witcher_props, "xml_text", text="Local Instance XML", 
                                expand=True)
                # row = layout.row()
                # row.prop(context.scene, "path_a", text="Source Path")
                # row = layout.row()
                # row.prop(context.scene, "path_b", text="Destination Path")
                # row = layout.row()
                # row.operator("object.move_textures", text="Move Textures")


# class MoveTexturesPanel(bpy.types.Panel):
#     bl_idname = "OBJECT_PT_move_textures_panel"
#     bl_label = "Move Textures"
#     bl_space_type = "VIEW_3D"
#     bl_region_type = "UI"

#     def draw(self, context):
#         mat = context.material
#         layout = self.layout
#         row = layout.row()
#         row.prop(context.scene, "path_a", text="Source Path")
#         row = layout.row()
#         row.prop(context.scene, "path_b", text="Destination Path")
#         row = layout.row()
#         row.operator("object.move_textures", text="Move Textures")

# class MoveTexturesOperator(bpy.types.Operator):
#     bl_idname = "object.move_textures"
#     bl_label = "Move Textures"
#     def execute(self, context):
#         path_a = context.scene.path_a
#         path_b = context.scene.path_b
#         return {'FINISHED'}


class NodeGroupInputProperties(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Name")
    value: bpy.props.StringProperty(name="Value")
    value_float: bpy.props.FloatProperty(name="Value")
    value_vector:bpy.props.FloatVectorProperty(name="Value")
    #type: bpy.props.EnumProperty(name="Type", items=[("FLOAT", "Float", ""), ("VECTOR", "Vector", ""), ("COLOR", "Color", "")])
    type: bpy.props.StringProperty(name="Type")
    is_enabled: bpy.props.BoolProperty(name="Is Enabled", default=False)
    is_enabled_temp: bpy.props.BoolProperty(name="Export", default=False)
    is_linked: bpy.props.BoolProperty(name="is_linked", default=False)

class WitcherMaterialProperties(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="name", default="Material")
    enableMask: bpy.props.BoolProperty(name="enableMask", default=False, description="Enable Mask of hair etc")
    local: bpy.props.BoolProperty(name="local", default=True, description="Local materials will be embedded in the .w2mesh. Non-local will use the defined base material without any instances.")
    #base: bpy.props.StringProperty(name="base", default="engine\materials\graphs\pbr_std.w2mg")
    bind_name: bpy.props.BoolProperty(name="Use Blender Material Name", default=True)
    node_group_name: bpy.props.StringProperty(name="Node Group", default="")
    input_props: bpy.props.CollectionProperty(type=NodeGroupInputProperties)
    input_props_index: bpy.props.IntProperty()
    xml_text : bpy.props.StringProperty(name="XML Text")



    # base_options = [
    #     ("custom", "Custom", "Description for value 1"),
    #     (r"engine\materials\graphs\pbr_std.w2mg", r"engine\materials\graphs\pbr_std.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_std_colorshift.w2mg", r"engine\materials\graphs\pbr_std_colorshift.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_std_tint_mask_2det.w2mg", r"engine\materials\graphs\pbr_std_tint_mask_2det.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_std_tint_mask_2det_fresnel.w2mg", r"engine\materials\graphs\pbr_std_tint_mask_2det_fresnel.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_std_tint_mask_det.w2mg", r"engine\materials\graphs\pbr_std_tint_mask_det.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_std_tint_mask_det_fresnel.w2mg", r"engine\materials\graphs\pbr_std_tint_mask_det_fresnel.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_std_tint_mask_det_pattern.w2mg", r"engine\materials\graphs\pbr_std_tint_mask_det_pattern.w2mg" , ""),
    #     (r"engine\materials\diffusecubemap.w2mg", r"engine\materials\diffusecubemap.w2mg" , ""),
    #     (r"engine\materials\diffusemap.w2mg", r"engine\materials\diffusemap.w2mg" , ""),
    #     (r"engine\materials\gridmat.w2mg", r"engine\materials\gridmat.w2mg" , ""),
    #     (r"engine\materials\lens_flare.w2mg", r"engine\materials\lens_flare.w2mg" , ""),
    #     (r"engine\materials\normalmap.w2mg", r"engine\materials\normalmap.w2mg" , ""),
    #     (r"engine\materials\defaults\apex.w2mg", r"engine\materials\defaults\apex.w2mg" , ""),
    #     (r"engine\materials\defaults\flare.w2mg", r"engine\materials\defaults\flare.w2mg" , ""),
    #     (r"engine\materials\defaults\mergedmesh.w2mg", r"engine\materials\defaults\mergedmesh.w2mg" , ""),
    #     (r"engine\materials\defaults\mesh.w2mg", r"engine\materials\defaults\mesh.w2mg" , ""),
    #     (r"engine\materials\defaults\volume.w2mg", r"engine\materials\defaults\volume.w2mg" , ""),
    #     (r"engine\materials\editor\terrain_selector.w2mg", r"engine\materials\editor\terrain_selector.w2mg" , ""),
    #     (r"engine\materials\graphs\character_dismemberment_fx.w2mg", r"engine\materials\graphs\character_dismemberment_fx.w2mg" , ""),
    #     (r"engine\materials\graphs\debug.w2mg", r"engine\materials\graphs\debug.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_det.w2mg", r"engine\materials\graphs\pbr_det.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_eye.w2mg", r"engine\materials\graphs\pbr_eye.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_hair.w2mg", r"engine\materials\graphs\pbr_hair.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_hair_moving.w2mg", r"engine\materials\graphs\pbr_hair_moving.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_hair_simple.w2mg", r"engine\materials\graphs\pbr_hair_simple.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_simple.w2mg", r"engine\materials\graphs\pbr_simple.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_simple_no_emmisive.w2mg", r"engine\materials\graphs\pbr_simple_no_emmisive.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_skin.w2mg", r"engine\materials\graphs\pbr_skin.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_skin_decal.w2mg", r"engine\materials\graphs\pbr_skin_decal.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_skin_simple.w2mg", r"engine\materials\graphs\pbr_skin_simple.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_skin_simple_under.w2mg", r"engine\materials\graphs\pbr_skin_simple_under.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_spec.w2mg", r"engine\materials\graphs\pbr_spec.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_spec_tint_mask_det.w2mg", r"engine\materials\graphs\pbr_spec_tint_mask_det.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_swarm.w2mg", r"engine\materials\graphs\pbr_swarm.w2mg" , ""),
    #     (r"engine\materials\graphs\pbr_vert_blend.w2mg", r"engine\materials\graphs\pbr_vert_blend.w2mg" , ""),
    #     (r"engine\materials\graphs\transparent_lit.w2mg", r"engine\materials\graphs\transparent_lit.w2mg" , ""),
    #     (r"engine\materials\graphs\transparent_lit_vert.w2mg", r"engine\materials\graphs\transparent_lit_vert.w2mg" , ""),
    #     (r"engine\materials\graphs\transparent_reflective.w2mg", r"engine\materials\graphs\transparent_reflective.w2mg" , ""),
    #     (r"engine\materials\graphs\eyeshadow\pbr_eye_shadow.w2mg", r"engine\materials\graphs\eyeshadow\pbr_eye_shadow.w2mg" , ""),
    #     (r"engine\materials\graphs\morphblend\pbr_skin_morph.w2mg", r"engine\materials\graphs\morphblend\pbr_skin_morph.w2mg" , ""),
    #     (r"engine\materials\graphs\morphblend\pbr_std_morph.w2mg", r"engine\materials\graphs\morphblend\pbr_std_morph.w2mg" , ""),
    #     (r"engine\materials\graphs\normalblend\pbr_skin_normalblend.w2mg", r"engine\materials\graphs\normalblend\pbr_skin_normalblend.w2mg" , ""),
    #     (r"engine\materials\render\billboard.w2mg", r"engine\materials\render\billboard.w2mg" , ""),
    #     (r"engine\materials\render\fallback.w2mg", r"engine\materials\render\fallback.w2mg" , "")
    # ]
    # base: bpy.props.EnumProperty(
    #     name="Base",
    #     description="Select a value from the dropdown or enter a custom value",
    #     items=base_options,
    #     default=r"engine\materials\graphs\pbr_std.w2mg",
    # )
    base_custom: bpy.props.StringProperty(
        name="Base Path",
        description="Enter a .w2mi or .w2mg path",
        default=r"engine\materials\graphs\pbr_std.w2mg",
    )
    
    
    material_version_options = [
        #("custom", "Custom", "Description for value 1"),
        ("witcher3", "Witcher 3", "This is a Witcher 3 material"),
        ("witcher2", "Witcher 2", "This is a Witcher 2 material"),
    ]
    material_version: bpy.props.EnumProperty(
        name="Game",
        description="What game this material was orignally for",
        items=material_version_options,
        default="witcher3",
    )
    
def get_group_inputs(mat):
    if mat and mat.witcher_props and mat.node_tree and mat.node_tree.nodes:
        node_tree = mat.node_tree
        for node in node_tree.nodes:
            if node.type == 'GROUP':
                group_outputs = node.outputs
                for output_socket in group_outputs:
                    if output_socket.is_linked:
                        target_node = output_socket.links[0].to_node
                        if target_node.type == 'OUTPUT_MATERIAL':
                            group_inputs = node.inputs
                            return group_inputs
                            #for input_socket in group_inputs:
    return None

from io_import_w2l import get_texture_path
from pathlib import Path
import os


possible_folders = [
    'files\\Raw\\Mod',
    'files\\Raw\\DLC',
    'files\\Mod\\Cooked',
    'files\\Mod\\Uncooked',
    'files\\DLC\\Cooked',
    'files\\DLC\\Uncooked',
]

from io_import_w2l import get_mod_directory, get_texture_path, get_modded_texture_path
def get_repo_from_abs_path(texture_path_input):
    texture_path = os.path.realpath(bpy.path.abspath(texture_path_input))
    TEXTURE_PATH = get_texture_path(bpy.context)
    MOD_DIR = get_mod_directory(bpy.context)
    MOD_TEX_PATH = get_modded_texture_path(bpy.context)
    
    #path_obj = Path(texture_path)
    TEXTURE_PATH_obj = Path(TEXTURE_PATH)
    MOD_DIR_obj = Path(MOD_DIR)
    MOD_TEX_PATH_obj = Path(MOD_TEX_PATH)
    
    if TEXTURE_PATH_obj.exists() and TEXTURE_PATH in texture_path:
        texture_path = texture_path.replace(TEXTURE_PATH+'\\', '')
    elif MOD_DIR_obj.exists() and MOD_DIR in texture_path:
        texture_path = texture_path.replace(MOD_DIR+'\\', '')
        for folder in possible_folders:
            if folder in texture_path:
                texture_path = texture_path.replace(folder+'\\', '')
                break
    elif MOD_TEX_PATH_obj.exists() and MOD_TEX_PATH in texture_path:
        texture_path = texture_path.replace(MOD_TEX_PATH+'\\', '')

    return texture_path

def get_socket_value(input_socket):
    if input_socket.is_linked:
        linked_socket = input_socket.links[0].from_socket
        if linked_socket.node.type == 'TEX_IMAGE' and linked_socket.node.image:
            rel_path = linked_socket.node.image.filepath
            abs_path = bpy.path.abspath(rel_path)
            texture_path = os.path.normpath(abs_path)
            return get_repo_from_abs_path(texture_path)
        elif linked_socket.node.type == 'RGB':
            color_value = linked_socket.node.outputs[0].default_value
            return " ; ".join(str(x) for x in color_value)
        elif linked_socket.node.type == 'VALUE':
            value = linked_socket.node.outputs[0].default_value
            return value
        elif linked_socket.type == 'VECTOR':
            value = [
                linked_socket.node.inputs[0].default_value,
                linked_socket.node.inputs[1].default_value,
                linked_socket.node.inputs[2].default_value,
            ]
            default_value = " ; ".join(str(x) for x in value)
            return value
    try:
        default_value = " ; ".join(str(x) for x in input_socket.default_value)
    except Exception as e:
        default_value = str(input_socket.default_value)
    return default_value

def update_node_group_inputs(depsgraph):
    for ob in depsgraph.objects:
        mat = ob.active_material
        group_inputs = get_group_inputs(mat)
        if group_inputs:
            for input_socket in group_inputs:
                # if 'BigWaves' in input_socket.name:
                #     pass
                input_prop = next((ip for ip in mat.witcher_props.input_props if ip.name == input_socket.name), None)
                if input_prop is None:
                    input_prop = mat.witcher_props.input_props.add()
                    input_prop.name = input_socket.name
                    input_prop.type = str(input_socket.type) #set the type of the socket
                    input_prop.is_enabled_temp = input_prop.is_enabled
                if input_socket.type == 'RGBA':
                    input_prop.value = get_socket_value(input_socket)
                elif input_socket.type == 'VALUE':
                    input_prop.value = str(get_socket_value(input_socket))
                elif input_socket.type == 'VECTOR':
                    input_prop.value = str(get_socket_value(input_socket))
                else:
                    input_prop.value = str(input_socket.default_value)
                input_prop.is_linked = input_socket.is_linked
                # for pro in mat.witcher_props.input_props:
                #     pass
            # for idx, prop in enumerate(mat.witcher_props.input_props):
            #     for input in group_inputs:
            #         found = True if prop.name == input.name else False
            #     mat.witcher_props.input_props.remove(idx) if not found else None
        elif mat and mat.witcher_props and mat.witcher_props.input_props:
            pass #mat.witcher_props.input_props.clear()


class ClearInputPropsOperator(bpy.types.Operator):
    """Clear Input Props Operator"""
    bl_idname = "material.clear_input_props"
    bl_label = "Clear Input Props"

    def execute(self, context):
        mat = context.material
        mat.witcher_props.input_props.clear()
        depsgraph = context.evaluated_depsgraph_get()
        update_node_group_inputs(depsgraph)
        return {'FINISHED'}

__classes = [
    ClearInputPropsOperator,
    WITCH_PT_materials
]

def register():
    bpy.types.Node.witcher_include = bpy.props.BoolProperty(default=False)
    bpy.utils.register_class(NodeGroupInputProperties) #! imp to reg first
    bpy.utils.register_class(WitcherMaterialProperties)
    bpy.types.Material.witcher_props = bpy.props.PointerProperty(type=WitcherMaterialProperties)

    
    for __class in __classes:
        bpy.utils.register_class(__class)
    #bpy.app.handlers.depsgraph_update_post.append(update_node_group_inputs)


    #bpy.utils.register_class(MyNodeMenu)
    #bpy.types.SpaceNodeEditor.draw_handler_add(open_menu, (), 'WINDOW', 'POST_PIXEL')
    
    # bpy.utils.register_class(MoveTexturesPanel)
    # bpy.utils.register_class(MoveTexturesOperator)
    # bpy.types.Scene.path_a = bpy.props.StringProperty(name="Path A", description="Source Path")
    # bpy.types.Scene.path_b = bpy.props.StringProperty(name="Path B", description="Destination Path")

    
def unregister():
    # bpy.utils.unregister_class(MoveTexturesPanel)
    # bpy.utils.unregister_class(MoveTexturesOperator)
    # del bpy.types.Scene.path_a
    # del bpy.types.Scene.path_b
    
    bpy.utils.unregister_class(WitcherMaterialProperties)
    bpy.utils.unregister_class(NodeGroupInputProperties) #! imp to reg first
    # if update_node_group_inputs in bpy.app.handlers.depsgraph_update_post:
    #     bpy.app.handlers.depsgraph_update_post.remove(update_node_group_inputs)
    #for handle in bpy.app.handlers.depsgraph_update_post:

    for __class in __classes:
        bpy.utils.unregister_class(__class)
    del bpy.types.Material.witcher_props
    del bpy.types.Node.witcher_include
    #bpy.types.SpaceNodeEditor.draw_handler_remove(open_menu, 'WINDOW')
