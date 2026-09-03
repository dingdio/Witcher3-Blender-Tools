# Witcher Tools Importer

UE 5.8 editor plugin for importing bundles produced by Witcher 3 Blender Tools.

Install from Blender through **Unreal Export > Project Plugin**:

- Project: select the target `.uproject`
- Plugin Source: leave blank to use the importer plugin bundled with the add-on
- Install/Update: copies the plugin to `<Project>/Plugins/WitcherToolsImporter`

If Unreal reports that modules are missing or cannot be built, regenerate project
files if needed and build the editor target from Visual Studio or Rider. The
Blender installer excludes generated UE folders such as `Binaries` and
`Intermediate`, so the target project always rebuilds from source.

The plugin listens on `127.0.0.1:40777` for a 4-byte little-endian length prefix
followed by UTF-8 JSON:

```json
{
  "command": "import_bundle",
  "schema": "witcher_unreal_export.v2",
  "manifest_path": "F:/path/to/bundle/witcher_unreal_export.json"
}
```

## Depot mirroring (schema v2)

Imported assets mirror the Witcher depot layout under the manifest's content
root (default `/Game/Witcher3`, or `/Game/Witcher2` for W2 exports):

- `characters\models\geralt\body\model\t_01_mg__body_hires.w2mesh`
  -> `/Game/Witcher3/characters/models/geralt/body/model/t_01_mg__body_hires`
- Textures import at their `.xbm` depot paths and are shared between materials.
- Each `.w2mi` in a material's baseMaterial chain becomes a Material Instance
  at its depot path; the terminal `.w2mg` becomes a master material at its
  depot path (e.g. `/Game/Witcher3/engine/materials/graphs/pbr_std`).
- Local (in-mesh) materials become instances next to their mesh.
- A `.w2rig` exports as a skeletal mesh at its depot path; its `_Skeleton`
  asset is shared by every skeletal mesh in the bundle.
- Enabled Blender animation Export Set entries import as Animation Sequences
  below their source `.w2anims` depot path, one asset per clip name. With an
  empty Export Set, the action currently applied to the exported armature is
  sent instead, so "Send to Unreal" ships whatever is playing in the viewport.
- Character bundles can also get an actor blueprint; when a `.w2rig` is
  exported, that rig mesh becomes the base SkeletalMeshComponent driver and
  the part meshes follow it via leader pose (the Unreal equivalent of RED's
  CAnimatedComponent + CMeshSkinningAttachment setup). The base component is
  set to play the first exported animation as a looping single-node clip.

Existing assets win: if a master material, texture, chain instance, or
blueprint already exists at the mirrored path it is reused untouched, so
hand-authored masters (pbr_std etc.) keep working across re-imports. Two
exceptions refresh on every export so Blender iteration flows through: local
mesh material instances update their parameter values, and an existing
blueprint updates its preview animation plus required skeletal component
settings. Delete an asset in Unreal to force full blueprint regeneration.

Masters that do not exist yet are generated with the graph's parameters
(texture/scalar/vector) and a basic Diffuse->BaseColor, Normal->Normal,
Rough->Roughness wiring; refine them in place and later imports will pick the
refined version up automatically.

## FBX scale and importer notes

- All bundle imports force the legacy FBX importer (the Interchange pipeline,
  default since UE 5.7, ignores parts of the legacy options and handles the
  Blender armature root differently).
- Blender FBXs carry the m->cm conversion on the armature root null. The
  legacy importer folds that into the root bone for skeletal meshes (ref pose
  root scale = 100) but divides it out of animation root tracks; the plugin
  compensates with ImportUniformScale=100 on animation imports so animations
  match the skeleton instead of collapsing 100x.
- Placed actors only play their preview animation in PIE/Simulate; the level
  editor viewport shows a static pose.
