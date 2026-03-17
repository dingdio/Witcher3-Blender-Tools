# Modified w3_material.py from Mets3D orignal
# https://github.com/Mets3D/batch_import_witcher3_fbx

import logging
log = logging.getLogger(__name__)

from pathlib import Path
from math import radians
from .CR2W import CR2W_reader
import bpy, os
from typing import List, Dict
from bpy.types import Image, Material, Object, Node

from xml.etree import ElementTree
Element = ElementTree.Element

from .w3_material_constants import *
from . import get_modded_texture_path, get_uncook_path, get_mod_directory, get_tex_ext, get_texture_path
from .ui.blender_fun import convert_xbm_to_dds, load_w2cube_image, load_w2cube_blick_equirect_image
from .CR2W.common_blender import repo_file, win_safe_path, bpy_image_load_safe, win_path_key, win_unprefix_path

possible_folders = [
    'files\\Raw\\Mod',
    'files\\Raw\\DLC',
]

tex_types = [
    '.tga',
    '.dds',
    '.png'
]


def _strip_invalid_xml_chars(value) -> str:
    if value is None:
        return ""
    text = str(value)
    return "".join(
        ch for ch in text
        if ch in ("\t", "\n", "\r") or (0x20 <= ord(ch) <= 0xD7FF) or (0xE000 <= ord(ch) <= 0xFFFD)
    )


def _sanitize_xml_attr(value, fallback: str = "") -> str:
    clean = _strip_invalid_xml_chars(value).strip()
    return clean if clean else fallback

def repo_file_mat(filepath: str):
    if filepath.endswith(get_tex_ext(bpy.context)):
        modded_texture = os.path.join(get_modded_texture_path(bpy.context), filepath)
        if os.path.exists(modded_texture):
            return modded_texture
        else:
            for folder in possible_folders:
                modded_texture = os.path.join(get_mod_directory(bpy.context)+'\\'+folder, filepath)
                if os.path.exists(modded_texture):
                   return modded_texture
    # if filepath.endswith('.tga'):
    #     changed_filepath = filepath.replace(".tga", get_tex_ext(bpy.context))
    # elif filepath.endswith('.dds'):
    #     changed_filepath = filepath.replace(".dds", get_tex_ext(bpy.context))
    # elif filepath.endswith('.png'):
    #     changed_filepath = filepath.replace(".png", get_tex_ext(bpy.context))
    
    return filepath

def hide_unused_sockets(node, inp=True, out=True):
    if inp:
        for socket in node.inputs:
            socket.hide = True		# Blender will prevent it if it's used, no need for us to check.
    if out:
        for socket in node.outputs:
            socket.hide = True

def ensure_node_group(ng_name, resource_path=RES_PATH):
    """Check if a nodegroup exists, and if not, append it from the addon's resource file."""

    if ng_name not in bpy.data.node_groups:
        with bpy.data.libraries.load(resource_path) as (data_from, data_to):
            for ng in data_from.node_groups:
                if ng == ng_name:
                    data_to.node_groups.append(ng)

    ng = bpy.data.node_groups[ng_name]
    ng.use_fake_user = False

    return ng


def normalize_depot_path(path: str) -> str:
    return (path or "").replace("/", "\\").lower()


def is_witcher2_material(material: Material) -> bool:
    props = getattr(material, "witcher_props", None)
    return bool(props and props.material_version == 'witcher2')


def resolve_witcher2_shader_type(mat_base: str, shader_type: str) -> str:
    normalized_base = normalize_depot_path(mat_base)
    direct_match = WITCHER2_SHADER_BY_BASE_PATH.get(normalized_base)
    if not direct_match:
        for base_path, mapped_shader in WITCHER2_SHADER_BY_BASE_PATH.items():
            if normalized_base.endswith(base_path):
                direct_match = mapped_shader
                break
    if direct_match:
        return direct_match

    if shader_type in SHADER_MAPPING_W2:
        return shader_type

    guessed_shader = guess_shader_type(shader_type)
    log.info(f"Witcher 2 shader fallback: {shader_type} -> {guessed_shader}")
    return guessed_shader


def get_shader_resources_for_material(material: Material):
    if is_witcher2_material(material):
        return SHADER_MAPPING_W2, 'Witcher2_Main', RES_PATH
    return SHADER_MAPPING, 'Witcher3_Main', RES_PATH


def load_w3_materials_XML(
        obj: Object
        ,uncook_path: str
        ,xml_path: str
        ,force_mat_update = False
    ):
    """Read XML data and sets up all materials on the object.
    This unavoidable requires that the materials were not renamed
    after the FBX import in any way, including any .001 shennanigans.
    """
    root: Element = readXML(xml_path)

    for root_element in root:
        if root_element.tag == 'materials':
            for xml_data in root_element:
                xml_mat_name = xml_data.get('name')
                if xml_mat_name == "":
                    log.info("No material name? " + obj.name)
                    continue
                # Find corresponding blender material.
                target_mat = None
                for mat in obj.data.materials:
                    if not mat:
                        # Idk how, but this happens.
                        continue
                    if "Material" not in mat.name:
                        # This material was already processed.
                        continue
                    #remove any images the model imported so it doesn't conflict with repo import
                    for node in mat.node_tree.nodes:
                        if node.type == "TEX_IMAGE"and node.image:
                            bpy.data.images.remove(node.image)
                        mat.node_tree.nodes.remove( node )
                    #mat.node_tree.asset_clear()
                    
                    # Compare the number at the end of the blender material name "MaterialX"
                    # to the last character of the XML material.
                    material_number = mat.name.split("Material")[1]
                    assert mat.name[-4] != ".", f"ERROR: Material {mat.name} has .00x suffix. This must be avoided!"
                    xml_material_number = xml_mat_name.split("Material")[1]
                    if "Material" in mat.name and material_number == xml_material_number:
                        target_mat = mat
                        break
                if not target_mat:
                    # Didn't find a matching blender material.
                    # Must be a material that's only for LODs, so let's ignore.
                    continue
                finished_mat = setup_w3_material(uncook_path, target_mat, xml_data, xml_path, force_update=force_mat_update)
                obj.material_slots[target_mat.name].material = finished_mat

def find_mapping_nodes(node_tree):
    mapping_nodes = []
    for node in node_tree.nodes:
        if node.bl_idname == 'ShaderNodeMapping':
            mapping_nodes.append(node)
    return mapping_nodes

def readXML(xml_path) -> Element:
    """Read Witcher 3 material info read from an .xml file, and return the root Element."""
    try:
        with open(xml_path, 'r') as myFile:
            # XXX: Parsing the file directly doesn't work due to a bug in ElementTree
            # that rejects UTF-16, so we have to use fromstring().
            data = myFile.read()
    except Exception:
        with open(xml_path, 'r', encoding='utf-16-le') as myFile:
            # XXX: Parsing the file directly doesn't work due to a bug in ElementTree
            # that rejects UTF-16, so we have to use fromstring().
            data = myFile.read()
    return ElementTree.fromstring(data)

def _ngt_new_input(ngt, socket_type, name):
    """Add an input socket to a node group tree, compatible with Blender 4.0+."""
    if bpy.app.version >= (4, 0, 0):
        ngt.interface.new_socket(name=name, in_out='INPUT', socket_type=socket_type)
    else:
        ngt.inputs.new(socket_type, name)

def _ngt_new_output(ngt, socket_type, name):
    """Add an output socket to a node group tree, compatible with Blender 4.0+."""
    if bpy.app.version >= (4, 0, 0):
        ngt.interface.new_socket(name=name, in_out='OUTPUT', socket_type=socket_type)
    else:
        ngt.outputs.new(socket_type, name)

def _new_mix_color_node(node_tree):
    """Create a color mix node compatible with Blender 4.0+."""
    if bpy.app.version >= (4, 0, 0):
        node = node_tree.nodes.new('ShaderNodeMix')
        node.data_type = 'RGBA'
        node.blend_type = 'MIX'
    else:
        node = node_tree.nodes.new('ShaderNodeMixRGB')
        node.blend_type = 'MIX'
    return node

def _mix_fac_input(mix_node):
    """Return the factor input index for a mix node."""
    if bpy.app.version >= (4, 0, 0):
        return mix_node.inputs[0]  # "Factor" at index 0
    return mix_node.inputs[0]  # "Fac" at index 0

def _mix_color_inputs(mix_node):
    """Return (color_A_input, color_B_input) for a mix node."""
    if bpy.app.version >= (4, 0, 0):
        return mix_node.inputs[6], mix_node.inputs[7]  # A, B for RGBA
    return mix_node.inputs[1], mix_node.inputs[2]  # Color1, Color2

