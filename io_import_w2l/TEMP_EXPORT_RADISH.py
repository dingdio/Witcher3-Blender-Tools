import yaml
import bpy
from pathlib import Path
import math

class MeshItem:
    def __init__(self, name, mesh, pos, rot, scale):
        self.name = name
        self.mesh = mesh
        self.meshpreview = r"dlc/modtemplates/radishquestui/meshes/"+Path(self.mesh).stem+".w2ent"
        self.pos = pos
        self.rot = rot
        self.scale = scale

a=bpy.context.view_layer.active_layer_collection.collection

collection = bpy.data.collections.get(a.name)
print(collection.name)

items = []
for obj in collection.all_objects:
    print("obj: ", obj.name.replace(".","_"))
    print(obj['repo_path'])
    print(obj.rotation_euler)
    rot_z = math.degrees(obj.rotation_euler[2])
    if math.degrees(obj.rotation_euler[1]) == 0.0 and math.degrees(obj.rotation_euler[0]) == 0.0:
        rot_z = rot_z + 180

    items.append(
        MeshItem(obj.name.replace(".","_"),
        obj['repo_path'],
        [obj.location[0],obj.location[1],obj.location[2]],
            [math.degrees(obj.rotation_euler[1]),
             math.degrees(obj.rotation_euler[0]),
             rot_z
            ],
        [obj.scale[0],obj.scale[1],obj.scale[2]])
    )



with open(r"E:\w3_uncook\export_yml\newtree.yaml", "w") as f:

    layerName = 'architecture'
    worldName = "prologworld"
    dict = {}
    dict['layers'] = {}
    dict['layers'][layerName] = {}
    dict['layers'][layerName]['statics'] = {}
    dict['layers'][layerName]['statics']
    dict['layers'][layerName]["world"] = worldName
    for item in items:
        dict['layers'][layerName]['statics'][item.name] = {}
        dict['layers'][layerName]['statics'][item.name]['.type'] = "CEntity"
        dict['layers'][layerName]['statics'][item.name]['.debug'] = {}
        dict['layers'][layerName]['statics'][item.name]['.debug']['mesh'] = item.mesh
        dict['layers'][layerName]['statics'][item.name]['.debug']['meshpreview'] = item.meshpreview
        dict['layers'][layerName]['statics'][item.name]['transform'] = {}
        dict['layers'][layerName]['statics'][item.name]['transform']['pos'] = item.pos
        dict['layers'][layerName]['statics'][item.name]['transform']['rot'] = item.rot
        dict['layers'][layerName]['statics'][item.name]['transform']['scale'] = item.scale
        dict['layers'][layerName]['statics'][item.name]['streamingDistance'] = 200
        dict['layers'][layerName]['statics'][item.name]['components'] = {}
        dict['layers'][layerName]['statics'][item.name]['components']['mesh'] = {}
        dict['layers'][layerName]['statics'][item.name]['components']['mesh']['.type'] = "CStaticMeshComponent"
        dict['layers'][layerName]['statics'][item.name]['components']['mesh']['isStreamed'] = True
        dict['layers'][layerName]['statics'][item.name]['components']['mesh']['forceAutoHideDistance'] = 200
        dict['layers'][layerName]['statics'][item.name]['components']['mesh']['drawableFlags'] = {}
        dict['layers'][layerName]['statics'][item.name]['components']['mesh']['drawableFlags'] = ["IsVisible", "CastShadows"]
        dict['layers'][layerName]['statics'][item.name]['components']['mesh']['mesh'] = item.mesh
    yaml.dump(dict, f, indent=None)

#for collection in bpy.data.collections:
#   print(collection.name)
#   for obj in collection.all_objects:
#      print("obj: ", obj.name)
#      print(obj['repo_path'])