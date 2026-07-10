"""Geometry Nodes integration for imported APX cloth assets."""

import logging
from typing import Dict

import bpy
from bpy.types import Object

from .. import get_redcloth_simulation_enabled, get_redcloth_wind_velocity


log = logging.getLogger(__name__)

__all__ = [
    "apply_redcloth_runtime_defaults",
    "create_collision_proxy_object",
    "find_clothsimulation_modifier",
    "fix_connection_objects_transform_space",
    "patch_clothsimulation_to_object_proxies",
]


def find_clothsimulation_modifier(obj: Object):
    if obj is None or obj.type != 'MESH':
        return None
    for mod in obj.modifiers:
        if mod.type != 'NODES':
            continue
        if mod.name == "ClothSimulation":
            return mod
        node_group = getattr(mod, "node_group", None)
        if node_group and node_group.name.startswith("ClothSimulation"):
            return mod
    return None


def apply_redcloth_runtime_defaults(cloth_obj: Object, context) -> None:
    """Apply global runtime defaults (simulation enabled + wind velocity) to imported APX cloth."""
    mod = find_clothsimulation_modifier(cloth_obj)
    if mod is None:
        return

    try:
        sim_enabled = bool(get_redcloth_simulation_enabled(context))
    except Exception:
        sim_enabled = True
    try:
        wind_velocity = float(get_redcloth_wind_velocity(context))
    except Exception:
        wind_velocity = 0.0

    try:
        mod.show_viewport = sim_enabled
    except Exception:
        pass
    try:
        mod.show_render = sim_enabled
    except Exception:
        pass

    try:
        mod["Socket_5"] = wind_velocity
        node_group = getattr(mod, "node_group", None)
        if node_group is not None:
            node_group.interface_update(context)
    except Exception as e:
        log.debug("Could not apply redcloth wind velocity to %s: %s", getattr(cloth_obj, "name", "<cloth>"), e)

    # Do not write into io_mesh_apx UI state here. Its update callback assumes the
    # active object owns a ClothSimulation modifier and can raise during batch/entity import.


def fix_connection_objects_transform_space(connection_objects):
    """Switch Object Info nodes in SphereConnection GN groups from ORIGINAL to RELATIVE.

    APX creates connection objects whose SphereConnectionTemplate GN reads sphere
    positions in ORIGINAL (world) space.  When these objects are parented to a moving
    hierarchy the parent chain shifts them by D AND the GN geometry is also already at
    the new world position → double transform.  RELATIVE makes the geometry relative to
    the modifier object, so the parent chain offset cancels correctly.
    """
    for obj in connection_objects:
        if obj is None or obj.name not in bpy.data.objects:
            continue
        for mod in obj.modifiers:
            ng = getattr(mod, 'node_group', None)
            if ng is None:
                continue
            for node in ng.nodes:
                if node.bl_idname == 'GeometryNodeObjectInfo' and hasattr(node, 'transform_space'):
                    try:
                        node.transform_space = 'RELATIVE'
                    except Exception as e:
                        log.debug("Could not set transform_space RELATIVE on %s in %s: %s", node.name, ng.name, e)


def _ensure_geometry_output_interface(node_group):
    """Create a geometry output socket for runtime-created GN groups (Blender 4.x API)."""
    try:
        node_group.interface.new_socket(
            name="Geometry",
            in_out='OUTPUT',
            socket_type='NodeSocketGeometry',
        )
        return
    except Exception:
        pass
    # Blender compatibility fallback (older API)
    try:
        node_group.outputs.new("NodeSocketGeometry", "Geometry")
    except Exception:
        pass


def _find_socket_by_names(sockets, names):
    names_l = {n.lower() for n in names if n}
    for sock in sockets:
        if (getattr(sock, "name", "") or "").lower() in names_l:
            return sock
    return None


def _first_geometry_output(node):
    for sock in getattr(node, "outputs", []):
        if getattr(sock, "type", None) == 'GEOMETRY':
            return sock
    if getattr(node, "outputs", None):
        return node.outputs[0]
    return None


def _first_geometry_input(node):
    for sock in getattr(node, "inputs", []):
        if getattr(sock, "type", None) == 'GEOMETRY':
            return sock
    if getattr(node, "inputs", None):
        return node.inputs[0]
    return None


def _role_from_token(token: str):
    t = (token or "").lower()
    if not t:
        return None
    if "input_14" in t or "collision sphere" in t or "spheres" in t or "sphere" in t:
        return "spheres"
    if "socket_2" in t or "connection" in t:
        return "connections"
    if "socket_3" in t or "capsule" in t:
        return "capsules"
    return None


def _classify_collection_info_node(node, mod=None):
    # Try linked group-input socket identifiers first (usually Input_14/Socket_2/Socket_3).
    coll_input = _find_socket_by_names(node.inputs, {"Collection"})
    if coll_input and coll_input.is_linked:
        link = coll_input.links[0]
        from_sock = link.from_socket
        for token in (
            getattr(from_sock, "identifier", ""),
            getattr(from_sock, "name", ""),
        ):
            role = _role_from_token(token)
            if role:
                return role

        if mod is not None:
            for token in (getattr(from_sock, "identifier", ""), getattr(from_sock, "name", "")):
                if not token:
                    continue
                try:
                    coll_val = mod[token]
                except Exception:
                    coll_val = None
                role = _role_from_token(getattr(coll_val, "name", ""))
                if role:
                    return role

    # Fall back to node label/name/default collection name.
    for token in (
        getattr(node, "label", ""),
        getattr(node, "name", ""),
    ):
        role = _role_from_token(token)
        if role:
            return role
    try:
        default_coll = coll_input.default_value if coll_input else None
        role = _role_from_token(getattr(default_coll, "name", ""))
        if role:
            return role
    except Exception:
        pass
    return None


def _create_object_join_proxy_nodegroup(group_name: str, source_objects):
    ng = bpy.data.node_groups.new(group_name, 'GeometryNodeTree')
    _ensure_geometry_output_interface(ng)

    nodes = ng.nodes
    links = ng.links
    nodes.clear()

    group_out = nodes.new('NodeGroupOutput')
    group_out.location = (450, 0)
    join = nodes.new('GeometryNodeJoinGeometry')
    join.location = (220, 0)

    y = 0
    for src_obj in source_objects:
        if src_obj is None or src_obj.name not in bpy.data.objects:
            continue
        obj_info = nodes.new('GeometryNodeObjectInfo')
        obj_info.label = f"ProxySource: {src_obj.name}"
        obj_info.location = (-120, y)

        obj_socket = _find_socket_by_names(obj_info.inputs, {"Object"})
        if obj_socket is not None:
            try:
                obj_socket.default_value = src_obj
            except Exception as e:
                log.debug("Failed assigning proxy source object %s: %s", src_obj.name, e)

        as_instance_socket = _find_socket_by_names(obj_info.inputs, {"As Instance"})
        if as_instance_socket is not None:
            try:
                as_instance_socket.default_value = False
            except Exception:
                pass

        if hasattr(obj_info, "transform_space"):
            try:
                obj_info.transform_space = 'RELATIVE'
            except Exception:
                pass

        out_sock = _first_geometry_output(obj_info)
        in_sock = _first_geometry_input(join)
        if out_sock is not None and in_sock is not None:
            links.new(out_sock, in_sock)
        y -= 140

    join_out = _first_geometry_output(join)
    out_in = _first_geometry_input(group_out)
    if join_out is not None and out_in is not None:
        links.new(join_out, out_in)

    return ng


