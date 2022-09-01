# Modified w3_material.py from Mets3D orignal
# https://github.com/Mets3D/batch_import_witcher3_fbx

import logging
## for file logging
logging.basicConfig(level=logging.CRITICAL,
        format='%(asctime)s %(levelname)s %(threadName)-10s %(message)s',)


from pathlib import Path
from . import CR2W
import bpy, os, filecmp, shutil
from typing import List, Tuple, Dict
from bpy.types import Image, Material, Object, Node
import re

from xml.etree import ElementTree
Element = ElementTree.Element

from .w3_material_constants import *

from io_import_w2l import get_modded_texture_path

def repo_file(filepath: str):
    if filepath.endswith('.tga'):
        return os.path.join(get_modded_texture_path(bpy.context), filepath)

def load_texture_table() -> Dict[str, List[str]]:
    """Load the texture table from the file."""

    tex_table_file = os.path.abspath(__file__).replace("w3_material.py", "texture_table.txt")
    tex_table = {}
    with open(tex_table_file, 'r') as f:
        tex_table = eval(f.read())
    return tex_table

def make_texture_table(uncook_path) -> Dict[str, List[str]]:
    """Sometimes the .fbx importer imports textures with insane filepaths.
    For such cases, we create a dictionary mapping each texture filename to its full path.

    Use this function to generate the texture table that will be loaded by load_texture_table().
    """
    tex_table = {}

    for subdir, dirs, files in os.walk(uncook_path):
        for f in files:
            if not f.endswith(".tga"):
                continue
            full_path = os.path.join(subdir, f)
            rel_path = full_path.replace(uncook_path, "")
            if '.texarray.' in f:
                # Materials will refer to texarray textures without their
                # extension or texture number.
                # We have no way to guess the texture number atm.
                f = ".".join(f.split(".")[:-2])

            if f in tex_table:
                skip = False
                for saved_path in tex_table[f]:
                    other_file = os.path.join(uncook_path, saved_path)
                    if filecmp.cmp(full_path, other_file, shallow=False):
                        # Another exact copy of this file is already in the table, skip it.
                        skip = True
                        break
                if skip:
                    continue

                # There are some textures that are named identically but have unique content.
                tex_table[f].append(rel_path)
            else:
                tex_table[f] = [rel_path]

    # Write texture map to a file in the addon.
    tex_table_file = os.path.abspath(__file__).replace("w3_material.py", "texture_table.txt")
    with open(tex_table_file, 'w') as f:
        f.write(str(tex_table))

    return tex_table

def hide_unused_sockets(node, inp=True, out=True):
    if inp:
        for socket in node.inputs:
            socket.hide = True		# Blender will prevent it if it's used, no need for us to check.
    if out:
        for socket in node.outputs:
            socket.hide = True

def ensure_node_group(ng_name):
    """Check if a nodegroup exists, and if not, append it from the addon's resource file."""

    if ng_name not in bpy.data.node_groups:
        with bpy.data.libraries.load(RES_PATH) as (data_from, data_to):
            for ng in data_from.node_groups:
                if ng == ng_name:
                    data_to.node_groups.append(ng)

    ng = bpy.data.node_groups[ng_name]
    ng.use_fake_user = False

    return ng