def _mix_color_output(mix_node):
    """Return the color output socket for a mix node."""
    if bpy.app.version >= (4, 0, 0):
        return mix_node.outputs[2]  # Result (RGBA)
    return mix_node.outputs[0]  # Color

def create_instance_group(  material,
                            xml_data,
                            xml_path,
                            mat_base,
                            shader_type,
                            uncook_path,
                            x_loc):
    nodes = material.node_tree.nodes
    links = material.node_tree.links

    nodegroup_node = init_instance_nodes(material, shader_type, clear = False, x_loc = x_loc)
    nodegroup_node.name = material.name
    #nodes_create_outputs(material, nodes, links, nodegroup_node, xml_data, xml_path)

    ngt = nodegroup_node.node_tree

    # create group inputs
    group_inputs = ngt.nodes.new('NodeGroupInput')
    group_inputs.location = (-550,0)
    # create group outputs
    group_outputs = ngt.nodes.new('NodeGroupOutput')
    group_outputs.location = (300,0)

    # Order parameters so input nodes get created in a specified order, from top to bottom relative to the inputs of the nodegroup.
    # Purely for neatness of the node noodles.
    ordered_params = order_elements_by_attribute(xml_data, PARAM_ORDER, 'name')


    for idx, p in enumerate(ordered_params):
        par_name = p.get('name')
        par_type = p.get('type')
        par_value = p.get('value')
        if par_type == "Color":
            _ngt_new_input(ngt, 'NodeSocketColor', par_name)
            _ngt_new_output(ngt, 'NodeSocketColor', par_name)
            values = [float(f) for f in par_value.split("; ")]
            d_val = (
                values[0] / 255
                ,values[1] / 255
                ,values[2] / 255
                ,values[3] / 255
            )
            ngt.inputs[par_name].default_value = d_val
            nodegroup_node.inputs[par_name].default_value = d_val
        elif par_type == "Float":
            _ngt_new_input(ngt, 'NodeSocketFloat', par_name)
            _ngt_new_output(ngt, 'NodeSocketFloat', par_name)
            ngt.inputs[par_name].default_value = float(par_value)
            nodegroup_node.inputs[par_name].default_value = float(par_value)

            ngt.links.new(group_inputs.outputs[par_name], group_outputs.inputs[par_name])
        elif par_type == "handle:ITexture":
            _ngt_new_input(ngt, 'NodeSocketColor', par_name)
            _ngt_new_output(ngt, 'NodeSocketColor', par_name)
            _ngt_new_input(ngt, 'NodeSocketFloat', par_name+"_active")

            # create three math nodes in a group
            mix_node_1 = _new_mix_color_node(ngt)
            mix_node_1.location = (0,0+(-500*idx))
            color_a, color_b = _mix_color_inputs(mix_node_1)
            ngt.links.new(group_inputs.outputs[par_name], color_b)
            ngt.links.new(_mix_color_output(mix_node_1), group_outputs.inputs[par_name])

            math_node_1 = ngt.nodes.new('ShaderNodeMath')
            math_node_1.location = (-320,200+(-500*idx))
            math_node_1.operation = 'GREATER_THAN'


            ngt.links.new(_mix_fac_input(mix_node_1), math_node_1.outputs[0])
            ngt.links.new(math_node_1.inputs[0], group_inputs.outputs[par_name+"_active"])

            #node = ngt.nodes.new(type="ShaderNodeTexImage")
            #node.width = 300
            node = create_node_texture(material, p, ngt, 0+(500*idx), uncook_path, 0, using_node_tree = True)

            node.location = (-320,0+(-500*idx))
            if node and node.image:
                if par_name in ['Diffuse', 'SpecularTexture', 'SnowDiffuse']:
                    node.image.colorspace_settings.name = 'sRGB'
                else:
                    node.image.colorspace_settings.name = 'Non-Color'

            if node and node.image and len(node.outputs[0].links) > 0:
                pin_name = node.outputs[0].links[0].to_socket.name
                if pin_name in ['Diffuse', 'SpecularTexture', 'SnowDiffuse']:
                    node.image.colorspace_settings.name = 'sRGB'
                else:
                    node.image.colorspace_settings.name = 'Non-Color'
            ngt.links.new(node.outputs["Color"], color_a)

        elif par_type == 'Vector':
            _ngt_new_input(ngt, 'NodeSocketVector', par_name)
            _ngt_new_output(ngt, 'NodeSocketVector', par_name)
            ngt.links.new(group_inputs.outputs[par_name], group_outputs.inputs[par_name])

            values = [float(f) for f in par_value.split("; ")]
            d_val = (
                values[0]
                ,values[1]
                ,values[2]
            )
            ngt.inputs[par_name].default_value = d_val
            nodegroup_node.inputs[par_name].default_value = d_val
        else:
            _ngt_new_input(ngt, 'NodeSocketFloat', par_name)
            _ngt_new_output(ngt, 'NodeSocketFloat', par_name)
            ngt.links.new(group_inputs.outputs[par_name], group_outputs.inputs[par_name])

    return (ordered_params, nodegroup_node)

def xml_data_from_CR2W(mat_bin, name = None):
    base_var = mat_bin.GetVariableByName('baseMaterial')
    if base_var:
        mat_base = base_var.Handles[0].DepotPath
    elif hasattr(mat_bin, 'DepotPath') and mat_bin.DepotPath:
        # CMaterialGraph referenced directly (no instance wrapper) — the chunk IS the shader
        mat_base = mat_bin.DepotPath
    else:
        mat_base = r'engine\materials\graphs\pbr_std.w2mg'
    shader_type = mat_base.split("\\")[-1][:-5]	# The .w2mg or .w2mi file, minus the extension.
    
    if name == None:
        filePath = mat_bin._CLASS__CR2WFILE.fileName
    
    new_xml = ElementTree.Element('material')
    new_xml.set('name', _sanitize_xml_attr(name if name else Path(filePath).stem, "material"))
    new_xml.set('local', "true")
    new_xml.set('base', _sanitize_xml_attr(mat_base, r"engine\materials\graphs\pbr_std.w2mg"))

    w2mi_params = {}
    read_instance_params(mat_bin, w2mi_params)
    for name, attrs in w2mi_params.items():
        create_param(
            xml_data = new_xml
            ,name = name 
            ,type = attrs[0]
            ,value = attrs[1]
        )
    return new_xml

def resolve_w2mg(w2mi_path):
    """Follow w2mi baseMaterial chain to find the final .w2mg shader path."""
    try:
        full_path = repo_file(w2mi_path)
        if not os.path.exists(full_path):
            return None
        material_bin = CR2W_reader.load_material(full_path)[0]
        base_var = material_bin.GetVariableByName('baseMaterial')
        if not base_var:
            return None
        base_path = base_var.Handles[0].DepotPath
        if base_path.endswith(".w2mg"):
            return base_path
        elif base_path.endswith(".w2mi"):
            return resolve_w2mg(base_path)
    except Exception:
        pass
    return None

def get_all_w2mi(w2mi_path, all_instances):
    full_path = repo_file(w2mi_path) #os.path.join(get_uncook_path(bpy.context), w2mi_path)
    material_bin = CR2W_reader.load_material(full_path)[0]

    xml_data = xml_data_from_CR2W(material_bin)
    mat_base = xml_data.get('base')
    all_instances.append(xml_data)

    if mat_base.endswith(".w2mi"):
        return get_all_w2mi(mat_base, all_instances)
    else:
        return mat_base