def create_collision_proxy_object(name: str, parent: Object, owner_collection, source_objects):
    if not source_objects:
        return None

    mesh = bpy.data.meshes.new(name + "_MESH")
    proxy = bpy.data.objects.new(name, mesh)
    owner_collection.objects.link(proxy)
    proxy.parent = parent
    proxy.hide_render = True
    proxy.hide_select = True
    try:
        proxy.display_type = 'WIRE'
    except Exception:
        pass
    try:
        proxy["witcher_apx_collision_proxy"] = True
    except Exception:
        pass

    node_group = _create_object_join_proxy_nodegroup(name + "_GN", source_objects)
    mod = proxy.modifiers.new(name="WitcherAPXCollisionProxy", type='NODES')
    mod.node_group = node_group
    return proxy


def _replace_collection_info_node_with_object_info(node_group, coll_node, proxy_obj):
    if node_group is None or coll_node is None or proxy_obj is None:
        return False

    nodes = node_group.nodes
    links = node_group.links

    obj_info = nodes.new('GeometryNodeObjectInfo')
    obj_info.location = coll_node.location
    obj_info.label = f"Proxy:{proxy_obj.name}"
    obj_info.name = coll_node.name + "_Proxy"

    obj_socket = _find_socket_by_names(obj_info.inputs, {"Object"})
    if obj_socket is not None:
        obj_socket.default_value = proxy_obj
    as_instance_socket = _find_socket_by_names(obj_info.inputs, {"As Instance"})
    if as_instance_socket is not None:
        try:
            as_instance_socket.default_value = False
        except Exception:
            pass
    if hasattr(obj_info, "transform_space"):
        try:
            obj_info.transform_space = 'RELATIVE'
        except Exception:
            pass

    old_out = _first_geometry_output(coll_node)
    new_out = _first_geometry_output(obj_info)
    if old_out is None or new_out is None:
        nodes.remove(obj_info)
        return False

    outgoing = [(link.to_socket, link.to_node) for link in list(old_out.links)]
    for to_socket, _to_node in outgoing:
        try:
            links.new(new_out, to_socket)
        except Exception as e:
            log.debug("Failed rewiring cloth collision proxy link on %s: %s", node_group.name, e)

    try:
        nodes.remove(coll_node)
    except Exception:
        return False
    return True


def _find_clothsim_colliders_group_node(node_group):
    if node_group is None:
        return None
    # APX template (from your example script) usually uses "Group.008" for SoftBody.Init.Colliders.
    for node in node_group.nodes:
        if node.bl_idname != 'GeometryNodeGroup':
            continue
        sub_tree = getattr(node, "node_tree", None)
        sub_name = getattr(sub_tree, "name", "") or ""
        node_name = getattr(node, "name", "") or ""
        if "SoftBody.Init.Colliders" in sub_name or "Softbody.Init Colliders" in sub_name:
            return node
        if node_name == "Group.008":
            return node
    return None


def _patch_clothsim_groupnode_colliders_to_proxies(node_group, proxy_objects: Dict[str, Object]) -> bool:
    """Patch the actual APX ClothSimulation group node graph (Group.008) to use object proxies."""
    colliders_group_node = _find_clothsim_colliders_group_node(node_group)
    if colliders_group_node is None:
        return False

    nodes = node_group.nodes
    links = node_group.links
    colliders_out = _first_geometry_output(colliders_group_node)
    if colliders_out is None:
        return False

    # Collect outgoing targets before rewiring.
    outgoing_links = list(colliders_out.links)
    outgoing_targets = [lnk.to_socket for lnk in outgoing_links]

    # Create or reuse a join node inside the actual ClothSimulation node tree.
    join_name = "WitcherAPXColliderProxyJoin"
    join_node = nodes.get(join_name)
    if join_node is None or join_node.bl_idname != 'GeometryNodeJoinGeometry':
        if join_node is not None:
            nodes.remove(join_node)
        join_node = nodes.new('GeometryNodeJoinGeometry')
        join_node.name = join_name
    join_node.label = "Witcher APX Collision Proxies"
    try:
        base_loc = colliders_group_node.location
        join_node.location = (base_loc[0] + 240.0, base_loc[1] - 40.0)
    except Exception:
        pass

    join_input = _first_geometry_input(join_node)
    join_output = _first_geometry_output(join_node)
    if join_input is None or join_output is None:
        return False

    # Reset join inputs so reruns don't duplicate links.
    for link in list(join_input.links):
        try:
            links.remove(link)
        except Exception:
            pass

    role_order = ("spheres", "connections", "capsules")
    role_y = {
        "spheres": 160.0,
        "connections": 0.0,
        "capsules": -160.0,
    }
    wired_any_proxy = False
    for role in role_order:
        proxy_obj = proxy_objects.get(role)
        if proxy_obj is None or proxy_obj.name not in bpy.data.objects:
            continue

        node_name = f"WitcherAPXColliderProxy_{role}"
        obj_info = nodes.get(node_name)
        if obj_info is None or obj_info.bl_idname != 'GeometryNodeObjectInfo':
            if obj_info is not None:
                nodes.remove(obj_info)
            obj_info = nodes.new('GeometryNodeObjectInfo')
            obj_info.name = node_name
        obj_info.label = f"Proxy {role}"
        try:
            base_loc = colliders_group_node.location
            obj_info.location = (base_loc[0], base_loc[1] + role_y[role])
        except Exception:
            pass

        obj_socket = _find_socket_by_names(obj_info.inputs, {"Object"})
        if obj_socket is not None:
            try:
                obj_socket.default_value = proxy_obj
            except Exception as e:
                log.debug("Could not assign ClothSimulation proxy object %s (%s): %s", role, proxy_obj.name, e)
        as_instance_socket = _find_socket_by_names(obj_info.inputs, {"As Instance"})
        if as_instance_socket is not None:
            try:
                as_instance_socket.default_value = False
            except Exception:
                pass
        if hasattr(obj_info, "transform_space"):
            try:
                obj_info.transform_space = 'RELATIVE'
            except Exception:
                pass

        out_sock = _first_geometry_output(obj_info)
        if out_sock is None:
            continue
        try:
            links.new(out_sock, join_input)
            wired_any_proxy = True
        except Exception as e:
            log.debug("Failed linking ClothSimulation proxy %s into join node: %s", role, e)

    if not wired_any_proxy:
        return False

    # Replace all downstream consumers of Group.008 output with the proxy join output.
    for link in outgoing_links:
        try:
            links.remove(link)
        except Exception:
            pass
    for to_socket in outgoing_targets:
        try:
            links.new(join_output, to_socket)
        except Exception as e:
            log.debug("Failed rewiring ClothSimulation collider output to proxy join: %s", e)

    # Keep the APX colliders group node in place (for compatibility / easier inspection) but mute it.
    try:
        colliders_group_node.mute = True
        colliders_group_node.label = "APX Colliders (Bypassed by Witcher Proxy Patch)"
    except Exception:
        pass
    return True


