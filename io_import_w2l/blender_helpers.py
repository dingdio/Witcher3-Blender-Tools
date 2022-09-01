import bpy
import os

def get_texture_node(tex_name):
    for node in bpy.data.materials[1].node_tree.nodes:
        if node.type == 'TEX_IMAGE':
            if node.image.name == tex_name:
                return node
    return None

def insert_color(mat, principled, tex, mapping, color_path):
    tex.location = (principled.location[0]-320, principled.location[1])
    tex.image = bpy.data.images.load(color_path, check_existing=True)
    tex.image.colorspace_settings.name = 'sRGB'
    mat.node_tree.links.new(principled.inputs["Base Color"], tex.outputs[0])
    mat.node_tree.links.new(tex.inputs["Vector"], mapping.outputs["Vector"])
    mat.node_tree.links.new(principled.inputs["Metallic"], tex.outputs["Alpha"])

def insert_normal(mat, principled, normal_map_node):
    normal_map_node.location = (principled.location[0]-300, principled.location[1]-250)
    mat.node_tree.links.new(principled.inputs["Normal"], normal_map_node.outputs[0])

def insert_normal_tex(mat, normal_map_node, normal_tex_n, mapping, normal_path, principled):
    normal_tex_n.location = (normal_map_node.location[0], normal_map_node.location[1]-250)
    normal_tex_n.image = bpy.data.images.load(normal_path, check_existing=True)
    normal_tex_n.image.colorspace_settings.name = 'Non-Color'
    mat.node_tree.links.new(normal_map_node.inputs["Color"], normal_tex_n.outputs["Color"])
    mat.node_tree.links.new(normal_tex_n.inputs["Vector"], mapping.outputs["Vector"])
    mat.node_tree.links.new(principled.inputs["Roughness"], normal_tex_n.outputs["Alpha"])

def get_normal_node(principled):
    if principled.inputs["Normal"].is_linked:
        soc = principled.inputs["Normal"].links[0].from_socket
        print(soc.node.type)
        print(soc.name)
        if soc.node.type == 'NORMAL_MAP' and soc.name == "Normal":
            return soc.node
    return None

def get_normal_texture_node(normal_map_node):
    if normal_map_node.inputs["Color"].is_linked:
        soc = normal_map_node.inputs["Color"].links[0].from_socket
        print(soc.node.type)
        print(soc.name)
        if soc.node.type == 'TEX_IMAGE':
            return soc.node
    return None