def load_w3_materials(
        obj: Object
        ,uncook_path: str
        ,tex_table: Dict[str, List[str]]
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
                    logging.info("No material name? " + obj.name)
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
                finished_mat = setup_w3_material(uncook_path, tex_table, target_mat, xml_data, xml_path, force_update=force_mat_update)
                obj.material_slots[target_mat.name].material = finished_mat

def readXML(xml_path) -> Element:
    """Read Witcher 3 material info read from an .xml file, and return the root Element."""
    try:
        with open(xml_path, 'r') as myFile:
            # XXX: Parsing the file directly doesn't work due to a bug in ElementTree
            # that rejects UTF-16, so we have to use fromstring().
            data = myFile.read()
    except:
        with open(xml_path, 'r', encoding='utf-16-le') as myFile:
            # XXX: Parsing the file directly doesn't work due to a bug in ElementTree
            # that rejects UTF-16, so we have to use fromstring().
            data = myFile.read()
    return ElementTree.fromstring(data)

def setup_w3_material(
        uncook_path: str
        ,tex_table: Dict[str, List[str]]
        ,material: Material
        ,xml_data: Element
        ,xml_path: str
        ,force_update = False	# Set to True when re-importing stuff to test changes with the latest material set-up code.
        ):
    # Checks for duplicate materials
    # Saves XML data in custom properties
    # Creates nodes
    # Loads images

    mat_base = xml_data.get('base')		# Path to the .w2mg or .w2mi file.
    if not mat_base:
        # Never seen this happen, but just in case.
        logging.info("No material base, skipping: " + material.name)
        return

    params = {}
    for p in xml_data:
        params[p.get('name')] = p.get('value')

    shader_type = mat_base.split("\\")[-1][:-5]	# The .w2mg or .w2mi file, minus the extension.

    nodes = material.node_tree.nodes
    links = material.node_tree.links

    if mat_base.endswith(".w2mi"):
        # The XML contains little to no info about material instances, but the FBX importer
        # imported some image nodes we can use.
        shader_type = guess_shader_type(shader_type)
        w2mi_path = xml_data.get('base')
        #w2mi_tex_params = read_2wmi_params(material, uncook_path, w2mi_path, shader_type)
        w2mi_params = read_2wmi_params(material, uncook_path, w2mi_path, shader_type)

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

    #logging.warning(ElementTree.tostring(xml_data, encoding='utf8', method='xml'))
    all_children2 = list(xml_data.iter())
    # Clean existing nodes and create core nodegroup.
    nodegroup_node = init_material_nodes(material, shader_type)
    nodegroup_node.name = mat_base[-60:]
    nodes_create_outputs(material, nodes, links, nodegroup_node, xml_data, xml_path)

    # Order parameters so input nodes get created in a specified order, from top to bottom relative to the inputs of the nodegroup.
    # Purely for neatness of the node noodles.
    ordered_params = order_elements_by_attribute(xml_data, PARAM_ORDER, 'name')

    mat_load_params_into_nodes(material, tex_table, ordered_params, nodegroup_node, uncook_path)
    hide_unused_sockets(nodegroup_node)

    if existing_mat and force_update:
        existing_mat.user_remap(material)

    mat_set_name_by_diffuse(material, nodegroup_node, nodes)
    mat_ensure_dummy_transparent_img_node(material, nodegroup_node, shader_type, nodes)
    mat_apply_settings(material, shader_type)


    #\w3_uncook\FBXs\characters\models\main_npc\cerys\model\i_02_wa__cerys.fbx
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
        mat: Material
        ,uncook_path: str
        ,material_bin: str
        ,shader_type: str
        ) -> Dict[str, str]:
    final_params: Dict[str, str] = {}	# texture filepath : texture type
    material = material_bin
    baseMaterial = material.GetVariableByName('baseMaterial')
    if baseMaterial:
        handle = baseMaterial.Handles[0]
        if baseMaterial.theType == "handle:IMaterial" and handle.ClassName == "CMaterialInstance":
            more_tex_params = read_2wmi_params(mat, uncook_path, handle.DepotPath, shader_type)
            #TODO THESE PARAMS SHOULD NOT OVERRIDE EXISTING PARAMS
            #TODO NEED TO COMPARE and replace PROPs
            final_params.update(more_tex_params)
    for mat_param in material.InstanceParameters.elements:
        PROP = mat_param.PROP
        if PROP.theType == "Float":
            final_params[PROP.theName] = (PROP.theType, str(PROP.Value))
        elif PROP.theType == "Vector" or PROP.theType == "Color":
            theValue = (str(PROP.More[0].Value)+"; "
                        +str(PROP.More[1].Value)+"; "
                        +str(PROP.More[2].Value)+"; "
                        +str(PROP.More[3].Value))
            final_params[PROP.theName] = (PROP.theType, theValue)
        elif PROP.theType == "handle:ITexture":
                # ,name = param[0]
                # ,type = param[1]
                # ,value = param[2]
            if PROP.Handles[0].DepotPath:
                file_path = PROP.Handles[0].DepotPath
                file_path = file_path.replace(".xbm", ".tga")
                #texture_paths.append(file_path)
                final_params[PROP.theName] = (PROP.theType, file_path)
    return final_params