def _find_gn_group_node(node_group, node_name: str = "", subtree_name_contains: str = ""):
    if node_group is None:
        return None
    node_name = (node_name or "").lower()
    subtree_name_contains = (subtree_name_contains or "").lower()
    for node in node_group.nodes:
        if node.bl_idname != 'GeometryNodeGroup':
            continue
        if node_name and (getattr(node, "name", "") or "").lower() == node_name:
            return node
        if subtree_name_contains:
            sub_tree = getattr(node, "node_tree", None)
            sub_name = (getattr(sub_tree, "name", "") or "").lower()
            if subtree_name_contains in sub_name:
                return node
    return None


def _find_top_level_node(node_group, bl_idname: str, node_name: str = ""):
    if node_group is None:
        return None
    node_name_l = (node_name or "").lower()
    for node in node_group.nodes:
        if node.bl_idname != bl_idname:
            continue
        if not node_name_l or (getattr(node, "name", "") or "").lower() == node_name_l:
            return node
    return None


def _rewire_input_socket(links, input_socket, from_socket) -> bool:
    if input_socket is None or from_socket is None:
        return False
    for lnk in list(input_socket.links):
        try:
            links.remove(lnk)
        except Exception:
            pass
    try:
        links.new(from_socket, input_socket)
        return True
    except Exception:
        return False


def _ensure_clothsim_physical_source_index_attr(node_group, attr_name: str = "witcher_src_vert_idx"):
    """Patch the APX physical-mesh builder (inside Softbody.Init.Cloth) to store a source vertex index.

    This is evaluated once through the APX Bake path and lets us sample live skinned positions cheaply later.
    """
    if node_group is None:
        return None

    init_cloth_node = _find_gn_group_node(
        node_group,
        node_name="Group.005",
        subtree_name_contains="softbody.init.cloth",
    )
    init_cloth_tree = getattr(init_cloth_node, "node_tree", None) if init_cloth_node else None
    if init_cloth_tree is None:
        return None

    phys_builder_node = _find_gn_group_node(init_cloth_tree, node_name="Group.005")
    if phys_builder_node is None:
        for node in init_cloth_tree.nodes:
            if node.bl_idname != 'GeometryNodeGroup':
                continue
            if _find_socket_by_names(getattr(node, "outputs", []), {"Full Physical Mesh"}) is not None and \
               _find_socket_by_names(getattr(node, "outputs", []), {"Simulated Mesh"}) is not None:
                phys_builder_node = node
                break
    phys_builder_tree = getattr(phys_builder_node, "node_tree", None) if phys_builder_node else None
    if phys_builder_tree is None:
        return None

    nodes = phys_builder_tree.nodes
    links = phys_builder_tree.links
    triangulate_node = nodes.get("Triangulate")
    group_input_node = nodes.get("Group Input")
    if triangulate_node is None or group_input_node is None:
        # Fallback to older patch point if APX variant differs.
        separate_node = nodes.get("Separate Geometry")
        merge_node = nodes.get("Merge by Distance")
        if separate_node is None or merge_node is None:
            return None
    else:
        separate_node = None
        merge_node = None

    idx_node = nodes.get("WitcherAPXSrcVertIndex")
    if idx_node is None or idx_node.bl_idname != 'GeometryNodeInputIndex':
        if idx_node is not None:
            nodes.remove(idx_node)
        idx_node = nodes.new('GeometryNodeInputIndex')
        idx_node.name = "WitcherAPXSrcVertIndex"
    store_node = nodes.get("WitcherAPXStoreSrcVertIndex")
    if store_node is None or store_node.bl_idname != 'GeometryNodeStoreNamedAttribute':
        if store_node is not None:
            nodes.remove(store_node)
        store_node = nodes.new('GeometryNodeStoreNamedAttribute')
        store_node.name = "WitcherAPXStoreSrcVertIndex"

    try:
        store_node.data_type = 'INT'
    except Exception:
        pass
    try:
        store_node.domain = 'POINT'
    except Exception:
        pass
    try:
        if len(store_node.inputs) > 1:
            store_node.inputs[1].default_value = True
    except Exception:
        pass
    try:
        if len(store_node.inputs) > 2:
            store_node.inputs[2].default_value = attr_name
    except Exception:
        pass
    store_geom_in = _find_socket_by_names(store_node.inputs, {"Geometry"}) or (store_node.inputs[0] if store_node.inputs else None)
    store_val_in = _find_socket_by_names(store_node.inputs, {"Value"}) or (store_node.inputs[3] if len(store_node.inputs) > 3 else None)
    store_geom_out = _first_geometry_output(store_node)
    if store_geom_in is None or store_val_in is None or store_geom_out is None:
        return None

    # Preferred path: compute source vertex mapping on the final triangulated physical mesh (more stable than pre-merge).
    if triangulate_node is not None and group_input_node is not None:
        nearest_node = nodes.get("WitcherAPXSrcVertNearest")
        if nearest_node is None or nearest_node.bl_idname != 'GeometryNodeSampleNearest':
            if nearest_node is not None:
                nodes.remove(nearest_node)
            nearest_node = nodes.new('GeometryNodeSampleNearest')
            nearest_node.name = "WitcherAPXSrcVertNearest"
        try:
            nearest_node.domain = 'POINT'
        except Exception:
            pass

        pos_node = nodes.get("WitcherAPXSrcVertNearestPos")
        if pos_node is None or pos_node.bl_idname != 'GeometryNodeInputPosition':
            if pos_node is not None:
                nodes.remove(pos_node)
            pos_node = nodes.new('GeometryNodeInputPosition')
            pos_node.name = "WitcherAPXSrcVertNearestPos"

        tri_geom_out = _first_geometry_output(triangulate_node)
        src_geom_out = _find_socket_by_names(group_input_node.outputs, {"Geometry"}) or (group_input_node.outputs[0] if group_input_node.outputs else None)
        pos_out = _find_socket_by_names(pos_node.outputs, {"Position"}) or (pos_node.outputs[0] if pos_node.outputs else None)
        near_geom_in = _find_socket_by_names(nearest_node.inputs, {"Mesh", "Geometry"}) or (nearest_node.inputs[0] if nearest_node.inputs else None)
        near_pos_in = _find_socket_by_names(nearest_node.inputs, {"Sample Position", "Position", "Value"}) or (nearest_node.inputs[1] if len(nearest_node.inputs) > 1 else None)
        near_idx_out = _find_socket_by_names(nearest_node.outputs, {"Index", "Value"}) or (nearest_node.outputs[0] if nearest_node.outputs else None)

        if all([tri_geom_out, src_geom_out, pos_out, near_geom_in, near_pos_in, near_idx_out]):
            # Wire nearest-sample fields.
            _rewire_input_socket(links, near_geom_in, src_geom_out)
            _rewire_input_socket(links, near_pos_in, pos_out)
            _rewire_input_socket(links, store_geom_in, tri_geom_out)
            _rewire_input_socket(links, store_val_in, near_idx_out)

            # Replace downstream consumers of Triangulate with Store Named Attribute output.
            outgoing_links = [lnk for lnk in list(tri_geom_out.links) if lnk.to_socket != store_geom_in]
            outgoing_targets = [lnk.to_socket for lnk in outgoing_links]
            for lnk in outgoing_links:
                try:
                    links.remove(lnk)
                except Exception:
                    pass
            for to_socket in outgoing_targets:
                try:
                    links.new(store_geom_out, to_socket)
                except Exception:
                    pass

            try:
                sx, sy = triangulate_node.location
                store_node.location = (sx + 180.0, sy - 20.0)
                nearest_node.location = (sx - 200.0, sy - 220.0)
                pos_node.location = (sx - 410.0, sy - 230.0)
                idx_node.location = (sx - 410.0, sy - 380.0)  # keep legacy helper node out of the way
            except Exception:
                pass
            try:
                store_node.label = "Witcher Source Vert Index (Baked, final phys mesh)"
            except Exception:
                pass
            return attr_name

    # Fallback: older pre-merge index path.
    if separate_node is None or merge_node is None:
        return None

    sep_geom_out = _first_geometry_output(separate_node)
    merge_geom_in = _first_geometry_input(merge_node)
    idx_out = _find_socket_by_names(idx_node.outputs, {"Index"}) or (idx_node.outputs[0] if idx_node.outputs else None)
    if not all([sep_geom_out, merge_geom_in, idx_out]):
        return None

    _rewire_input_socket(links, store_geom_in, sep_geom_out)
    _rewire_input_socket(links, store_val_in, idx_out)
    if not _rewire_input_socket(links, merge_geom_in, store_geom_out):
        return None

    try:
        sx, sy = separate_node.location
        store_node.location = (sx + 180.0, sy - 20.0)
        idx_node.location = (sx + 10.0, sy - 210.0)
    except Exception:
        pass
    try:
        store_node.label = "Witcher Source Vert Index (Baked, fallback)"
    except Exception:
        pass
    return attr_name


