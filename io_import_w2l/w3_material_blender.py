
from pathlib import Path
from io_import_w2l import CR2W
import bpy
from bpy.types import Image, Material, Object, Node

def init_shader_nodes(material: Material, group_name: str, clear:bool = True, x_loc:int = -250):
    """Wipe all nodes, then create a node group node and return it."""
    ng_name = group_name
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

def create_shader_group( params_to_create, bl_material, group_name):
    bl_material.use_nodes = True
    nodes = bl_material.node_tree.nodes
    links = bl_material.node_tree.links

    nodegroup_node = init_shader_nodes(bl_material, group_name, clear = True, x_loc = 0) #x_loc)
    nodegroup_node.name = group_name
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
    #ordered_params = order_elements_by_attribute(xml_data, PARAM_ORDER, 'name')

    
    for idx, p in enumerate(params_to_create):
        par_name = p.get('name')
        par_type = p.get('type')
        par_value = p.get('value')
        if par_type == "CMaterialParameterColor": # "Color":
            ngt.inputs.new('NodeSocketColor', par_name)
            #ngt.outputs.new('NodeSocketColor',par_name)
            values = par_value#[float(f) for f in par_value.split("; ")]
            d_val = (
                values[0] / 255
                ,values[1] / 255
                ,values[2] / 255
                ,values[3] / 255
            )
            ngt.inputs[par_name].default_value = d_val
            nodegroup_node.inputs[par_name].default_value = d_val
        elif par_type == "CMaterialParameterScalar": #"Float":
            ngt.inputs.new('NodeSocketFloat', par_name)
            #ngt.outputs.new('NodeSocketFloat',par_name)
            ngt.inputs[par_name].default_value = float(par_value)
            nodegroup_node.inputs[par_name].default_value = float(par_value)
            
            #ngt.links.new(group_inputs.outputs[par_name], group_outputs.inputs[par_name])
        elif par_type == "CMaterialParameterTexture": #"handle:ITexture":
            ngt.inputs.new('NodeSocketColor', par_name)
            #ngt.outputs.new('NodeSocketColor',par_name)
            #active_node = ngt.inputs.new('NodeSocketFloat', par_name+"_active")
            
            # create three math nodes in a group
            #mix_node_1 = ngt.nodes.new('ShaderNodeMixRGB')
            #mix_node_1.blend_type = 'MIX'
            #mix_node_1.location = (0,0+(-500*idx))
            #ngt.links.new(group_inputs.outputs[par_name], mix_node_1.inputs["Color2"])
            #ngt.links.new(mix_node_1.outputs["Color"], group_outputs.inputs[par_name])
            
            #math_node_1 = ngt.nodes.new('ShaderNodeMath')
            #math_node_1.location = (-320,200+(-500*idx))
            #math_node_1.operation = 'GREATER_THAN'
            
            
            #ngt.links.new(mix_node_1.inputs[0], math_node_1.outputs[0])
            #ngt.links.new(math_node_1.inputs[0], group_inputs.outputs[par_name+"_active"])
            
            #node = ngt.nodes.new(type="ShaderNodeTexImage")
            #node.width = 300
            #node = create_node_texture(material, p, ngt, 0+(500*idx), uncook_path, 0, using_node_tree = True)

            # node.location = (-320,0+(-500*idx))
            # if node and node.image:
            #     if par_name in ['Diffuse', 'SpecularTexture', 'SnowDiffuse']:
            #         node.image.colorspace_settings.name = 'sRGB'
            #     else:
            #         node.image.colorspace_settings.name = 'Non-Color'
                    
            # if node and node.image and len(node.outputs[0].links) > 0:
            #     pin_name = node.outputs[0].links[0].to_socket.name
            #     if pin_name in ['Diffuse', 'SpecularTexture', 'SnowDiffuse']:
            #         node.image.colorspace_settings.name = 'sRGB'
            #     else:
            #         node.image.colorspace_settings.name = 'Non-Color'
            # ngt.links.new(node.outputs["Color"], mix_node_1.inputs["Color1"])
            
        elif par_type == "CMaterialParameterVector": # 'Vector':
            ngt.inputs.new('NodeSocketVector', par_name)
            #ngt.outputs.new('NodeSocketVector',par_name)
            #ngt.links.new(group_inputs.outputs[par_name], group_outputs.inputs[par_name])

            values = par_value
            d_val = (
                values[0]
                ,values[1]
                ,values[2]
            )
            ngt.inputs[par_name].default_value = d_val
            nodegroup_node.inputs[par_name].default_value = d_val
        else:
            ngt.inputs.new('NodeSocketFloat', par_name)
            #ngt.outputs.new('NodeSocketFloat',par_name)
            #ngt.links.new(group_inputs.outputs[par_name], group_outputs.inputs[par_name])
        
    return nodegroup_node