def setup_w3_material(
        uncook_path: str
        ,material: Material
        ,xml_data: Element
        ,xml_path: str
        ,force_update = False	# Set to True when re-importing stuff to test changes with the latest material set-up code.
        ,is_instance_file = False
        ):

    force_update = False
    is_instance_file = False # This is the multi-group method, still not working

    # Checks for duplicate materials
    # Saves XML data in custom properties
    # Creates nodes
    # Loads images

    mat_base = xml_data.get('base')		# Path to the .w2mg or .w2mi file.
    if not mat_base:
        # Never seen this happen, but just in case.
        log.info("No material base, skipping: " + material.name)
        return


    do_instance = False
    params = {}
    for p in xml_data:
        params[p.get('name')] = p.get('value')
        par_name = p.get('name')
        if par_name in EQUIVALENT_PARAMS:
            do_instance = True
            log.info(f"EQUIVALENT_PARAMS found {par_name}, replacing {EQUIVALENT_PARAMS[par_name]}, creating new material node instance for {bpy.context.active_object.name}")

    shader_type = mat_base.split("\\")[-1][:-5]	# The .w2mg or .w2mi file, minus the extension.
    resolved_mat_base = mat_base

    nodes = material.node_tree.nodes
    links = material.node_tree.links

    if mat_base.endswith(".w2mi"):
        # The XML contains little to no info about material instances, but the FBX importer
        # imported some image nodes we can use.
        fallback_shader_type = guess_shader_type(shader_type)
        w2mi_path = xml_data.get('base')
        #w2mi_tex_params = read_2wmi_params(material, uncook_path, w2mi_path, shader_type)
        w2mi_params = read_2wmi_params(w2mi_path)

        # Try to resolve the actual .w2mg base shader from the w2mi chain
        resolved_w2mg = resolve_w2mg(w2mi_path)
        if resolved_w2mg:
            resolved_mat_base = resolved_w2mg
            shader_type = resolved_w2mg.split("\\")[-1][:-5]
            log.info(f"Resolved shader type from w2mg: {shader_type}")
        else:
            shader_type = fallback_shader_type

        for par_name in w2mi_params.keys():
            if par_name in EQUIVALENT_PARAMS:
                do_instance = True
                log.info(f"EQUIVALENT_PARAMS found {par_name}, replacing {EQUIVALENT_PARAMS[par_name]}, creating new material node instance for {bpy.context.active_object.name}")

    if is_witcher2_material(material):
        shader_type = resolve_witcher2_shader_type(resolved_mat_base, shader_type)


    # Checking if this material was already imported by comparing some custom properties
    # that we create on imported materials.
    existing_mat = find_material(mat_base, params)
    if existing_mat:
        if not force_update:
            return existing_mat

    # Backing up all the info from the XML into custom properties. This is used for duplicate checking.
    # (See just above)
    material['witcher3_mat_base'] = mat_base
    material['witcher3_mat_params'] = params

    #TODO Create the material instance NodeGroup
    #TODO instances contained within w2mesh files will be imported as materials.
    if is_instance_file: #! Cange name of this option to "instance_group_mode" or something.
        
        all_instances = [xml_data] # xml data for each instance

        if mat_base.endswith(".w2mi"):
            final_base_mat = get_all_w2mi(mat_base, all_instances)


        #clear all nodes in the main material
        nodes.clear()
        # find each instance
        # create group for each instance
        #link them all up to the base material group at the end
        all_instances_params = []
        for i, instance_xml_data in enumerate(reversed(all_instances)):
            (ordered_params, nodegroup_node) = create_instance_group(material,
                                instance_xml_data,
                                xml_path,
                                mat_base,
                                shader_type,
                                uncook_path,
                                x_loc = -350 + i*-500)
            all_instances_params.append((ordered_params, nodegroup_node))
        
        all_instances_params_rev = all_instances_params[::-1]

        for idx in range(len(all_instances_params_rev)-1):
            from_group = all_instances_params_rev[idx]
            to_group = all_instances_params_rev[idx+1]
            for p in from_group[0]:
                par_name = p.get('name')
                try:
                    material.node_tree.links.new(from_group[1].outputs[par_name], to_group[1].inputs[par_name])
                    active_node = to_group[1].inputs.get( par_name+"_active")
                    if active_node:
                        active_node.default_value = 1.0
                    if par_name == 'DetailTile':
                        mapping_nodes = find_mapping_nodes(to_group[1].node_tree)
                        
                        def get_group_input(node_tree):
                            for node in node_tree.nodes:
                                if node.type == 'GROUP_INPUT':
                                    return node
                            return None
                        group_input = get_group_input(to_group[1].node_tree)
                        DetailTile_input = group_input.outputs['DetailTile']
                        if DetailTile_input and mapping_nodes:
                            for mapping in mapping_nodes:
                                to_group[1].node_tree.links.new(
                                    DetailTile_input,
                                    mapping.inputs[3])
                                            

                except Exception as e:
                    log.critical(f"MATERIAL ERROR {e}")
                    log.warning("Material setup error: %s", e)
        

        nodegroup_node_base_shader = init_material_nodes(material, shader_type, clear = False)
        nodegroup_node_base_shader.name = mat_base[-60:]
        nodes_create_outputs(material, nodes, links, nodegroup_node_base_shader, xml_data, xml_path)
        for idx, p in enumerate(all_instances_params[0][0]):
            par_name = p.get('name')
            par_type = p.get('type')
            par_value = p.get('value')
            try:
                material.node_tree.links.new(all_instances_params[0][1].outputs[par_name], nodegroup_node_base_shader.inputs[par_name])
            except Exception as e:
                log.critical(f"MATERIAL ERROR {e}") #raise e
    else:
        if mat_base.endswith(".w2mi"):
            #remove w2mi_params the main material instance already provided
            for name, attrs in params.items():
                w2mi_params.pop(name, None)

            for name, attrs in w2mi_params.items():
                create_param(
                    xml_data = xml_data
                    ,name = name 
                    ,type = attrs[0]
                    ,value = attrs[1]
                )
            # for tex_path, tex_type in w2mi_tex_params.items():
            #     create_texture_param(
            #         xml_data = xml_data
            #         ,name = tex_type
            #         ,tex_filepath = tex_path
            #     )


        only_basic_maps = True
        # if only_basic_maps:
        #     new_xml = ElementTree.Element(xml_data.tag, xml_data.attrib)
        #     for value in list(xml_data.iter()):
        #         if 'Diffuse' == value.attrib['name'] or 'Normal' == value.attrib['name']:
        #             new_xml.append(value)
        #     xml_data = new_xml

        #log.warning(ElementTree.tostring(xml_data, encoding='utf8', method='xml'))
        #all_children2 = list(xml_data.iter())
        # Clean existing nodes and create core nodegroup.
        nodegroup_node = init_material_nodes(material, shader_type)
        nodegroup_node.name = mat_base[-60:]
        if do_instance:
            nodegroup_node.node_tree = nodegroup_node.node_tree.copy()

        nodes_create_outputs(material, nodes, links, nodegroup_node, xml_data, xml_path)

        # Order parameters so input nodes get created in a specified order, from top to bottom relative to the inputs of the nodegroup.
        # Purely for neatness of the node noodles.
        ordered_params = order_elements_by_attribute(xml_data, PARAM_ORDER, 'name')
        
        for name, attrs in params.items():
            for param1 in ordered_params:
                if param1.attrib['name'] == name:
                    param1.set("witcher_include", True)

        
        #links nodes to created output
        #! Missing params will be created by this function
        mat_load_params_into_nodes(material, ordered_params, nodegroup_node, uncook_path)
        if shader_type == 'pbr_eye':
            setup_eye_reflection_nodes(material, nodegroup_node, nodes, links)
        hide_unused_sockets(nodegroup_node)
    
        if existing_mat and force_update:
            existing_mat.user_remap(material)

        #if the material is a .w2mi file use the filename, otherwise use diffues name for materal
        if not is_instance_file:
            pass
            #mat_set_name_by_diffuse(material, nodegroup_node, nodes)
        mat_ensure_dummy_transparent_img_node(material, nodegroup_node, shader_type, nodes)
        mat_apply_settings(material, shader_type)

        DetailTile_node = nodes.get("DetailTile")
        Pattern_Array_mapping_node = nodes.get("Pattern_Array_Mapping")
        if DetailTile_node and Pattern_Array_mapping_node:
            Pattern_Array_mapping_node.inputs[3].default_value[0] = DetailTile_node.inputs[3].default_value[0]
            Pattern_Array_mapping_node.inputs[3].default_value[1] = DetailTile_node.inputs[3].default_value[1]

    return material

def find_material(mat_base, params):
    """Find a material based on the Witcher 3 shader type and shader parameters,
    which we store in custom properties on import.
    This is useful for checking whether a material was already imported.
    """
    for m in bpy.data.materials:
        if (
            'witcher3_mat_params' in m and \
            mat_base == m['witcher3_mat_base'] and \
            params == m['witcher3_mat_params'].to_dict()
        ):
            # A material with the same parameters is already imported,
            return m

def read_2wmi_params2(
        material_bin: str
        ) -> Dict[str, str]:
    final_params: Dict[str, str] = {}	# texture filepath : texture type
    baseMaterial = material_bin.GetVariableByName('baseMaterial')
    if baseMaterial:
        handle = baseMaterial.Handles[0]
        if baseMaterial.theType == "handle:IMaterial" and handle.ClassName == "CMaterialInstance":
            more_tex_params = read_2wmi_params(handle.DepotPath)
            #TODO THESE PARAMS SHOULD NOT OVERRIDE EXISTING PARAMS
            #TODO NEED TO COMPARE and replace PROPs
            final_params.update(more_tex_params)
    read_instance_params(material_bin, final_params)
    return final_params
    
