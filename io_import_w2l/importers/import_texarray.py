from io_import_w2l.CR2W.third_party_libs import yaml
import bpy
import os
import json

from io_import_w2l.importers import import_texarray_create_groups

def srgb_to_linearrgb(c):
    if   c < 0:       return 0
    elif c < 0.04045: return c/12.92
    else:             return ((c+0.055)/1.055)**2.4

def hex_to_rgb(h,alpha=1):
    r = (h & 0xff0000) >> 16
    g = (h & 0x00ff00) >> 8
    b = (h & 0x0000ff)
    return tuple([srgb_to_linearrgb(c/0xff) for c in (r,g,b)] + [alpha])

class TexturePath:
    def __init__(self, index, color_id, color_path, normal_path, bkgrnd, overlay):
        self.index = index
        self.color_id = color_id
        self.color_path = color_path
        self.normal_path = normal_path
        self.bkgrnd = bkgrnd
        self.overlay = overlay
    @classmethod
    def from_json(cls, data):
        return cls(**data)

# class TerrainMaterial:
#     def __init__(self, paths=[]):
#         self.paths = paths
#     @classmethod
#     def from_json(cls, data):
#         paths = list(map(TexturePath.from_json, data["meshes"]))
#         data["paths"] = paths
#         return cls(**data)


def loadTerrainMaterial(filename):
    dirpath, file = os.path.split(filename)
    basename, ext = os.path.splitext(file)
    with open(filename) as file:
        data = file.read()
        if ext.lower() in ('.json'):
                jsonData = json.loads(data)
                final_data = list(map(TexturePath.from_json, jsonData))
        if ext.lower() in ('.yml'):
                yamlData = yaml.full_load(data)
                final_data = list(map(TexturePath.from_json, yamlData['world']['terrain_materials']))
                print("ckae")
        else:
            final_data = None

    return final_data

#def get_texture_node(principled):
#    if principled.inputs["Base Color"].is_linked:
#        soc = principled.inputs["Base Color"].links[0].from_socket
#        if soc.node.type == 'TEX_IMAGE' and soc.name == "Color":
#            return soc.node
#    return None

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

def get_texture_node(tex_name, mat):
    for node in mat.node_tree.nodes:
        if node.type == 'TEX_IMAGE':
            if node.image.name == tex_name:
                return node
    return None

def insert_heightmap_to_disp(mat, disp_node, heightmap_texture_node, heightmap_path):
    heightmap_texture_node.image = bpy.data.images.load(heightmap_path, check_existing=True)
    heightmap_texture_node.image.colorspace_settings.name = 'Non-Color'
    heightmap_texture_node.location = (disp_node.location[0]-300, disp_node.location[1]-250)
    mat.node_tree.links.new(disp_node.inputs[0], heightmap_texture_node.outputs[0])




def create_normal_map_group(principled_node, mat, group_node):
    pl = principled_node.location
    ## NORMAL MAP
    #check if normalmap exist on principled
    normal_map_node = get_normal_node(principled_node)
    if normal_map_node == None:
        normal_map_node = mat.node_tree.nodes.new("ShaderNodeNormalMap")
    normal_map_node.location = (pl[0] + -500, pl[1] + 200)
    #mat.node_tree.links.new(normal_map_node.inputs[0], group_outer.outputs[0])

    Separate = mat.node_tree.nodes.new(type="ShaderNodeSeparateRGB")
    Separate.location = (pl[0] + -500,pl[1] + 300) 
    Combine = mat.node_tree.nodes.new(type="ShaderNodeCombineRGB")
    Combine.location = (pl[0] + -500,pl[1] + 600) 
    Invert = mat.node_tree.nodes.new(type="ShaderNodeInvert")
    Invert.location = (pl[0] + -500,pl[1] + 700) 

    mat.node_tree.links.new(Separate.inputs[0], group_node.outputs["normal"])

    mat.node_tree.links.new(Combine.inputs[0], Separate.outputs[0])
    mat.node_tree.links.new(Invert.inputs["Color"], Separate.outputs[1])
    mat.node_tree.links.new(Combine.inputs[1], Invert.outputs[0])
    mat.node_tree.links.new(Combine.inputs[2], Separate.outputs[2])

    mat.node_tree.links.new(normal_map_node.inputs["Color"], Combine.outputs[0])
    mat.node_tree.links.new(principled_node.inputs["Normal"], normal_map_node.outputs[0])
    #####--------------------------############
    mat.node_tree.links.new(principled_node.inputs["Roughness"], group_node.outputs["normalAlpha"])