def _patch_clothsim_add_live_armature_pose(node_group) -> bool:
    """Add a fast live armature-driven pose path while preserving APX Bake/static setup."""
    if node_group is None:
        return False

    links = node_group.links
    nodes = node_group.nodes

    group_input_node = _find_top_level_node(node_group, 'NodeGroupInput', node_name="Group Input")
    sim_input_node = _find_top_level_node(node_group, 'GeometryNodeSimulationInput', node_name="Simulation Input")
    step_cloth_node = _find_gn_group_node(node_group, node_name="Group.003", subtree_name_contains="softbody.step.cloth")
    update_graphical_node = _find_gn_group_node(node_group, node_name="Group", subtree_name_contains="updategraphicalmesh")
    grab_selection_node = _find_gn_group_node(node_group, node_name="Group.006", subtree_name_contains="grabselection")

    if not group_input_node or not sim_input_node or not step_cloth_node:
        return False

    src_geom_out = _find_socket_by_names(group_input_node.outputs, {"Geometry"}) or (group_input_node.outputs[0] if group_input_node.outputs else None)
    sim_phys_out = _find_socket_by_names(sim_input_node.outputs, {"Physical Mesh"}) or (sim_input_node.outputs[1] if len(sim_input_node.outputs) > 1 else None)
    sim_graph_out = _find_socket_by_names(sim_input_node.outputs, {"Graphical Mesh"}) or (sim_input_node.outputs[2] if len(sim_input_node.outputs) > 2 else None)
    step_phys_in = _find_socket_by_names(step_cloth_node.inputs, {"Physical Mesh"}) or (step_cloth_node.inputs[0] if len(step_cloth_node.inputs) > 0 else None)

    if src_geom_out is None or sim_phys_out is None or step_phys_in is None:
        return False

    # Ensure the APX init/bake path stores a source-vertex mapping on the baked physical mesh.
    phys_src_attr_name = _ensure_clothsim_physical_source_index_attr(node_group)

    # Build/reuse shared field nodes.
    idx_node = nodes.get("WitcherAPXLivePoseIndex")
    if idx_node is None or idx_node.bl_idname != 'GeometryNodeInputIndex':
        if idx_node is not None:
            nodes.remove(idx_node)
        idx_node = nodes.new('GeometryNodeInputIndex')
        idx_node.name = "WitcherAPXLivePoseIndex"

    pos_node = nodes.get("WitcherAPXLivePosePosition")
    if pos_node is None or pos_node.bl_idname != 'GeometryNodeInputPosition':
        if pos_node is not None:
            nodes.remove(pos_node)
        pos_node = nodes.new('GeometryNodeInputPosition')
        pos_node.name = "WitcherAPXLivePosePosition"

    sim_attr_node = nodes.get("WitcherAPXLivePoseSimulatedAttr")
    if sim_attr_node is None or sim_attr_node.bl_idname != 'GeometryNodeInputNamedAttribute':
        if sim_attr_node is not None:
            nodes.remove(sim_attr_node)
        sim_attr_node = nodes.new('GeometryNodeInputNamedAttribute')
        sim_attr_node.name = "WitcherAPXLivePoseSimulatedAttr"
    try:
        sim_attr_node.data_type = 'BOOLEAN'
    except Exception:
        pass
    try:
        sim_attr_node.inputs[0].default_value = "simulated"
    except Exception:
        pass

    not_node = nodes.get("WitcherAPXLivePoseNot")
    if not_node is None or not_node.bl_idname != 'FunctionNodeBooleanMath':
        if not_node is not None:
            nodes.remove(not_node)
        not_node = nodes.new('FunctionNodeBooleanMath')
        not_node.name = "WitcherAPXLivePoseNot"
    try:
        not_node.operation = 'NOT'
    except Exception:
        pass

    # Physical-mesh live pose injection (non-simulated verts only).
    phys_maxdist_attr_node = nodes.get("WitcherAPXLivePosePhysMaxDistAttr")
    if phys_maxdist_attr_node is None or phys_maxdist_attr_node.bl_idname != 'GeometryNodeInputNamedAttribute':
        if phys_maxdist_attr_node is not None:
            nodes.remove(phys_maxdist_attr_node)
        phys_maxdist_attr_node = nodes.new('GeometryNodeInputNamedAttribute')
        phys_maxdist_attr_node.name = "WitcherAPXLivePosePhysMaxDistAttr"
    try:
        phys_maxdist_attr_node.data_type = 'FLOAT'
    except Exception:
        pass
    try:
        phys_maxdist_attr_node.inputs[0].default_value = "PhysXMaximumDistanceScaled"
    except Exception:
        pass

    phys_sim_cmp_node = nodes.get("WitcherAPXLivePosePhysSimulatedCmp")
    if phys_sim_cmp_node is None or phys_sim_cmp_node.bl_idname != 'FunctionNodeCompare':
        if phys_sim_cmp_node is not None:
            nodes.remove(phys_sim_cmp_node)
        phys_sim_cmp_node = nodes.new('FunctionNodeCompare')
        phys_sim_cmp_node.name = "WitcherAPXLivePosePhysSimulatedCmp"
    try:
        phys_sim_cmp_node.data_type = 'FLOAT'
        phys_sim_cmp_node.mode = 'ELEMENT'
        phys_sim_cmp_node.operation = 'GREATER_THAN'
    except Exception:
        pass
    try:
        # Float compare "B" is usually input 1.
        if len(phys_sim_cmp_node.inputs) > 1:
            phys_sim_cmp_node.inputs[1].default_value = 0.0
    except Exception:
        pass

    phys_not_node = nodes.get("WitcherAPXLivePosePhysNot")
    if phys_not_node is None or phys_not_node.bl_idname != 'FunctionNodeBooleanMath':
        if phys_not_node is not None:
            nodes.remove(phys_not_node)
        phys_not_node = nodes.new('FunctionNodeBooleanMath')
        phys_not_node.name = "WitcherAPXLivePosePhysNot"
    try:
        phys_not_node.operation = 'NOT'
    except Exception:
        pass

    phys_src_attr_node = nodes.get("WitcherAPXLivePosePhysSourceVertAttr")
    if phys_src_attr_node is None or phys_src_attr_node.bl_idname != 'GeometryNodeInputNamedAttribute':
        if phys_src_attr_node is not None:
            nodes.remove(phys_src_attr_node)
        phys_src_attr_node = nodes.new('GeometryNodeInputNamedAttribute')
        phys_src_attr_node.name = "WitcherAPXLivePosePhysSourceVertAttr"
    try:
        phys_src_attr_node.data_type = 'INT'
    except Exception:
        pass
    try:
        phys_src_attr_node.inputs[0].default_value = phys_src_attr_name or "witcher_src_vert_idx"
    except Exception:
        pass

    phys_sample_node = nodes.get("WitcherAPXLivePosePhysSample")
    if phys_sample_node is None or phys_sample_node.bl_idname != 'GeometryNodeSampleIndex':
        if phys_sample_node is not None:
            nodes.remove(phys_sample_node)
        phys_sample_node = nodes.new('GeometryNodeSampleIndex')
        phys_sample_node.name = "WitcherAPXLivePosePhysSample"
    try:
        phys_sample_node.clamp = False
    except Exception:
        pass
    try:
        phys_sample_node.data_type = 'FLOAT_VECTOR'
    except Exception:
        pass
    try:
        phys_sample_node.domain = 'POINT'
    except Exception:
        pass

    src_normal_node = nodes.get("WitcherAPXLivePoseSourceNormal")
    if src_normal_node is None or src_normal_node.bl_idname != 'GeometryNodeInputNormal':
        if src_normal_node is not None:
            nodes.remove(src_normal_node)
        src_normal_node = nodes.new('GeometryNodeInputNormal')
        src_normal_node.name = "WitcherAPXLivePoseSourceNormal"
    try:
        # Blender API compatibility (property exists on some versions).
        src_normal_node.legacy_corner_normals = True
    except Exception:
        pass

    phys_normal_sample_node = nodes.get("WitcherAPXLivePosePhysNormalSample")
    if phys_normal_sample_node is None or phys_normal_sample_node.bl_idname != 'GeometryNodeSampleIndex':
        if phys_normal_sample_node is not None:
            nodes.remove(phys_normal_sample_node)
        phys_normal_sample_node = nodes.new('GeometryNodeSampleIndex')
        phys_normal_sample_node.name = "WitcherAPXLivePosePhysNormalSample"
    try:
        phys_normal_sample_node.clamp = False
    except Exception:
        pass
    try:
        phys_normal_sample_node.data_type = 'FLOAT_VECTOR'
    except Exception:
        pass
    try:
        phys_normal_sample_node.domain = 'POINT'
    except Exception:
        pass

    phys_normalize_node = nodes.get("WitcherAPXLivePosePhysNormalNormalize")
    if phys_normalize_node is None or phys_normalize_node.bl_idname != 'ShaderNodeVectorMath':
        if phys_normalize_node is not None:
            nodes.remove(phys_normalize_node)
        phys_normalize_node = nodes.new('ShaderNodeVectorMath')
        phys_normalize_node.name = "WitcherAPXLivePosePhysNormalNormalize"
    try:
        phys_normalize_node.operation = 'NORMALIZE'
    except Exception:
        pass

    setpos_node = nodes.get("WitcherAPXLivePoseSetPosition")
    if setpos_node is None or setpos_node.bl_idname != 'GeometryNodeSetPosition':
        if setpos_node is not None:
            nodes.remove(setpos_node)
        setpos_node = nodes.new('GeometryNodeSetPosition')
        setpos_node.name = "WitcherAPXLivePoseSetPosition"
    try:
        setpos_node.inputs[3].default_value = (0.0, 0.0, 0.0)
    except Exception:
        pass

    phys_store_pinned_pos_node = nodes.get("WitcherAPXLivePoseStorePinnedPosition")
    if phys_store_pinned_pos_node is None or phys_store_pinned_pos_node.bl_idname != 'GeometryNodeStoreNamedAttribute':
        if phys_store_pinned_pos_node is not None:
            nodes.remove(phys_store_pinned_pos_node)
        phys_store_pinned_pos_node = nodes.new('GeometryNodeStoreNamedAttribute')
        phys_store_pinned_pos_node.name = "WitcherAPXLivePoseStorePinnedPosition"
    try:
        phys_store_pinned_pos_node.data_type = 'FLOAT_VECTOR'
        phys_store_pinned_pos_node.domain = 'POINT'
    except Exception:
        pass
    try:
        phys_store_pinned_pos_node.inputs[1].default_value = True
        phys_store_pinned_pos_node.inputs[2].default_value = "pinned_position"
    except Exception:
        pass

    phys_store_pinned_norm_node = nodes.get("WitcherAPXLivePoseStorePinnedNormal")
    if phys_store_pinned_norm_node is None or phys_store_pinned_norm_node.bl_idname != 'GeometryNodeStoreNamedAttribute':
        if phys_store_pinned_norm_node is not None:
            nodes.remove(phys_store_pinned_norm_node)
        phys_store_pinned_norm_node = nodes.new('GeometryNodeStoreNamedAttribute')
        phys_store_pinned_norm_node.name = "WitcherAPXLivePoseStorePinnedNormal"
    try:
        phys_store_pinned_norm_node.data_type = 'FLOAT_VECTOR'
        phys_store_pinned_norm_node.domain = 'POINT'
    except Exception:
        pass
    try:
        phys_store_pinned_norm_node.inputs[1].default_value = True
        phys_store_pinned_norm_node.inputs[2].default_value = "pinned_normal"
    except Exception:
        pass

    phys_store_old_pos_node = nodes.get("WitcherAPXLivePoseStoreOldPosition")
    if phys_store_old_pos_node is None or phys_store_old_pos_node.bl_idname != 'GeometryNodeStoreNamedAttribute':
        if phys_store_old_pos_node is not None:
            nodes.remove(phys_store_old_pos_node)
        phys_store_old_pos_node = nodes.new('GeometryNodeStoreNamedAttribute')
        phys_store_old_pos_node.name = "WitcherAPXLivePoseStoreOldPosition"
    try:
        phys_store_old_pos_node.data_type = 'FLOAT_VECTOR'
        phys_store_old_pos_node.domain = 'POINT'
    except Exception:
        pass
    try:
        phys_store_old_pos_node.inputs[1].default_value = True
        phys_store_old_pos_node.inputs[2].default_value = "old_position"
    except Exception:
        pass

    phys_store_vel_node = nodes.get("WitcherAPXLivePoseStoreVelocity")
    if phys_store_vel_node is None or phys_store_vel_node.bl_idname != 'GeometryNodeStoreNamedAttribute':
        if phys_store_vel_node is not None:
            nodes.remove(phys_store_vel_node)
        phys_store_vel_node = nodes.new('GeometryNodeStoreNamedAttribute')
        phys_store_vel_node.name = "WitcherAPXLivePoseStoreVelocity"
    try:
        phys_store_vel_node.data_type = 'FLOAT_VECTOR'
        phys_store_vel_node.domain = 'POINT'
    except Exception:
        pass
    try:
        phys_store_vel_node.inputs[1].default_value = True
        phys_store_vel_node.inputs[2].default_value = "velocity"
        # Zero velocity for non-simulated verts when we snap them to the live rig pose.
        phys_store_vel_node.inputs[3].default_value = (0.0, 0.0, 0.0)
    except Exception:
        pass

    # Graphical-mesh live pose update for non-simulated verts (keeps baked APX mapping attrs).
    graph_sample_node = nodes.get("WitcherAPXLivePoseGraphSample")
    if graph_sample_node is None or graph_sample_node.bl_idname != 'GeometryNodeSampleIndex':
        if graph_sample_node is not None:
            nodes.remove(graph_sample_node)
        graph_sample_node = nodes.new('GeometryNodeSampleIndex')
        graph_sample_node.name = "WitcherAPXLivePoseGraphSample"
    try:
        graph_sample_node.clamp = False
    except Exception:
        pass
    try:
        graph_sample_node.data_type = 'FLOAT_VECTOR'
    except Exception:
        pass
    try:
        graph_sample_node.domain = 'POINT'
    except Exception:
        pass

    graph_setpos_node = nodes.get("WitcherAPXLivePoseGraphSetPosition")
    if graph_setpos_node is None or graph_setpos_node.bl_idname != 'GeometryNodeSetPosition':
        if graph_setpos_node is not None:
            nodes.remove(graph_setpos_node)
        graph_setpos_node = nodes.new('GeometryNodeSetPosition')
        graph_setpos_node.name = "WitcherAPXLivePoseGraphSetPosition"
    try:
        graph_setpos_node.inputs[3].default_value = (0.0, 0.0, 0.0)
    except Exception:
        pass

    # Layout near APX solver nodes.
    try:
        sx, sy = step_cloth_node.location
        setpos_node.location = (sx - 520.0, sy + 40.0)
        phys_store_pinned_pos_node.location = (sx - 280.0, sy + 30.0)
        phys_store_pinned_norm_node.location = (sx - 40.0, sy + 30.0)
        phys_store_old_pos_node.location = (sx + 200.0, sy + 30.0)
        phys_store_vel_node.location = (sx + 440.0, sy + 30.0)

        phys_sample_node.location = (sx - 780.0, sy + 20.0)
        phys_normal_sample_node.location = (sx - 780.0, sy + 190.0)
        phys_normalize_node.location = (sx - 560.0, sy + 200.0)
        src_normal_node.location = (sx - 1030.0, sy + 170.0)
        phys_src_attr_node.location = (sx - 1030.0, sy - 80.0)
        phys_maxdist_attr_node.location = (sx - 1030.0, sy + 10.0)
        phys_sim_cmp_node.location = (sx - 840.0, sy - 120.0)
        phys_not_node.location = (sx - 650.0, sy - 120.0)
        idx_node.location = (sx - 760.0, sy + 310.0)
        pos_node.location = (sx - 760.0, sy + 120.0)
        sim_attr_node.location = (sx - 530.0, sy + 230.0)
        not_node.location = (sx - 360.0, sy + 230.0)
        graph_sample_node.location = (sx - 520.0, sy + 350.0)
        graph_setpos_node.location = (sx - 280.0, sy + 370.0)
    except Exception:
        pass

    phys_sample_geom_in = _find_socket_by_names(phys_sample_node.inputs, {"Geometry"}) or (phys_sample_node.inputs[0] if len(phys_sample_node.inputs) > 0 else None)
    phys_sample_val_in = _find_socket_by_names(phys_sample_node.inputs, {"Value"}) or (phys_sample_node.inputs[1] if len(phys_sample_node.inputs) > 1 else None)
    phys_sample_idx_in = _find_socket_by_names(phys_sample_node.inputs, {"Index"}) or (phys_sample_node.inputs[2] if len(phys_sample_node.inputs) > 2 else None)
    phys_sample_val_out = _find_socket_by_names(phys_sample_node.outputs, {"Value"}) or (phys_sample_node.outputs[0] if phys_sample_node.outputs else None)
    phys_normal_sample_geom_in = _find_socket_by_names(phys_normal_sample_node.inputs, {"Geometry"}) or (phys_normal_sample_node.inputs[0] if len(phys_normal_sample_node.inputs) > 0 else None)
    phys_normal_sample_val_in = _find_socket_by_names(phys_normal_sample_node.inputs, {"Value"}) or (phys_normal_sample_node.inputs[1] if len(phys_normal_sample_node.inputs) > 1 else None)
    phys_normal_sample_idx_in = _find_socket_by_names(phys_normal_sample_node.inputs, {"Index"}) or (phys_normal_sample_node.inputs[2] if len(phys_normal_sample_node.inputs) > 2 else None)
    phys_normal_sample_val_out = _find_socket_by_names(phys_normal_sample_node.outputs, {"Value"}) or (phys_normal_sample_node.outputs[0] if phys_normal_sample_node.outputs else None)

    setpos_geom_in = _find_socket_by_names(setpos_node.inputs, {"Geometry"}) or (setpos_node.inputs[0] if len(setpos_node.inputs) > 0 else None)
    setpos_sel_in = _find_socket_by_names(setpos_node.inputs, {"Selection"}) or (setpos_node.inputs[1] if len(setpos_node.inputs) > 1 else None)
    setpos_pos_in = _find_socket_by_names(setpos_node.inputs, {"Position"}) or (setpos_node.inputs[2] if len(setpos_node.inputs) > 2 else None)
    setpos_geom_out = _first_geometry_output(setpos_node)

    phys_store_pinned_pos_geom_in = _find_socket_by_names(phys_store_pinned_pos_node.inputs, {"Geometry"}) or (phys_store_pinned_pos_node.inputs[0] if len(phys_store_pinned_pos_node.inputs) > 0 else None)
    phys_store_pinned_pos_val_in = _find_socket_by_names(phys_store_pinned_pos_node.inputs, {"Value"}) or (phys_store_pinned_pos_node.inputs[3] if len(phys_store_pinned_pos_node.inputs) > 3 else None)
    phys_store_pinned_pos_out = _first_geometry_output(phys_store_pinned_pos_node)
    phys_store_pinned_norm_geom_in = _find_socket_by_names(phys_store_pinned_norm_node.inputs, {"Geometry"}) or (phys_store_pinned_norm_node.inputs[0] if len(phys_store_pinned_norm_node.inputs) > 0 else None)
    phys_store_pinned_norm_val_in = _find_socket_by_names(phys_store_pinned_norm_node.inputs, {"Value"}) or (phys_store_pinned_norm_node.inputs[3] if len(phys_store_pinned_norm_node.inputs) > 3 else None)
    phys_store_pinned_norm_out = _first_geometry_output(phys_store_pinned_norm_node)
    phys_store_old_pos_geom_in = _find_socket_by_names(phys_store_old_pos_node.inputs, {"Geometry"}) or (phys_store_old_pos_node.inputs[0] if len(phys_store_old_pos_node.inputs) > 0 else None)
    phys_store_old_pos_sel_in = _find_socket_by_names(phys_store_old_pos_node.inputs, {"Selection"}) or (phys_store_old_pos_node.inputs[1] if len(phys_store_old_pos_node.inputs) > 1 else None)
    phys_store_old_pos_val_in = _find_socket_by_names(phys_store_old_pos_node.inputs, {"Value"}) or (phys_store_old_pos_node.inputs[3] if len(phys_store_old_pos_node.inputs) > 3 else None)
    phys_store_old_pos_out = _first_geometry_output(phys_store_old_pos_node)
    phys_store_vel_geom_in = _find_socket_by_names(phys_store_vel_node.inputs, {"Geometry"}) or (phys_store_vel_node.inputs[0] if len(phys_store_vel_node.inputs) > 0 else None)
    phys_store_vel_sel_in = _find_socket_by_names(phys_store_vel_node.inputs, {"Selection"}) or (phys_store_vel_node.inputs[1] if len(phys_store_vel_node.inputs) > 1 else None)
    phys_store_vel_out = _first_geometry_output(phys_store_vel_node)

    phys_attr_out = _find_socket_by_names(phys_src_attr_node.outputs, {"Attribute"}) or (phys_src_attr_node.outputs[0] if phys_src_attr_node.outputs else None)
    phys_maxdist_attr_out = _find_socket_by_names(phys_maxdist_attr_node.outputs, {"Attribute"}) or (phys_maxdist_attr_node.outputs[0] if phys_maxdist_attr_node.outputs else None)
    phys_cmp_a_in = _find_socket_by_names(phys_sim_cmp_node.inputs, {"A"}) or (phys_sim_cmp_node.inputs[0] if phys_sim_cmp_node.inputs else None)
    phys_cmp_out = _find_socket_by_names(phys_sim_cmp_node.outputs, {"Result"}) or (phys_sim_cmp_node.outputs[0] if phys_sim_cmp_node.outputs else None)
    phys_not_in = _find_socket_by_names(phys_not_node.inputs, {"Boolean"}) or (phys_not_node.inputs[0] if phys_not_node.inputs else None)
    phys_not_out = _find_socket_by_names(phys_not_node.outputs, {"Boolean"}) or (phys_not_node.outputs[0] if phys_not_node.outputs else None)
    src_normal_out = _find_socket_by_names(src_normal_node.outputs, {"Normal"}) or (src_normal_node.outputs[0] if src_normal_node.outputs else None)
    phys_normalize_in = _find_socket_by_names(phys_normalize_node.inputs, {"Vector"}) or (phys_normalize_node.inputs[0] if phys_normalize_node.inputs else None)
    phys_normalize_out = _find_socket_by_names(phys_normalize_node.outputs, {"Vector"}) or (phys_normalize_node.outputs[0] if phys_normalize_node.outputs else None)

    if not all([
        phys_sample_geom_in, phys_sample_val_in, phys_sample_idx_in, phys_sample_val_out,
        phys_normal_sample_geom_in, phys_normal_sample_val_in, phys_normal_sample_idx_in, phys_normal_sample_val_out,
        setpos_geom_in, setpos_sel_in, setpos_pos_in, setpos_geom_out,
        phys_store_pinned_pos_geom_in, phys_store_pinned_pos_val_in, phys_store_pinned_pos_out,
        phys_store_pinned_norm_geom_in, phys_store_pinned_norm_val_in, phys_store_pinned_norm_out,
        phys_store_old_pos_geom_in, phys_store_old_pos_sel_in, phys_store_old_pos_val_in, phys_store_old_pos_out,
        phys_store_vel_geom_in, phys_store_vel_sel_in, phys_store_vel_out,
        phys_attr_out, phys_maxdist_attr_out, phys_cmp_a_in, phys_cmp_out, phys_not_in, phys_not_out,
        src_normal_out, phys_normalize_in, phys_normalize_out,
    ]):
        return False

    pos_out = _find_socket_by_names(pos_node.outputs, {"Position"}) or (pos_node.outputs[0] if pos_node.outputs else None)
    idx_out = _find_socket_by_names(idx_node.outputs, {"Index"}) or (idx_node.outputs[0] if idx_node.outputs else None)
    not_out = _find_socket_by_names(not_node.outputs, {"Boolean"}) or (not_node.outputs[0] if not_node.outputs else None)
    sim_attr_out = _find_socket_by_names(sim_attr_node.outputs, {"Attribute"}) or (sim_attr_node.outputs[0] if sim_attr_node.outputs else None)
    not_in = _find_socket_by_names(not_node.inputs, {"Boolean"}) or (not_node.inputs[0] if not_node.inputs else None)

    if pos_out is None or idx_out is None or not_out is None or sim_attr_out is None or not_in is None:
        return False

    # Fast path: sample live positions from modifier input geometry using a baked source-index attr on physical mesh.
    if phys_src_attr_name:
        _rewire_input_socket(links, phys_sample_geom_in, src_geom_out)
        _rewire_input_socket(links, phys_sample_val_in, pos_out)
        _rewire_input_socket(links, phys_sample_idx_in, phys_attr_out)
        _rewire_input_socket(links, phys_normal_sample_geom_in, src_geom_out)
        _rewire_input_socket(links, phys_normal_sample_val_in, src_normal_out)
        _rewire_input_socket(links, phys_normal_sample_idx_in, phys_attr_out)
    else:
        # Fallback (slower): sample from live Softbody.Init.Cloth if the source-index bake patch could not be applied.
        init_cloth_node = _find_gn_group_node(node_group, node_name="Group.005", subtree_name_contains="softbody.init.cloth")
        init_phys_out = _find_socket_by_names(getattr(init_cloth_node, "outputs", []), {"Physical Mesh"}) if init_cloth_node else None
        if init_phys_out is None and init_cloth_node and len(init_cloth_node.outputs) > 1:
            init_phys_out = init_cloth_node.outputs[1]
        if init_phys_out is None:
            return False
        _rewire_input_socket(links, phys_sample_geom_in, init_phys_out)
        _rewire_input_socket(links, phys_sample_val_in, pos_out)
        _rewire_input_socket(links, phys_sample_idx_in, idx_out)
        # No reliable physical->source map means we cannot safely update pinned normals/anchors.
        # Leave those attributes as APX-baked defaults in this fallback mode.

    _rewire_input_socket(links, setpos_geom_in, sim_phys_out)
    _rewire_input_socket(links, setpos_pos_in, phys_sample_val_out)
    _rewire_input_socket(links, phys_cmp_a_in, phys_maxdist_attr_out)
    _rewire_input_socket(links, phys_not_in, phys_cmp_out)
    _rewire_input_socket(links, setpos_sel_in, phys_not_out)
    _rewire_input_socket(links, not_in, sim_attr_out)

    # Normalize sampled source normals before writing pinned_normal.
    _rewire_input_socket(links, phys_normalize_in, phys_normal_sample_val_out)

    # Update APX anchor attributes per frame so armature motion moves cloth constraints, not just visible verts.
    phys_chain_out = setpos_geom_out
    if phys_src_attr_name:
        _rewire_input_socket(links, phys_store_pinned_pos_geom_in, setpos_geom_out)
        _rewire_input_socket(links, phys_store_pinned_pos_val_in, phys_sample_val_out)
        _rewire_input_socket(links, phys_store_pinned_norm_geom_in, phys_store_pinned_pos_out)
        _rewire_input_socket(links, phys_store_pinned_norm_val_in, phys_normalize_out)
        phys_chain_out = phys_store_pinned_norm_out

    _rewire_input_socket(links, phys_store_old_pos_geom_in, phys_chain_out)
    _rewire_input_socket(links, phys_store_old_pos_sel_in, phys_not_out)
    _rewire_input_socket(links, phys_store_old_pos_val_in, phys_sample_val_out)
    _rewire_input_socket(links, phys_store_vel_geom_in, phys_store_old_pos_out)
    _rewire_input_socket(links, phys_store_vel_sel_in, phys_not_out)

    patched_any = _rewire_input_socket(links, step_phys_in, phys_store_vel_out)

    # Update the baked graphical mesh positions for non-simulated verts using the live modifier input geometry.
    graph_sample_geom_in = _find_socket_by_names(graph_sample_node.inputs, {"Geometry"}) or (graph_sample_node.inputs[0] if len(graph_sample_node.inputs) > 0 else None)
    graph_sample_val_in = _find_socket_by_names(graph_sample_node.inputs, {"Value"}) or (graph_sample_node.inputs[1] if len(graph_sample_node.inputs) > 1 else None)
    graph_sample_idx_in = _find_socket_by_names(graph_sample_node.inputs, {"Index"}) or (graph_sample_node.inputs[2] if len(graph_sample_node.inputs) > 2 else None)
    graph_sample_val_out = _find_socket_by_names(graph_sample_node.outputs, {"Value"}) or (graph_sample_node.outputs[0] if graph_sample_node.outputs else None)
    graph_setpos_geom_in = _find_socket_by_names(graph_setpos_node.inputs, {"Geometry"}) or (graph_setpos_node.inputs[0] if len(graph_setpos_node.inputs) > 0 else None)
    graph_setpos_sel_in = _find_socket_by_names(graph_setpos_node.inputs, {"Selection"}) or (graph_setpos_node.inputs[1] if len(graph_setpos_node.inputs) > 1 else None)
    graph_setpos_pos_in = _find_socket_by_names(graph_setpos_node.inputs, {"Position"}) or (graph_setpos_node.inputs[2] if len(graph_setpos_node.inputs) > 2 else None)
    graph_setpos_out = _first_geometry_output(graph_setpos_node)

    live_graph_out = sim_graph_out
    if all([sim_graph_out, graph_sample_geom_in, graph_sample_val_in, graph_sample_idx_in, graph_sample_val_out,
            graph_setpos_geom_in, graph_setpos_sel_in, graph_setpos_pos_in, graph_setpos_out]):
        _rewire_input_socket(links, graph_sample_geom_in, src_geom_out)
        _rewire_input_socket(links, graph_sample_val_in, pos_out)
        _rewire_input_socket(links, graph_sample_idx_in, idx_out)
        _rewire_input_socket(links, graph_setpos_geom_in, sim_graph_out)
        _rewire_input_socket(links, graph_setpos_pos_in, graph_sample_val_out)
        _rewire_input_socket(links, graph_setpos_sel_in, not_out)
        live_graph_out = graph_setpos_out
        patched_any = True

    # Feed the live-updated graphical mesh into display/grab paths (instead of frozen first-frame positions).
    if live_graph_out is not None:
        if update_graphical_node is not None:
            graph_in = _find_socket_by_names(update_graphical_node.inputs, {"Graphical Mesh"})
            if graph_in is None and len(update_graphical_node.inputs) > 1:
                graph_in = update_graphical_node.inputs[1]
            if graph_in is not None and _rewire_input_socket(links, graph_in, live_graph_out):
                patched_any = True

        if grab_selection_node is not None:
            grab_graph_in = _find_socket_by_names(grab_selection_node.inputs, {"Graphical Mesh"})
            if grab_graph_in is None and grab_selection_node.inputs:
                grab_graph_in = grab_selection_node.inputs[0]
            if grab_graph_in is not None and _rewire_input_socket(links, grab_graph_in, live_graph_out):
                patched_any = True

    if patched_any:
        try:
            if hasattr(setpos_node, "label"):
                setpos_node.label = "Witcher Live Pose Injection (Physical)"
            if hasattr(graph_setpos_node, "label"):
                graph_setpos_node.label = "Witcher Live Pose Injection (Graphical)"
            if hasattr(phys_store_pinned_pos_node, "label"):
                phys_store_pinned_pos_node.label = "Witcher Live pinned_position"
            if hasattr(phys_store_pinned_norm_node, "label"):
                phys_store_pinned_norm_node.label = "Witcher Live pinned_normal"
        except Exception:
            pass
    return patched_any