def _read_graph_param_chunks(chunks, final_params):
    """Read CMaterialParameter* chunks from a .w2mg graph file into final_params.
    This extracts the default parameter values defined as graph nodes (textures,
    colours, scalars, vectors) that are siblings of the CMaterialGraph chunk.
    """
    for chunk in chunks:
        name_var = chunk.GetVariableByName('parameterName')
        if name_var is None:
            continue
        par_name = name_var.Index.String
        if not par_name:
            continue
        try:
            if chunk.Type == "CMaterialParameterTexture":
                texture = chunk.GetVariableByName('texture')
                if texture and texture.Handles and texture.Handles[0].DepotPath:
                    final_params[par_name] = ('handle:ITexture', texture.Handles[0].DepotPath)
            elif chunk.Type == "CMaterialParameterColor":
                color = chunk.GetVariableByName('color')
                if color:
                    R = color.GetVariableByName('Red').Value
                    G = color.GetVariableByName('Green').Value
                    B = color.GetVariableByName('Blue').Value
                    A = color.GetVariableByName('Alpha').Value
                    final_params[par_name] = ('Color', f"{R}; {G}; {B}; {A}")
            elif chunk.Type == "CMaterialParameterScalar":
                scalar = chunk.GetVariableByName('scalar')
                if scalar:
                    final_params[par_name] = ('Float', str(scalar.Value))
            elif chunk.Type == "CMaterialParameterVector":
                vector = chunk.GetVariableByName('vector')
                if vector:
                    X = vector.GetVariableByName('X').Value
                    Y = vector.GetVariableByName('Y').Value
                    Z = vector.GetVariableByName('Z').Value
                    W = vector.GetVariableByName('W').Value
                    final_params[par_name] = ('Vector', f"{X}; {Y}; {Z}; {W}")
        except Exception as e:
            log.warning(f"Failed to read graph param chunk {chunk.Type} '{par_name}': {e}")


def read_instance_params(material, final_params):
    mat_instance = getattr(material, 'CMaterialInstance', None)
    if mat_instance is None:
        # CMaterialGraph used directly — read default params from attached graph parameter chunks
        graph_params = getattr(material, '_graph_params', None)
        if graph_params:
            _read_graph_param_chunks(graph_params, final_params)
        return final_params
    for mat_param in mat_instance.InstanceParameters.elements:
        PROP = mat_param.PROP
        if PROP.theType == "Float":
            final_params[PROP.theName] = (PROP.theType, str(PROP.Value))
        elif PROP.theType == "Vector" or PROP.theType == "Color":
            theValue = (str(PROP.More[0].Value)+"; "
                        +str(PROP.More[1].Value)+"; "
                        +str(PROP.More[2].Value)+"; "
                        +str(PROP.More[3].Value))
            final_params[PROP.theName] = (PROP.theType, theValue)
        elif PROP.theType in ("handle:ITexture", "handle:CTextureArray", "handle:CCubeTexture"):
            if PROP.Handles[0].DepotPath:
                file_path = PROP.Handles[0].DepotPath
                final_params[PROP.theName] = (PROP.theType, file_path)
        else:
            log.warning(f'Unsupported param type in CR2W "{PROP.theType}"')
    return final_params

def read_2wmi_params(
        w2mi_path: str
        ) -> Dict[str, str]:
    # Check if the .w2mi file references any textures or texarrays, and do the same there.
    # Load the .w2mi file.
    log.info("READING W2MI: " + w2mi_path) # FIX PATHS WITH SPACES bob_broken_woods_longpile

    extra = []
    #texture_paths = []
    #uncook_path_mats = get_uncook_path(bpy.context)
    full_path = repo_file(w2mi_path) #os.path.join(uncook_path_mats, w2mi_path)
    if os.path.exists(full_path):
        material_bin = CR2W_reader.load_material(full_path)[0]
        return read_2wmi_params2(material_bin)
    else:
        return {}

def guess_texture_type_by_link(mat: Material, img_node):
        socket_name = img_node.outputs[0].links[0].to_socket.name
        if socket_name == 'Base Color':
            return 'Diffuse'
        if socket_name == 'Color':	# Normal maps are connected to a Normal Map node's "Color" input.
            return 'Normal'
        else:
            log.info(f"Image {img_node.image.name} on material {mat.name} attaches to {socket_name}, yo!")
            return

def create_param(
            xml_data: Element
            ,name: str
            ,type: str
            ,value: str
        ) -> Element:
    """Create a parameter sub-Element in the xml_data Element."""
    new_param = ElementTree.SubElement(xml_data, 'param')
    new_param.set('name', _sanitize_xml_attr(name, "param"))
    new_param.set('type', _sanitize_xml_attr(type, "Float"))
    new_param.set('value', _sanitize_xml_attr(value))

    return new_param

def create_texture_param(
            xml_data: Element
            ,name: str
            ,tex_filepath: str
        ) -> Element:
    """Create a texture parameter sub-Element in the xml_data Element."""
    new_param = ElementTree.SubElement(xml_data, 'param')
    new_param.set('name', _sanitize_xml_attr(name, "Texture"))
    new_param.set('type', 'handle:ITexture')

    # The param's 'value' needs to be the texture path relative to the uncook folder.
    new_param.set('value', _sanitize_xml_attr(tex_filepath))

    return new_param

def is_file_referenced_in_xml(xml_data: ElementTree, search_file: str) -> bool:
    """Return whether any sub-Elements of an Element reference a given filename.
    The path to the file is ignored, only the filename (including extension) is compared.
    """
    for param in xml_data:
        par_type = param.get('type')
        par_value = param.get('value')
        if par_type != 'handle:ITexture' or par_value == 'NULL':
            continue

        filename = par_value.split("\\")[-1]
        if filename == search_file:
            # This parameter references a file with this name!
            return True

    # No parameters referenced the searched file.
    return False

def guess_shader_type(shader_type: str) -> str:
    """Guesssing the shader type. This is to simplify the set of shaders found in the game.
    Eg., the game has several hair and skin shaders, but we have no way to know the
    difference between these, so we just use a smaller number of shaders.
    """
    if 'sword_rune' in shader_type:
        return 'sword_final'
    if 'hair' in shader_type:
        return 'pbr_hair'
    if 'skin' in shader_type:
        return 'pbr_skin'
    if 'eye' in shader_type and "eyelashes" not in shader_type:
        return 'pbr_eye'
    if 'transparent_lit' in shader_type:
        return 'transparent_lit'
    if 'component__shadow' in shader_type:
        return 'pbr_eye_shadow'

    return 'pbr_std'

def init_material_nodes(material: Material, shader_type: str, clear:bool = True):
    """Wipe all nodes, then create a node group node and return it."""
    shader_mapping, fallback_ng_name, resource_path = get_shader_resources_for_material(material)
    ng_name = shader_mapping.get(shader_type)
    if not ng_name:
        log.debug(f"Unknown shader type: {shader_type} (Fell back to default)")
        ng_name = fallback_ng_name
    ng = ensure_node_group(ng_name, resource_path=resource_path)			# Nodegroup node tree  (bpy.types.ShaderNodeTree)
    node_ng = None							# Nodegroup group node (bpy.types.ShaderNodeGroup)
    assert ng, f"Node group {ng_name} not found. Resources didn't append correctly?"

    nodes = material.node_tree.nodes
    if clear:
        # Wipe nodes created by fbx importer.
        nodes.clear()

    # Create main node group node
    node_ng = nodes.new(type='ShaderNodeGroup')
    node_ng.node_tree = ng
    node_ng.label = shader_type

    node_ng.location = (500, 200)
    node_ng.width = 350

    return node_ng

def init_instance_nodes(material: Material, shader_type: str, clear:bool = True, x_loc:int = -250):
    """Wipe all nodes, then create a node group node and return it."""
    ng_name = material.name #SHADER_MAPPING.get(shader_type)
    ng = bpy.data.node_groups.new(ng_name, 'ShaderNodeTree')
    nodes = material.node_tree.nodes
    
    if clear:
        nodes.clear()

    # Create main node group node
    node_ng = nodes.new(type='ShaderNodeGroup')
    node_ng.node_tree = ng
    node_ng.label = ng_name

    node_ng.location = (x_loc, 200)
    node_ng.width = 350

    return node_ng

