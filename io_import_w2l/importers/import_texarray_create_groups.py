import bpy
import os

def create_landscape_outer():
    # create a group
    test_group = bpy.data.node_groups.new('landscape_mix_outer', 'ShaderNodeTree')

    # create group inputs
    group_inputs = test_group.nodes.new('NodeGroupInput')
    group_inputs.location = (-350,0)
    last_group = False
    last_group = False
    for index in range(0,31):
        this_index = index+1
        test_group.inputs.new('NodeSocketColor','color'+str(this_index))
        test_group.inputs.new('NodeSocketColor','normal'+str(this_index))
        test_group.inputs.new('NodeSocketColor','normalAlpha'+str(this_index))
        test_group.inputs.new('NodeSocketColor','blend'+str(this_index))
        #create the mix groups
        group_inner = test_group.nodes.new("ShaderNodeGroup")
        group_inner.node_tree = bpy.data.node_groups['landscape_mix_inner']
        group_inner.location = (0,-300*index)
        if index < 31:
            if index > 0:
                test_group.links.new(group_inner.inputs[0], last_group.outputs[0])
                test_group.links.new(group_inner.inputs[1], last_group.outputs[1])
                test_group.links.new(group_inner.inputs[2], last_group.outputs[2])
                test_group.links.new(group_inputs.outputs['color'+str(this_index)], group_inner.inputs[4])
                test_group.links.new(group_inputs.outputs['normal'+str(this_index)], group_inner.inputs[5])
                test_group.links.new(group_inputs.outputs['normalAlpha'+str(this_index)], group_inner.inputs[6])
                test_group.links.new(group_inputs.outputs['blend'+str(this_index)], group_inner.inputs[3])
                group_inner.location = (group_inner.location[0]+300*index,-300*index)
            else:
                # link inputs
                test_group.links.new(group_inputs.outputs['color'+str(this_index)], group_inner.inputs[4])
                test_group.links.new(group_inputs.outputs['normal'+str(this_index)], group_inner.inputs[5])
                test_group.links.new(group_inputs.outputs['normalAlpha'+str(this_index)], group_inner.inputs[6])
                test_group.links.new(group_inputs.outputs['blend'+str(this_index)], group_inner.inputs[3])
            last_group = group_inner

    # create group outputs
    group_outputs = test_group.nodes.new('NodeGroupOutput')
    group_outputs.location = (300,0)
    test_group.outputs.new('NodeSocketColor','color')
    test_group.outputs.new('NodeSocketColor','normal')
    test_group.outputs.new('NodeSocketColor','normalAlpha')
    test_group.links.new(last_group.outputs[0], group_outputs.inputs[0])
    test_group.links.new(last_group.outputs[1], group_outputs.inputs[1])
    test_group.links.new(last_group.outputs[2], group_outputs.inputs[2])
    
def create_landscape_inner():
    # create a group
    test_group = bpy.data.node_groups.new('landscape_mix_inner', 'ShaderNodeTree')
    # create group inputs
    group_inputs = test_group.nodes.new('NodeGroupInput')
    group_inputs.location = (-550,0)
    
    test_group.inputs.new('NodeSocketColor','color1')
    test_group.inputs.new('NodeSocketColor','normal1')
    test_group.inputs.new('NodeSocketColor','normalAlpha1')
    test_group.inputs.new('NodeSocketColor','blend')
    test_group.inputs.new('NodeSocketColor','color2')
    test_group.inputs.new('NodeSocketColor','normal2')
    test_group.inputs.new('NodeSocketColor','normalAlpha2')

    # create group outputs
    group_outputs = test_group.nodes.new('NodeGroupOutput')
    group_outputs.location = (300,0)
    test_group.outputs.new('NodeSocketColor','color')
    test_group.outputs.new('NodeSocketColor','normal')
    test_group.outputs.new('NodeSocketColor','normalAlpha')

    # create three math nodes in a group
    mix_node_1 = test_group.nodes.new('ShaderNodeMixRGB')
    mix_node_1.blend_type = 'MIX'
    mix_node_1.location = (-100,300)
    
    mix_node_2 = test_group.nodes.new('ShaderNodeMixRGB')
    mix_node_2.blend_type = 'OVERLAY'
    #mix_node_2.label = ''
    mix_node_2.location = (-100,0)

    mix_node_3 = test_group.nodes.new('ShaderNodeMixRGB')
    mix_node_3.blend_type = 'MIX'
    #mix_node_3.label = ''
    mix_node_3.location = (-100,-300)
    
    #normal map
#    normal_map = test_group.nodes.new('ShaderNodeNormalMap')
#    normal_map.location = (-300,0)
#    
    #color ramp
    color_ramp = test_group.nodes.new('ShaderNodeValToRGB')
    color_ramp.location = (-400,-300)
    

#    # link nodes together
    #test_group.links.new(mix_node_2.inputs[1], normal_map.outputs[0])
    test_group.links.new(mix_node_1.inputs[0], color_ramp.outputs[0])
    test_group.links.new(mix_node_2.inputs[0], color_ramp.outputs[0])
    test_group.links.new(mix_node_3.inputs[0], color_ramp.outputs[0])

#    # link inputs
    test_group.links.new(group_inputs.outputs['color1'], mix_node_1.inputs[1])
    test_group.links.new(group_inputs.outputs['normal1'], mix_node_2.inputs[1])
    test_group.links.new(group_inputs.outputs['normalAlpha1'], mix_node_3.inputs[1])
    test_group.links.new(group_inputs.outputs['blend'], color_ramp.inputs[0])
    test_group.links.new(group_inputs.outputs['color2'], mix_node_1.inputs[2])
    test_group.links.new(group_inputs.outputs['normal2'], mix_node_2.inputs[2])
    test_group.links.new(group_inputs.outputs['normalAlpha2'], mix_node_3.inputs[2])



#    #link output
    test_group.links.new(mix_node_1.outputs[0], group_outputs.inputs['color'])
    test_group.links.new(mix_node_2.outputs[0], group_outputs.inputs['normal'])
    test_group.links.new(mix_node_3.outputs[0], group_outputs.inputs['normalAlpha'])

    
    #bpy.data.node_groups["landscape_mix_outer"].nodes["Group"]
    test_group.inputs[0].default_value = (0.1, 0.1, 0.1, 1)
    test_group.inputs[1].default_value = (0.5, 0.5, 1, 1)
    test_group.inputs[2].default_value = (0, 0, 0, 1)