def read_2wmi_params(
        mat: Material
        ,uncook_path: str
        ,w2mi_path: str
        ,shader_type: str
        ) -> Dict[str, str]:
    """
    """


    # Check if the .w2mi file references any textures or texarrays, and do the same there.
    # Load the .w2mi file.
    logging.info("READING W2MI: " + w2mi_path) # FIX PATHS WITH SPACES bob_broken_woods_longpile

    extra = []
    #texture_paths = []
    full_path = os.path.join(uncook_path, w2mi_path)
    material = CR2W.CR2W_reader.load_material(full_path)[0]

    return read_2wmi_params2(mat,uncook_path,material,shader_type)
    if False:
        with open(full_path, 'rb') as f:
            # We read the file as bytes to avoid decoding errors,
            # since .w2mi files are binary files with some readable texture paths in them.
            content = str(f.read()).replace("\\x00", " ") # Look, I don't need a full on decode here, okay?
            
            parts = []
            parts = re.split(r" |\.", content)
            texture_paths = []
            for i, part in enumerate(parts):
                if part in ["w2mi", "w2mg", "texarray", "xbm"]:
                    file_path = parts[i-1] + "." + parts[i]
                    if parts[i-2].startswith("dlc") and parts[i-2].endswith("lower") and file_path.startswith("city"): # fix "lower city" folder with space
                        file_path = parts[i-2] +" "+ file_path
                    if part == "w2mi":
                        # Recurse and add the textures of this w2mi to our dict as well.
                        more_tex_params = read_2wmi_params(mat, uncook_path, file_path, shader_type)
                        texture_params.update(more_tex_params)
                    else:
                        file_path = file_path.replace(".xbm", ".tga")
                        texture_paths.append(file_path)
                    logging.info(file_path)

            # texture_paths = [p.replace("\\\\", "\\").replace(".xbm", ".tga") for p in parts if p.endswith(".xbm") or p.endswith(".texarray")]

            normal = ""
            for tex_filepath in texture_paths[:]:
                par_name = guess_texture_type_by_filename(tex_filepath, shader_type)
                texture_params[tex_filepath] = par_name
                if par_name == 'Normal':
                    normal = tex_filepath

            unknown_textures = [tex for tex, typ in texture_params.items() if typ=='Unknown']
            if len(unknown_textures) > 0 and 'Diffuse' not in texture_params.values():
                # If there is no diffuse texture but there are still unknown type textures,

                if normal:
                    # If there is a normal map, try to check for similarly named textures to find a diffuse.
                    for unknown in unknown_textures:
                        diffuse_possibilities = [
                            normal.replace("_n", "")
                            ,normal.replace("_n0", "")
                            ,normal.replace("_n", "_d")
                        ]
                        if unknown in diffuse_possibilities:
                            texture_params[unknown] = 'Diffuse'
                            unknown_textures.remove(unknown)
                            break

            if len(unknown_textures) > 0 and 'Diffuse' not in texture_params.values():
                # If we still don't have a diffuse, let's see if there's any unknown textures that aren't tileable.
                # "rune" textures are for the dozens of swords in the game, and they are actually normal maps.
                for tex in unknown_textures:
                    if 'tileable' not in tex and 'rune' not in tex:
                        texture_params[tex] = 'Diffuse'
                        unknown_textures.remove(tex)
                        break

            if len(unknown_textures) > 0 and 'Diffuse' not in texture_params.values():
                # As a last resort, assign the first unknown as the diffuse and hope for the best.
                texture_params[unknown_textures[0]] = 'Diffuse'
    return texture_params

def guess_texture_type_by_link(mat: Material, img_node):
        socket_name = img_node.outputs[0].links[0].to_socket.name
        if socket_name == 'Base Color':
            return 'Diffuse'
        if socket_name == 'Color':	# Normal maps are connected to a Normal Map node's "Color" input.
            return 'Normal'
        else:
            logging.info(f"Image {img_node.image.name} on material {mat.name} attaches to {socket_name}, yo!")
            return

def create_param(
            xml_data: Element
            ,name: str
            ,type: str
            ,value: str
        ) -> Element:
    """Create a parameter sub-Element in the xml_data Element."""
    new_param = ElementTree.SubElement(xml_data, 'param')
    new_param.set('name', name)
    new_param.set('type', type)
    new_param.set('value', value)

    return new_param

