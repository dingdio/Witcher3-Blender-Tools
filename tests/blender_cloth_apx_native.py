"""Native Blender regression checks for APX cloth integration.

Run with Blender 4.5+:
  blender --background --factory-startup --python tests/blender_cloth_apx_native.py
"""

import sys
import tempfile
import types
from pathlib import Path

import bpy


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _pointer(data_block):
    return int(data_block.as_pointer()) if data_block is not None else 0


def _socket_key(socket):
    return (
        getattr(socket, "identifier", "") or "",
        getattr(socket, "name", "") or "",
    )


def _node_tree_signature(node_tree):
    if node_tree is None:
        return None
    nodes = tuple(sorted(
        (
            node.name,
            node.bl_idname,
            getattr(node, "label", "") or "",
            bool(getattr(node, "mute", False)),
            _pointer(getattr(node, "node_tree", None)),
        )
        for node in node_tree.nodes
    ))
    links = tuple(sorted(
        (
            link.from_node.name,
            _socket_key(link.from_socket),
            link.to_node.name,
            _socket_key(link.to_socket),
        )
        for link in node_tree.links
    ))
    return nodes, links


def _scene_data_signature():
    """Capture scene data that importing or registering the add-on must not alter."""
    inventories = {
        "objects": tuple(sorted((_pointer(item), item.name) for item in bpy.data.objects)),
        "collections": tuple(sorted((_pointer(item), item.name) for item in bpy.data.collections)),
        "meshes": tuple(sorted((_pointer(item), item.name) for item in bpy.data.meshes)),
        "armatures": tuple(sorted((_pointer(item), item.name) for item in bpy.data.armatures)),
        "materials": tuple(sorted((_pointer(item), item.name) for item in bpy.data.materials)),
        "node_groups": tuple(sorted((_pointer(item), item.name) for item in bpy.data.node_groups)),
    }
    objects = tuple(sorted(
        (
            _pointer(obj),
            obj.name,
            obj.type,
            _pointer(getattr(obj, "data", None)),
            _pointer(getattr(obj, "parent", None)),
            tuple(
                (
                    modifier.name,
                    modifier.type,
                    _pointer(getattr(modifier, "node_group", None)),
                    bool(modifier.show_viewport),
                    bool(modifier.show_render),
                )
                for modifier in obj.modifiers
            ),
        )
        for obj in bpy.data.objects
    ))
    materials = tuple(sorted(
        (
            _pointer(material),
            material.name,
            bool(material.use_nodes),
            _node_tree_signature(material.node_tree),
        )
        for material in bpy.data.materials
    ))
    node_groups = tuple(sorted(
        (
            _pointer(node_group),
            node_group.name,
            _node_tree_signature(node_group),
        )
        for node_group in bpy.data.node_groups
    ))
    return inventories, objects, materials, node_groups


def _new_mesh_object(name, vertices, faces, collection=None):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    (collection or bpy.context.scene.collection).objects.link(obj)
    return obj


def _new_geometry_group(name):
    node_group = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    node_group.interface.new_socket(
        name="Geometry",
        in_out='OUTPUT',
        socket_type='NodeSocketGeometry',
    )
    return node_group


def _meaningful_links(node_group):
    return {
        (
            link.from_node.name,
            link.from_socket.name,
            link.to_node.name,
            link.to_socket.name,
        )
        for link in node_group.links
    }


