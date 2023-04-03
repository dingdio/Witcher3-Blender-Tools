import os
import time
from pathlib import Path

from io_import_w2l.setup_logging_bl import *
log = logging.getLogger(__name__)

import bpy
from bpy.props import StringProperty, BoolProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper
import addon_utils

from io_import_w2l import CR2W, get_uncook_path
from io_import_w2l.importers import import_w2l
from io_import_w2l.importers import import_w2w

from io_import_w2l.exporters import export_radish

class WITCH_OT_radish_w2L(bpy.types.Operator, ExportHelper):
    """Export radish layer"""
    bl_idname = "witcher.export_w2l_yml"
    bl_label = "export .yml"
    filename_ext = ".yml"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.yml', options={'HIDDEN'})

    def execute(self, context):
        fdir = self.filepath
        print("exporting layer now!")
        exporter = export_radish.radishExporter()
        exporter.export(fdir)
        return {'FINISHED'}


class WITCH_OT_w2L(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Level"""
    bl_idname = "witcher.import_w2l"
    bl_label = "Import .w2l"
    filename_ext = ".w2l"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.w2l', options={'HIDDEN'})
    files: bpy.props.CollectionProperty(
            type=bpy.types.OperatorFileListElement,
            options={'HIDDEN', 'SKIP_SAVE'},
        )
    keep_lod_meshes: BoolProperty(
        name="Keep LODs",
        default=False,
        description="If enabled, it will keep low quality meshes. An extra empty transfrom will be created for each group of meshes"
    )
    keep_empty_lods: BoolProperty(
        name="Keep Empty LODs",
        default=False,
        description="If enabled, it will keep empty mesh LODs with zero polygons"
    )
    keep_proxy_meshes: BoolProperty(
        name="Keep Proxy Meshes",
        default=True,
        description="If enabled, it will always keep any proxy meshes regardless of lod"
    )
    def execute(self, context):
        print("Importing layer now!")
        fdir = self.filepath
        files = self.files
        file: bpy.types.OperatorFileListElement
            
        start_time = time.time()
        if fdir.endswith(".w2l"):
            cur_dir = Path(self.filepath).parent
            
            for file in files:
                filepath = str(cur_dir / file.name)
                print("Importing file:", filepath)
                levelFile = CR2W.CR2W_reader.load_w2l(filepath)
                import_w2l.btn_import_W2L(levelFile, context, self.keep_lod_meshes,
                                          keep_empty_lods = self.keep_empty_lods,
                                          keep_proxy_meshes = self.keep_proxy_meshes)
        else:
            log.warn('Did not select .w2l')
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        message = f"Finished importing layer in {time.time() - round(start_time, 2)} seconds."
        log.info(message)
        self.report({'INFO'}, message)
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"levels\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class WITCH_OT_w2w(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Level"""
    bl_idname = "witcher.import_w2w"
    bl_label = "Import .w2w"
    filename_ext = ".w2w"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.w2w;*.yml', options={'HIDDEN'})

    def execute(self, context):
        print("importing world now!")
        filePath = self.filepath

        if os.path.isdir(filePath):
            log.warn('Did not select .w2w')
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        if filePath.endswith('.yml'):
            import_w2w.btn_import_radish(filePath)
        else:
            worldFile = CR2W.CR2W_reader.load_w2w(filePath)
            import_w2w.btn_import_w2w(worldFile, filePath)
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"levels\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

def import_group(coll, uncook_path):
    for child in coll.children:
        if child.group_type and child.group_type == "LayerInfo":
            print("LOADING LEVEL "+child.name)
            if child.level_path:
                fdir =  os.path.join(uncook_path, child.level_path)
                if Path(fdir).exists():
                    levelFile = CR2W.CR2W_reader.load_w2l(fdir)
                    import_w2l.btn_import_W2L(levelFile)
                else:
                    print("Can't find level "+fdir)
    for child in coll.children:
        if child.group_type and child.group_type == "LayerGroup":
            print("LAYER_GROUP "+child.name)
            import_group(child, uncook_path)

class WITCH_OT_load_layer_group(bpy.types.Operator):
    """IMPORT_LAYER_ButtonOperator"""
    bl_idname = "witcher.load_layer_group"
    bl_label = "Load This LayerGroup"

    def execute(self, context):
        coll = context.collection
        if coll:
            #loop all child colls
            #if LayerInfo load level
            #if LayerGroup load group

            uncook_path = get_uncook_path(context)
            
            start_time = time.time()
            import_group(coll, uncook_path)
            log.info(' Finished importing LayerGroup in %f seconds.', time.time() - start_time)
            #CLayerInfo
            #coll.level_path
            #coll.layerBuildTag
        return {'FINISHED'}

class WITCH_OT_load_layer(bpy.types.Operator):
    """Load Layer ButtonOperator"""
    bl_idname = "witcher.load_layer"
    bl_label = "Load This Layer"

    # @classmethod
    # def poll(cls, context):
    #     return context.layer_collection is not None

    def execute(self, context):
        coll = context.collection
        if coll:
            #CLayerInfo
            #coll.level_path
            #coll.layerBuildTag
            uncook_path = get_uncook_path(context)
            fdir =  os.path.join(uncook_path, coll.level_path) 
            levelFile = CR2W.CR2W_reader.load_w2l(fdir)
            import_w2l.btn_import_W2L(levelFile)
        return {'FINISHED'}



# from bpy.utils import (register_class, unregister_class)

# _classes = [
#     WITCH_OT_w2L,
#     WITCH_OT_w2w
# ]

# def register():
#     for cls in _classes:
#         register_class(cls)

# def unregister():
#     for cls in _classes:
#         unregister_class(cls)