def create_texture_param(
            xml_data: Element
            ,name: str
            ,tex_filepath: str
        ) -> Element:
    """Create a texture parameter sub-Element in the xml_data Element."""
    new_param = ElementTree.SubElement(xml_data, 'param')
    new_param.set('name', name)
    new_param.set('type', 'handle:ITexture')

    # The param's 'value' needs to be the texture path relative to the uncook folder.
    new_param.set('value', tex_filepath)

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

def guess_texture_type_by_filename(filename: str, shader_type: str) -> str:
    """
    By the textures' naming conventions, there are two places
    in the texture name that can tell us what type of texture it is:
    some_texture_d.xbm	"d" is the 5th character from the back
    some_texture_d01.xbm	"d" is the 7th character from the back
    """

    if '_d0' in filename or '_d.' in filename or '_diff' in filename or '_alpha' in filename or 'plain_wood_old' in filename:
        return 'Diffuse'
    if '_n0' in filename or '_n.' in filename or '_normal' in filename:
        return 'Normal'
    if '_s.' in filename:
        return 'SpecularTexture'
    if '_h.' in filename:
        return 'Height'
    if '_a0' in filename in filename:
        if shader_type == 'pbr_skin':
            return 'Ambient'
        else:
            return 'TintMask'

    return 'Unknown'

def guess_shader_type(shader_type: str) -> str:
    """Guesssing the shader type. This is to simplify the set of shaders found in the game.
    Eg., the game has several hair and skin shaders, but we have no way to know the
    difference between these, so we just use a smaller number of shaders.
    """
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

def init_material_nodes(material: Material, shader_type: str):
    """Wipe all nodes, then create a node group node and return it."""
    ng_name = SHADER_MAPPING.get(shader_type)
    if not ng_name:
        logging.warning(f"Unknown shader type: {shader_type} (Fell back to default)")
        ng_name = 'Witcher3_Main'
    ng = ensure_node_group(ng_name)			# Nodegroup node tree  (bpy.types.ShaderNodeTree)
    node_ng = None							# Nodegroup group node (bpy.types.ShaderNodeGroup)
    assert ng, f"Node group {ng_name} not found. Resources didn't append correctly?"

    nodes = material.node_tree.nodes
    # Wipe nodes created by fbx importer.
    nodes.clear()

    # Create main node group node
    node_ng = nodes.new(type='ShaderNodeGroup')
    node_ng.node_tree = ng
    node_ng.label = shader_type

    node_ng.location = (500, 200)
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
        ,tex_table: Dict[str, List[str]]
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
        node = create_node_for_param(mat, param, node_ng, tex_table, uncook_path, y_loc, texarray_index)
        if not node:
            continue
        if node.type == 'TEX_IMAGE':
            y_loc -= 320
        elif node.type == 'RGB':
            y_loc -= 220
        else:
            y_loc -= 170

