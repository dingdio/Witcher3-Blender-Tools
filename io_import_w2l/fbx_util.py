from io_import_w2l import get_texture_path
#from io_import_w2l import get_keep_lod_meshes
from io_import_w2l.importers import import_mesh

from typing import List, Tuple, Dict
from bpy.types import Operator, Object

import bpy, os, sys
from math import pi

from .w3_material import load_w3_materials_XML

def enable_print(bool):
    """For suppressing prints from fbx importer and remove_doubles()."""
    if not bool:
        sys.stdout = open(os.devnull, 'w')
    else:
        sys.stdout = sys.__stdout__

def get_object_blacklist() -> List[str]:
    blacklist_file = os.path.abspath(__file__).replace("import_witcher3_fbx.py", "object_blacklist.txt")
    with open(blacklist_file, 'r') as f:
        return list(eval(f.read()))

def update_object_blacklist(ob_blacklist: List[str]) -> List[str]:
    """Write a new entry into the blacklist file."""
    blacklist_file = os.path.abspath(__file__).replace("import_witcher3_fbx.py", "object_blacklist.txt")
    with open(blacklist_file, 'w') as f:
        f.write(str(sorted(list(set(ob_blacklist)))).replace(",", ",\n"))

def is_object_useless(ob_blacklist: List[str], o: Object) -> str:
    """If the object is detected to be useless, return an explanation as to why."""

    error = ""

    if o.name in ob_blacklist:
        error = "Object already on blacklist"
    if len(o.data.vertices) == 0 or len(o.data.polygons) == 0:
        error = "No geometry"
    if max(o.dimensions) < 0.0001:
        error = "Too tiny"
    # if o.name.endswith("_volume"):
    # 	error = "Volume object"

    if error:
        ob_blacklist.append(o.name)
        return error

def import_w3_fbx(context
        ,filepath: str
        ,uncook_path: str
        ,ob_blacklist: List[str] = []
        ,remove_doubles = True
        ,keep_lod_meshes = False
        ,quadrangulate = True
        ,fix_armature = True
        ,force_mat_update = False
    ) -> Tuple[List[Object], List[Object]]:
    if not filepath.endswith(".fbx"):
        return ([], [])

    filename = filepath.split("\\")[-1].split(".")[0]
    enable_print(False)
    # Small note: The imported objects automatically became selected on import.
    bpy.ops.import_scene.fbx(
        filepath = filepath
        ,use_image_search = False
        ,use_custom_normals = True
        ,do_namespace_fix = True
        ,do_UV_fix = False
        #,use_custom_props = False
    )
    enable_print(True)
    obj_name = filename

    #bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    # Discarding LOD meshes.
    if not keep_lod_meshes:
        for o in reversed(context.selected_objects):
            if "lod1" in o.name or \
                "lod2" in o.name or \
                "lod3" in o.name or \
                "lod4" in o.name:
                bpy.data.objects.remove(o)

    armatures = []
    meshes = []
    for o in context.selected_objects[:]:
        bpy.ops.object.select_all(action='DESELECT')
        assert o.type != 'EMPTY', "You didn't fix import_fbx.py"
        if o.type == 'MESH':
            o.name = obj_name+'_'+o.name
            error = is_object_useless(ob_blacklist, o)
            if error:
                # print(o.name, error, "Skipping")
                bpy.data.objects.remove(o)
                continue

            enable_print(False)
            # cleanup_mesh(context, o
            # 	,remove_doubles = remove_doubles
            # 	,quadrangulate = quadrangulate
            # 	,weight_normals = True
            # 	,seams_from_islands = True
            # )
            enable_print(True)
            # while len(o.data.vertex_colors) > 0:
            #     o.data.vertex_colors.remove(o.data.vertex_colors[0])

            #uncook_path = get_uncook_path(context)
            xml_path = filepath.replace(".fbx", ".xml")
            xml_path = xml_path.replace("_CONVERT_","")
            try:
                load_w3_materials_XML(o, uncook_path, xml_path, force_mat_update=force_mat_update)
            except ValueError as err:
                print("WARNING: Problem loading material in fbx importer", err.reason)
            if len(o.data.vertices) == 0:
                bpy.data.objects.remove(o)
                continue
            meshes.append(o)



        if o.type == 'ARMATURE':
            o.name = obj_name + "_Skeleton"
            armatures.append(o)
            # if fix_armature:
            # 	cleanup_w3_armature(context, o)
        o.data.name = "Data_" + o.name

        #checks if you're trying to import the fbx from a repo
        if uncook_path in filepath:
            final_path = filepath.replace(uncook_path, "")
            final_path = final_path.replace("FBXs\\", "")
            o['repo_path'] = final_path.replace(".fbx", ".w2mesh")

    # Apply transforms. (Armatures have a scale of 0.1 for some reason)
    for o in armatures+meshes:
        o.select_set(True)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

    # Recursive Purge Unused Datablocks
    enable_print(False)
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    # De-duplicate images, then purge again...
    deduplicate_images()
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    enable_print(True)

    # for o in reversed(meshes):
    #     if "lod1" in o.name or \
    #         "lod2" in o.name or \
    #         "lod3" in o.name or \
    #         "lod4" in o.name:
    #         o.hide_viewport = True
    # for o in reversed(meshes):
    #     if "_proxy" in o.name:
    #         o.hide_viewport = False
    return (meshes, armatures)

def set_render_settings(context):
    """Set the necessary Eeevee render settings in the scene."""
    context.scene.eevee.use_ssr = True
    context.scene.eevee.use_ssr_refraction = True

def deduplicate_images():
    # Go through the image list, and try to de-duplicate images whose names end in .001.
    filepaths = {}
    imgs_alphabetical = sorted(bpy.data.images, key=lambda i: i.name)
    for img in imgs_alphabetical:
        if img.filepath not in filepaths:
            filepaths[img.filepath] = img
        else:
            img.user_remap(filepaths[img.filepath])

def importFbx(filepath, ns="cake", name=":", uncook_path=False, keep_lod_meshes = False):
    
    if filepath.endswith(".w2mesh"):
        (meshes, armatures) = import_mesh.import_mesh(filepath, do_merge_normals = True)
    else:
        context = bpy.context
        if not os.path.exists(filepath):
            print("Can't find FBX file", filepath)
            #cmds.confirmDialog( title='Error', button='OK', message='Can\'t find "{0}". Check it exists in the FBX depo.'.format( filepath ))
        #bpy.ops.import_scene.fbx(filepath=filepath)
        # bpy.ops.import_scene.witcher3_fbx_batch(filepath=filepath,
        #     files=[{"name":filepath,
        #     "name":filepath}],
        #     directory=filepath+"\\",
        #     force_update_mats=False)
        uncook_path = get_texture_path(context)+"\\" #! THE PATH WITH THE TEXTURES NOT THE FBX FILES

        (meshes, armatures) = import_w3_fbx(context
            ,filepath = filepath
            ,uncook_path = uncook_path
            ,remove_doubles = False #remove_doubles
            ,keep_lod_meshes = keep_lod_meshes
            ,quadrangulate = False #quadrangulate
            ,fix_armature = True
            ,force_mat_update = True#self.force_update_mats
        )
    return (meshes, armatures)

