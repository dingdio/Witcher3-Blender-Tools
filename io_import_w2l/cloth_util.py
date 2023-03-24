from pathlib import Path
from .w3_material import create_param, read_2wmi_params2, setup_w3_material, xml_data_from_CR2W
from . import CR2W
import bpy, os, filecmp, shutil
from typing import List, Tuple, Dict
from bpy.types import Image, Material, Object, Node
import re
import numpy as np
from xml.etree import ElementTree
Element = ElementTree.Element
from xml.dom import minidom

from io_import_w2l import get_uncook_path
from io_import_w2l import get_fbx_uncook_path
from io_import_w2l import get_texture_path

from . import CR2W
import bpy
import io_scene_apx
from io_scene_apx.importer.import_clothing import read_clothing

from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

def prettify(elem):
    """Return a pretty-printed XML string for the Element.
    """
    rough_string = ElementTree.tostring(elem, 'utf-8')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="\t")

def setup_w3_material_CR2W(
        uncook_path: str
        ,bl_material: Material
        ,mat_bin:str
        ,force_update = False	# Set to True when re-importing stuff to test changes with the latest material set-up code.
        ,mat_filename = str
        ,is_instance_file = False
        ):
        new_xml = xml_data_from_CR2W(mat_bin, bl_material.name)
        bl_material.use_nodes = True
                    
        ##return base mat path and if it is local chunk handle
        bl_material.witcher_props.name = bl_material.name
        #bl_material.witcher_props.base = "custom"
        bl_material.witcher_props.base_custom = new_xml.get('base')
        bl_material.witcher_props.local = True
        bl_material.witcher_props.xml_text = prettify(new_xml)
        #enableMask
        # if hasattr(mat_bin , 'local') and mat_bin.local == True:
        #     bl_material.witcher_props.local = True
        if hasattr(mat_bin ,'DepotPath') and hasattr(mat_bin , 'local') and mat_bin.local == False:
            bl_material.witcher_props.base_custom = mat_bin.DepotPath
            bl_material.witcher_props.local = False
        
        enableMask = mat_bin.GetVariableByName('enableMask')
        if enableMask and enableMask.Value == 1:
            bl_material.witcher_props.enableMask = True
        return setup_w3_material(uncook_path, bl_material, xml_data=new_xml, xml_path=mat_filename, force_update=force_update, is_instance_file = is_instance_file)

def load_w3_materials_CR2W(
        obj: Object
        ,uncook_path: str
        ,materials_bin: str
        ,material_names: str
        ,force_mat_update = False
        ,mat_filename = str
    ):
    for idx, mat in enumerate(materials_bin):
        xml_mat_name = material_names[idx]
        log.info(xml_mat_name)
        target_mat = False
        if xml_mat_name in obj.data.materials:
            target_mat = obj.data.materials[xml_mat_name] #None
        if not target_mat:
            for m in obj.data.materials:
                if m.name in xml_mat_name:
                    log.info("partial material match {m.name} {xml_mat_name}")
                    target_mat = m
            if not target_mat:
                # Didn't find a matching blender material.
                # Must be a material that's only for LODs, so let's ignore.
                continue

        finished_mat = setup_w3_material_CR2W(uncook_path, target_mat, mat, force_update=force_mat_update, mat_filename=mat_filename)
        obj.material_slots[target_mat.name].material = finished_mat


def getGeometryCenter(obj):
		sumWCoord = [0,0,0]
		numbVert = 0
		if obj.type == 'MESH':
			for vert in obj.data.vertices:
				wmtx = obj.matrix_world
				worldCoord = vert.co @ wmtx
				sumWCoord[0] += worldCoord[0]
				sumWCoord[1] += worldCoord[1]
				sumWCoord[2] += worldCoord[2]
				numbVert += 1
			sumWCoord[0] = sumWCoord[0]/numbVert
			sumWCoord[1] = sumWCoord[1]/numbVert
			sumWCoord[2] = sumWCoord[2]/numbVert
		return sumWCoord
	
def setOrigin(obj):
    oldLoc = obj.location
    newLoc = getGeometryCenter(obj)
    for vert in obj.data.vertices:
        vert.co[0] -= newLoc[0] - oldLoc[0]
        vert.co[1] -= newLoc[1] - oldLoc[1]
        vert.co[2] -= newLoc[2] - oldLoc[2]
    obj.location = newLoc 

