
import logging
import os
import re
import time
import bpy
import json

log = logging.getLogger(__name__)

from .. import CR2W, file_helpers
from ..importers import import_w2l
from ..importers import import_entity
from ..importers import import_anims
from ..ui.ui_utils import WITCH_PT_Base
from ..ui.armature_context import (
    draw_main_armature_selector,
    get_main_armature_and_rig_settings,
)
from bpy.types import Panel, Operator, UIList
from bpy.props import IntProperty, StringProperty, BoolProperty, EnumProperty
from ..importers.import_entity import test_load_entity, fixed_chunk_paths
from ..CR2W import w3_types

from .. import (
    get_uncook_path,
    get_all_addon_prefs,
)
from ..read_game_bin import (
    auto_detect_witcher3_game_path,
    get_witcher3_exe_path,
    is_valid_witcher3_game_path,
    update_witcher_game_path,
    WITCHER3_EXE_REL,
)
from ..CR2W.common_blender import (
    repo_file,
    mod_loading_context,
)

from . import ui_file_browser

from bpy_extras.io_utils import (
        ImportHelper
        )

def _get_attr_or_key(obj, key, default=None):
    """Get attribute from object or key from dict."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _short_panel_header_text(text: str, max_len: int = 28) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    return value if len(value) <= max_len else (value[: max_len - 1] + "…")


def _find_coloring_objects(context, component_name):
    """Find matching mesh objects inside the active character hierarchy only."""
    component_name = str(component_name or "").strip()
    if not component_name:
        return []
    return _build_coloring_object_index(context).get(component_name, [])


def _build_coloring_object_index(context):
    """Build a component->objects index for the active character hierarchy."""
    main_arm_obj, _rig_settings = get_main_armature_and_rig_settings(
        context, prefer_active=True, remember=False, fallback=True,
    )
    if not main_arm_obj:
        return {}
    return import_entity.build_component_mesh_index_in_hierarchy(main_arm_obj)


def _show_coloring_object_props(layout, objects):
    """Show editable colorShift custom properties for matching objects."""
    for obj in objects:
        has_shift = any(obj.get(k) is not None for k in ('colorShift1_hue', 'colorShift2_hue'))
        if not has_shift:
            continue
        prop_box = layout.box()
        prop_box.label(text=f"{obj.name}", icon='OBJECT_DATA')
        if obj.get('colorShift1_hue') is not None:
            row = prop_box.row(align=True)
            row.prop(obj, '["colorShift1_hue"]', text="H1")
            row.prop(obj, '["colorShift1_saturation"]', text="S1")
            row.prop(obj, '["colorShift1_luminance"]', text="L1")
        if obj.get('colorShift2_hue') is not None:
            row = prop_box.row(align=True)
            row.prop(obj, '["colorShift2_hue"]', text="H2")
            row.prop(obj, '["colorShift2_saturation"]', text="S2")
            row.prop(obj, '["colorShift2_luminance"]', text="L2")


def _get_character_panel_header_status(context) -> str:
    try:
        main_arm_obj, rig_settings = get_main_armature_and_rig_settings(
            context, prefer_active=True, remember=True, fallback=True,
        )
    except Exception:
        return "No target"

    if not main_arm_obj:
        return "No target"

    if rig_settings and getattr(rig_settings, "app_list", None):
        idx = int(getattr(rig_settings, "app_list_index", -1))
        app_list = rig_settings.app_list
        if 0 <= idx < len(app_list):
            app_name = str(getattr(app_list[idx], "name", "") or "").strip()
            if app_name:
                return _short_panel_header_text(app_name)

    return _short_panel_header_text(getattr(main_arm_obj, "name", "Character"))


def _get_witcher3_game_path_issue(context) -> str:
    addon_prefs = get_all_addon_prefs(context)
    raw_game_path = (getattr(addon_prefs, "witcher_game_path", "") or "").strip()
    if not raw_game_path:
        return f"Set Witcher 3 install folder ({WITCHER3_EXE_REL}) in addon preferences."
    game_path = bpy.path.abspath(raw_game_path)
    if is_valid_witcher3_game_path(game_path):
        return ""
    return f"Invalid Witcher 3 path. Missing: {get_witcher3_exe_path(game_path)}"


def _ensure_witcher3_game_path_initialized(context) -> bool:
    addon_prefs = get_all_addon_prefs(context)
    current_game_path = (getattr(addon_prefs, "witcher_game_path", "") or "").strip()
    current_game_path_abs = bpy.path.abspath(current_game_path) if current_game_path else ""
    if not current_game_path and not is_valid_witcher3_game_path(current_game_path_abs):
        detected_game_path = auto_detect_witcher3_game_path()
        if detected_game_path and detected_game_path != current_game_path:
            addon_prefs.witcher_game_path = detected_game_path
    update_witcher_game_path(addon_prefs, context)
    return not bool(_get_witcher3_game_path_issue(context))


class WITCH_UL_InventoryPreview(UIList):
    bl_idname = "WITCH_UL_InventoryPreview"
    layout_type = "DEFAULT"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index, flt_flag):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            # Checkbox for selection
            row.prop(item, "selected", text="")
            # Item label
            name = item.item_name if item.item_name else "random"
            label = f"{item.category}:{name}" if item.category else name
            row.label(text=label)
            # Mount indicator
            if item.is_mount:
                row.label(text="EQUIP", icon='ARMATURE_DATA')
            # Random indicator
            if not item.item_name or item.item_name.lower() == "random":
                row.label(text="RND", icon='QUESTION')
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")

class InventoryPreviewItem(bpy.types.PropertyGroup):
    item: StringProperty(name="Item", default="")
    category: StringProperty(name="Category", default="")
    item_name: StringProperty(name="Item Name", default="")
    appearance: StringProperty(name="Appearance", default="")
    is_mount: bpy.props.BoolProperty(name="Mounted", default=False)
    quantity: IntProperty(name="Quantity", default=0)
    quantity_min: IntProperty(name="Quantity Min", default=0)
    quantity_max: IntProperty(name="Quantity Max", default=0)
    probability: bpy.props.FloatProperty(name="Probability", default=0.0)
    is_lootable: bpy.props.BoolProperty(name="Lootable", default=False)
    selected: bpy.props.BoolProperty(name="Import", default=True, description="Include this item in import")

def _collect_inventory_preview(filepath):
    preview = []
    try:
        entity = test_load_entity(filepath)
    except Exception as e:
        error_msg = str(e)
        log.error(f"Failed to parse .w2ent file: {e}")
        return preview, f"Parse error: {error_msg[:50]}"
    if not entity:
        return preview, "No entity data"

    # Debug: log entity attributes
    entity_attrs = dir(entity) if not isinstance(entity, dict) else list(entity.keys())
    log.debug(f"Entity type: {type(entity)}, attributes: {[a for a in entity_attrs if not a.startswith('_')]}")

    def _get_attr(obj, key, default=None):
        """Get attribute from object or dict."""
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _collect_from_inv_defs(inv_defs, app_name=""):
        log.debug(f"Collecting from {len(inv_defs)} inv_defs for app={app_name}")
        for inv_def in inv_defs:
            entries = _get_attr(inv_def, "entries", None) or []
            log.debug(f"  inv_def has {len(entries)} entries")
            for entry in entries:
                category = _get_attr(entry, "category", "") or ""
                item_name = _get_attr(entry, "item", "") or ""
                initializer = _get_attr(entry, "initializer", None)
                if initializer is not None:
                    init_item = _get_attr(initializer, "itemName", None) or _get_attr(initializer, "item", None)
                    if init_item:
                        item_name = init_item
                log.debug(f"    Entry: category={category}, item_name={item_name}, initializer={type(initializer)}")
                if not item_name and initializer is not None:
                    # Treat missing itemName as random initializer
                    item_name = "random"
                qty = _get_attr(entry, "quantity", None)
                if qty is None:
                    qty = _get_attr(entry, "quantityMin", 0) or 0
                qty_min = _get_attr(entry, "quantityMin", 0) or 0
                qty_max = _get_attr(entry, "quantityMax", 0) or 0
                probability = _get_attr(entry, "probability", 0.0) or 0.0
                is_lootable = bool(_get_attr(entry, "isLootable", False))
                is_mount = _get_attr(entry, "isMount", None)
                if is_mount is None:
                    is_mount = bool(category)
                else:
                    is_mount = bool(is_mount)
                display_item = f"{category}:{item_name}" if category else str(item_name)
                preview.append((display_item, category, item_name, app_name, is_mount, qty, qty_min, qty_max, probability, is_lootable))

    # Appearance-level inventory
    appearances = _get_attr(entity, "appearances", []) or []
    log.debug(f"Entity has {len(appearances)} appearances")
    for app in appearances:
        app_name = _get_attr(app, "name", "")
        inv_defs = _get_attr(app, "inventoryDefinitions", None) or []
        _collect_from_inv_defs(inv_defs, app_name)

    # Entity-level inventory (templateParams)
    inv_defs = _get_attr(entity, "inventoryDefinitions", None) or []
    log.debug(f"Entity-level inventoryDefinitions: {len(inv_defs)}")
    _collect_from_inv_defs(inv_defs, "entity")

    if not preview:
        # Additional debug: check if entity has templateParams or other inventory sources
        template_params = _get_attr(entity, "templateParams", None)
        log.debug(f"Entity templateParams: {template_params}")
        return preview, "No inventory entries found"
    return preview, f"{len(preview)} inventory entries"

def _update_inventory_preview(self):
    filepath = self.filepath
    if not filepath or not os.path.isfile(filepath):
        self.inventory_preview_items.clear()
        self.inventory_preview_status = "Select a .w2ent file"
        self.inventory_preview_path = ""
        self.inventory_preview_mtime = 0.0
        return False

    if not (filepath.lower().endswith(".w2ent") or filepath.lower().endswith(".w2ent.json")):
        self.inventory_preview_items.clear()
        self.inventory_preview_status = "Not a .w2ent file"
        self.inventory_preview_path = filepath
        self.inventory_preview_mtime = 0.0
        return False

    try:
        mtime = os.path.getmtime(filepath)
    except Exception:
        mtime = 0.0

    if filepath == self.inventory_preview_path and mtime == self.inventory_preview_mtime:
        return False

    # Preserve existing selection state by category:item_name key
    old_selection = {}
    for item in self.inventory_preview_items:
        key = (item.category, item.item_name)
        old_selection[key] = item.selected

    self.inventory_preview_items.clear()
    preview, status = _collect_inventory_preview(filepath)
    for display_item, category, item_name, app_name, is_mount, qty, qty_min, qty_max, probability, is_lootable in preview:
        row = self.inventory_preview_items.add()
        row.item = str(display_item)
        row.category = str(category)
        row.item_name = str(item_name)
        row.appearance = str(app_name)
        row.is_mount = is_mount
        row.quantity = int(qty) if qty is not None else 0
        row.quantity_min = int(qty_min) if qty_min is not None else 0
        row.quantity_max = int(qty_max) if qty_max is not None else 0
        row.probability = float(probability) if probability is not None else 0.0
        row.is_lootable = bool(is_lootable)
        # Restore selection if this item existed before, otherwise default to True for mounts
        key = (category, item_name)
        if key in old_selection:
            row.selected = old_selection[key]
        else:
            row.selected = is_mount  # Default: select mounted items
    self.inventory_preview_status = status
    self.inventory_preview_path = filepath
    self.inventory_preview_mtime = mtime
    return True


def _import_inventory_items(context, armature, rig_settings, preview_items):
    """Import inventory items directly from preview items.

    Uses a two-pass approach to avoid Blender crashes:
      Pass 1: Resolve items, update slot data, unload old objects
      Pass 2: Load new equipment one at a time with depsgraph updates between each

    Args:
        context: Blender context
        armature: The armature object (can be None for standalone import)
        rig_settings: The rig settings from the armature (can be None)
        preview_items: List of preview item dicts with category, item_name, is_mount, selected

    Returns:
        dict with 'imported', 'skipped', 'created_slots', 'random', 'messages', 'imported_objects'
    """
    import random as random_module
    from ..importers.import_entity import (
        _normalize_key, _resolve_inventory_item,
        _build_equipment_lookup, _derive_template_from_item
    )

    result = {'imported': 0, 'skipped': 0, 'created_slots': 0, 'random': 0, 'messages': [], 'imported_objects': []}

    try:
        from ..ui import ui_equipment as _equip_mod
        from ..ui.ui_equipment import (
            EquipmentDefinitionEntry, load_equipment_item, remove_objects_by_guid,
        )
    except Exception as e:
        result['messages'].append(f"Failed to import equipment functions: {e}")
        return result

    item_lookup, template_lookup = _build_equipment_lookup()

    # Build slot lookup if we have rig_settings
    slots = None
    slot_by_category = {}
    if rig_settings:
        slots = rig_settings.equipment_slots
        slot_by_category = {slot.category: (idx, slot) for idx, slot in enumerate(slots) if slot.category}

    # =========================================================================
    # Pass 1: Resolve all items, update slot data, unload old objects
    # =========================================================================
    pending_loads = []  # List of (slot_index, item_name, template) for armature path
    standalone_loads = []  # List of (category, item_name, template) for standalone path

    for item_data in preview_items:
        category = item_data.get('category', '')
        item_name = item_data.get('item_name', '')
        is_mount = item_data.get('is_mount', False)

        # Handle "random" items - pick a random item from the category
        if not item_name or item_name.lower() in {"none", "random", "null", ""}:
            if category and category in EquipmentDefinitionEntry.category_items:
                cat_items = EquipmentDefinitionEntry.category_items[category]
                # Filter out "None" entries
                valid_items = [(name, display, tmpl) for name, display, tmpl in cat_items
                               if name and name.lower() not in {"none", ""}]
                if valid_items:
                    chosen = random_module.choice(valid_items)
                    item_name = chosen[0]
                    result['random'] += 1
                    result['messages'].append(f"Random pick for {category}: {item_name}")
                else:
                    result['skipped'] += 1
                    result['messages'].append(f"No valid items for random category: {category}")
                    continue
            else:
                result['skipped'] += 1
                result['messages'].append(f"Cannot resolve random item for unknown category: {category}")
                continue

        # Try to resolve item to template
        resolved = _resolve_inventory_item(item_name, item_lookup, template_lookup)
        resolved_category = resolved[0] if resolved else ""
        resolved_item_name = resolved[1] if resolved else ""
        resolved_template = resolved[2] if resolved else ""

        log.debug(f"Resolving item: {category}:{item_name} -> resolved={resolved}")

        # Determine template
        template = resolved_template
        if not template:
            if category:
                for name, _display, tmpl in EquipmentDefinitionEntry.category_items.get(category, []):
                    if _normalize_key(name) == _normalize_key(item_name):
                        template = tmpl
                        log.debug(f"Found template via category lookup: {tmpl}")
                        break
            if not template:
                template = _derive_template_from_item(item_name)
                if template:
                    log.debug(f"Derived template from item: {template}")
        if not template:
            template = item_name
            log.debug(f"Using item name as template fallback: {template}")

        if not template or template == "None":
            result['skipped'] += 1
            result['messages'].append(f"No template found for: {category}:{item_name}")
            log.warning(f"Skipping item - no template: {category}:{item_name}")
            continue

        log.info(f"Importing {category}:{item_name} with template: {template}")

        # If we have rig_settings, use the slot system
        if rig_settings and slots is not None:
            slot_index = None
            slot = None

            # Try to find existing slot by category
            if category and category in slot_by_category:
                slot_index, slot = slot_by_category[category]
            elif resolved_category and resolved_category in slot_by_category:
                slot_index, slot = slot_by_category[resolved_category]

            # If no slot found, create one for this category
            if slot is None and category:
                slot = slots.add()
                slot.category = category
                slot_index = len(slots) - 1
                slot_by_category[category] = (slot_index, slot)
                result['created_slots'] += 1
                result['messages'].append(f"Created new slot for category: {category}")

            if slot is None:
                result['skipped'] += 1
                result['messages'].append(f"No slot for item: {item_name} (category: {category})")
                continue

            # Update slot data
            slot.item_name = resolved_item_name if resolved_item_name else item_name
            slot.equip_template = template
            slot.base_equip_template = template
            slot.is_inventory = True

            # Get slot attributes if available
            attrs = EquipmentDefinitionEntry.item_attributes.get(item_name, {})
            if not attrs and resolved_item_name:
                attrs = EquipmentDefinitionEntry.item_attributes.get(resolved_item_name, {})
            if attrs:
                slot.equip_slot = attrs.get('equip_slot', slot.equip_slot)
                slot.hold_slot = attrs.get('hold_slot', slot.hold_slot)
                slot.weapon = attrs.get('weapon', slot.weapon)
                slot.attachment_type = attrs.get('attachment_type', slot.attachment_type)
                try:
                    slot.variants_json = json.dumps(attrs.get('variants', []))
                except Exception:
                    pass
                try:
                    slot.bound_items_json = json.dumps(attrs.get('bound_items', []))
                except Exception:
                    pass

            # Unload existing item in this slot
            if slot.is_loaded and slot.equip_guid:
                remove_objects_by_guid(slot.equip_guid, "witcher_equip_guid")
                slot.equip_guid = ""
                slot.is_loaded = False

            pending_loads.append((slot_index, item_name, template))
        else:
            standalone_loads.append((category, item_name, template))

    # Let Blender process all the object deletions from pass 1
    if pending_loads or standalone_loads:
        try:
            bpy.context.view_layer.update()
        except Exception:
            pass

    # =========================================================================
    # Pass 2: Load equipment one at a time with depsgraph updates between each
    # =========================================================================

    # Suppress variant refresh during bulk load - it triggers recursive
    # load_equipment_item calls that can crash Blender.
    saved_variant_flag = _equip_mod._VARIANT_REFRESHING
    _equip_mod._VARIANT_REFRESHING = True
    try:
        for slot_index, item_name, template in pending_loads:
            success = load_equipment_item(context, armature, slot_index, rig_settings)
            if success:
                result['imported'] += 1
            else:
                result['skipped'] += 1
                result['messages'].append(f"Failed to load: {item_name} ({template})")

            # Let Blender settle between imports to avoid access violations
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
    finally:
        _equip_mod._VARIANT_REFRESHING = saved_variant_flag

    # Now do a single variant refresh for the entire batch
    if pending_loads and rig_settings:
        try:
            from ..ui.ui_equipment import _refresh_variants_and_reload
            _refresh_variants_and_reload(context, armature, rig_settings)
        except Exception:
            pass

    # Standalone imports (no armature)
    for category, item_name, template in standalone_loads:
        success = _import_standalone_item(context, category, item_name, template, result)
        if success:
            result['imported'] += 1

    return result


def _import_standalone_item(context, category, item_name, template, result):
    """Import an equipment item without binding to a character."""
    from ..CR2W.witcher_cache.Bundles import LoadBundleManager
    from ..CR2W.common_blender import repo_file
    import os

    try:
        bundle_manager = LoadBundleManager()
        search_pattern = "\\" + template + ".w2ent"
        items = bundle_manager.find_item_by_partial_hash(start="items", end=search_pattern)

        if not items:
            result['messages'].append(f"Bundle not found for: {template}")
            return False

        final_item = items[-1]
        if isinstance(final_item, list) and len(final_item) > 0:
            final_item = final_item[-1]

        if not hasattr(final_item, 'name'):
            result['messages'].append(f"Invalid bundle item for: {template}")
            return False

        export_path = repo_file(final_item.name)
        if not os.path.exists(export_path):
            final_item.extract_to_file(export_path)

        # Import the entity
        from ..importers import import_entity
        import_entity.import_ent_template(export_path, False, 1, None)

        result['messages'].append(f"Imported standalone: {category}:{item_name}")
        return True

    except Exception as e:
        result['messages'].append(f"Error importing {template}: {e}")
        return False


class WITCH_UL_ENTITY_List(UIList):
    """Demo UIList."""
    bl_idname = "WITCH_UL_ENTITY_List"
    layout_type = "DEFAULT" # could be "COMPACT" or "GRID"

    def draw_item(self, context,
                    layout, # Layout to draw the item
                    data, # Data from which to take Collection property
                    item, # Item of the collection property
                    icon, # Icon of the item in the collection
                    active_data, # Data from which to take property for the active element
                    active_propname, # Identifier of property in active_data, for the active element
                    index, # Index of the item in the collection - default 0
                    flt_flag # The filter-flag result for this item - default 0
            ):
        #Make sure your code supports all 3 layout types
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.name)

        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")

class WITCH_OT_w3app(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Appearance File"""
    bl_idname = "witcher.import_w3app"
    bl_label = "Import .w3app"
    filename_ext = ".w3app"
    def execute(self, context):
        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        ext = file_helpers.getFilenameType(fdir)
        if ext == ".w3app":
            entity_w3a = test_load_entity(fdir)
            #entity_w3a = fixed_chunk_paths(entity_w3a, entity_w3a.version)
            main_arm_obj, rig_settings = get_main_armature_and_rig_settings(
                context,
                prefer_active=True,
                remember=True,
                fallback=True,
            )
            if not main_arm_obj or not rig_settings:
                self.report({'WARNING'}, "No character armature target selected.")
                return {'CANCELLED'}
            treeList = rig_settings.app_list
            item = treeList.add()
            node = entity_w3a.appearances[0]
            item.name = node.name
            entity, _entity_data = import_entity.get_rig_entity_state(rig_settings)
            if entity is None:
                self.report({'WARNING'}, "No cached entity state on the target armature.")
                return {'CANCELLED'}
            entity.appearances.append(entity_w3a.appearances[0])
            import_entity.cache_rig_entity_state(rig_settings, entity, update_json=True)
            rig_settings.app_list_index = len(entity.appearances)-1
            with mod_loading_context(context):
                import_entity.import_from_list_item(context, item)
        else:
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        return {'FINISHED'}
    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"dlc\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)