def old_thing(params_to_create, bl_material):
    bl_material.use_nodes = True
    for node in bl_material.node_tree.nodes:
        bl_material.node_tree.nodes.remove(node)
    for param in params_to_create:
        param_name = param["name"]
        if param_name in bl_material.node_tree.nodes:
            group_node = bl_material.node_tree.nodes[param_name]
            group_node.node_tree.nodes.clear()
        else:
            group_node = bl_material.node_tree.nodes.new(type="ShaderNodeGroup")
            group_node.name = param_name
            group_node.label = param_name
        group_node.node_tree = bpy.data.node_groups[param["type"]]
        group_node.inputs[0].default_value = param["value"]
    
    
def import_w2mg(mat_fileName, self = None):
    material = CR2W.CR2W_reader.load_material(mat_fileName)
    shader_name = Path(mat_fileName).stem+'_bmat'
    mesh_name = shader_name+'_DISP_MESH'
    
    if mesh_name in bpy.data.objects:
        obj = bpy.data.objects[mesh_name]
    else:
        bpy.ops.mesh.primitive_plane_add()
        obj = bpy.context.object
        bpy.context.object.name = mesh_name

    target_mat = None
    if self.do_update_mats:
        if shader_name in obj.data.materials:
            target_mat = obj.data.materials[shader_name] #None
        if shader_name in bpy.data.materials:
            target_mat = bpy.data.materials[shader_name] #None
    if not target_mat:
        target_mat = bpy.data.materials.new(name=shader_name)
    
    
    shader_classes = [ "CMaterialParameterColor",
    "CMaterialParameterCube",
    "CMaterialParameterScalar",
    "CMaterialParameterTexture",
    "CMaterialParameterTextureArray",
    "CMaterialParameterVector",]
    
    params_to_create = []
    for chunk in material:
        if chunk.Type in shader_classes:
            name = chunk.GetVariableByName('parameterName').Index.String
            val = {
                'name' : name,
                'type' : chunk.Type,
                'value' : None,
            }
            if chunk.Type == "CMaterialParameterColor":
                color = chunk.GetVariableByName('color')
                RGBA = [
                    color.GetVariableByName('Red').Value,
                    color.GetVariableByName('Green').Value,
                    color.GetVariableByName('Blue').Value,
                    color.GetVariableByName('Alpha').Value,
                ]
                val['value'] = RGBA
            elif chunk.Type == "CMaterialParameterCube":
                pass
            elif chunk.Type == "CMaterialParameterScalar":
                scaler = chunk.GetVariableByName('scalar')
                val['value'] = scaler.Value if scaler else False
            elif chunk.Type == "CMaterialParameterTexture":
                texture = chunk.GetVariableByName('texture')
                val['value'] = texture.Handles[0].DepotPath
            elif chunk.Type == "CMaterialParameterTextureArray":
                pass #texture = chunk.GetVariableByName('texture')
            elif chunk.Type == "CMaterialParameterVector":
                vector = chunk.GetVariableByName('vector')
                XYZW = [
                    vector.GetVariableByName('X').Value,
                    vector.GetVariableByName('Y').Value,
                    vector.GetVariableByName('Z').Value,
                    vector.GetVariableByName('W').Value,
                ]
                val['value'] = XYZW
            params_to_create.append(val)
    
    
    create_shader_group(params_to_create, target_mat, Path(mat_fileName).stem)
    finished_mat = target_mat

    if shader_name in obj.data.materials: #and not self.do_update_mats:
        obj.material_slots[target_mat.name].material = finished_mat
    else:
        obj.data.materials.append(finished_mat)
    return
    bpy.ops.mesh.primitive_plane_add()
    obj = bpy.context.selected_objects[:][0]
    instance_filename = Path(fdir).stem
    materials = []
    material_file_chunks = CR2W.CR2W_reader.load_material(fdir)
    for idx, mat in enumerate(material_file_chunks):
        # if idx > 0:
        #     raise Exception('wut')
        target_mat = False
        if self.do_update_mats:
            if instance_filename in obj.data.materials:
                target_mat = obj.data.materials[instance_filename] #None
            if instance_filename in bpy.data.materials:
                target_mat = bpy.data.materials[instance_filename] #None
        if not target_mat:
            target_mat = bpy.data.materials.new(name=instance_filename)

        finished_mat = setup_w3_material_CR2W(get_texture_path(context), target_mat, mat, force_update=True, mat_filename=instance_filename, is_instance_file = True)

        if instance_filename in obj.data.materials and not self.do_update_mats:
            obj.material_slots[target_mat.name].material = finished_mat
        else:
            obj.data.materials.append(finished_mat)