def createEmpty(prefix = None, name = "", parent = None):
    bpy.ops.object.empty_add(type="PLAIN_AXES", radius=0.1)
    transform = bpy.context.object
    transform.name = prefix+":"+name if prefix else name
    transform.parent = parent if parent else None
    return transform

def color_to_weights(obj, src_vcol, src_channel_idx, dst_vgroup_idx):
    mesh = obj.data
    
    cols = []
    for col in src_vcol.data:
        cols.append(col)

    # build 2d array containing sum of color channel value, number of values
    # used to calculate average for vertex when setting weights
    vertex_values = [[0.0, 0] for i in range(0, len(mesh.vertices))]
    vertex_values1 = [[0.0, 0] for i in range(0, len(mesh.vertices))]
    vertex_values2 = [[0.0, 0] for i in range(0, len(mesh.vertices))]
    vertex_values3 = [[0.0, 0] for i in range(0, len(mesh.vertices))]
    
    for idx, vertex in enumerate(vertex_values):
        vertex_values[idx][0] = src_vcol.data[idx].color[1]
        vertex_values1[idx][0] = src_vcol.data[idx].color[1]
        vertex_values2[idx][0] = src_vcol.data[idx].color[2]
        vertex_values3[idx][0] = src_vcol.data[idx].color[3]
    
    group = obj.vertex_groups[dst_vgroup_idx]
    mode = 'REPLACE'

    for i in range(0, len(mesh.vertices)):
        weight = vertex_values[i][0]
        # if weight == 0.0:
        #     group.add([i], weight, mode)
        # else:
        reverse = (1 - weight)
        reverse = reverse if reverse > 0.99 else reverse/2.5
        group.add([i], reverse, mode)

    mesh.update()
    
red_id = 'R'
green_id = 'G'
blue_id = 'B'
alpha_id = 'A'
def channel_id_to_idx(id):
    if id is red_id:
        return 0
    if id is green_id:
        return 1
    if id is blue_id:
        return 2
    if id is alpha_id:
        return 3
    # default to red
    return 0