def create_node_for_param(
        mat: Material
        ,param: Element
        ,node_ng: Node
        ,tex_table: Dict[str, List[str]]
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

    if par_value == 'NULL' or par_name in IGNORED_PARAMS:
        return

    node_label = par_name
    node = None

    if par_type in ['handle:ITexture', 'handle:CTextureArray']:
        node = create_node_texture(mat, param, node_ng, y_loc, tex_table, uncook_path, texarray_index)
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
    elif par_type == 'Float':
        node = create_node_float(mat, param, node_ng)
    elif par_type == 'Color':
        node = create_node_color(mat, param, node_ng)
    elif par_type == 'Vector':
        node = create_node_vector(mat, param, node_ng)
    else:
        logging.warning("Unknown material parameter type: "+par_type)
        node = create_node_attribute(mat, param, node_ng)
        node_label = "Unknown type: " + par_type

    if not node:
        return

    node.location = (-450, y_loc)
    node.name = par_name
    node.label = node_label

    # Linking the node to the nodegroup
    if par_name in EQUIVALENT_PARAMS:
        input_pin = node_ng.inputs.get(EQUIVALENT_PARAMS[par_name])
    else:
        input_pin = node_ng.inputs.get(par_name)

    if input_pin and len(input_pin.links) == 0:
        # Only connect the node if some other node isn't already connected.
        # This is because if there are two diffuse textures defined, we are better off prioritizing
        # the first one.
        links.new(node.outputs[0], input_pin)

    return node

def create_node_texture(
        mat: Material
        ,param: Element
        ,node_ng: Node
        ,y_loc: int
        ,tex_table: Dict[str, List[str]]
        ,uncook_path: str
        ,texarray_index: str = '0'
    ):
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
            
    
    
    if par_value.endswith('.texarray'):
        par_value = par_value+".texture_%s.tga" % texarray_index
    # We use os.path.abspath() to make sure the filepath has consistent slashes and backslashes,
    # so that we can compare image file paths to each other for duplicate checking.
    final_tga_path = par_value.replace(".xbm", ".tga")
    try:
        final_texture = repo_file(final_tga_path) # TODO fix loading texarray
        if not Path(final_texture).exists():
            final_texture = uncook_path + os.sep + final_tga_path
    except Exception as e:
        #raise e
        final_texture= None
    
    tex_path = os.path.abspath( final_texture )
    node.image = load_texture(mat, tex_path, tex_table, uncook_path)
    if not node.image:
        node.label = "MISSING:" + par_value

    return node

def load_texture(
        mat: Material
        ,tex_path: str
        ,tex_table: Dict[str, List[str]]
        ,uncook_path: str
    ) -> Image:
    img_filename = os.path.basename(tex_path)	# Filename with extension.

    # Check if an image with this filepath is already loaded.
    img = None
    for i in bpy.data.images:
        #if bpy.path.basename(i.filepath) == img_filename:
        if Path(i.filepath) == Path(tex_path):
            img = i
            break
    # Check if the file exists
    if not img and not os.path.isfile(tex_path):
        logging.info("Image not found: " + tex_path + " (Usually unimportant)")
        return
    elif not img:
        img = bpy.data.images.load(tex_path,check_existing=True)

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

    return img

def create_node_float(mat, param, node_ng):
    nodes = mat.node_tree.nodes
    par_name = param.get('name')
    par_value = param.get('value')

    if 'Rotation' in par_name:
        normal_node = nodes.get(par_name.replace('Rotation', 'Normal'))
        if normal_node != None:
            mapping_node = normal_node.inputs[0].links[0].from_node
            # Set Z rotation
            mapping_node.inputs[1].default_value[2] = float(par_value)
            return
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

def create_node_vector(mat, param, node_ng):
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
                logging.warning(f"Expected a mapping node for {par_name}, got {mapping_node.type} instead!")
                return
            mapping_node.label = mapping_node.name = par_name
        else:
            logging.warning(f"Warning: Node {target_node.name} in material {mat.name} was expected to have a Mapping node plugged into it!")

    # Handling UV scale/tile nodes params
    if 'Tile' in par_name:
        for name in ['Diffuse', 'Normal']:
            target_node = nodes.get(par_name.replace('Tile', name))
            assign_uv_scale_values(mat, target_node)
    elif par_name == 'SpecularShiftUVScale':
        target_node = nodes.get('SpecularShiftTexture')
        assign_uv_scale_values(mat, target_node)
        return
    # if values[3] != 1 and values[3] != 0:
    # 	The 4th value on vectors is probably always useless.
    # 	logging.warning("Warning: Discarded vector 4th value: " + str(values) + " in parameter: " + par_name)

    node = nodes.new(type='ShaderNodeCombineXYZ')
    node.inputs[0].default_value = values[0]
    node.inputs[1].default_value = values[1]
    node.inputs[2].default_value = values[2]

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
        # These cases should be investigated and avoided.
        mat.name = "!3 No Texture"

def mat_apply_settings(mat, shader_type: str):
    """Setting material viewport settings."""
    mat.metallic = 0
    mat.roughness = 0.5
    mat.diffuse_color = (0.3, 0.3, 0.3, 1)
    if shader_type == 'pbr_eye_shadow':
        mat.blend_method = 'BLEND'
        mat.show_transparent_back = False
        mat.use_screen_refraction = True
        mat.use_sss_translucency = True
        mat.shadow_method = 'HASHED'
    elif shader_type == 'pbr_eye':
        mat.use_screen_refraction = True
    elif shader_type == 'transparent_lit':
        mat.blend_method = 'BLEND'
        mat.show_transparent_back = False	# TODO: Is this correct most of the time? Can we tell by some material parameter?
        mat.use_screen_refraction = True
        mat.use_sss_translucency = True # We don't use this right now, but just in case.
        mat.shadow_method = 'HASHED' # TODO: Could use some testing.
    else:
        mat.blend_method = 'CLIP'