def setup_terrain(tearrainMat):
    bpy.context.scene.cycles.feature_set = 'EXPERIMENTAL'
    bpy.context.scene.render.engine = 'CYCLES'
    bpy.context.scene.cycles.device = 'GPU'

    heightfield = r"E:\w3.modding\w3terrain-extract-v2020-03-30\prolog_village\prolog_village.heightmap.png"
    minHeight = -37.0
    maxHeight = 45.0

    bpy.ops.mesh.primitive_plane_add(size=2000, enter_editmode=False, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))
    obj = bpy.context.selected_objects[:][0]

    subsurface = obj.modifiers.new(type='SUBSURF', name="subsurface")
    subsurface.subdivision_type = 'SIMPLE'
    obj.cycles.use_adaptive_subdivision = True


    mat = bpy.data.materials.new(name="MaterialName") #set new material to variable
    mat.use_nodes = True
    mat.cycles.displacement_method = 'DISPLACEMENT'

    obj.data.materials.append(mat) #add the material to the object
    #mat.diffuse_color = (0.8, 0.0869164, 0.127191, 1) #change color


    Material_Output = mat.node_tree.nodes.get("Material Output")



    disp_material = mat.node_tree.nodes.get(mat.name+"_Displacement")
    if disp_material == None:
        disp_material =  mat.node_tree.nodes.new("ShaderNodeDisplacement")
        disp_material.name = mat.name+"_Displacement"
        disp_material.location = (disp_material.location[0]+320, disp_material.location[1]-300)

    disp_material.inputs[1].default_value = maxHeight/100
    disp_material.inputs[2].default_value = maxHeight + abs(minHeight)



    mat.node_tree.links.new(Material_Output.inputs["Displacement"], disp_material.outputs["Displacement"])

    #Disp MAP
    path = heightfield
    filename = os.path.basename(path)
    tex = get_texture_node(filename, mat)
    if tex == None:
        tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    insert_heightmap_to_disp(mat, disp_material, tex, path)


    #remove nodes
    # for a in bpy.data.node_groups:
    #     bpy.data.node_groups.remove(a, do_unlink=True)

    import_texarray_create_groups.create_landscape_inner()
    import_texarray_create_groups.create_landscape_outer()

    group_outer = mat.node_tree.nodes.get("landscape_outer")
    if group_outer == None:
        group_outer = mat.node_tree.nodes.new("ShaderNodeGroup")
        group_outer.name = "landscape_outer"
    group_outer.node_tree = bpy.data.node_groups['landscape_mix_outer']
    group_outer.location = (-400,0)

    group_background = mat.node_tree.nodes.get("landscape_outer_bkgrnd")
    if group_background == None:
        group_background = mat.node_tree.nodes.new("ShaderNodeGroup")
        group_background.name = "landscape_outer_bkgrnd"
    group_background.node_tree = bpy.data.node_groups['landscape_mix_outer']
    group_background.location = (0,-3000)


    principled = mat.node_tree.nodes.get("Principled BSDF")
    if principled == None:
        principled =  mat.node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    mat.node_tree.links.new(principled.inputs[0], group_outer.outputs[0])
    
    principled_background = None
    principled_background = mat.node_tree.nodes.get("Principled BSDF bkgrnd")
    if principled_background is None:
        principled_background =  mat.node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    principled_background.location = (0,-2000)
    mat.node_tree.links.new(principled_background.inputs[0], group_background.outputs[0])

    create_normal_map_group(principled, mat, group_outer)
    create_normal_map_group(principled_background, mat, group_background)

    mix_node = mat.node_tree.nodes.new("ShaderNodeMixShader")
    mix_node.location = (400,-1700)
    mat.node_tree.links.new(mix_node.inputs[1], principled.outputs[0])
    mat.node_tree.links.new(mix_node.inputs[2], principled_background.outputs[0])
    mat.node_tree.links.new(Material_Output.inputs[0], mix_node.outputs[0])



    ## TEX COORD MAP
    texCoord = mat.node_tree.nodes.get("Texture Coordinate")
    if texCoord == None:
        texCoord =  mat.node_tree.nodes.new("ShaderNodeTexCoord")
        texCoord.name = "Texture Coordinate"
    texCoord.location = (-3200, -655)

    mapping =  mat.node_tree.nodes.get("Mapping Node")
    if mapping == None:
        mapping =  mat.node_tree.nodes.new("ShaderNodeMapping")
        mapping.name = "Mapping Node"

    mapping.location = (-2854, -655)
    mat.node_tree.links.new(mapping.inputs[0], texCoord.outputs[2])
    mapping.inputs[3].default_value = (1024,1024,1024)


    for idx, item in enumerate(tearrainMat):
    #   color_path: E:\w3.modding\w3terrain-extract-v2020-03-30\texture_array_prolog\prolog_village.texarray.texture_0.tga
    #   normal_path: E:\w3.modding\w3terrain-extract-v2020-03-30\texture_array_prolog\prolog_village_normals.texarray.texture_0.tga
    #   bkgrnd: E:\w3.modding\w3terrain-extract-v2020-03-30\index_color_reader\bkgrnd\1_bkgrnd.png
    #   overlay: E:\w3.modding\w3terrain-extract-v2020-03-30\index_color_reader\overlay\1_TEST.png
        #COLOUR MAP
        color_path = item.color_path #r"E:\w3.modding\w3terrain-extract-v2020-03-30\texture_array_prolog\Blender\atlas_prolog.tga"
        color_filename = os.path.basename(color_path)
        # tex = get_texture_node(color_filename, mat)
        # if tex == None:
        tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
        tex.location = (-2000,-100*idx)
        mat.node_tree.links.new(group_outer.inputs["color"+str(idx+1)], tex.outputs["Color"])
        mat.node_tree.links.new(group_background.inputs["color"+str(idx+1)], tex.outputs["Color"])
        #insert_color(mat, principled, tex, mapping, color_path)

        tex.image = bpy.data.images.load(color_path, check_existing=True)
        tex.image.colorspace_settings.name = 'sRGB'
        mat.node_tree.links.new(tex.inputs[0], mapping.outputs[0])

        #NORMAL MAP
        normal_path = item.normal_path
        #check if normalmap exist on principled
        # normal_map_node = get_normal_node(principled)
        # if normal_map_node == None:
        #normal_map_node = mat.node_tree.nodes.new("ShaderNodeNormalMap")
        #insert_normal(mat, principled, normal_map_node)

        #GET THE NORMAL TEXTURE
        # normal_texture_node = get_normal_texture_node(normal_map_node)
        # if normal_texture_node == None:
        normal_texture_node = mat.node_tree.nodes.new("ShaderNodeTexImage")
        normal_texture_node.location = (-2000,-400*idx)
        mat.node_tree.links.new(group_outer.inputs["normal"+str(idx+1)], normal_texture_node.outputs["Color"])
        mat.node_tree.links.new(group_outer.inputs["normalAlpha"+str(idx+1)], normal_texture_node.outputs["Alpha"])

        mat.node_tree.links.new(group_background.inputs["normal"+str(idx+1)], normal_texture_node.outputs["Color"])
        mat.node_tree.links.new(group_background.inputs["normalAlpha"+str(idx+1)], normal_texture_node.outputs["Alpha"])
        #insert_normal_tex(mat, normal_map_node, normal_texture_node, mapping, normal_path, principled)
        normal_texture_node.image = bpy.data.images.load(normal_path, check_existing=True)
        normal_texture_node.image.colorspace_settings.name = 'Non-Color'
        mat.node_tree.links.new(normal_texture_node.inputs[0], mapping.outputs[0])
        
        blend_texture = mat.node_tree.nodes.new("ShaderNodeTexImage")
        blend_texture.location = (-2000,-800*idx)
        mat.node_tree.links.new(group_outer.inputs["blend"+str(idx+1)], blend_texture.outputs["Color"])
        blend_texture.image = bpy.data.images.load(item.overlay, check_existing=True)
        blend_texture.image.colorspace_settings.name = 'Non-Color'

        blend_texture_background = mat.node_tree.nodes.new("ShaderNodeTexImage")
        blend_texture_background.location = (-2000,-800*idx)
        mat.node_tree.links.new(group_background.inputs["blend"+str(idx+1)], blend_texture_background.outputs["Color"])
        blend_texture_background.image = bpy.data.images.load(item.bkgrnd, check_existing=True)
        blend_texture_background.image.colorspace_settings.name = 'Non-Color'




    ###################




