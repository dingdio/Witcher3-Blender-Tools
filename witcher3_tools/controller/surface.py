"""Resolve footstep surfaces from geometry, collision data or terrain maps."""
import logging

log = logging.getLogger(__name__)
# Game physical material names, excluding TEST.
PHYSICAL_MATERIALS = (
    "carpet", "dirt_hard", "dirt_soil", "grass_long", "grass_short", "gravel_large", "gravel_small", "ice_debris",
    "ice_solid", "ice_thin", "leaves", "metal", "mud", "sand", "snow_deep", "snow_firm", "stone_debris", "stone_solid",
    "swamp", "water_deep", "water_puddle", "water_shallow", "wood_hollow", "wood_debris", "wood_solid", "rubber", "glass",
    "iron", "flesh", "custom_sword", "clay_tile", "sand_wet", "mud_dry", "hay", "metal_spoons", "water_deep_river",
    "dettlaff_flesh", "gold_coins",
)
OVERRIDES = {"water_deep_river": "water_deep", "dettlaff_flesh": "mud"}      # game remaps
_KEYWORDS = (("water", "water_shallow"), ("puddle", "water_puddle"), ("snow", "snow_firm"), ("ice", "ice_solid"),
             ("swamp", "swamp"), ("mud", "mud"), ("sand", "sand"), ("beach", "sand"), ("gravel", "gravel_small"),
             ("grass", "grass_short"), ("moss", "grass_short"), ("leaves", "leaves"), ("wood", "wood_solid"),
             ("plank", "wood_solid"), ("floor_board", "wood_solid"), ("carpet", "carpet"), ("rug", "carpet"),
             ("metal", "metal"), ("iron", "metal"), ("stone", "stone_solid"), ("rock", "stone_solid"),
             ("cobble", "stone_solid"), ("brick", "stone_solid"), ("marble", "stone_solid"), ("soil", "dirt_soil"),
             ("dirt", "dirt_hard"), ("ground", "dirt_hard"), ("hay", "hay"), ("straw", "hay"))
TERRAIN_TABLE = r"gameplay\globals\terrain_textures_physical_materials.csv"


def from_name(text: str) -> str:
    text = (text or "").lower()
    for name in sorted(PHYSICAL_MATERIALS, key=len, reverse=True):
        if name in text:
            return name
    return next((name for keyword, name in _KEYWORDS if keyword in text), "")


def parse_terrain_table(text: str) -> dict:
    """Map terrain texture paths to physical materials."""
    out = {}
    for row in text.splitlines()[1:]:
        cells = row.split(";")
        if len(cells) >= 2 and cells[0].strip():
            out[cells[0].strip().replace("/", "\\").lower()] = cells[1].strip()
    return out


def _load_terrain_table() -> dict:
    from ..repo_paths import repo_file_for_source
    try:
        with open(repo_file_for_source(TERRAIN_TABLE, "w3"), encoding="utf-8-sig") as f:
            return parse_terrain_table(f.read())
    except OSError as exc:
        log.info("player: no terrain physical material table (%s)", exc)
        return {}


def _first_material(names) -> str:
    for name in ([names] if isinstance(names, str) else names or ()):
        if name and name not in ("nxs_material", "default", "none", "None"):
            return str(name)
    return ""


class Probe:
    """Resolve and cache the surface under the player."""

    def __init__(self, world, default: str = ""):
        self.world, self.default = world, default
        self.last = ""
        self._objects, self._tiles = {}, {}
        self._terrain_table = None
        self._collision = None

    def at(self, x: float, y: float, z: float) -> str:
        hit = self.world.ground_hit(x, y, z) if self.world is not None else None
        name = self.resolve(*hit) if hit is not None else ""
        self.last = OVERRIDES.get(name, name) or self.default or "default"
        return self.last

    def resolve(self, obj, material_index: int, uv) -> str:
        if uv is not None and obj.get("witcher_terrain_tile"):
            found = self._terrain(obj, uv)
            if found:
                return found
        key = (obj.name, material_index)
        if key not in self._objects:
            self._objects[key] = self._object(obj, material_index)
        return self._objects[key]

    def _object(self, obj, material_index: int) -> str:
        explicit = obj.get("witcher_physical_material")
        if explicit:
            return str(explicit)
        settings = getattr(obj, "witcherui_MeshSettings", None)
        repo = str(getattr(settings, "item_repo_path", "") or "") if settings is not None else ""
        found = self._mesh_collision(repo) if repo else ""
        if found:
            return found
        slots = obj.material_slots
        material = slots[material_index].material if 0 <= material_index < len(slots) else None
        return from_name(material.name if material is not None else "") or from_name(obj.name)

    def _mesh_collision(self, repo: str) -> str:
        try:
            if self._collision is None:
                from ..CR2W.witcher_cache.CollisionCache.CollisionManager import CollisionManager
                self._collision = {k.replace("/", "\\").lower(): v for k, v in CollisionManager.Get().Items.items()}
            items = self._collision.get(repo.replace("/", "\\").lower())
            item = items[0] if isinstance(items, list) else items
            return next((m for m in (_first_material(s[3]) for s in item.get_shapes_with_data()) if m), "") if item else ""
        except Exception:
            log.exception("player: collision material lookup failed for %s", repo)
            self._collision = self._collision or {}
            return ""

    def _terrain(self, obj, uv) -> str:
        from ..importers import import_w2w, terrain_detail
        if obj.name not in self._tiles:
            try:
                meta = import_w2w.ensure_terrain_inspector_metadata(obj)
                lattice = terrain_detail.load_tile_control_lattice(
                    meta["texture_buffer"], meta["res"],
                    positive_x_texture_buffer=meta["positive_x_texture_buffer"],
                    positive_y_texture_buffer=meta["positive_y_texture_buffer"],
                    positive_xy_texture_buffer=meta["positive_xy_texture_buffer"])
                self._tiles[obj.name] = (lattice, meta["layers"]) if lattice is not None else None
            except Exception as exc:
                log.info("player: terrain tile %s has no control map (%s)", obj.name, exc)
                self._tiles[obj.name] = None
        tile = self._tiles[obj.name]
        if tile is None:
            return ""
        if self._terrain_table is None:
            self._terrain_table = _load_terrain_table()
        layers = terrain_detail.inspect_terrain_control_lattice(tile[0], float(uv[0]), float(uv[1]), tile[1])
        for entry in layers.get("horizontal_layers", ()):
            source = str(entry["layer"].get("diffuse_source", "") or "").replace("/", "\\").lower()
            if source in self._terrain_table:
                return self._terrain_table[source]
        return ""