def nodes_create_outputs(material, nodes, links, node_ng, xml_data, xml_path):
    """Create and link up separate output nodes for Cycles and Eevee."""
    node_output_default = nodes.new(type='ShaderNodeOutputMaterial')
    node_output_default.location = (900, 200)
    node_output_default.name = xml_path[-60:]
    links.new(node_ng.outputs[0], node_output_default.inputs[0])

    if len(node_ng.outputs) == 1:
        return node_output_default

    node_output_default.target = 'CYCLES'

    node_output_eevee = nodes.new(type='ShaderNodeOutputMaterial')
    node_output_eevee.target = 'EEVEE'
    node_output_eevee.location = (900, 0)
    node_output_eevee.name = xml_path[-60:]
    links.new(node_ng.outputs[1], node_output_eevee.inputs[0])

def order_elements_by_attribute(
        elements: List[Element]
        ,order: List[str]
        ,attribute = 'name'
    ) -> List[Element]:
    """Return a list of Element objects ordered by the value of an
    attribute and an arbitrary order. Used to order nodes so that more
    useful input nodes are at the top of the node graph, and
    miscellanaea are at the bottom.
    """
    ordered = []
    unordered = elements[:]
    for name in order:
        for p in elements:
            if p.get('name') == name:
                ordered.append(p)
                if p in unordered:
                    unordered.remove(p)
    ordered.extend(unordered)
    return ordered

def mat_load_params_into_nodes(
        mat: Material
        ,ordered_params: List[Element]
        ,node_ng: Node
        ,uncook_path: str
    ):
    """Load parameters into nodes."""

    texarray_index = '0'
    for param1 in ordered_params:
        if param1.attrib['name'] == "Pattern_Index":
            texarray_index = param1.attrib['value']

    y_loc = 1000	# Y location of the next param node to spawn.
    for param in ordered_params:
        node = create_node_for_param(mat, param, node_ng, uncook_path, y_loc, texarray_index)
        if not node:
            continue
        if node.type == 'TEX_IMAGE':
            y_loc -= 320
        elif node.type == 'RGB':
            y_loc -= 220
        else:
            y_loc -= 170
        if param.get("witcher_include"):
            node.witcher_include = True


def fix_texture_node(par_name, node):
    if node and node.image:
        if par_name in ['Diffuse', 'SpecularTexture', 'SnowDiffuse','diffuse','diffusemap','diff', 'tex_Diffuse'] or 'DiffuseArray' in par_name:
            node.image.colorspace_settings.name = 'sRGB'
        else:
            node.image.colorspace_settings.name = 'Non-Color'
            
    if node and node.image and len(node.outputs[0].links) > 0:
        pin_name = node.outputs[0].links[0].to_socket.name
        if pin_name in ['Diffuse', 'SpecularTexture', 'SnowDiffuse','diffuse','diffusemap','diff', 'tex_Diffuse'] or 'DiffuseArray' in par_name:
            node.image.colorspace_settings.name = 'sRGB'
        else:
            node.image.colorspace_settings.name = 'Non-Color'
    return node

def node_tree_inputs_new(node_ng, par_type, par_name ):
    if bpy.app.version >= (4, 0, 0):
        node_ng.node_tree.interface.new_socket(name=par_name, in_out='INPUT', socket_type=par_type)
    else:
        node_ng.node_tree.inputs.new(par_type, par_name)

def create_node_for_param(
        mat: Material
        ,param: Element
        ,node_ng: Node
        ,uncook_path: str
        ,y_loc: int
        ,texarray_index: int = 0
    ) -> bpy.types.Node:
    """Create and hook up the nodes for a Witcher 3 shader parameter to the primary nodegroup."""
    links = mat.node_tree.links

    par_name = param.get('name')
    par_type = param.get('type')
    par_value = param.get('value')

    if 'debug' in par_value:
        return

    if par_value == 'NULL': #or par_name in IGNORED_PARAMS:
        return
    if par_name in IGNORED_PARAMS:
        log.debug('Skipping ignored param %s', par_name)
        return

    node_label = par_name
    node = None

    if par_type in ['handle:ITexture']:
        node = create_node_texture(mat, param, node_ng, y_loc, uncook_path, texarray_index)
        node = fix_texture_node(par_name, node)

    elif par_type == 'handle:CCubeTexture':
        node = create_node_cubemap(mat, param, node_ng, y_loc, uncook_path)

    elif par_type in ['handle:CTextureArray']:
        #create all the textures for this array
        #create the tex array and link it

        texture_array = []
        tex_index = 0
        
        texarray_path = os.path.abspath( uncook_path + os.sep + f"{par_value}.texture_{str(tex_index)}{get_tex_ext(bpy.context)}" )
        create_one = True
        
        while create_one or Path(texarray_path).exists():
            create_one = False
            sub_param = {
                'name' : f"{par_value}.texture_{str(tex_index)}{get_tex_ext(bpy.context)}",
                'value' :texarray_path
            }
            sub_node = create_node_texture(mat, sub_param, node_ng, y_loc, uncook_path, texarray_index)
            sub_node = fix_texture_node(par_name, sub_node)
            sub_node.location = (-800, y_loc)
            texture_array.append(sub_node)
            tex_index+=1
            texarray_path = os.path.abspath( uncook_path + os.sep + f"{par_value}.texture_{str(tex_index)}{get_tex_ext(bpy.context)}" )

        #full_path = os.path.join(get_uncook_path(bpy.context), par_value)
        #texarray_ = CR2W_reader.load_material(full_path)

        tex_array_group = create_texarray( ARRAY_SIZE = len(texture_array))
        TexArray_ng = mat.node_tree.nodes.new(type='ShaderNodeGroup')
        TexArray_ng.node_tree = tex_array_group
        TexArray_ng.location = (200, 0)
        
        for idx, sub_n in enumerate(texture_array):
            links.new(sub_n.outputs[0], TexArray_ng.inputs[idx])
        
        
        #node_ng.width = 350
        node = TexArray_ng

    elif par_type == 'Float':
        node = create_node_float(mat, param, node_ng)
    elif par_type == 'Color':
        node = create_node_color(mat, param, node_ng)
    elif par_type == 'Vector':
        (node, node_w) = create_node_vector(mat, param, node_ng, do_vec_4 = True)
    else:
        log.debug("Unhandled material parameter type: %s", par_type)
        node = create_node_attribute(mat, param, node_ng)
        node_label = "Unknown type: " + par_type

    if not node:
        return

    node.location = (-450, y_loc)
    node.name = par_name
    node.label = node_label

    # Linking the node to the nodegroup
    if par_name in EQUIVALENT_PARAMS:
        log.info(f"EQUIVALENT_PARAMS found {par_name} replacing {EQUIVALENT_PARAMS[par_name]}")
        #todo clone the material group and replace instead of changing param name?
        input_pin = node_ng.inputs.get(EQUIVALENT_PARAMS[par_name])
        if input_pin != None:
            
            if bpy.app.version >= (4, 0, 0):
                input_inner = node_ng.node_tree.interface.items_tree.get(EQUIVALENT_PARAMS[par_name])
            else:
                input_inner = node_ng.node_tree.inputs.get(EQUIVALENT_PARAMS[par_name])
            input_inner.name = par_name
    else:
        input_pin = node_ng.inputs.get(par_name)
    
    input_pin_vec_W = None
    if par_type == 'Vector':
        #create the W float node and pins
        input_pin_vec_W = node_ng.inputs.get(par_name+'_W')
        if input_pin_vec_W == None:
            node_tree_inputs_new( node_ng, 'NodeSocketFloat', par_name+'_W')
            input_pin_vec_W = node_ng.inputs.get(par_name+'_W')
        node_w.location = (-450, y_loc)
        node_w.name = par_name+'_W'
        node_w.label = node_label+'_W'

    #this will create the input pin on the shader node gorup if it doesn't exist. Idealy all shader pins would be defined. But some w2mi have values that don't exist on their shader
    #TODO check for same names but differnt types defined on instance vs shader.
    if input_pin == None:
        if par_type == "Color":
            node_tree_inputs_new( node_ng, 'NodeSocketColor', par_name)
        elif par_type == "Float":
            node_tree_inputs_new( node_ng, 'NodeSocketFloat', par_name)
        elif par_type == "handle:ITexture":
            node_tree_inputs_new( node_ng, 'NodeSocketColor', par_name)
        elif par_type == 'handle:CTextureArray':
            node_tree_inputs_new( node_ng, 'NodeSocketColor', par_name)
        elif par_type == 'Vector':
            node_tree_inputs_new( node_ng, 'NodeSocketVector', par_name)
        input_pin = node_ng.inputs.get(par_name)

    if input_pin and len(input_pin.links) == 0:
        # Only connect the node if some other node isn't already connected.
        # This is because if there are two diffuse textures defined, we are better off prioritizing
        # the first one.
        try:
            links.new(node.outputs[0], input_pin)
            if par_type == 'Vector':
                links.new(node_w.outputs[0], input_pin_vec_W)
            # Connect texture alpha to {par_name}_alpha if that input exists on the nodegroup
            if par_type in ['handle:ITexture'] and node.type == 'TEX_IMAGE':
                alpha_pin = node_ng.inputs.get(f"{par_name}_alpha")
                if alpha_pin and len(alpha_pin.links) == 0:
                    links.new(node.outputs[1], alpha_pin)
        except Exception as e:
            log.critical(f'PIN LINKING ERROR {e}')
    return node

