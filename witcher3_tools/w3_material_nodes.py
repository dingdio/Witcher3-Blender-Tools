import bpy
from .w3_material import init_material_nodes
from . import get_all_addon_prefs

import bpy

class ReplacePrincipledBSDFOperator(bpy.types.Operator):
    """Replace the selected Principled BSDF with a custom node group and reconnect inputs"""
    bl_idname = "witcher.replace_principled_bsdf"
    bl_label = "Replace Principled BSDF"

    def execute(self, context):
        # Get the current material and node tree
        material = context.material
        if not material:
            self.report({'ERROR'}, "No material selected")
            return {'CANCELLED'}

        node_tree = material.node_tree
        active_node = context.active_node
        if not active_node or active_node.type != 'BSDF_PRINCIPLED':
            self.report({'ERROR'}, "Please select a Principled BSDF node")
            return {'CANCELLED'}

        # Find the Material Output node
        output_node = next((n for n in node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
        if not output_node:
            self.report({'ERROR'}, "No active Material Output node found")
            return {'CANCELLED'}

        surface_input = output_node.inputs.get('Surface')
        if not (surface_input and surface_input.is_linked and surface_input.links[0].from_node == active_node):
            self.report({'ERROR'}, "Selected Principled BSDF is not connected to Material Output")
            return {'CANCELLED'}

        # Step 1: Store connections from Principled BSDF inputs
        base_color_input = active_node.inputs.get("Base Color")
        base_color_from_socket = base_color_input.links[0].from_socket if base_color_input and base_color_input.is_linked else None

        roughness_input = active_node.inputs.get("Roughness")
        roughness_from_socket = roughness_input.links[0].from_socket if roughness_input and roughness_input.is_linked else None

        normal_input = active_node.inputs.get("Normal")
        normal_from_socket = None
        if normal_input and normal_input.is_linked:
            normal_link = normal_input.links[0]
            normal_from_node = normal_link.from_node
            if normal_from_node.type == 'NORMAL_MAP':
                # If connected to a Normal Map, get the texture from its "Color" input
                color_input = normal_from_node.inputs.get("Color")
                if color_input and color_input.is_linked:
                    normal_from_socket = color_input.links[0].from_socket
            else:
                # Otherwise, use the direct connection
                normal_from_socket = normal_link.from_socket

        # Step 2: Store location and remove the Principled BSDF node
        node_location = active_node.location.copy()
        node_tree.nodes.remove(active_node)

        # Step 3: Add the new node group
        # Assuming init_material_nodes(material, "cake", clear=False) creates and returns the node group
        nodegroup = init_material_nodes(material, "cake", clear=False)
        if not nodegroup:
            self.report({'ERROR'}, "Failed to create node group")
            return {'CANCELLED'}
        nodegroup.location = node_location

        # Step 4: Connect the node group’s output to Material Output
        if nodegroup.outputs:
            node_tree.links.new(nodegroup.outputs[0], surface_input)
        else:
            self.report({'ERROR'}, "Node group has no outputs")
            return {'CANCELLED'}

        # Step 5: Reconnect the stored inputs to the node group
        if base_color_from_socket and "Diffuse" in nodegroup.inputs:
            node_tree.links.new(base_color_from_socket, nodegroup.inputs["Diffuse"])
        if roughness_from_socket and "Roughness" in nodegroup.inputs:
            node_tree.links.new(roughness_from_socket, nodegroup.inputs["Roughness"])
        if normal_from_socket and "Normal" in nodegroup.inputs:
            node_tree.links.new(normal_from_socket, nodegroup.inputs["Normal"])

        # Optional: Set the node group’s name based on the material
        nodegroup.name = material.name[-60:]

        self.report({'INFO'}, "Principled BSDF replaced successfully")
        return {'FINISHED'}

class WITCH_PT_materials(bpy.types.Panel):
    bl_label = "Witcher"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Witcher"

    
    
    def draw(self, context):
        layout = self.layout
        mat = context.material
        if mat and mat.witcher_props:

            box = layout.box()
            row = box.row(align=False)
            row.prop(mat.witcher_props, "witcher_material_settings_collapse", icon="TRIA_DOWN" if not mat.witcher_props.witcher_material_settings_collapse else "TRIA_RIGHT", icon_only=True, emboss=False)
            row.label(text="Global Settings")

            if not mat.witcher_props.witcher_material_settings_collapse:
                addon_prefs = get_all_addon_prefs(context)

                # Add UI elements for editing preferences
                box.prop(addon_prefs, "mod_directory")
                
                box.label(text="Texture Root Paths:")
                # New list of paths
                row = box.row()
                col = row.column()
                col.template_list(
                    "WITCHER_UL_path_list", 
                    "", 
                    addon_prefs, "path_list", 
                    addon_prefs, "active_path_index"
                )
                col = row.column()
                top = col.column(align=True)
                top.operator("witcher.add_path", text="", icon="ADD")
                top.operator("witcher.remove_path", text="", icon="REMOVE")

                # Editable field for the selected path
                if addon_prefs.path_list and 0 <= addon_prefs.active_path_index < len(addon_prefs.path_list):
                    selected_item = addon_prefs.path_list[addon_prefs.active_path_index]
                    box.prop(selected_item, "path", text="Selected Path")
                
            # Create a box for the texture override properties and operator
            box = layout.box()
            box.prop(mat.witcher_props, "override_texture_root", text="Override Texture Root")
            row = box.row()
            row.enabled = mat.witcher_props.override_texture_root
            row.prop(mat.witcher_props, "custom_texture_root", text="Texture Root")
            box.operator("witcher.replace_principled_bsdf", text="Replace Principled BSDF")
            
            #layout.operator("witcher.clear_input_props", text="Reset Parameters")
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
                                    rel_path = win_unprefix_path(linked_socket.node.image.filepath)
                                    abs_path = win_unprefix_path(bpy.path.abspath(rel_path))
                                    texture_path = os.path.normpath(abs_path)
                                    final_path = get_repo_from_abs_path(texture_path)
                                    if mat.witcher_props.override_texture_root:
                                        display_path = mat.witcher_props.custom_texture_root + os.path.basename(final_path)
                                    else:
                                        display_path = final_path
                                    resolved = is_path_resolved(display_path)
                                    icon = 'CHECKMARK' if resolved else 'ERROR'
                                    # Show filename for readability, full path via copy button tooltip
                                    path_row = layout.row(align=True)
                                    path_row.label(text="", icon=icon)
                                    path_row.label(text=display_path)
                                    op = path_row.operator("witcher.copy_texture_path", text="", icon='COPYDOWN')
                                    op.path = display_path
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
    witcher_material_settings_collapse: bpy.props.BoolProperty(default = False)
    override_texture_root: bpy.props.BoolProperty(name="override_texture_root", default=False, description="Specify a root path")
    custom_texture_root: bpy.props.StringProperty(name="custom_texture_root", default="", description="Root path of textures for this material")



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

from . import get_texture_path
from pathlib import Path
import os
from .CR2W.common_blender import win_unprefix_path


possible_folders = [
    'files\\Raw\\Mod',
    'files\\Raw\\DLC',
    'files\\Mod\\Cooked',
    'files\\Mod\\Uncooked',
    'files\\DLC\\Cooked',
    'files\\DLC\\Uncooked',
]

from . import get_mod_directory, get_texture_path, get_modded_texture_path, get_uncook_path
# def get_repo_from_abs_path(texture_path_input):
#     texture_path = os.path.realpath(bpy.path.abspath(texture_path_input))
#     TEXTURE_PATH = get_texture_path(bpy.context)
#     MOD_DIR = get_mod_directory(bpy.context)
#     MOD_TEX_PATH = get_modded_texture_path(bpy.context)
    
#     #path_obj = Path(texture_path)
#     TEXTURE_PATH_obj = Path(TEXTURE_PATH)
#     MOD_DIR_obj = Path(MOD_DIR)
#     MOD_TEX_PATH_obj = Path(MOD_TEX_PATH)
    
#     if TEXTURE_PATH_obj.exists() and TEXTURE_PATH in texture_path:
#         texture_path = texture_path.replace(TEXTURE_PATH+'\\', '')
#     elif MOD_DIR_obj.exists() and MOD_DIR in texture_path:
#         texture_path = texture_path.replace(MOD_DIR+'\\', '')
#         for folder in possible_folders:
#             if folder in texture_path:
#                 texture_path = texture_path.replace(folder+'\\', '')
#                 break
#     elif MOD_TEX_PATH_obj.exists() and MOD_TEX_PATH in texture_path:
#         texture_path = texture_path.replace(MOD_TEX_PATH+'\\', '')

#     return texture_path

def get_repo_from_abs_path(texture_path_input, extension='.xbm'):
    texture_path_input = win_unprefix_path(texture_path_input)
    texture_path = os.path.realpath(bpy.path.abspath(texture_path_input))
    texture_path = win_unprefix_path(texture_path)

    TEXTURE_PATH = get_texture_path(bpy.context)
    UNCOOK_PATH = get_uncook_path(bpy.context)
    MOD_DIR = get_mod_directory(bpy.context)
    MOD_TEX_PATH = get_modded_texture_path(bpy.context)

    addon_prefs = get_all_addon_prefs(bpy.context)

    # Ensure the path ends with the specified extension
    texture_path_no_ext = os.path.splitext(texture_path)[0]
    texture_path = texture_path_no_ext + extension

    def _try_strip_root(path, root):
        """Strip a root directory from the path, returning game-relative path or None."""
        root = win_unprefix_path(os.path.realpath(bpy.path.abspath(root)))
        if root and Path(root).exists() and root in path:
            return path.replace(root + '\\', '')
        return None

    # Check paths in path_list first (user custom roots)
    for path_item in addon_prefs.path_list:
        result = _try_strip_root(texture_path, path_item.path)
        if result:
            return result

    # REDkit project paths
    for path_item in addon_prefs.redkit_projects:
        if path_item.path:
            # Try workspace subfolder first (REDkit convention)
            result = _try_strip_root(texture_path, os.path.join(path_item.path, "workspace"))
            if not result:
                result = _try_strip_root(texture_path, path_item.path)
            if result:
                return result

    # REDkit uncooked depot
    result = _try_strip_root(texture_path, addon_prefs.redkit_uncooked_path)
    if result:
        return result

    # REDkit depot (r4data)
    result = _try_strip_root(texture_path, addon_prefs.redkit_depot_path)
    if result:
        return result

    # Texture uncook path
    result = _try_strip_root(texture_path, TEXTURE_PATH)
    if result:
        return result

    # Uncook path
    result = _try_strip_root(texture_path, UNCOOK_PATH)
    if result:
        return result

    # Mod directory
    if MOD_DIR and Path(MOD_DIR).exists() and MOD_DIR in texture_path:
        texture_path = texture_path.replace(MOD_DIR + '\\', '')
        for folder in possible_folders:
            if folder in texture_path:
                texture_path = texture_path.replace(folder + '\\', '')
                break
        return texture_path

    # Modded texture path
    result = _try_strip_root(texture_path, MOD_TEX_PATH)
    if result:
        return result

    game_repo_path = os.path.splitdrive(texture_path)[1]
    return game_repo_path.lstrip('\\/')


def is_path_resolved(path):
    """Check if a path is a game-relative (resolved) path vs an absolute path."""
    if not path:
        return True
    # Absolute paths have drive letters (C:\) or UNC paths (\\)
    return not os.path.isabs(path)



def get_socket_value(input_socket):
    if input_socket.is_linked:
        linked_socket = input_socket.links[0].from_socket
        if linked_socket.node.type == 'TEX_IMAGE' and linked_socket.node.image:
            mat = next((m for m in bpy.data.materials if m.node_tree == input_socket.node.id_data and hasattr(m, 'witcher_props')), None)
            rel_path = win_unprefix_path(linked_socket.node.image.filepath)
            abs_path = win_unprefix_path(bpy.path.abspath(rel_path))
            texture_path = os.path.normpath(abs_path)
            final_path = get_repo_from_abs_path(texture_path)
            if mat.witcher_props.override_texture_root:
                return mat.witcher_props.custom_texture_root + os.path.basename(final_path)
            else:
                return final_path
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
    bl_idname = "witcher.clear_input_props"
    bl_label = "Clear Input Props"

    def execute(self, context):
        mat = context.material
        mat.witcher_props.input_props.clear()
        depsgraph = context.evaluated_depsgraph_get()
        update_node_group_inputs(depsgraph)
        return {'FINISHED'}

class WITCH_OT_copy_texture_path(bpy.types.Operator):
    """Copy texture export path to clipboard"""
    bl_idname = "witcher.copy_texture_path"
    bl_label = "Copy Path"

    path: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        return properties.path if properties.path else "No path"

    def execute(self, context):
        context.window_manager.clipboard = self.path
        self.report({'INFO'}, f"Copied: {self.path}")
        return {'FINISHED'}

__classes = [
    ClearInputPropsOperator,
    WITCH_PT_materials,
    ReplacePrincipledBSDFOperator,
    WITCH_OT_copy_texture_path,
]

def register():
    bpy.types.Node.witcher_include = bpy.props.BoolProperty(default=False)
    bpy.types.Node.witcher_final_path = bpy.props.StringProperty(default="")
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
    del bpy.types.Node.witcher_final_path
    #bpy.types.SpaceNodeEditor.draw_handler_remove(open_menu, 'WINDOW')