class WITCH_OT_w2ent(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Entity File"""
    bl_idname = "witcher.import_w2ent"
    bl_label = "Import .w2ent"
    filename_ext = ".w2ent"
    filter_glob: StringProperty(default='*.w2ent;*.w2ent.json', options={'HIDDEN'})

    def execute(self, context):
        log.debug("importing entity")
        fdir = self.filepath
        with mod_loading_context(context):
            if os.path.isdir(fdir):
                self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
                return {'CANCELLED'}
            ext = file_helpers.getFilenameType(fdir)
            if ext == ".w2ent" or fdir.endswith(".json"):
                import_entity.import_direct_entity_file(
                    fdir,
                    load_face_poses=False,
                    import_apperance=0,
                    parent_transform=None,
                )
            else:
                self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
                return {'CANCELLED'}
            return {'FINISHED'}

    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"items\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)


class WITCH_OT_flyr(bpy.types.Operator, ImportHelper):
    """Load Witcher 3 Foliage File"""
    bl_idname = "witcher.import_flyr"
    bl_label = "Import .flyr"
    filename_ext = ".flyr"

    filter_glob: StringProperty(default='*.flyr', options={'HIDDEN'})

    def execute(self, context):
        log.debug("importing foliage")
        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        ext = file_helpers.getFilenameType(fdir)
        if ext != ".flyr":
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}
        foliage = CR2W.CR2W_reader.load_foliage(fdir)
        import_w2l.btn_import_w2ent(foliage)
        return {'FINISHED'}

    def invoke(self, context, event):
        UNCOOK_PATH = get_uncook_path(context)
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)


class WITCH_OT_ENTITY_w2ent_chara(bpy.types.Operator, ImportHelper):
    """Load a Witcher 3 character (.w2ent) file"""
    bl_idname = "witcher.import_w2ent_character"
    bl_label = "Import Character"
    filename_ext = ".w2ent"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.w2ent;*.w2ent.json', options={'HIDDEN'})
    import_apperance: IntProperty(
        name="Select Apperance",
        default=0,
        description="Select index of apperance. 0 will only import character base"
    )
    def draw(self, context):
        filepath = self.filepath
        layout = self.layout

        # check if the file is a file and has the .w2ent extension
        if os.path.isfile(self.filepath) and self.filepath.endswith('.w2ent'):
            pass

        else:
            layout.label(text="Selected file is not a .w2ent file.")

        sections = ["Settings"]
        section_options = {
            "Settings" : [
                        "import_apperance",
                        ]
        }
        for section in sections:
            row = layout.row()
            box = row.box()
            box.label(text=section)
            for prop in section_options[section]:
                box.prop(self, prop)
        addon_prefs = get_all_addon_prefs(context)
        redcloth_box = layout.box()
        redcloth_box.label(text="Global Redcloth Settings", icon='MATCLOTH')
        redcloth_box.prop(addon_prefs, "do_import_redcloth")
        redcloth_box.prop(addon_prefs, "DO_WEAR_CLOTH")
        redcloth_box.prop(addon_prefs, "redcloth_simulation_enabled")
        redcloth_box.prop(addon_prefs, "redcloth_wind_velocity")

    def execute(self, context):
        log.debug("importing character")
        fdir = self.filepath

        with mod_loading_context(context):
            if os.path.isdir(fdir):
                self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
                return {'CANCELLED'}

            s = time.time()
            if fdir.endswith(".w2ent") or fdir.endswith(".json"):
                from ..ui.ui_equipment import EquipmentDefinitionEntry as _EDE
                if not getattr(_EDE, "item_attributes", None):
                    bpy.ops.witcher.equipment_refresh_categories()
                import_entity.import_ent_template(
                    fdir,
                    False,
                    self.import_apperance,
                    parent_transform=None,
                )
            else:
                self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
                return {'CANCELLED'}
            message = f'Read character file in {time.time() - s} seconds.'
            log.info(message)
            self.report({'INFO'}, message)
            return {'FINISHED'}

    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context),"characters\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)


class WITCH_OT_ENTITY_import_inventory(bpy.types.Operator, ImportHelper):
    """Import inventory from a .w2ent and apply to current character or standalone"""
    bl_idname = "witcher.import_w2ent_inventory"
    bl_label = "Import Inventory (.w2ent)"
    filename_ext = ".w2ent"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(default='*.w2ent;*.w2ent.json', options={'HIDDEN'})

    import_mode: bpy.props.EnumProperty(
        name="Import Mode",
        items=[
            ('MOUNTS', "Equipment Only", "Import only mounted equipment items"),
            ('ALL', "All Items", "Import all inventory items"),
        ],
        default='MOUNTS',
        description="Which items to import from the inventory"
    )

    inventory_preview_items: bpy.props.CollectionProperty(type=InventoryPreviewItem)
    inventory_preview_index: IntProperty(default=0)
    inventory_preview_status: StringProperty(default="Select a .w2ent file")
    inventory_preview_path: StringProperty(default="")
    inventory_preview_mtime: bpy.props.FloatProperty(default=0.0)

    def draw(self, context):
        layout = self.layout

        # Check if armature is selected
        ob = context.object
        has_armature = ob and ob.type == "ARMATURE"

        settings = layout.box()
        settings.label(text="Import Settings")
        settings.prop(self, "import_mode")

        if not has_armature:
            settings.label(text="No armature selected - standalone import", icon='INFO')

        preview_box = layout.box()
        preview_box.label(text="Inventory Preview")
        if self.inventory_preview_items:
            preview_box.template_list("WITCH_UL_InventoryPreview", "", self, "inventory_preview_items", self, "inventory_preview_index", rows=8)

            # Count selected items by mode
            if self.import_mode == 'MOUNTS':
                count = sum(1 for item in self.inventory_preview_items if item.selected and item.is_mount)
                total = sum(1 for item in self.inventory_preview_items if item.is_mount)
                preview_box.label(text=f"Will import: {count}/{total} equipment items")
            else:
                count = sum(1 for item in self.inventory_preview_items if item.selected)
                total = len(self.inventory_preview_items)
                preview_box.label(text=f"Will import: {count}/{total} items")

            idx = self.inventory_preview_index
            if 0 <= idx < len(self.inventory_preview_items):
                item = self.inventory_preview_items[idx]
                details = preview_box.column(align=True)
                details.label(text=f"Category: {item.category}")
                details.label(text=f"Item: {item.item_name if item.item_name else 'random'}")
                if item.appearance:
                    details.label(text=f"Appearance: {item.appearance}")
                details.label(text=f"Mounted: {item.is_mount}")
                if item.quantity_min or item.quantity_max:
                    details.label(text=f"Quantity: {item.quantity_min}-{item.quantity_max}")
                else:
                    details.label(text=f"Quantity: {item.quantity}")
                if item.probability:
                    details.label(text=f"Probability: {item.probability}")
                details.label(text=f"Lootable: {item.is_lootable}")
        preview_box.label(text=self.inventory_preview_status)

    def check(self, context):
        return _update_inventory_preview(self)

    def execute(self, context):
        fdir = self.filepath
        if os.path.isdir(fdir):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}

        if not (fdir.endswith(".w2ent") or fdir.endswith(".json")):
            self.report({'ERROR'}, "ERROR File Format unrecognized, operation cancelled.")
            return {'CANCELLED'}

        # If called directly (EXEC_DEFAULT from dev panel etc), build preview internally
        if not self.inventory_preview_items:
            preview_data, status = _collect_inventory_preview(fdir)
            if not preview_data:
                self.report({'WARNING'}, f"No inventory items found in file. ({status})")
                return {'CANCELLED'}
            for display_item, cat, item_name, app_name, is_mount, qty, qty_min, qty_max, probability, is_lootable in preview_data:
                row = self.inventory_preview_items.add()
                row.item = str(display_item)
                row.category = str(cat)
                row.item_name = str(item_name)
                row.appearance = str(app_name)
                row.is_mount = is_mount
                row.quantity = int(qty) if qty is not None else 0
                row.quantity_min = int(qty_min) if qty_min is not None else 0
                row.quantity_max = int(qty_max) if qty_max is not None else 0
                row.probability = float(probability) if probability is not None else 0.0
                row.is_lootable = bool(is_lootable)
                row.selected = is_mount  # Default: select mounted items

        armature, rig_settings = get_main_armature_and_rig_settings(
            context,
            prefer_active=True,
            remember=True,
            fallback=True,
        )
        if rig_settings:
            entity, _entity_data = import_entity.get_rig_entity_state(rig_settings)
            if entity is None:
                armature = None
                rig_settings = None

        # Build list of items to import from preview
        preview_items = []
        for item in self.inventory_preview_items:
            # Skip items that are not selected in preview
            if not item.selected:
                continue
            # Filter by import mode
            if self.import_mode == 'MOUNTS' and not item.is_mount:
                continue

            preview_items.append({
                'category': item.category,
                'item_name': item.item_name,
                'is_mount': item.is_mount
            })

        if not preview_items:
            self.report({'WARNING'}, "No items to import based on current filter.")
            return {'CANCELLED'}

        # Ensure categories are loaded (skip if already populated)
        try:
            from ..importers import import_entity as _import_entity
            from ..ui.ui_equipment import (
                EquipmentDefinitionEntry as _EDE,
                ensure_equipment_catalog_for_search_roots as _ensure_w2_catalog,
                get_equipment_source_game_for_search_roots as _catalog_game,
            )
            source_roots = _import_entity._get_armature_source_roots(armature)
            if _catalog_game(source_roots) == "w2":
                _ensure_w2_catalog(source_roots)
            elif not getattr(_EDE, "item_attributes", None):
                bpy.ops.witcher.equipment_refresh_categories()
        except Exception:
            pass  # May fail if XML not configured, continue anyway

        with mod_loading_context(context):
            result = _import_inventory_items(context, armature, rig_settings, preview_items)

        # Sync persistent equipment slots back to temp UI entries so
        # category dropdowns reflect the inventory changes
        if rig_settings and result.get('imported', 0) > 0:
            try:
                from ..ui.ui_equipment import sync_equipment_slots_to_temp
                sync_equipment_slots_to_temp(context, rig_settings)
            except Exception as e:
                log.warning(f"Failed to sync equipment slots to temp: {e}")

        imported_count = result.get('imported', 0)
        skipped_count = result.get('skipped', 0)
        created_slots = result.get('created_slots', 0)
        random_count = result.get('random', 0)
        messages = result.get('messages', [])

        # Report results
        if imported_count > 0:
            msg = f"Imported {imported_count} item(s)"
            if created_slots > 0:
                msg += f", created {created_slots} new slot(s)"
            if random_count > 0:
                msg += f" ({random_count} random)"
            if skipped_count > 0:
                msg += f", skipped {skipped_count}"
            self.report({'INFO'}, msg)
        else:
            self.report({'WARNING'}, f"No items imported. Skipped: {skipped_count}.")
            for msg in messages[:5]:  # Show first 5 messages
                self.report({'INFO'}, msg)

        for msg in messages:
            log.info(msg)

        return {'FINISHED'}

    def invoke(self, context, event):
        UNCOOK_PATH = os.path.join(get_uncook_path(context), "gameplay\\")
        if os.path.exists(UNCOOK_PATH):
            self.filepath = UNCOOK_PATH if self.filepath == '' else self.filepath
        return ImportHelper.invoke(self, context, event)


class WITCH_OT_ENTITY_import_geralt(bpy.types.Operator):
    """Import Geralt (player.w2ent) with default equipment"""
    bl_idname = "witcher.import_geralt"
    bl_label = "Import Geralt"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = time.time()

        with mod_loading_context(context):
            rel_path = "gameplay\\templates\\characters\\player\\player.w2ent"
            uncook_path = os.path.join(get_uncook_path(context), rel_path)
            use_uncook_file = os.path.exists(uncook_path)

            if not use_uncook_file:
                _ensure_witcher3_game_path_initialized(context)
                game_path_issue = _get_witcher3_game_path_issue(context)
                if game_path_issue:
                    self.report({'ERROR'}, "Import Geralt needs either a valid Witcher 3 path for bundle fallback or an already-exported player.w2ent in the uncook folder.")
                    self.report({'WARNING'}, game_path_issue)
                    return {'CANCELLED'}

            # Enhanced Import Geralt workflow:
            # 1. Load default categories (refresh only if not already populated)
            from ..ui.ui_equipment import EquipmentDefinitionEntry as _EDE
            if not getattr(_EDE, "item_attributes", None):
                bpy.ops.witcher.equipment_refresh_categories()
            bpy.ops.witcher.equipment_insert_default_categories()

            # 2. Import Geralt with slots and default equipment
            path = uncook_path if use_uncook_file else repo_file(rel_path)
            if not os.path.exists(path):
                self.report({'ERROR'}, f"player.w2ent not found and could not be extracted at: {path}")
                return {'CANCELLED'}

            # import_apperance=1 means app_idx=0 (first appearance with equipment entries)
            import_entity.import_ent_template(path, False, 1,
                                              parent_transform=None)

            # 3. Auto-load default equipment items (handled in import_entity.py)
            # The equipment slots are automatically populated during import

            message = f'Imported Geralt with slots and default equipment in {time.time() - s:.2f} seconds.'
            log.info(message)
            self.report({'INFO'}, message)
            return {'FINISHED'}


class WITCH_OT_ENTITY_import_ciri(bpy.types.Operator):
    """Import Ciri (ciri_player.w2ent) with embedded inventory"""
    bl_idname = "witcher.import_ciri"
    bl_label = "Import Ciri"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = time.time()

        with mod_loading_context(context):
            rel_path = "gameplay\\templates\\characters\\player\\ciri_player.w2ent"
            uncook_path = os.path.join(get_uncook_path(context), rel_path)
            use_uncook_file = os.path.exists(uncook_path)

            if not use_uncook_file:
                _ensure_witcher3_game_path_initialized(context)
                game_path_issue = _get_witcher3_game_path_issue(context)
                if game_path_issue:
                    self.report({'ERROR'}, "Import Ciri needs either a valid Witcher 3 path for bundle fallback or an already-exported ciri_player.w2ent in the uncook folder.")
                    self.report({'WARNING'}, game_path_issue)
                    return {'CANCELLED'}

            path = uncook_path if use_uncook_file else repo_file(rel_path)
            if not os.path.exists(path):
                self.report({'ERROR'}, f"ciri_player.w2ent not found and could not be extracted at: {path}")
                return {'CANCELLED'}

            try:
                from ..ui.ui_equipment import EquipmentDefinitionEntry as _EDE
                if not getattr(_EDE, "item_attributes", None):
                    bpy.ops.witcher.equipment_refresh_categories()
            except Exception:
                pass

            # import_apperance=1 means app_idx=0 (first appearance)
            import_entity.import_ent_template(path, False, 1,
                                              parent_transform=None)

            message = f'Imported Ciri with inventory in {time.time() - s:.2f} seconds.'
            log.info(message)
            self.report({'INFO'}, message)
            return {'FINISHED'}


class WITCH_OT_RevealAnimInExplorer(Operator):
    """Open the file's containing folder in Windows Explorer"""
    bl_idname = "witcher.reveal_anim_in_explorer"
    bl_label = "Reveal in Explorer"
    bl_description = "Open the folder containing this animation file in Windows Explorer"

    path: StringProperty(default="")

    def execute(self, context):
        if not self.path:
            return {'CANCELLED'}
        full_path = os.path.join(get_uncook_path(context), self.path)
        try:
            with mod_loading_context(context):
                resolved = repo_file(self.path)
            if resolved:
                full_path = resolved
        except Exception:
            # Fallback to uncook path when bundle extraction is unavailable.
            pass
        if not os.path.isfile(full_path) and os.path.isfile(full_path + ".json"):
            full_path = full_path + ".json"
        folder = os.path.dirname(full_path)
        if os.path.isdir(folder):
            import subprocess
            subprocess.Popen(f'explorer /select,"{full_path}"' if os.path.isfile(full_path) else f'explorer "{folder}"')
        else:
            self.report({'WARNING'}, f"Folder not found: {folder}")
        return {'FINISHED'}


class WITCH_OT_AnimSetPathInfo(Operator):
    """Show repo and local paths for an animation set"""
    bl_idname = "witcher.animset_path_info"
    bl_label = "Animation Set Path"
    bl_description = "Show the repo path and local uncook path for this animation set"

    path: StringProperty(default="")
    uncook_path: StringProperty(default="", options={'SKIP_SAVE'})
    resolved_path: StringProperty(default="", options={'SKIP_SAVE'})
    status_text: StringProperty(default="", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        repo_rel = (self.path or "").replace("/", os.sep).replace("\\", os.sep)
        self.uncook_path = os.path.normpath(os.path.join(get_uncook_path(context), repo_rel)) if repo_rel else ""
        self.resolved_path = ""
        exists = False

        if self.uncook_path and os.path.isfile(self.uncook_path + ".json"):
            self.resolved_path = self.uncook_path + ".json"
            exists = True
        elif self.uncook_path and os.path.exists(self.uncook_path):
            self.resolved_path = self.uncook_path
            exists = True

        self.status_text = "Exists on disk" if exists else "Not extracted yet (Reveal/Load may extract it)"
        return context.window_manager.invoke_props_dialog(self, width=780)

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="Animation Set Path", icon='QUESTION')
        col.prop(self, "path", text="Repo")
        col.prop(self, "uncook_path", text="Uncook")
        if self.resolved_path:
            col.prop(self, "resolved_path", text="Resolved")
        icon = 'CHECKMARK' if "Exists" in self.status_text else 'INFO'
        col.label(text=self.status_text, icon=icon)

    def execute(self, context):
        return {'FINISHED'}


class WITCH_OT_coloring_select_component(Operator):
    """Select mesh objects matching this coloring entry's component name"""
    bl_idname = "witcher.coloring_select_component"
    bl_label = "Select Component"
    bl_options = {'REGISTER', 'UNDO'}

    component_name: StringProperty(default="")

    def execute(self, context):
        if not self.component_name:
            self.report({'WARNING'}, "No component name specified.")
            return {'CANCELLED'}

        bpy.ops.object.select_all(action='DESELECT')
        found = _find_coloring_objects(context, self.component_name)
        for obj in found:
            obj.select_set(True)

        if found:
            context.view_layer.objects.active = found[0]
            self.report({'INFO'}, f"Selected {len(found)} object(s) for '{self.component_name}'")
        else:
            self.report({'WARNING'}, f"No mesh objects found with component name '{self.component_name}'")
            return {'CANCELLED'}
        return {'FINISHED'}


class WITCH_OT_ENTITY_list_loadapp(Operator):
    """ Load appearance or animation set for this character"""
    bl_idname = "witcher.list_loadapp"
    bl_label = "Load"
    bl_description = "Load the selected item for this character"

    action: StringProperty(default="default")
    path: StringProperty(default="")  # Optional: when set, loads this path directly (bypasses animset_list_index)
    @classmethod
    def poll(cls, context):
        return context.scene

    def execute(self, context):
        main_arm_obj, rig_settings = get_main_armature_and_rig_settings(
            context,
            prefer_active=True,
            remember=True,
            fallback=True,
        )
        if not main_arm_obj or not rig_settings:
            self.report({'WARNING'}, "No character armature target selected.")
            return {'CANCELLED'}

        scene = context.scene
        action = self.action

        with mod_loading_context(context):
            if "w2anims" == action:
                log.debug("load w2anims, skeleton: %s", rig_settings.main_entity_skeleton)

                if self.path:
                    anim_path = self.path
                    # Keep the rig animset index in sync with direct row-button loads.
                    try:
                        for idx, anim_item in enumerate(rig_settings.animset_list):
                            if getattr(anim_item, "path", "") == self.path:
                                rig_settings.animset_list_index = idx
                                break
                    except Exception:
                        pass
                elif rig_settings.animset_list_index >= 0 and rig_settings.animset_list:
                    anim_path = rig_settings.animset_list[rig_settings.animset_list_index].path
                else:
                    anim_path = None

                if anim_path and ":" not in anim_path:
                    from ..CR2W.common_blender import repo_file as _repo_file
                    _repo_file(anim_path)  # Extract from bundle to uncook dir if not already present
                    fdir = os.path.join(get_uncook_path(context), anim_path)
                    log.debug("Loading anims from: %s", fdir)
                    if os.path.exists(fdir + '.json'):
                        fdir = fdir + '.json'
                    if "_mimic_" in fdir:
                        skel = (rig_settings.main_face_skeleton or "").strip()
                        import_anims.start_import(context, fdir, rigPath=_repo_file(skel) if skel else None)
                    else:
                        skel = (rig_settings.main_entity_skeleton or "").strip()
                        import_anims.start_import(context, fdir, rigPath=_repo_file(skel) if skel else None)

            if "load" == action:
                log.debug("load appearance")
                if rig_settings.app_list_index >= 0 and rig_settings.app_list:
                    item = rig_settings.app_list[rig_settings.app_list_index]

                    import_entity.import_from_list_item(context, item)
                    bpy.ops.object.select_all(action='DESELECT')
                    main_arm_obj.select_set(True)
                    bpy.context.view_layer.objects.active = main_arm_obj
                # context.rig_settings.app_list.add()
            elif "clear" == action:
                log.debug("Debug Clear")
                bpy.context.rig_settings.app_list.clear()

        return {'FINISHED'}

class WITCH_OT_ENTITY_lod_toggle(Operator):
    """ Add an Item to the UIList"""
    bl_idname = "witcher.lod_toggle"
    bl_label = "Toggle"
    bl_description = "Change lod level for all meshes."

    action: StringProperty(default="default")
    @classmethod
    def poll(cls, context):
        return context.scene

    def execute(self, context):
        scene = context.scene
        action = self.action
        ob = context.object
        lods = ['_lod0','_lod1','_lod2']
        if '_collision' in action:
            meshes = set(o for o in scene.objects if o.type == 'MESH')
            hidden_bool = True
            if 'Show' in action:
                hidden_bool = False

            _col_suffixes = ("_col", "_tri", "_box", "_sphere", "_capsule")
            for o in meshes:
                base = re.sub(r'\.\d+$', '', o.name)
                is_collision = (
                    "_proxy" in o.name or
                    "_shadowmesh" in o.name or
                    "_volume" in o.name or
                    "blockout_box" in o.name or
                    o.name.startswith("capsule_") or
                    o.name.startswith("box_") or
                    any(base.endswith(s) for s in _col_suffixes)
                )
                if is_collision:
                    log.debug("hiding collision object %s", o.name)
                    o.hide_set(hidden_bool)

        elif action in lods:
            lod_idx = int(action[-1:])
            lod_meshes = []
            for mesh in scene.objects:
                # only for meshes
                if mesh.type == 'MESH':
                    if 'lod_level' in mesh.witcherui_MeshSettings:
                        if mesh.witcherui_MeshSettings['lod_level'] == lod_idx:
                            mesh.hide_set(False)
                        else:
                            mesh.hide_set(True)
            #         if mesh.name[-5:-1] == "_lod":
            #             mesh.hide_viewport = True
            #             mesh.hide_render = True
            #             if mesh.name[:-5] not in lod_meshes:
            #                 lod_meshes.append(mesh.name[:-5])
            # for lod_mesh in lod_meshes:
            #     mesh_bl = scene.objects.get(lod_mesh+action)
            #     if mesh_bl:
            #         mesh_bl.hide_viewport = False
            #         mesh_bl.hide_render = False
            #     else:
            #         mesh_bl = scene.objects.get(lod_mesh+"_lod"+str(lod_idx-1))
            #         if mesh_bl:
            #             mesh_bl.hide_viewport = False
            #             mesh_bl.hide_render = False
            #         else:
            #             mesh_bl = scene.objects.get(lod_mesh+"_lod"+str(lod_idx-1))
            #             if mesh_bl:
            #                 mesh_bl.hide_viewport = False
            #                 mesh_bl.hide_render = False
            #             else:
            #                 log.debug("LOD ERROR")

            # if mesh.name[-5:] == action:
            #     mesh.hide_viewport = False
            #     mesh.hide_render = False
        return {'FINISHED'}

##############################
#       Animset List         #
##############################

class WITCH_PT_ENTITY_ANIMSET_UL_List(UIList):
    """List for the Animsets"""
    bl_idname = "WITCH_PT_ENTITY_ANIMSET_UL_List"
    layout_type = "DEFAULT" # could be "COMPACT" or "GRID"

    def draw_item(self, context,
                    layout, # Layout to draw the item
                    data, # Data from which to take Collection property
                    item, # Item of the collection property
                    icon, # Icon of the item in the collection
                    active_data, # Data from which to take property for the active element
                    active_propname, # Identifier of property in active_data, for the active element
                    index, # Index of the item in the collection - default 0
                    flt_flag # The filter-flag result for this item - default 0
            ):
        #Make sure your code supports all 3 layout types
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text=item.path)

        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="")