def create_node_texture(
        mat: Material
        ,param: Element
        ,node_ng: Node
        ,y_loc: int
        ,uncook_path: str
        ,texarray_index: str = '0'
        ,using_node_tree:bool = False
    ):
    if using_node_tree:
        nodes = node_ng.nodes
        links = node_ng.links
    else:
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

    par_name = param.get('name')
    par_value = param.get('value')

    node = nodes.new(type="ShaderNodeTexImage")
    node.width = 300

    # Some texture types need special treatment.
    if par_name == 'Normal':
        # Roughness is stored in the alpha channel of Normal maps, so let's connect it.
        roughness_pin = node_ng.inputs.get('Roughness')
        if roughness_pin:
            links.new(node.outputs[1], roughness_pin)
    elif par_name == 'Diffuse':
        # Similarly, the alpha channel of the diffuse is of course used for transparency.
        alpha_pin = node_ng.inputs.get('Alpha')
        if alpha_pin and len(alpha_pin.links) == 0:
            links.new(node.outputs[1], alpha_pin)
    elif par_name in ['SpecularShiftTexture', 'SnowDiffuse', 'SnowNormal', 'Pattern_Array'] or \
            ('Normal' in par_name and 'Detail' in par_name):
        # DetailNormals need a Mapping node to apply the DetailScale and DetailRotation to.
        # Snow textures also need a Mapping node to apply the SnowTile value to.
        node_mapping = nodes.new(type='ShaderNodeMapping')
        node_mapping.location = (-600, y_loc-200)
        node_mapping.hide = True
        links.new(node_mapping.outputs[0], node.inputs[0])

        node_uv = nodes.new(type='ShaderNodeUVMap')
        node_uv.location = (node_mapping.location.x-200, node_mapping.location.y)
        node_uv.hide = True
        links.new(node_uv.outputs[0], node_mapping.inputs[0])
        
        # Set default X and Y scale values to the DetailTile value.
        # Value based on pbr_std_tint_mask_det.w2mg material graph TODO check
        if par_name == "Pattern_Array":
            node_mapping.name = "Pattern_Array_Mapping"
        node_mapping.inputs[3].default_value[0] = 5
        node_mapping.inputs[3].default_value[1] = 5

    if par_name == 'rune_normal':
        node_mapping = nodes.new(type='ShaderNodeMapping')
        node_mapping.location = (-600, y_loc-200)
        node_mapping.hide = True
        node_mapping.inputs[1].default_value[0] = 0.75  # X Location
        node_mapping.inputs[3].default_value[0] = 0.25  # X Scale
        links.new(node_mapping.outputs[0], node.inputs[0])

        node_uv = nodes.new(type='ShaderNodeUVMap')
        node_uv.uv_map = "SecondUV"
        node_uv.location = (node_mapping.location.x-200, node_mapping.location.y)
        node_uv.hide = True
        links.new(node_uv.outputs[0], node_mapping.inputs[0])

    if par_value.endswith('.texarray'):
        par_value = f"{par_value}.texture_{texarray_index}{get_tex_ext(bpy.context)}"
    else:
        FULL_REPO_NAME = repo_file(par_value)
    # We use os.path.abspath() to make sure the filepath has consistent slashes and backslashes,
    # so that we can compare image file paths to each other for duplicate checking.
    final_tex_path = par_value.replace(".xbm", get_tex_ext(bpy.context))
    try:
        final_texture = repo_file_mat(final_tex_path) # TODO fix loading texarray
        if not os.path.exists(win_safe_path(final_texture)):
            final_texture = uncook_path + os.sep + final_tex_path
    except Exception as e:
        #raise e
        log.critical(f"TEXTURE ERROR {e}")
        final_texture= None
    
    tex_path = os.path.abspath( final_texture )
    
            
    if not os.path.exists(win_safe_path(tex_path)) and '_proxy' in os.path.basename(tex_path):
        bundle_texture = repo_file(par_value) #os.path.join(get_uncook_path(bpy.context), par_value)
        bundle_texture_xbm = bundle_texture.rsplit('.', 1)[0] + '.xbm'
        tex_path = bundle_texture
    
    ## didn't find the texture, try find and convert xbm
    if not os.path.exists(win_safe_path(tex_path)):
        
        #check if texture and uncook path are different
        #if different change text_path to
        #dds textures should go to texture folders
        
        for ext in ['.tga','.dds', '.png']:
            if tex_path.endswith(ext):
                xbm_path = tex_path.replace(ext, ".xbm")
                dds_path = tex_path.replace(ext, ".dds") if ext != '.dds' else tex_path
                break
        #create dds if none exist
        if not os.path.exists(win_safe_path(dds_path)):
            if not os.path.exists(win_safe_path(xbm_path)):
                TEXTURE_PATH = get_texture_path(bpy.context)
                GAME_UNCOOK_PATH = get_uncook_path(bpy.context)
                same_root = os.path.normcase(os.path.normpath(TEXTURE_PATH)) == os.path.normcase(os.path.normpath(GAME_UNCOOK_PATH))
                if not same_root:
                    uncook_xbm_path = xbm_path.replace(TEXTURE_PATH, GAME_UNCOOK_PATH)
                    uncook_dds_path = dds_path.replace(TEXTURE_PATH, GAME_UNCOOK_PATH)
                    if os.path.exists(win_safe_path(uncook_dds_path)) or os.path.exists(win_safe_path(uncook_xbm_path)):
                        xbm_path = uncook_xbm_path
                        dds_path = uncook_dds_path
                    else:
                        # Last fallback: repo_file() will use bundle fallback extraction in uncook for textures.
                        bundle_xbm_path = repo_file(par_value)
                        if bundle_xbm_path and os.path.exists(win_safe_path(bundle_xbm_path)):
                            xbm_path = bundle_xbm_path
                            dds_path = os.path.splitext(bundle_xbm_path)[0] + ".dds"
                else:
                    xbm_path = xbm_path.replace(TEXTURE_PATH, GAME_UNCOOK_PATH)
                    dds_path = dds_path.replace(TEXTURE_PATH, GAME_UNCOOK_PATH)
            if os.path.exists(win_safe_path(dds_path)):
                tex_path = dds_path
            elif os.path.exists(win_safe_path(xbm_path)):
                try:
                    convert_xbm_to_dds(xbm_path)
                except Exception as e:
                    log.warning("Failed to convert xbm_to_dds")
                if os.path.exists(win_safe_path(dds_path)):
                    tex_path = dds_path


        else:
            tex_path = dds_path

    
    node.image = load_texture(mat, tex_path, uncook_path)
    if not node.image:
        node.label = "MISSING:" + par_value

    return node


def create_node_cubemap(
        mat: Material
        ,param: Element
        ,node_ng: Node
        ,y_loc: int
        ,uncook_path: str
    ):
    """Create a ShaderNodeTexEnvironment for a handle:CCubeTexture parameter.

    Converts the .w2cube file to a cubemap DDS, loads it into an
    Environment Texture node, and connects it to the node group input.
    """
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links

    par_name = param.get('name')
    par_value = param.get('value')

    # Resolve the .w2cube file path
    w2cube_path = repo_file(par_value) if par_value and not os.path.isabs(par_value) else par_value
    if not w2cube_path or not os.path.exists(win_safe_path(w2cube_path)):
        w2cube_path = os.path.join(uncook_path, par_value) if par_value else None

    dds_path = None
    loaded_img = None
    if w2cube_path and os.path.exists(win_safe_path(w2cube_path)):
        try:
            if par_name == 'BlickCube':
                loaded_img, dds_path = load_w2cube_blick_equirect_image(w2cube_path, colorspace='sRGB')
            else:
                loaded_img, dds_path = load_w2cube_image(w2cube_path, colorspace='sRGB')
        except Exception as e:
            log.warning(f"Failed to convert w2cube '{par_value}': {e}")

    # Environment Texture node (Blender's cubemap-aware image node)
    node = nodes.new(type="ShaderNodeTexEnvironment")
    node.width = 300
    node.label = par_name
    if loaded_img:
        node.image = loaded_img

    if not node.image and dds_path and os.path.exists(win_safe_path(dds_path)):
        try:
            img = bpy_image_load_safe(dds_path, check_existing=True)
            if img:
                img.colorspace_settings.name = 'sRGB'
                node.image = img
                if par_name == 'BlickCube':
                    log.warning("BlickCube equirect build failed, falling back to DDS cubemap image: %s", dds_path)
        except Exception as e:
            log.warning(f"Failed to load cubemap DDS '{dds_path}': {e}")
    if not node.image:
        node.label = "MISSING:" + par_value

    # Add a CubeMap input to the node group if it doesn't already have one
    if not node_ng.inputs.get(par_name):
        node_tree_inputs_new(node_ng, 'NodeSocketColor', par_name)

    input_pin = node_ng.inputs.get(par_name)
    if input_pin:
        links.new(node.outputs[0], input_pin)

    return node