# Establish real scene data before importing the add-on. Importing, registering,
# and unregistering may add RNA definitions, but must not mutate these data blocks.
sentinel_obj = _new_mesh_object(
    "W3TB_ClothImportSentinel",
    [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
    [(0, 1, 2)],
)
sentinel_material = bpy.data.materials.new("W3TB_ClothImportSentinelMaterial")
sentinel_material.use_nodes = True
sentinel_obj.data.materials.append(sentinel_material)
sentinel_group = _new_geometry_group("W3TB_ClothImportSentinelGroup")
sentinel_group.nodes.new('NodeGroupOutput')

before_addon_import = _scene_data_signature()

import witcher3_tools as addon

assert _scene_data_signature() == before_addon_import, (
    "Importing witcher3_tools mutated existing Blender scene data"
)

from witcher3_tools import cloth

assert _scene_data_signature() == before_addon_import, (
    "Importing the cloth package mutated existing Blender scene data"
)

from witcher3_tools.cloth import geometry_nodes, import_state, importer

assert _scene_data_signature() == before_addon_import, (
    "Importing cloth Geometry Nodes/state/importer modules mutated existing Blender scene data"
)

addon_registered = False
try:
    addon.register()
    addon_registered = True
    assert _scene_data_signature() == before_addon_import, (
        "Registering witcher3_tools mutated existing Blender scene data"
    )
finally:
    if addon_registered:
        addon.unregister()

assert _scene_data_signature() == before_addon_import, (
    "Unregistering witcher3_tools mutated existing Blender scene data"
)


def test_collision_proxy_evaluated_geometry():
    source_a = _new_mesh_object(
        "W3TB_APXSourceA",
        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        [(0, 1, 2)],
    )
    source_b = _new_mesh_object(
        "W3TB_APXSourceB",
        [(2.0, 0.0, 0.0), (3.0, 0.0, 0.0), (2.0, 1.0, 0.0)],
        [(0, 1, 2)],
    )
    parent = bpy.data.objects.new("W3TB_APXProxyParent", None)
    bpy.context.scene.collection.objects.link(parent)

    proxy = geometry_nodes.create_collision_proxy_object(
        "W3TB_APXCollisionProxy",
        parent,
        bpy.context.scene.collection,
        [source_a, source_b],
    )
    assert proxy is not None
    assert proxy.parent is parent
    assert proxy.hide_render
    assert proxy.display_type != 'WIRE'
    assert bool(proxy.get("witcher_apx_collision_proxy", False))

    modifier = proxy.modifiers.get("WitcherAPXCollisionProxy")
    assert modifier is not None and modifier.type == 'NODES'
    node_group = modifier.node_group
    assert node_group is not None
    output_sockets = [
        item for item in node_group.interface.items_tree
        if getattr(item, "item_type", "") == 'SOCKET'
        and getattr(item, "in_out", "") == 'OUTPUT'
    ]
    assert any(socket.name == "Geometry" for socket in output_sockets)

    object_info_nodes = [
        node for node in node_group.nodes
        if node.bl_idname == 'GeometryNodeObjectInfo'
    ]
    assert len(object_info_nodes) == 2
    assert all(node.transform_space == 'RELATIVE' for node in object_info_nodes)
    assert {
        node.inputs["Object"].default_value
        for node in object_info_nodes
    } == {source_a, source_b}

    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated_proxy = proxy.evaluated_get(depsgraph)
    evaluated_mesh = evaluated_proxy.to_mesh()
    try:
        assert len(evaluated_mesh.vertices) == 6
        assert len(evaluated_mesh.polygons) == 2
    finally:
        evaluated_proxy.to_mesh_clear()

    importer._parent_and_namespace_collision_objects("W3TB", parent, [source_a, source_b])
    assert all(source.hide_render for source in (source_a, source_b))
    assert all(source.display_type == 'SOLID' for source in (source_a, source_b))
    return proxy, source_a, source_b


def test_nested_collider_rewire_is_idempotent(proxy_objects):
    collider_tree = _new_geometry_group("SoftBody.Init.Colliders.W3TB")
    collider_tree.nodes.new('NodeGroupOutput')

    cloth_tree = _new_geometry_group("ClothSimulation.W3TB")
    colliders_node = cloth_tree.nodes.new('GeometryNodeGroup')
    colliders_node.name = "Group.008"
    colliders_node.node_tree = collider_tree
    group_output = cloth_tree.nodes.new('NodeGroupOutput')
    cloth_tree.links.new(
        colliders_node.outputs["Geometry"],
        group_output.inputs["Geometry"],
    )

    assert geometry_nodes._patch_clothsim_groupnode_colliders_to_proxies(
        cloth_tree,
        proxy_objects,
    )
    join_node = cloth_tree.nodes.get("WitcherAPXColliderProxyJoin")
    assert join_node is not None
    assert colliders_node.mute
    assert not colliders_node.outputs["Geometry"].links
    assert len(join_node.inputs["Geometry"].links) == 3
    assert len(join_node.outputs["Geometry"].links) == 1
    assert _pointer(join_node.outputs["Geometry"].links[0].to_node) == _pointer(group_output)

    for role, proxy_obj in proxy_objects.items():
        proxy_node = cloth_tree.nodes.get(f"WitcherAPXColliderProxy_{role}")
        assert proxy_node is not None
        assert proxy_node.inputs["Object"].default_value is proxy_obj
        assert proxy_node.transform_space == 'RELATIVE'

    first_nodes = {
        (node.name, node.bl_idname)
        for node in cloth_tree.nodes
    }
    first_links = _meaningful_links(cloth_tree)

    assert geometry_nodes._patch_clothsim_groupnode_colliders_to_proxies(
        cloth_tree,
        proxy_objects,
    )
    assert {
        (node.name, node.bl_idname)
        for node in cloth_tree.nodes
    } == first_nodes
    assert _meaningful_links(cloth_tree) == first_links
    assert len(cloth_tree.nodes.get("WitcherAPXColliderProxyJoin").inputs["Geometry"].links) == 3


def test_cloth_import_state_restores_context():
    original_collection = bpy.data.collections.new("W3TB_APXContextOriginal")
    alternate_collection = bpy.data.collections.new("W3TB_APXContextAlternate")
    bpy.context.scene.collection.children.link(original_collection)
    bpy.context.scene.collection.children.link(alternate_collection)

    original_active = _new_mesh_object(
        "W3TB_APXContextActive",
        [(0.0, 0.0, 0.0)],
        [],
        original_collection,
    )
    original_selected = _new_mesh_object(
        "W3TB_APXContextSelected",
        [(0.0, 0.0, 0.0)],
        [],
        original_collection,
    )
    alternate_object = _new_mesh_object(
        "W3TB_APXContextAlternateObject",
        [(0.0, 0.0, 0.0)],
        [],
        alternate_collection,
    )

    bpy.ops.object.select_all(action='DESELECT')
    original_active.select_set(True)
    original_selected.select_set(True)
    bpy.context.view_layer.objects.active = original_active
    assert import_state.restore_active_layer_collection(
        bpy.context,
        original_collection,
    )

    saved_selection = {_pointer(original_active), _pointer(original_selected)}
    state = import_state.ClothImportState.capture(bpy.context)
    assert _pointer(state.active_object) == _pointer(original_active)
    assert {_pointer(obj) for obj in state.selected_objects} == saved_selection
    assert _pointer(state.active_collection) == _pointer(original_collection)

    bpy.ops.object.select_all(action='DESELECT')
    alternate_object.select_set(True)
    bpy.context.view_layer.objects.active = alternate_object
    assert import_state.restore_active_layer_collection(
        bpy.context,
        alternate_collection,
    )

    state.restore_context()

    assert _pointer(bpy.context.view_layer.objects.active) == _pointer(original_active)
    assert {_pointer(obj) for obj in bpy.context.selected_objects} == saved_selection
    assert _pointer(
        bpy.context.view_layer.active_layer_collection.collection
    ) == _pointer(original_collection)


def test_imported_collection_uses_transaction_delta():
    base_name = "W3TB_APXCollisionSpheres"
    previous_owner = bpy.data.collections.new("W3TB_APXPreviousImport")
    current_owner = bpy.data.collections.new("W3TB_APXCurrentImport")
    bpy.context.scene.collection.children.link(previous_owner)
    bpy.context.scene.collection.children.link(current_owner)

    previous_collision = bpy.data.collections.new(base_name)
    previous_owner.children.link(previous_collision)
    state = import_state.ClothImportState.capture(bpy.context)

    current_collision = bpy.data.collections.new(base_name)
    current_owner.children.link(current_collision)
    assert current_collision.name.startswith(base_name + ".")

    current_collections = state.new_collections()
    assert _pointer(previous_collision) not in {
        _pointer(collection) for collection in current_collections
    }
    assert _pointer(current_collision) in {
        _pointer(collection) for collection in current_collections
    }

    found = importer._find_imported_collection(
        current_collections,
        base_name,
        owner_collection=current_owner,
    )
    assert _pointer(found) == _pointer(current_collision)
    assert _pointer(found) != _pointer(previous_collision)

    current_collision_name = current_collision.name
    state.rollback()
    assert bpy.data.collections.get(current_collision_name) is None
    assert bpy.data.collections.get(previous_collision.name) is previous_collision


def test_failed_import_cleanup_preserves_snapshot_identity():
    preserved_collection = bpy.data.collections.new("W3TB_APXPreservedCollection")
    bpy.context.scene.collection.children.link(preserved_collection)
    preserved_object = _new_mesh_object(
        "W3TB_APXPreservedObject",
        [(0.0, 0.0, 0.0)],
        [],
        preserved_collection,
    )
    preserved_material = bpy.data.materials.new("W3TB_APXPreservedMaterial")
    preserved_node_group = _new_geometry_group("W3TB_APXPreservedNodeGroup")
    preserved_armature = bpy.data.armatures.new("W3TB_APXPreservedArmature")

    preserved = {
        "collection": (preserved_collection, _pointer(preserved_collection)),
        "object": (preserved_object, _pointer(preserved_object)),
        "mesh": (preserved_object.data, _pointer(preserved_object.data)),
        "material": (preserved_material, _pointer(preserved_material)),
        "node_group": (preserved_node_group, _pointer(preserved_node_group)),
        "armature": (preserved_armature, _pointer(preserved_armature)),
    }
    snapshot = import_state.snapshot_blender_import_state()

    transient_parent = bpy.data.collections.new("W3TB_APXTransientParent")
    transient_child = bpy.data.collections.new("W3TB_APXTransientChild")
    bpy.context.scene.collection.children.link(transient_parent)
    transient_parent.children.link(transient_child)
    transient_mesh_obj = _new_mesh_object(
        "W3TB_APXTransientMeshObject",
        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)],
        [],
        transient_child,
    )
    transient_armature_data = bpy.data.armatures.new("W3TB_APXTransientArmature")
    transient_armature_obj = bpy.data.objects.new(
        "W3TB_APXTransientArmatureObject",
        transient_armature_data,
    )
    transient_child.objects.link(transient_armature_obj)
    transient_material = bpy.data.materials.new("W3TB_APXTransientMaterial")
    transient_node_group = _new_geometry_group("W3TB_APXTransientNodeGroup")

    transient_names = {
        "collections": (transient_parent.name, transient_child.name),
        "objects": (transient_mesh_obj.name, transient_armature_obj.name),
        "meshes": (transient_mesh_obj.data.name,),
        "armatures": (transient_armature_data.name,),
        "materials": (transient_material.name,),
        "node_groups": (transient_node_group.name,),
    }

    import_state.cleanup_failed_cloth_import(snapshot)

    for data_block, original_pointer in preserved.values():
        assert _pointer(data_block) == original_pointer
    assert bpy.data.collections.get(preserved_collection.name) is preserved_collection
    assert bpy.data.objects.get(preserved_object.name) is preserved_object
    assert bpy.data.meshes.get(preserved_object.data.name) is preserved_object.data
    assert bpy.data.materials.get(preserved_material.name) is preserved_material
    assert bpy.data.node_groups.get(preserved_node_group.name) is preserved_node_group
    assert bpy.data.armatures.get(preserved_armature.name) is preserved_armature

    for collection_name in transient_names["collections"]:
        assert bpy.data.collections.get(collection_name) is None
    for object_name in transient_names["objects"]:
        assert bpy.data.objects.get(object_name) is None
    for mesh_name in transient_names["meshes"]:
        assert bpy.data.meshes.get(mesh_name) is None
    for armature_name in transient_names["armatures"]:
        assert bpy.data.armatures.get(armature_name) is None
    for material_name in transient_names["materials"]:
        assert bpy.data.materials.get(material_name) is None
    for node_group_name in transient_names["node_groups"]:
        assert bpy.data.node_groups.get(node_group_name) is None


