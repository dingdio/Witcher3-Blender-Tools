# Master materials

Schema v2 no longer uses a bundled `M_W3_PBR_Std` asset.

Master materials are created in the *project* at the depot path of each
Witcher `.w2mg` shader graph, e.g.
`/Game/Witcher3/engine/materials/graphs/pbr_std`. If an asset already
exists at that path (hand-authored or from a previous import) it is reused
untouched; otherwise the importer generates one with the graph's declared
parameters and a basic BaseColor/Normal/Roughness wiring.

Some known Witcher graphs have bespoke generated fallbacks. For example,
`engine/materials/graphs/eyeshadow/pbr_eye_shadow.w2mg` builds the same
`Base Color`, `Color`, and `Gamma` network used by the Blender material:
`Base Color` drives Unreal Base Color, while `Color` and `Gamma` derive
translucent opacity.

To customize a master: let one import generate it (or create it yourself at
the mirrored path), edit it in place, and keep the Witcher parameter names:
material instances set parameters by their Witcher names (`Diffuse`,
`Normal`, `SpecularColor`, ...).