class WITCH_PT_ENTITY_Panel(WITCH_PT_Base, Panel):
    bl_idname = "WITCH_PT_ENTITY_Panel"
    bl_label = "Character"
    bl_description = "Browse assets, manage appearances, and configure character settings"
    bl_options = set()  # Open by default — primary character workspace

    def draw_header(self, context):
        self.layout.label(text="", icon='OUTLINER_OB_ARMATURE')

    def draw_header_preset(self, context):
        text = _get_character_panel_header_status(context)
        ui_scale = context.preferences.system.ui_scale
        max_chars = max(8, int((context.region.width - 135 * ui_scale) / (7 * ui_scale)))
        if len(text) > max_chars:
            text = text[:max_chars - 3] + "..."
        self.layout.label(text=text)

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False  # Must be False so prop_enum() shows text ON buttons, not beside them
        layout.use_property_decorate = False
        scene = context.scene

        def section(section_id, label, icon, default_closed=False):
            container = layout.box()
            header, body = container.panel(section_id, default_closed=default_closed)
            header.label(text=label, icon=icon)
            return body

        # --- Persistent status banner ---
        main_arm_obj, rig_settings = get_main_armature_and_rig_settings(
            context, prefer_active=True, remember=True, fallback=True,
        )
        status_box = layout.box()
        status_row = status_box.row(align=True)
        if main_arm_obj and rig_settings:
            status_row.label(text=main_arm_obj.name, icon='ARMATURE_DATA')
            app_count = len(getattr(rig_settings, "app_list", []))
            status_row.label(text=f"{app_count} appearances", icon='OUTLINER_OB_GROUP_INSTANCE')
        else:
            status_row.alert = True
            status_row.label(text="No character selected", icon='INFO')

        target_box = layout.box()
        target_box.label(text="Character Target", icon='ARMATURE_DATA')
        target_col = target_box.column(align=True)
        draw_main_armature_selector(target_col, context, label="Character", fallback=True)
        if not (main_arm_obj and rig_settings):
            target_col.label(text="Import a character from Asset Browser, then set/select its armature", icon='INFO')

        # --- 4-tab local navigator ---
        char_tab = getattr(scene, "witcher_character_tab", "APPEARANCES")
        if char_tab == "BROWSE":
            char_tab = "APPEARANCES"
            try:
                scene.witcher_character_tab = 'APPEARANCES'
            except Exception:
                pass
        nav = layout.grid_flow(columns=2, even_columns=True, align=True)
        nav.scale_y = 1.6
        nav.prop_enum(scene, "witcher_character_tab", 'APPEARANCES')
        nav.prop_enum(scene, "witcher_character_tab", 'EQUIPMENT')
        nav.prop_enum(scene, "witcher_character_tab", 'MORPHS')
        nav.prop_enum(scene, "witcher_character_tab", 'INFO')
        layout.separator(factor=0.3)
        # ===================== APPEARANCES TAB =====================
        if char_tab == "APPEARANCES":
            if not (main_arm_obj and rig_settings):
                layout.label(text="No character armature selected.", icon='INFO')
                layout.label(text="Use the Asset Browser panel to import a character first.")
                return

            object = rig_settings
            col = layout.column(align=True)
            if len(object.app_list) == 0:
                col.label(text="No appearances loaded. Import a character to begin.", icon='INFO')
            list_row = col.row()
            list_row.template_list("WITCH_UL_ENTITY_List", "The_List", object, "app_list", object, "app_list_index")

            action_col = list_row.column(align=True)
            action_col.operator(WITCH_OT_ENTITY_list_loadapp.bl_idname, text="Load", icon='IMPORT').action = "load"
            action_col.operator(WITCH_OT_w3app.bl_idname, text=".w3app", icon='APPEND_BLEND')
            action_col.operator(WITCH_OT_ENTITY_import_inventory.bl_idname, text="Inventory", icon='PACKAGE')

            toggle_row = col.row(align=True)
            toggle_row.prop(context.scene, "witcher_load_app_on_select", text="Load on Select")
            if hasattr(bpy.ops.witcher, "equipment_load_all_appearances"):
                toggle_row.operator("witcher.equipment_load_all_appearances", text="Load All", icon='ASSET_MANAGER')

            if object.app_list_index >= 0 and object.app_list:
                active_app = object.app_list[object.app_list_index]
                col.separator()
                col.label(text=f"Selected: {active_app.name}", icon='CHECKMARK')

                _entity, entity_data = import_entity.get_rig_entity_state(rig_settings)
                if entity_data is None:
                    entity_data = {}
                try:
                    color_entries = ui_equipment._get_coloring_entries_for_appearance(entity_data, str(active_app.name))
                except Exception:
                    color_entries = []

                color_box = col.box()
                color_box.label(text="Coloring Entries", icon='MOD_HUE_SATURATION')
                if not color_entries:
                    color_box.label(text="No coloring entries for this appearance.", icon='INFO')
                else:
                    color_box.label(text=f"{len(color_entries)} entries", icon='CHECKMARK')
                    coloring_object_index = _build_coloring_object_index(context)
                    for entry in color_entries:
                        entry_box = color_box.box()
                        component_name = str(entry.get("componentName", "") or "<unnamed component>")
                        matching_objects = coloring_object_index.get(component_name, [])
                        header_row = entry_box.row(align=True)
                        header_row.label(text=component_name, icon='MESH_DATA')
                        sel_op = header_row.operator(
                            WITCH_OT_coloring_select_component.bl_idname,
                            text="", icon='RESTRICT_SELECT_OFF',
                        )
                        sel_op.component_name = component_name
                        try:
                            shift1 = ui_equipment._format_color_shift_summary(entry.get('colorShift1'))
                        except Exception:
                            shift1 = "-"
                        try:
                            shift2 = ui_equipment._format_color_shift_summary(entry.get('colorShift2'))
                        except Exception:
                            shift2 = "-"
                        entry_box.label(text=f"Shift 1: {shift1}")
                        entry_box.label(text=f"Shift 2: {shift2}")

                        # Show editable colorShift properties for any matching imported objects
                        _show_coloring_object_props(entry_box, matching_objects)

        # ===================== EQUIPMENT TAB =====================
        elif char_tab == "EQUIPMENT":
            if not (main_arm_obj and rig_settings):
                layout.label(text="No character armature selected.", icon='INFO')
                return
            from ..ui.ui_equipment import EQUIPMENT_PT_MainPanel
            EQUIPMENT_PT_MainPanel.draw(self, context)

        # ===================== MORPHS TAB =====================
        elif char_tab == "MORPHS":
            if not (main_arm_obj and rig_settings):
                layout.label(text="No character armature selected.", icon='INFO')
                return
            from ..ui.ui_morphs import WITCH_PT_WitcherMorphs
            WITCH_PT_WitcherMorphs.draw(self, context)

        # ===================== INFO TAB =====================
        elif char_tab == "INFO":
            if not (main_arm_obj and rig_settings):
                layout.label(text="No character armature selected.", icon='INFO')
                return

            object = rig_settings

            settings_box = layout.box()
            settings_box.label(text="Import Settings", icon='SETTINGS')
            col = settings_box.column(align=True)
            col.prop(rig_settings, "do_import_lods")
            addon_prefs = get_all_addon_prefs(context)
            cloth_box = col.box()
            cloth_box.label(text="Redcloth", icon='MOD_CLOTH')
            cloth_box.prop(addon_prefs, "do_import_redcloth")
            cloth_box.prop(addon_prefs, "DO_WEAR_CLOTH")
            cloth_box.prop(addon_prefs, "redcloth_simulation_enabled")
            cloth_box.prop(addon_prefs, "redcloth_wind_velocity")

            info_box = layout.box()
            info_box.label(text="Character Info", icon='INFO')
            info_col = info_box.column(align=True)
            if object.app_list_index >= 0 and object.app_list:
                item = object.app_list[object.app_list_index]
                info_col.prop(item, "name")
            info_col.prop(rig_settings, "main_entity_skeleton")
            info_col.prop(rig_settings, "main_face_skeleton")
            info_col.prop(rig_settings, "repo_path")

