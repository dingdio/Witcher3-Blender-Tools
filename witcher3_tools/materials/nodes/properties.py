"""Blender RNA properties for Witcher materials."""

import bpy

from ..chain import LOCAL_NODE_COLOR
from ..constants import DEFAULT_W2_MATERIAL_BASE, DEFAULT_W3_MATERIAL_BASE
from ..reader import normalize_depot_path
from .domain import (
    BASE_READ_VALUE_TYPE_FILTER_ITEMS,
    EXPORT_PARAMS_SORT_MODE_ITEMS,
    _update_base_material_chain_color,
    _update_base_material_local_color,
    _update_node_witcher_include,
)


class NodeGroupInputProperties(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Name")
    value: bpy.props.StringProperty(name="Value")
    value_float: bpy.props.FloatProperty(name="Value")
    value_vector:bpy.props.FloatVectorProperty(name="Value")
    #type: bpy.props.EnumProperty(name="Type", items=[("FLOAT", "Float", ""), ("VECTOR", "Vector", ""), ("COLOR", "Color", "")])
    type: bpy.props.StringProperty(name="Type")
    is_enabled: bpy.props.BoolProperty(name="Is Enabled", default=False)
    is_enabled_temp: bpy.props.BoolProperty(name="Export", default=False)
    is_linked: bpy.props.BoolProperty(name="is_linked", default=False)


class BaseMaterialPathItem(bpy.types.PropertyGroup):
    path: bpy.props.StringProperty(name="Path")


class BaseMaterialParamItem(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="Name")
    param_type: bpy.props.StringProperty(name="Type")
    value: bpy.props.StringProperty(name="Value")
    source_kind: bpy.props.StringProperty(name="Source Kind")
    source_path: bpy.props.StringProperty(name="Source Path")
    source_index: bpy.props.IntProperty(name="Source Index", default=-1)
    row_index: bpy.props.IntProperty(name="Row Index", default=-1)
    row_y: bpy.props.IntProperty(name="Row Y", default=-1)
    has_value: bpy.props.BoolProperty(name="Has Value", default=False)
    has_matching_socket: bpy.props.BoolProperty(name="Has Matching Socket", default=False)
    is_linked: bpy.props.BoolProperty(name="Is Linked", default=False)
    is_supported: bpy.props.BoolProperty(name="Is Supported", default=False)
    is_declared_only: bpy.props.BoolProperty(name="Is Declared Only", default=False)
    can_create: bpy.props.BoolProperty(name="Can Create", default=False)
    status: bpy.props.StringProperty(name="Status")
    message: bpy.props.StringProperty(name="Message")
    show_details: bpy.props.BoolProperty(name="Show Details", default=False)


class BaseMaterialChainItem(bpy.types.PropertyGroup):
    path: bpy.props.StringProperty(name="Path")
    source_kind: bpy.props.StringProperty(name="Source Kind")
    chunk_type: bpy.props.StringProperty(name="Chunk Type")
    source_index: bpy.props.IntProperty(name="Source Index", default=-1)
    node_color: bpy.props.FloatVectorProperty(
        name="Node Color",
        description="Color used by nodes created from this material-chain entry",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=(0.5, 0.5, 0.5),
        update=_update_base_material_chain_color,
    )


def _update_material_version(self, context):
    current_base = normalize_depot_path(getattr(self, "base_custom", ""))
    if self.material_version == "witcher2" and current_base == normalize_depot_path(DEFAULT_W3_MATERIAL_BASE):
        self.base_custom = DEFAULT_W2_MATERIAL_BASE
    elif self.material_version == "witcher3" and current_base == normalize_depot_path(DEFAULT_W2_MATERIAL_BASE):
        self.base_custom = DEFAULT_W3_MATERIAL_BASE


class WitcherMaterialProperties(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="name", default="Material")
    enableMask: bpy.props.BoolProperty(name="enableMask", default=False, description="Enable Mask of hair etc")
    local: bpy.props.BoolProperty(name="local", default=True, description="Local materials will be embedded in the .w2mesh. Non-local will use the defined base material without any instances.")
    material_ui_tab: bpy.props.EnumProperty(
        name="Material UI Tab",
        items=[
            ('EXPORT', "Export Params", "Export-connected local params"),
            ('BASE', "Material Chain", "Read the material chain and create params from it"),
        ],
        default='EXPORT',
    )
    export_params_sort_mode: bpy.props.EnumProperty(
        name="Export Sort",
        description="Sort the Export Params list",
        items=EXPORT_PARAMS_SORT_MODE_ITEMS,
        default='TYPE',
    )
    #base: bpy.props.StringProperty(name="base", default="engine\materials\graphs\pbr_std.w2mg")
    bind_name: bpy.props.BoolProperty(name="Use Blender Material Name", default=True)
    node_group_name: bpy.props.StringProperty(name="Node Group", default="")
    input_props: bpy.props.CollectionProperty(type=NodeGroupInputProperties)
    input_props_index: bpy.props.IntProperty()
    xml_text : bpy.props.StringProperty(name="XML Text")
    witcher_material_settings_collapse: bpy.props.BoolProperty(default = False)
    override_texture_root: bpy.props.BoolProperty(name="override_texture_root", default=False, description="Specify a root path")
    custom_texture_root: bpy.props.StringProperty(name="custom_texture_root", default="", description="Root path of textures for this material")
    base_read_status: bpy.props.StringProperty(name="Base Read Status", default="")
    base_read_message: bpy.props.StringProperty(name="Base Read Message", default="")
    base_read_requested_path: bpy.props.StringProperty(name="Base Read Requested Path", default="")
    base_read_resolved_graph: bpy.props.StringProperty(name="Base Read Resolved Graph", default="")
    base_read_chain_text: bpy.props.StringProperty(name="Base Read Chain", default="")
    base_read_chain: bpy.props.CollectionProperty(type=BaseMaterialChainItem)
    base_read_params: bpy.props.CollectionProperty(type=BaseMaterialParamItem)
    base_read_chain_frames_enabled: bpy.props.BoolProperty(name="Frame Chain Nodes", default=True)
    base_read_value_search: bpy.props.StringProperty(
        name="Search Values",
        description="Filter Material Chain values by name, source path, value, or status",
        default="",
    )
    base_read_value_type_filter: bpy.props.EnumProperty(
        name="Type",
        description="Filter Material Chain values by parameter type",
        items=BASE_READ_VALUE_TYPE_FILTER_ITEMS,
        default='ALL',
    )
    base_read_local_color: bpy.props.FloatVectorProperty(
        name="Local Color",
        description="Color used by nodes promoted to local material overrides",
        subtype='COLOR',
        size=3,
        min=0.0,
        max=1.0,
        default=LOCAL_NODE_COLOR,
        update=_update_base_material_local_color,
    )
    base_read_count_created: bpy.props.IntProperty(name="Base Read Created", default=0)
    base_read_count_present: bpy.props.IntProperty(name="Base Read Present", default=0)
    base_read_count_unsupported: bpy.props.IntProperty(name="Base Read Unsupported", default=0)
    base_read_count_declared_only: bpy.props.IntProperty(name="Base Read Declared Only", default=0)
    base_read_show_inspector: bpy.props.BoolProperty(name="Show Base Read Inspector", default=True)
    base_read_show_info: bpy.props.BoolProperty(name="Show Base Read Info", default=False)
    base_read_present_collapse: bpy.props.BoolProperty(name="Show Present Linked", default=False)
    base_read_available_collapse: bpy.props.BoolProperty(name="Show Available Defaults", default=False)
    base_read_declared_collapse: bpy.props.BoolProperty(name="Show Declared Unsupported", default=False)

    base_custom: bpy.props.StringProperty(
        name="Base Path",
        description="Enter a .w2mi or .w2mg path",
        default=DEFAULT_W3_MATERIAL_BASE,
    )

    material_version_options = [
        #("custom", "Custom", "Description for value 1"),
        ("witcher3", "Witcher 3", "This is a Witcher 3 material"),
        ("witcher2", "Witcher 2", "This is a Witcher 2 material"),
    ]
    material_version: bpy.props.EnumProperty(
        name="Game",
        description="What game this material was orignally for",
        items=material_version_options,
        default="witcher3",
        update=_update_material_version,
    )


PROPERTY_CLASSES = (
    NodeGroupInputProperties,
    BaseMaterialPathItem,
    BaseMaterialParamItem,
    BaseMaterialChainItem,
    WitcherMaterialProperties,
)

_NODE_PROPERTY_NAMES = (
    "witcher_include",
    "witcher_export",
    "witcher_final_path",
    "witcher_param_kind",
    "witcher_param_name",
    "witcher_vector_source",
    "witcher_vector_w",
    "witcher_texarray_source_path",
    "witcher_texarray_path_manual",
    "witcher_texture_source_path",
    "witcher_texture_path_manual",
)


def register():
    for cls in PROPERTY_CLASSES:
        bpy.utils.register_class(cls)

    bpy.types.Material.witcher_props = bpy.props.PointerProperty(
        type=WitcherMaterialProperties,
    )
    bpy.types.Node.witcher_include = bpy.props.BoolProperty(
        name="Local",
        description="Keep this linked node as a local material override",
        default=False,
        update=_update_node_witcher_include,
    )
    bpy.types.Node.witcher_export = bpy.props.BoolProperty(
        name="Export",
        description="Write this Local material parameter into the exported mesh",
        default=True,
    )
    bpy.types.Node.witcher_final_path = bpy.props.StringProperty(default="")
    bpy.types.Node.witcher_param_kind = bpy.props.StringProperty(default="")
    bpy.types.Node.witcher_param_name = bpy.props.StringProperty(default="")
    bpy.types.Node.witcher_vector_source = bpy.props.StringProperty(default="")
    bpy.types.Node.witcher_vector_w = bpy.props.FloatProperty(default=1.0)
    bpy.types.Node.witcher_texarray_source_path = bpy.props.StringProperty(
        name="Texarray Path",
        description="Repo path exported for CTextureArray nodes. Keep the .texarray path, not a generated texture slice",
        default="",
    )
    bpy.types.Node.witcher_texarray_path_manual = bpy.props.BoolProperty(
        name="Manual Texarray Path",
        description="Edit the CTextureArray repo path manually instead of using auto-resolve",
        default=False,
    )
    bpy.types.Node.witcher_texture_source_path = bpy.props.StringProperty(
        name="Texture Path",
        description="Repo path exported for texture nodes that need an explicit source path",
        default="",
    )
    bpy.types.Node.witcher_texture_path_manual = bpy.props.BoolProperty(
        name="Manual Texture Path",
        description="Edit the texture repo path manually instead of using auto-resolve",
        default=False,
    )


def unregister():
    if hasattr(bpy.types.Material, "witcher_props"):
        del bpy.types.Material.witcher_props
    for name in reversed(_NODE_PROPERTY_NAMES):
        if hasattr(bpy.types.Node, name):
            delattr(bpy.types.Node, name)
    for cls in reversed(PROPERTY_CLASSES):
        bpy.utils.unregister_class(cls)
