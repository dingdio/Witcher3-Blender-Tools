"""Hard-coded Witcher texture group settings for XBM export.

Mirrors the game's ``engine/textures/texturegroups.xml`` so Blender UI can
offer stable texture-group choices without depending on an external XML file.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextureGroupInfo:
    name: str
    compression: str
    has_mips: bool = True
    category: str = ""

    @property
    def dxgi_format(self) -> str | None:
        return compression_to_dxgi_format(self.compression)

    @property
    def uses_texconv(self) -> bool:
        return self.dxgi_format is not None


def compression_to_dxgi_format(compression: str) -> str | None:
    return _COMPRESSION_TO_DXGI.get(str(compression or "").strip(), None)


def get_texture_group_info(group_name: str) -> TextureGroupInfo:
    return TEXTURE_GROUPS.get(group_name, TEXTURE_GROUPS["Default"])


def get_texture_group_enum_items():
    return list(TEXTURE_GROUP_ENUM_ITEMS)


def _describe_group(info: TextureGroupInfo) -> str:
    mip_label = "with mips" if info.has_mips else "no mips"
    category = f"{info.category}: " if info.category else ""
    return f"{category}{info.compression}, {mip_label}"


_COMPRESSION_TO_DXGI = {
    "TCM_DXTNoAlpha": "BC1_UNORM",
    "TCM_Normals": "BC1_UNORM",
    "TCM_DXTAlpha": "BC3_UNORM",
    "TCM_NormalsHigh": "BC3_UNORM",
    "TCM_NormalsGloss": "BC3_UNORM",
    "TCM_QualityR": "BC4_UNORM",
    "TCM_QualityRG": "BC5_UNORM",
    "TCM_QualityColor": "BC7_UNORM",
}


_TEXTURE_GROUP_SPECS = (
    ("Default", "TCM_None", True, ""),
    ("SystemNoMips", "TCM_None", False, ""),
    ("MimicDecalsNormal", "TCM_DXTAlpha", True, ""),
    ("SpecialQuestDiffuse", "TCM_DXTNoAlpha", True, ""),
    ("TerrainNormalAtlas", "TCM_NormalsHigh", True, ""),
    ("TerrainDiffuse", "TCM_DXTNoAlpha", True, ""),
    ("Font", "TCM_None", False, ""),
    ("GUIWithoutAlpha", "TCM_DXTNoAlpha", False, ""),
    ("FoliageDiffuse", "TCM_DXTAlpha", True, ""),
    ("Flares", "TCM_None", True, ""),
    ("DetailNormalMap", "TCM_NormalsHigh", True, ""),
    ("WorldEmissive", "TCM_DXTNoAlpha", True, ""),
    ("WorldDiffuseWithAlpha", "TCM_DXTAlpha", True, ""),
    ("WorldNormalHQ", "TCM_NormalsHigh", True, ""),
    ("WorldDiffuse", "TCM_DXTNoAlpha", True, ""),
    ("Particles", "TCM_DXTAlpha", True, ""),
    ("TerrainSpecial", "TCM_None", False, ""),
    ("WorldSpecular", "TCM_DXTNoAlpha", True, ""),
    ("ParticlesWithoutAlpha", "TCM_DXTNoAlpha", True, ""),
    ("BillboardAtlas", "TCM_DXTAlpha", True, ""),
    ("TerrainDiffuseAtlas", "TCM_DXTAlpha", True, ""),
    ("GUIWithAlpha", "TCM_DXTAlpha", False, ""),
    ("WorldNormal", "TCM_Normals", True, ""),
    ("TerrainNormal", "TCM_Normals", True, ""),
    ("PostFxMap", "TCM_None", False, ""),
    ("SpecialQuestNormal", "TCM_NormalsHigh", True, ""),
    ("NormalmapGloss", "TCM_NormalsGloss", True, ""),
    ("CharacterDiffuse", "TCM_DXTNoAlpha", True, "Characters"),
    ("CharacterDiffuseWithAlpha", "TCM_DXTAlpha", True, "Characters"),
    ("CharacterEmissive", "TCM_DXTNoAlpha", True, "Characters"),
    ("CharacterNormal", "TCM_Normals", True, "Characters"),
    ("CharacterNormalHQ", "TCM_NormalsHigh", True, "Characters"),
    ("CharacterNormalmapGloss", "TCM_NormalsGloss", True, "Characters"),
    ("HeadDiffuse", "TCM_DXTNoAlpha", True, "Heads"),
    ("HeadDiffuseWithAlpha", "TCM_DXTAlpha", True, "Heads"),
    ("HeadEmissive", "TCM_DXTNoAlpha", True, "Heads"),
    ("HeadNormal", "TCM_Normals", True, "Heads"),
    ("HeadNormalHQ", "TCM_NormalsHigh", True, "Heads"),
    ("DiffuseNoMips", "TCM_DXTNoAlpha", False, ""),
    ("NormalsNoMips", "TCM_NormalsGloss", False, ""),
    ("QualityOneChannel", "TCM_QualityR", True, ""),
    ("QualityTwoChannels", "TCM_QualityRG", True, ""),
    ("QualityColor", "TCM_QualityColor", True, ""),
)


TEXTURE_GROUPS = {
    name: TextureGroupInfo(name=name, compression=compression, has_mips=has_mips, category=category)
    for name, compression, has_mips, category in _TEXTURE_GROUP_SPECS
}

TEXTURE_GROUP_ENUM_ITEMS = tuple(
    (info.name, info.name, _describe_group(info))
    for info in TEXTURE_GROUPS.values()
)
