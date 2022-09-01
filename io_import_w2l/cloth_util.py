from .w3_material import create_param, load_texture_table, read_2wmi_params2, setup_w3_material
from . import CR2W
import bpy, os, filecmp, shutil
from typing import List, Tuple, Dict
from bpy.types import Image, Material, Object, Node
import re
from xml.etree import ElementTree
Element = ElementTree.Element

from io_import_w2l import get_uncook_path
from io_import_w2l import get_fbx_uncook_path
from io_import_w2l import get_texture_path


from . import CR2W
import bpy
import io_scene_apx
from io_scene_apx.importer.import_clothing import read_clothing

def setup_w3_material_CR2W(
        uncook_path: str
        ,tex_table: Dict[str, List[str]]
        ,bl_material: Material
        ,mat_bin:str
        ,force_update = False	# Set to True when re-importing stuff to test changes with the latest material set-up code.
        ,mat_filename = str
        ):
        mat_base = mat_bin.GetVariableByName('baseMaterial').Handles[0].DepotPath
        shader_type = mat_base.split("\\")[-1][:-5]	# The .w2mg or .w2mi file, minus the extension.

        new_xml = ElementTree.Element('material')
        new_xml.set('name', bl_material.name)
        new_xml.set('local', "true")
        new_xml.set('base', mat_base)

        w2mi_params = read_2wmi_params2(mat_bin, uncook_path, mat_bin, shader_type)
        for name, attrs in w2mi_params.items():
            create_param(
                xml_data = new_xml
                ,name = name 
                ,type = attrs[0]
                ,value = attrs[1]
            )
        # for p in mat_bin.InstanceParameters.elements:
        #     PROP = p.PROP
        #     create_param(
        #         xml_data = new_xml
        #         ,name = p.theName 
        #         ,type = "Texture"
        #         ,value = "c:\\yes"
        #     )
        #     print("cake")
            #params[p.get('name')] = p.get('value')


        bl_material.use_nodes = True
        #all_cnew_xml= list(new_xml.iter())
        return setup_w3_material(uncook_path, tex_table, bl_material, xml_data=new_xml, xml_path=mat_filename, force_update=force_update)



def load_w3_materials_CR2W(
        obj: Object
        ,uncook_path: str
        ,tex_table: Dict[str, List[str]]
        ,materials_bin: str
        ,material_names: str
        ,force_mat_update = False
        ,mat_filename = str
    ):
    for idx, mat in enumerate(materials_bin):
        xml_mat_name = material_names[idx]
        print(xml_mat_name)
        target_mat = False
        if xml_mat_name in obj.data.materials:
            target_mat = obj.data.materials[xml_mat_name] #None
        if not target_mat:
            for m in obj.data.materials:
                if m.name in xml_mat_name:
                    print("partial material match",m.name, xml_mat_name)
                    target_mat = m
            if not target_mat:
                # Didn't find a matching blender material.
                # Must be a material that's only for LODs, so let's ignore.
                continue

        finished_mat = setup_w3_material_CR2W(uncook_path, tex_table, target_mat, mat, force_update=force_mat_update, mat_filename=mat_filename)
        obj.material_slots[target_mat.name].material = finished_mat
        print("cake")

        #!!!!!!!!!!!!!!
        # <material name="Material0" local="true" base="characters\models\common\materials\base_materials\base_eye.w2mi">
        #     <param name="Diffuse" type="handle:ITexture" value="characters\models\main_npc\ciri\h_01_wa__ciri\eye__ciri_d01.xbm" />
        # </material>

def importCloth(filename, mat_filename, ns="cake", name=":"):
    filepath = filename
    context = bpy.context

    uncook_path = get_texture_path(context)+"\\" # PATH WITH TEXTURES
    tex_table = load_texture_table()

    rm_db =True
    use_mat =True
    rotate_180=True
    scale_down=True
    minimal_armature=True #!change
    rm_ph_me=True

    #io_scene_apx.read_apx(context, filepath, rm_db, use_mat, rotate_180, scale_down, minimal_armature, rm_ph_me)
    read_clothing(context, filepath, rm_db, use_mat, rotate_180, minimal_armature, rm_ph_me)
    # objs = bpy.context.objects[:]
    # for obj in objs:
    #     print (obj.name)
    
    #get the cloth mesh and select it
    bpy.context.view_layer.objects.active = None
    bpy.ops.object.select_all(False)
    GMesh_objs = []
    for collection in bpy.data.collections:
        print(collection.name)
        for obj in collection.all_objects:
            if "GMesh_lod0" in obj.name:
                GMesh_objs.append(obj)
    gmesh = GMesh_objs[-1]
    print(gmesh.name)
    gmesh.select = True
    bpy.context.view_layer.objects.active = gmesh


    redcloth_material = CR2W.CR2W_reader.load_material(mat_filename)

    for chunk in redcloth_material:
        if chunk.name == "CApexClothResource":
            print(chunk.name)
            materials = [redcloth_material[o.Reference] for o in chunk.GetVariableByName('materials').Handles] 
            material_names = [o.String.split('::')[1] for o in chunk.GetVariableByName('apexMaterialNames').elements]
            caek = "adw"

    load_w3_materials_CR2W(gmesh, uncook_path, tex_table, materials, material_names, mat_filename=mat_filename)
            
    #baseMaterial = material.GetVariableByName('baseMaterial')
    # for obj in GMesh_objs:
    #     print("obj: ", obj.name)