def setup_eye_reflection_nodes(material: Material, nodegroup_node: Node, nodes, links):
    """Configure the pbr_eye Blick cubemap sampler without extra emission/fresnel nodes."""
    del material  # Kept for call-site compatibility.

    def find_blick_env_node():
        blick_input = nodegroup_node.inputs.get('BlickCube')
        if blick_input and blick_input.is_linked:
            for link in blick_input.links:
                if link.from_node and link.from_node.type == 'TEX_ENVIRONMENT':
                    return link.from_node

        node = nodes.get('BlickCube')
        if node and node.type == 'TEX_ENVIRONMENT':
            return node

        for node in nodes:
            if node.type != 'TEX_ENVIRONMENT':
                continue
            if node.name == 'BlickCube' or node.label == 'BlickCube':
                return node
        return None

    env_node = find_blick_env_node()
    if not env_node:
        return

    # Ensure the cubemap sampler feeds the eye group's BlickCube input.
    blick_input = nodegroup_node.inputs.get('BlickCube')
    if blick_input:
        if blick_input.is_linked:
            current_link = blick_input.links[0]
            if current_link.from_node != env_node:
                links.remove(current_link)
                links.new(env_node.outputs[0], blick_input)
        else:
            links.new(env_node.outputs[0], blick_input)

    # Reuse nodes if import is rerun on the same material.
    geo_node = nodes.get('eye_blick_geometry')
    if not geo_node or geo_node.type != 'NEW_GEOMETRY':
        geo_node = nodes.new(type='ShaderNodeNewGeometry')
        geo_node.name = 'eye_blick_geometry'
    geo_node.label = 'Surface Normal'

    vt_node = nodes.get('eye_blick_fetch_vector')
    if not vt_node or vt_node.type != 'VECT_TRANSFORM':
        vt_node = nodes.new(type='ShaderNodeVectorTransform')
        vt_node.name = 'eye_blick_fetch_vector'
    vt_node.label = 'BlickFetchVector (World->Camera)'
    vt_node.vector_type = 'NORMAL'
    vt_node.convert_from = 'WORLD'
    vt_node.convert_to = 'CAMERA'

    map_node = nodes.get('eye_blick_orientation')
    if not map_node or map_node.type != 'MAPPING':
        map_node = nodes.new(type='ShaderNodeMapping')
        map_node.name = 'eye_blick_orientation'
    map_node.label = 'Cubemap Orientation'
    map_node.vector_type = 'POINT'
    map_node.inputs['Location'].default_value = (0.0, 0.0, 0.0)
    map_node.inputs['Rotation'].default_value = (radians(105.0), radians(0.0), radians(-65.0))
    map_node.inputs['Scale'].default_value = (-1.1, 1.1, 0.5)

    env_node.label = 'Cube Sampler (BlickCube)'
    try:
        env_node.interpolation = 'Linear'
    except Exception:
        pass
    is_blick_equirect = False
    if getattr(env_node, 'image', None):
        try:
            is_blick_equirect = bool(env_node.image.get("witcher_blick_equirect_dds"))
        except Exception:
            is_blick_equirect = False
        if not is_blick_equirect:
            is_blick_equirect = "BlickCubemap_Equirect" in getattr(env_node.image, "name", "")
    if is_blick_equirect:
        try:
            env_node.projection = 'EQUIRECTANGULAR'
        except Exception:
            pass

    y = env_node.location.y
    geo_node.location = (env_node.location.x - 840, y)
    vt_node.location = (env_node.location.x - 560, y)
    map_node.location = (env_node.location.x - 280, y)

    env_vector_input = env_node.inputs.get('Vector')
    if not env_vector_input:
        return

    while env_vector_input.is_linked:
        links.remove(env_vector_input.links[0])

    links.new(geo_node.outputs['Normal'], vt_node.inputs['Vector'])
    links.new(vt_node.outputs['Vector'], map_node.inputs['Vector'])
    links.new(map_node.outputs['Vector'], env_vector_input)


def load_texture(
        mat: Material
        ,tex_path: str
        ,uncook_path: str
    ) -> Image:
    tex_path = win_unprefix_path(tex_path)
    img_filename = os.path.basename(tex_path)	# Filename with extension.

    # Check if an image with this filepath is already loaded.
    img = None
    tex_key = win_path_key(tex_path)
    for i in bpy.data.images:
        #if bpy.path.basename(i.filepath) == img_filename:
        if win_path_key(i.filepath) == tex_key:
            img = i
            break
    # Check if the file exists
    if not img and not os.path.isfile(win_safe_path(tex_path)):
        log.info("Image not found: " + tex_path + " (Usually unimportant)")

        img = bpy.data.images.new(img_filename, width=1024, height=1024)
        img.filepath = tex_path
        img.source = 'FILE'
        #return
    elif not img:
        img = bpy_image_load_safe(tex_path, check_existing=True)

    # Correct the image name.
    filepath = img.filepath.replace(os.sep, "/")
    filename = filepath.split("/")[-1]
    file_parts = filename.split(".")
    img_name = file_parts[0]
    # if 'texarray' in filepath:
    #     # Add the texture number at the end.
    #     end = file_parts[-2]
    #     img_name += end.split("texture")[1]
    img.name = img_name

    if tex_path.lower().endswith('.dds'):
        img.alpha_mode = 'CHANNEL_PACKED'

    return img

def create_node_float(mat, param, node_ng):
    nodes = mat.node_tree.nodes
    par_name = param.get('name')
    par_value = param.get('value')

    # if 'Rotation' in par_name:
    #     normal_node = nodes.get(par_name.replace('Rotation', 'Normal'))
    #     if normal_node != None:
    #         mapping_node = normal_node.inputs[0].links[0].from_node
    #         # Set Z rotation
    #         mapping_node.inputs[1].default_value[2] = float(par_value)
    #         return
    node = nodes.new(type='ShaderNodeValue')
    node.outputs[0].default_value = float(par_value)

    return node

def create_node_color(mat, param, node_ng):
    nodes = mat.node_tree.nodes
    par_value = param.get('value')

    values = [float(f) for f in par_value.split("; ")]
    node = nodes.new(type='ShaderNodeRGB')
    node.outputs[0].default_value = (
        values[0] / 255
        ,values[1] / 255
        ,values[2] / 255
        ,values[3] / 255
    )

    return node

def create_node_vector(mat, param, node_ng, do_vec_4 = False):
    nodes = mat.node_tree.nodes
    par_name = param.get('name')
    par_value = param.get('value')

    values = [float(f) for f in par_value.split("; ")]
    
    def assign_uv_scale_values(mat, target_node):
        if not target_node:
            return
        if len(target_node.inputs[0].links) > 0:
            mapping_node = target_node.inputs[0].links[0].from_node
            if mapping_node.type == 'MAPPING':
                # Set X and Y scale values to the DetailTile value.
                mapping_node.inputs[3].default_value[0] = values[0]
                mapping_node.inputs[3].default_value[1] = values[1]
            else:
                log.warning(f"Expected a mapping node for {par_name}, got {mapping_node.type} instead!")
                return
            mapping_node.label = mapping_node.name = par_name
        else:
            log.warning(f"Warning: Node {target_node.name} in material {mat.name} was expected to have a Mapping node plugged into it!")

    # Handling UV scale/tile nodes params
    if 'Tile' in par_name:
        for name in ['Diffuse', 'Normal']:
            target_node = nodes.get(par_name.replace('Tile', name))
            assign_uv_scale_values(mat, target_node)
    elif par_name == 'SpecularShiftUVScale':
        target_node = nodes.get('SpecularShiftTexture')
        assign_uv_scale_values(mat, target_node)
        #return
    # if values[3] != 1 and values[3] != 0:
    # 	The 4th value on vectors is probably always useless.
    # 	log.warning("Warning: Discarded vector 4th value: " + str(values) + " in parameter: " + par_name)

    node = nodes.new(type='ShaderNodeCombineXYZ')
    node.inputs[0].default_value = values[0]
    node.inputs[1].default_value = values[1]
    node.inputs[2].default_value = values[2]

    if do_vec_4:
        node_w = nodes.new(type='ShaderNodeValue')
        node_w.outputs[0].default_value = float(values[3])
        return node, node_w
    else:
        return node