def start_import(fileName):
    bpy.context.space_data.clip_end = 99999

    #tearrainMat = loadTerrainMaterial(r"E:\w3.modding\w3terrain-extract-v2020-03-30\index_color_reader\km_terraub.json")
    #tearrainMat = loadTerrainMaterial(r"E:\w3.modding\w3terrain-extract-v2020-03-30\terrain.json")
    tearrainMat = loadTerrainMaterial(r"E:\w3.modding\w3terrain-extract-v2020-03-30\terrain_prolog.yml")
    setup_terrain(tearrainMat)
    return
    #heightfield = r"E:\w3.modding\w3terrain-extract-v2020-03-30\prolog_village" +"\\"+ "prolog_village.heightmap.png"
    heightfield = r"E:\w3.modding\w3terrain-extract-v2020-03-30\prolog_village\prolog_village.heightmap.png"
    blendcontrol = r"E:\w3.modding\w3terrain-extract-v2020-03-30\prolog_village\prolog_village.blendcontrol.png"
    terrainSize = 2000
    bpy.ops.mesh.primitive_plane_add(size=terrainSize, enter_editmode=False, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))

    #for mat in bpy.data.materials:
    mat = bpy.data.materials[1]


    first_material = bpy.data.materials[1].node_tree.nodes.get("Base Shader")
    if first_material == None:
        first_material =  bpy.data.materials[1].node_tree.nodes.new("ShaderNodeBsdfPrincipled")
        first_material.name = "Base Shader"

    texCoord = bpy.data.materials[1].node_tree.nodes.get("Texture Coordinate")
    if texCoord == None:
        texCoord =  bpy.data.materials[1].node_tree.nodes.new("ShaderNodeTexCoord")
        texCoord.name = "Texture Coordinate"

    mapping =  bpy.data.materials[1].node_tree.nodes.get("Mapping.001")
    if mapping == None:
        mapping =  bpy.data.materials[1].node_tree.nodes.new("Mapping.001")

    #TODO ALL THE LINKING IF THESE THINGS DON'T EXIST
    last_mix_group = None
    last_principled_shader = None

    for idx, item in enumerate(tearrainMat):
        mix_group = bpy.data.materials[1].node_tree.nodes.get("color_id_"+ str(item.index))
        if mix_group == None:
            mix_group = bpy.data.materials[1].node_tree.nodes.new("ShaderNodeGroup")
            mix_group.name = "color_id_"+ str(item.index)
        mix_group.node_tree = bpy.data.node_groups['Mix by color']
        mix_group.location = (idx*800,250)
        mix_group.inputs['Color to pick'].default_value = hex_to_rgb(int(item.color_id, 0))

        principled = bpy.data.materials[1].node_tree.nodes.get("principled_"+ str(item.index))
        if principled == None:
            principled =  bpy.data.materials[1].node_tree.nodes.new("ShaderNodeBsdfPrincipled")
            principled.name = "principled_"+ str(item.index)
        principled.location = (idx*800,0)
        principled.inputs[0].default_value = hex_to_rgb(int(item.color_id, 0))

        #LINKING
        mat.node_tree.links.new(mix_group.inputs["Shader"], principled.outputs["BSDF"])
        if last_mix_group is None:
            mat.node_tree.links.new(mix_group.inputs["Base Shader"], first_material.outputs["BSDF"])
        else:
            mat.node_tree.links.new(mix_group.inputs["Base Shader"], last_mix_group.outputs["Shader"])

        last_mix_group = mix_group
        
        #COLOUR MAP
        color_path = item.color_path
        color_filename = os.path.basename(color_path)
        tex = get_texture_node(color_filename)
        if tex == None:
            tex = bpy.data.materials[1].node_tree.nodes.new("ShaderNodeTexImage")
        insert_color(mat, principled, tex, mapping, color_path)


        #NORMAL MAP
        normal_path = item.normal_path
        #check if normalmap exist on principled
        normal_map_node = get_normal_node(principled)
        if normal_map_node == None:
            normal_map_node = bpy.data.materials[1].node_tree.nodes.new("ShaderNodeNormalMap")
        insert_normal(mat, principled, normal_map_node)

        #GET THE NORMAL TEXTURE
        normal_texture_node = get_normal_texture_node(normal_map_node)
        if normal_texture_node == None:
            normal_texture_node = bpy.data.materials[1].node_tree.nodes.new("ShaderNodeTexImage")
        insert_normal_tex(mat, normal_map_node, normal_texture_node, mapping, normal_path, principled)



    #LINK TO MATERIAL NODE

    MaterialOutput = bpy.data.materials[1].node_tree.nodes.get("Material Output")
    mat.node_tree.links.new(MaterialOutput.inputs["Surface"], last_mix_group.outputs["Shader"])
