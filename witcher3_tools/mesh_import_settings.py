from dataclasses import dataclass, fields


_PREF_FIELD_MAP = {
    "do_import_mats": "mesh_import_do_import_mats",
    "do_import_armature": "mesh_import_do_import_armature",
    "keep_lod_meshes": "mesh_import_keep_lod_meshes",
    "keep_empty_lods": "mesh_import_keep_empty_lods",
    "rotate_180": "mesh_import_rotate_180",
    "hide_zero_weight_faces": "mesh_import_hide_zero_weight_faces",
}


@dataclass(slots=True)
class MeshImportSettings:
    do_import_mats: bool = True
    do_import_armature: bool = True
    keep_lod_meshes: bool = False
    keep_empty_lods: bool = False
    rotate_180: bool = False
    hide_zero_weight_faces: bool = True

    @classmethod
    def from_source(cls, source=None):
        settings = cls()
        if source is None:
            return settings

        if isinstance(source, dict):
            getter = lambda name, default=None: source.get(name, default)
        else:
            getter = lambda name, default=None: getattr(source, name, default)

        for field in fields(cls):
            value = getter(field.name, None)
            if value is not None:
                setattr(settings, field.name, bool(value))
        return settings

    @classmethod
    def from_addon_prefs(cls, addon_prefs=None):
        settings = cls()
        if addon_prefs is None:
            return settings

        for field_name, pref_name in _PREF_FIELD_MAP.items():
            value = getattr(addon_prefs, pref_name, None)
            if value is not None:
                setattr(settings, field_name, bool(value))
        return settings

    def apply_to(self, target):
        for field in fields(type(self)):
            if hasattr(target, field.name):
                setattr(target, field.name, bool(getattr(self, field.name)))
        return target

    def save_to_addon_prefs(self, addon_prefs):
        if addon_prefs is None:
            return

        for field_name, pref_name in _PREF_FIELD_MAP.items():
            if hasattr(addon_prefs, pref_name):
                setattr(addon_prefs, pref_name, bool(getattr(self, field_name)))

    def to_import_mesh_kwargs(self) -> dict:
        return {
            "do_import_mats": self.do_import_mats,
            "do_import_armature": self.do_import_armature,
            "keep_lod_meshes": self.keep_lod_meshes,
            "keep_empty_lods": self.keep_empty_lods,
            "rotate_180": self.rotate_180,
            "hide_zero_weight_faces": self.hide_zero_weight_faces,
        }