def patch_clothsimulation_to_object_proxies(cloth_obj: Object, proxy_objects: Dict[str, Object]) -> bool:
    """Patch the copied APX ClothSimulation node group to use object proxies instead of collection inputs."""
    mod = find_clothsimulation_modifier(cloth_obj)
    if mod is None or getattr(mod, "node_group", None) is None:
        return False

    node_group = mod.node_group
    patched_any = False

    # Some APX versions inline Collection Info nodes in the top-level ClothSimulation group.
    collection_nodes = [n for n in node_group.nodes if n.bl_idname == 'GeometryNodeCollectionInfo']
    if collection_nodes:
        remaining_roles = {k for k, v in proxy_objects.items() if v is not None}
        for coll_node in list(collection_nodes):
            role = _classify_collection_info_node(coll_node, mod=mod)
            if role is None and len(remaining_roles) == 1:
                role = next(iter(remaining_roles))
            proxy_obj = proxy_objects.get(role) if role else None
            if proxy_obj is None:
                continue
            if _replace_collection_info_node_with_object_info(node_group, coll_node, proxy_obj):
                patched_any = True
                if role in remaining_roles:
                    remaining_roles.remove(role)

    # APX template from your provided example stores colliders in nested Group.008 / SoftBody.Init.Colliders.
    if not patched_any:
        patched_any = _patch_clothsim_groupnode_colliders_to_proxies(node_group, proxy_objects)

    # Add a live armature-pose injection path while keeping the original APX static setup intact.
    live_pose_patched = _patch_clothsim_add_live_armature_pose(node_group)

    if patched_any:
        try:
            cloth_obj["witcher_apx_cloth_collision_mode"] = "object_proxy"
            cloth_obj["witcher_apx_cloth_node_group"] = node_group.name
            for role, proxy_obj in proxy_objects.items():
                if proxy_obj:
                    cloth_obj[f"witcher_apx_{role}_proxy"] = proxy_obj.name
        except Exception:
            pass
    if live_pose_patched:
        try:
            cloth_obj["witcher_apx_live_pose_patch"] = True
        except Exception:
            pass
    return patched_any