def importCloth(context, filepath, use_mat, rotate_180, rm_ph_me, mat_filename="", ns="cloth", name=":", DO_WEAR_CLOTH = True):

    save_selected = bpy.context.selected_objects[:]
    save_active = bpy.context.view_layer.objects.active
    
    if not context:
        context = bpy.context

    uncook_path = get_texture_path(context)+"\\" # PATH WITH TEXTURES

    read_clothing(context, filepath, use_mat, rotate_180, rm_ph_me)
    # objs = bpy.context.objects[:]
    # for obj in objs:
    #     print (obj.name)
    
    #get the cloth mesh and select it
    bpy.context.view_layer.objects.active = None
    bpy.ops.object.select_all(action='DESELECT')
    active_coll = bpy.context.view_layer.active_layer_collection.collection
    arma = None
    arma_objs = []
    for ob in active_coll.all_objects:
        if ob.type == "ARMATURE" and "Armature" in ob.name:
            arma_objs.append(ob)
    arma_objs.sort(key=lambda x: x.name, reverse=True)
    arma = arma_objs[0]
    filename = Path(filepath).stem

    if DO_WEAR_CLOTH:
        cloth_group = createEmpty(filename,"_grp")
        collision_transform = createEmpty(filename, "Collision Spheres", cloth_group)
        connections_transform = createEmpty(filename, "Collision Spheres Connections", cloth_group)
        arma.parent = cloth_group
        
        arma.name = filename
        arma.data.name = filename+"_ARM"
        arma.select_set(True)
        bpy.context.view_layer.objects.active = arma
        
        spheres_coll = bpy.data.collections.get("Collision Spheres")
        connect_coll = bpy.data.collections.get("Collision Spheres Connections")
        
        if spheres_coll:
            all_spheres_coll = np.concatenate((spheres_coll.all_objects, []))
        if connect_coll:
            all_connect_coll = np.concatenate(([], connect_coll.all_objects))

        obj_dict = {}
        constrains = []
        if spheres_coll:
            for obj in all_spheres_coll:
                bone = obj.name[7:-2]
                constrains.append([obj.name, bone])
                obj_dict.update({obj.name : obj})
                spheres_coll.objects.unlink(obj)
            bpy.data.collections.remove(spheres_coll)
        if connect_coll:
            for obj in all_connect_coll:
                x, y = obj.name.split('_to_')
                bone = x[7:-2]
                constrains.append([obj.name, bone])
                obj_dict.update({obj.name : obj})
                connect_coll.objects.unlink(obj)
            bpy.data.collections.remove(connect_coll)
            
        
        
        for constrain in constrains:
            sphere = obj_dict[constrain[0]]
            child_of = sphere.constraints.new('CHILD_OF')
            child_of.name = constrain[1] + " to " + sphere.name
            child_of.target = arma
            child_of.subtarget = constrain[1]

            # arm_child.data.bones.active = arm_child.data.bones[sphere.name]


            # bpy.ops.object.mode_set(mode='EDIT', toggle=False)
            # bpy.context.active_bone.parent = None
            # bpy.ops.object.mode_set(mode='POSE', toggle=False)

            #bpy.ops.constraint.childof_set_inverse(constraint=constrain[1] + " to " + sphere.name, owner='BONE')

        
        

        if spheres_coll:
            bpy.context.view_layer.objects.active = None
            bpy.ops.object.select_all(action='DESELECT')
            collision_transform.select_set(True)
            bpy.context.view_layer.objects.active = collision_transform
            for obj in all_spheres_coll:
                active_coll.objects.link(obj)
                obj.name = filename+":"+obj.name
                col_obj = obj.modifiers.new("collision", 'COLLISION')
                obj.select_set(True)
                setOrigin(obj)
            bpy.ops.object.parent_set(type='OBJECT', keep_transform=False)
            
        if connect_coll:
            bpy.context.view_layer.objects.active = None
            bpy.ops.object.select_all(action='DESELECT')
            connections_transform.select_set(True)
            bpy.context.view_layer.objects.active = connections_transform
            for obj in all_connect_coll:
                active_coll.objects.link(obj)
                obj.name = filename+":"+obj.name
                col_obj = obj.modifiers.new("collision", 'COLLISION')
                obj.select_set(True)
            bpy.ops.object.parent_set(type='OBJECT', keep_transform=False)



        
    bpy.context.view_layer.objects.active = None
    bpy.ops.object.select_all(action='DESELECT')
    GMesh_objs = []
    for ob in active_coll.all_objects:
        if ob.type == "MESH" and ob.name.startswith("GMesh_lod"):
            GMesh_objs.append(ob)
    GMesh_objs.sort(key=lambda x: x.name, reverse=False)
    gmesh = GMesh_objs[0]
    if DO_WEAR_CLOTH:
        gmesh.name = filename+":"+gmesh.name
    
    for o in reversed(GMesh_objs):
        if "lod1" in o.name or \
            "lod2" in o.name or \
            "lod3" in o.name or \
            "lod4" in o.name:
            bpy.data.objects.remove(o)

    gmesh.select_set(True)
    bpy.context.view_layer.objects.active = gmesh

    redcloth_material = CR2W.CR2W_reader.load_material(mat_filename)

    for chunk in redcloth_material:
        if chunk.name == "CApexClothResource":
            log.info(chunk.name)
            materials = [redcloth_material[o.Reference] for o in chunk.GetVariableByName('materials').Handles] 
            material_names = [o.String.split('::')[1] for o in chunk.GetVariableByName('apexMaterialNames').elements]

    load_w3_materials_CR2W(gmesh, uncook_path, materials, material_names, mat_filename=mat_filename)
    
    if DO_WEAR_CLOTH:
        vcol = gmesh.data.color_attributes['MaximumDistance']
        
        vgroup_id = 'SimplyPin'
        vgroup = gmesh.vertex_groups.new(name=vgroup_id)
        gmesh.vertex_groups.active_index = vgroup.index

        color_to_weights(gmesh, vcol, 0, vgroup.index)


        def remove_doubles():
            merge_threshold = 0.0001
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.mesh.remove_doubles(threshold = merge_threshold)
            bpy.ops.mesh.select_all(action='DESELECT')
        bpy.ops.object.mode_set(mode='EDIT', toggle=False)
        remove_doubles()
        bpy.ops.object.mode_set(mode='OBJECT')

        bpy.context.view_layer.objects.active = None
        bpy.ops.object.select_all(action='DESELECT')
        bpy.context.view_layer.objects.active = save_active
        for ob in save_selected:
            ob.select_set(True)
        

    return arma