def test_import_cloth_backend_failure_rolls_back_exactly():
    original_collection = bpy.data.collections.new("W3TB_APXFailureOriginalCollection")
    bpy.context.scene.collection.children.link(original_collection)
    original_active = _new_mesh_object(
        "W3TB_APXFailureOriginalActive",
        [(0.0, 0.0, 0.0)],
        [],
        original_collection,
    )
    original_selected = _new_mesh_object(
        "W3TB_APXFailureOriginalSelected",
        [(0.0, 0.0, 0.0)],
        [],
        original_collection,
    )

    bpy.ops.object.select_all(action='DESELECT')
    original_active.select_set(True)
    original_selected.select_set(True)
    bpy.context.view_layer.objects.active = original_active
    assert import_state.restore_active_layer_collection(
        bpy.context,
        original_collection,
    )

    expected_selection = {_pointer(original_active), _pointer(original_selected)}
    expected_active = _pointer(original_active)
    expected_layer_collection = _pointer(original_collection)
    expected_data = _scene_data_signature()

    backend_called = []

    def fake_read_clothing(context, filepath, rotate_180, rm_ph_me):
        backend_called.append((filepath, rotate_180, rm_ph_me))
        partial_collection = bpy.data.collections.new("W3TB_APXFailurePartialCollection")
        context.scene.collection.children.link(partial_collection)
        partial_mesh = bpy.data.meshes.new("W3TB_APXFailurePartialMesh")
        partial_object = bpy.data.objects.new(
            "W3TB_APXFailurePartialObject",
            partial_mesh,
        )
        partial_collection.objects.link(partial_object)
        bpy.data.materials.new("W3TB_APXFailurePartialMaterial")
        _new_geometry_group("W3TB_APXFailurePartialNodeGroup")

        bpy.ops.object.select_all(action='DESELECT')
        partial_object.select_set(True)
        context.view_layer.objects.active = partial_object
        context.view_layer.update()
        assert import_state.restore_active_layer_collection(
            context,
            partial_collection,
        )
        raise RuntimeError("synthetic io_mesh_apx import failure")

    fake_root = types.ModuleType("io_mesh_apx")
    fake_root.__path__ = []
    fake_importer = types.ModuleType("io_mesh_apx.importer")
    fake_importer.__path__ = []
    fake_clothing = types.ModuleType("io_mesh_apx.importer.import_clothing")
    fake_clothing.read_clothing = fake_read_clothing
    fake_root.importer = fake_importer
    fake_importer.import_clothing = fake_clothing

    module_names = (
        "io_mesh_apx",
        "io_mesh_apx.importer",
        "io_mesh_apx.importer.import_clothing",
    )
    missing_module = object()
    saved_modules = {
        name: sys.modules.get(name, missing_module)
        for name in module_names
    }
    saved_addon_enabled = importer._addon_enabled
    saved_runtime_ready = importer._io_mesh_apx_runtime_ready
    saved_wear_cloth = importer.get_DO_WEAR_CLOTH

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".apx",
        delete=False,
    ) as apx_file:
        apx_file.write("<NxParameters></NxParameters>")
        apx_path = apx_file.name

    try:
        sys.modules["io_mesh_apx"] = fake_root
        sys.modules["io_mesh_apx.importer"] = fake_importer
        sys.modules["io_mesh_apx.importer.import_clothing"] = fake_clothing
        importer._addon_enabled = lambda addon_id: addon_id == "io_mesh_apx"
        importer._io_mesh_apx_runtime_ready = lambda context=None: True
        importer.get_DO_WEAR_CLOTH = lambda context: False

        result = importer.import_cloth(
            bpy.context,
            apx_path,
            use_mat=False,
            rotate_180=False,
            rm_ph_me=False,
        )
        assert result is None
        assert len(backend_called) == 1
        assert _scene_data_signature() == expected_data
        assert _pointer(bpy.context.view_layer.objects.active) == expected_active
        assert {_pointer(obj) for obj in bpy.context.selected_objects} == expected_selection
        assert _pointer(
            bpy.context.view_layer.active_layer_collection.collection
        ) == expected_layer_collection
    finally:
        importer._addon_enabled = saved_addon_enabled
        importer._io_mesh_apx_runtime_ready = saved_runtime_ready
        importer.get_DO_WEAR_CLOTH = saved_wear_cloth
        for name, previous_module in saved_modules.items():
            if previous_module is missing_module:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous_module
        Path(apx_path).unlink(missing_ok=True)


proxy, source_a, source_b = test_collision_proxy_evaluated_geometry()
test_nested_collider_rewire_is_idempotent({
    "spheres": proxy,
    "connections": source_a,
    "capsules": source_b,
})
test_cloth_import_state_restores_context()
test_imported_collection_uses_transaction_delta()
test_failed_import_cleanup_preserves_snapshot_identity()
test_import_cloth_backend_failure_rolls_back_exactly()

print("W3TB_CLOTH_APX_NATIVE_OK")