classes = [
    #properties
    #ListItemApp,
    #ListItemAnimset,
    InventoryPreviewItem,
    WITCH_UL_InventoryPreview,

    #operators
    WITCH_OT_coloring_select_component,
    WITCH_OT_AnimSetPathInfo,
    WITCH_OT_RevealAnimInExplorer,
    WITCH_OT_w3app,
    WITCH_OT_w2ent,
    WITCH_OT_flyr,
    WITCH_OT_ENTITY_w2ent_chara,
    WITCH_OT_ENTITY_import_inventory,
    WITCH_OT_ENTITY_import_geralt,
    WITCH_OT_ENTITY_import_ciri,
    WITCH_OT_ENTITY_list_loadapp,
    WITCH_OT_ENTITY_lod_toggle,

    #lists
    WITCH_UL_ENTITY_List,

    #panels
    WITCH_PT_ENTITY_Panel,
    WITCH_PT_ENTITY_ANIMSET_UL_List,

    #assets

    # SimpleModalOperator,
    # ImageActionOperator,
    # BundleItemList,
]



#!TODO use the SimpleModalOperator for the list of files and the asset browser for the folders

from . import ui_equipment


def register():
    bpy.types.Scene.witcher_character_tab = EnumProperty(
        name="Character Tab",
        description="Active sub-section of the Character panel",
        items=[
            ('APPEARANCES', "Appearances", "Appearance list and coloring entries"),
            ('EQUIPMENT',   "Equipment",   "Equipment slots and template management"),
            ('MORPHS',      "Morphs",      "Face morphs and phoneme controls"),
            ('INFO',        "Info",        "Import settings and character metadata"),
        ],
        default='APPEARANCES',
    )
    ui_file_browser.register()
    # bpy.utils.register_class(BundleItemPropertyGroup)
    # bpy.types.Scene.bundle_items = CollectionProperty(type=BundleItemPropertyGroup)
    #bpy.types.Scene.bundle_item_index = bpy.props.IntProperty()
    if not hasattr(bpy.types.Scene, "witcher_load_app_on_select"):
        bpy.types.Scene.witcher_load_app_on_select = BoolProperty(
            name="Load on Select",
            description="Automatically load appearance when selecting it in the list",
            default=True
        )
    for c in classes:
        bpy.utils.register_class(c)
    ui_equipment.register()
    
def unregister():
    ui_equipment.unregister()
    ui_file_browser.unregister()
    for prop_name in ("witcher_character_tab",):
        if hasattr(bpy.types.Scene, prop_name):
            delattr(bpy.types.Scene, prop_name)
    
    # del bpy.types.rig_settings.app_list
    # del bpy.types.rig_settings.app_list_index

    # del bpy.types.rig_settings.main_entity_skeleton
    # del bpy.types.rig_settings.main_face_skeleton

    # del bpy.types.rig_settings.animset_list
    # del bpy.types.rig_settings.animset_list_index
    
    # bpy.utils.unregister_class(BundleItemPropertyGroup)
    # del bpy.types.Scene.bundle_items
    # del bpy.types.Scene.bundle_item_index
    if hasattr(bpy.types.Scene, "witcher_load_app_on_select"):
        del bpy.types.Scene.witcher_load_app_on_select
    for c in reversed(classes):
        bpy.utils.unregister_class(c)

if __name__ == '__main__':
    register()

