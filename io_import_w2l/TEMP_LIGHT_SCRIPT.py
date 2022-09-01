#MATRIX TEXT STUFF

import bpy
import math
import mathutils
from mathutils import Matrix
from mathutils import Euler
from math import radians

obj = bpy.data.objects["CGameplayEntity_empty_transform.001"]

print(obj.name)

#obj.rotation_euler = (0,0,0)

x, y, z = (radians(0.0), radians(0.0), radians(0.0))
mat = Euler((x, y, z)).to_matrix().to_4x4()
obj.matrix_world = obj.matrix_world @ mat

x, y, z = (radians(0.0), radians(0.0), radians(181.2404))
mat = Euler((x, y, z)).to_matrix().to_4x4()

mat[0][0], mat[0][1], mat[0][2] = -mat[0][0], -mat[0][1], mat[0][2]
mat[1][0], mat[1][1], mat[1][2] = -mat[1][0], -mat[1][1], mat[1][2]
mat[2][0], mat[2][1], mat[2][2] = -mat[2][0], -mat[2][1], mat[2][2]

obj.matrix_world = obj.matrix_world @ mat
















#######################


meshes = set(o.data for o in scene.objects if o.type == 'MESH')

import bpy
repo_path = r"environment\decorations\brothel_furniture\brothel_lanterns\lantern_red.w2mesh"


print("finding repo")
for o in bpy.context.scene.objects:
    if o.type != 'MESH':
        continue
    if 'repo_path' in o and o['repo_path'] == repo_path:
        print("COPYING", o['repo_path'])
        
        new_obj = o.copy()
        new_obj.data = o.data.copy()
        new_obj.animation_data_clear()
        bpy.context.collection.objects.link(new_obj)
        new_obj.location[0] = 0
        new_obj.location[1] = 0
        new_obj.location[2] = 0
        new_obj.rotation_euler[0] = 0
        new_obj.rotation_euler[1] = 0
        new_obj.rotation_euler[2] = 0
        new_obj.scale[0] = 1
        new_obj.scale[1] = 1
        new_obj.scale[2] = 1
        new_obj.parent = None
        break
        


################# LIGHT SCRIPT ################


import bpy

brightness = 40.0
radius = 3.0
loc_z = -1.524999976158142

color = (255/255,192/255,163/255)


name = "Light Ent"

bpy.ops.object.empty_add(type="SPHERE")

empty = bpy.context.object
empty.name = name
empty.location[2] = 5


bpy.ops.object.light_add(type='POINT', radius=1, align='WORLD', location=(0, 0, 0), scale=(1, 1, 1))

objs = bpy.context.selected_objects[:]

for obj in objs:
    obj.parent = empty
    bpy.context.object.data.energy = brightness
    bpy.context.object.data.color = color
    bpy.context.object.data.shadow_soft_size = radius
    obj.location[0] = 0
    obj.location[1] = 0
    obj.location[2] = loc_z