def create_node_attribute(mat, param, node_ng):
    nodes = mat.node_tree.nodes
    par_value = param.get('value')

    node = nodes.new(type="ShaderNodeAttribute")
    node.attribute_name = par_value

    return node

def mat_ensure_dummy_transparent_img_node(material, node_ng, shader_type, nodes):
    """If the material doesn't have a diffuse texture, but has a shader that supports transparency
    (likely glass or water), let's add a transparent image node, to make the material appear nicer
    in textured viewport.
    """
    if node_ng.node_tree.name not in ['Witcher3_Glass', 'Invisible']:
        # If this isn't a material that should be fully transparent, do nothing.
        return
    if node_ng and len(node_ng.inputs) > 0 and len(node_ng.inputs[0].links) > 0:
        # If there is already a diffuse texture, do nothing.
        return

    transp_img = bpy.data.images.get('Transparent')
    if not transp_img:
        # Create the transparent image for the first time.
        bpy.ops.image.new(name="Transparent", width=64, height=64, color=(0, 0, 0, 0), alpha=True)
        transp_img = bpy.data.images['Transparent']

    node = nodes.new(type='ShaderNodeTexImage')
    node.image = transp_img
    node.width = 300
    node.location = (-600, 1000+320)
    nodes.active = node

def mat_set_name_by_diffuse(mat, node_ng, nodes):
    """Set the material's name to the name of the diffuse texture.
    Also set the diffuse texture's node as the active node, for Textured Viewport shading.
    """

    if node_ng.node_tree.name == 'Invisible':
        mat.name = 'Invisible'
        return

    named = False
    for inp in node_ng.inputs:
        if len(inp.links) == 0:
            continue
        from_node = inp.links[0].from_node
        if from_node.type == 'TEX_IMAGE' and from_node.image:
            img_name = from_node.image.name
            if img_name.endswith("_d0") or img_name.endswith("_n0"):
                mat.name = img_name[:-3]
            elif img_name.endswith("_d") or img_name.endswith("_n"):
                mat.name = img_name[:-2]
            else:
                mat.name = img_name
            nodes.active = from_node
            named = True
            break
    if not named:
        # mat.name = "!3 No Texture"
        pass

def mat_apply_settings(mat, shader_type: str):
    """Setting material viewport settings."""
    mat.metallic = 0
    mat.roughness = 0.5
    mat.diffuse_color = (0.3, 0.3, 0.3, 1)
    # blend_method, show_transparent_back, use_screen_refraction, use_sss_translucency
    # were removed in Blender 4.2 (EEVEE Next). Only set them on older versions.
    _has_blend = hasattr(mat, 'blend_method')
    if shader_type == 'pbr_eye_shadow':
        if _has_blend:
            mat.blend_method = 'BLEND'
            mat.show_transparent_back = False
            mat.use_screen_refraction = True
            mat.use_sss_translucency = True
        set_shadow_method(mat)
    elif shader_type == 'pbr_eye':
        if _has_blend:
            mat.use_screen_refraction = True
    elif shader_type == 'transparent_lit':
        if _has_blend:
            mat.blend_method = 'BLEND'
            mat.show_transparent_back = False
            mat.use_screen_refraction = True
            mat.use_sss_translucency = True
        set_shadow_method(mat)
    else:
        if _has_blend:
            mat.blend_method = 'CLIP'




def create_texarray(group_name = "WitcherTexArray", ARRAY_SIZE = 2):
    vertex_color_data = []
    obj = bpy.context.active_object
    me = obj.data
    highest_green = 0
    if obj.type == "MESH":
        for vert in me.vertices:
            if me.color_attributes.active:
                elem = me.color_attributes.active.data[vert.index]
                color = elem.color if hasattr(elem, 'color') else elem.vector
                vertex_color_data.append(list(color))
                highest_green = max(highest_green, color[1])

    # # Check if group already exists
    # if group_name in bpy.data.node_groups:
    #     group = bpy.data.node_groups[group_name]
    #     group.nodes.clear()
    #     group.inputs.clear()
    # else:
    #     # Create a new node group
    group = bpy.data.node_groups.new(group_name, 'ShaderNodeTree')
    
    output = group.nodes.new('NodeGroupOutput')
    output.location = (700, 0)
    
    
    if bpy.app.version >= (4, 0, 0):
        group.interface.new_socket(name="Output", in_out='OUTPUT', socket_type='NodeSocketColor')
    else:
        group.outputs.new('NodeSocketColor','Output')
    # Create a single input with two sockets
    input = group.nodes.new('NodeGroupInput')
    input.name = 'Array'
    input.location = (-400, 0)
    
    try:
        array_step = highest_green/ARRAY_SIZE
    except Exception as e:
        log.critical('ERROR CREATING TEXTURE ARRAY')
        return group


    for index in range(0,ARRAY_SIZE):
        this_index = index

        if bpy.app.version >= (4, 0, 0):
            group.interface.new_socket(name=f"Array_{str(this_index)}", in_out='INPUT', socket_type='NodeSocketColor')
        else:
            group.inputs.new('NodeSocketColor', f"Array_{str(this_index)}")


    #create the first mix
    mix = _new_mix_color_node(group)
    _mix_fac_input(mix).default_value = 0.5
    mix.location = (0, -100)

    privious_mix = mix

    if ARRAY_SIZE > 1:
        mix_a, mix_b = _mix_color_inputs(mix)
        group.links.new(input.outputs[0], mix_a)
        group.links.new(input.outputs[1], mix_b)
    # for i in range(ARRAY_SIZE):
    #     group.links.new(input.outputs[i], mix.inputs[i+1])

    group.links.new(_mix_color_output(mix), output.inputs[0])

    if bpy.app.version >= (4, 0, 0):
        color_attr = group.nodes.new('ShaderNodeAttribute')
        color_attr.attribute_type = 'GEOMETRY'
        color_attr.attribute_name = "Color"
    else:
        color_attr = group.nodes.new('ShaderNodeVertexColor')
        color_attr.layer_name = "Color"
    color_attr.location = (-300, 400)
    
    color_ramp = group.nodes.new('ShaderNodeValToRGB')
    #color_ramp.color_ramp.elements[1].position = array_step/1.2
    color_ramp.color_ramp.elements[1].position = array_step
    color_ramp.location = (200, 400)

    separate_color = group.nodes.new('ShaderNodeSeparateColor')
    separate_color.location = (0, 400)


    group.links.new(color_attr.outputs[0], separate_color.inputs[0])
    group.links.new(separate_color.outputs[1], color_ramp.inputs[0])
    group.links.new(color_ramp.outputs[0], _mix_fac_input(mix))

    for i in range(ARRAY_SIZE-2):
        i+=1
        color_ramp = group.nodes.new('ShaderNodeValToRGB')
        #color_ramp.color_ramp.elements[1].position = (array_step*(i+1))/1.2
        color_ramp.color_ramp.elements[1].position = array_step*(i+1)
        color_ramp.location = (-200, -400 * i)

        mix = _new_mix_color_node(group)
        _mix_fac_input(mix).default_value = 0.5
        mix.location = (200, -400 * i )
        mix_a, mix_b = _mix_color_inputs(mix)
        group.links.new(color_ramp.outputs[0], _mix_fac_input(mix))
        group.links.new(_mix_color_output(privious_mix), mix_a)
        group.links.new(input.outputs[i+1], mix_b)
        group.links.new(separate_color.outputs[1], color_ramp.inputs[0])
        group.links.new(_mix_color_output(mix), output.inputs[0])

        privious_mix = mix

    
    
    return group

def set_shadow_method(mat):
    render_engine = bpy.context.scene.render.engine

    if render_engine in ('BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT'):
        if hasattr(mat, 'blend_method'):
            mat.blend_method = 'HASHED'
    elif render_engine == 'CYCLES':
        if hasattr(mat, 'use_transparent_shadow'):
            mat.use_transparent_shadow = True
