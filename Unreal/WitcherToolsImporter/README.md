# Witcher Tools Importer

UE 5.4+ editor plugin for importing bundles produced by Witcher 3 Blender Tools.

Install from Blender through **Unreal Export > Project Plugin**:

- Project: select the target `.uproject`
- Plugin Source: leave blank to use this repository's `Unreal/WitcherToolsImporter`
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

Imported assets mirror the REDkit depot layout under the manifest's content
root (default `/Game/ImportedFbx`):

- `characters\models\geralt\body\model\t_01_mg__body_hires.w2mesh`
  -> `/Game/ImportedFbx/characters/models/geralt/body/model/t_01_mg__body_hires`
- Textures import at their `.xbm` depot paths and are shared between materials.
- Each `.w2mi` in a material's baseMaterial chain becomes a Material Instance
  at its depot path; the terminal `.w2mg` becomes a master material at its
  depot path (e.g. `/Game/ImportedFbx/engine/materials/graphs/pbr_std`).
- Local (in-mesh) materials become instances next to their mesh.
- A `.w2rig` exports as a skeletal mesh at its depot path; its `_Skeleton`
  asset is shared by every skeletal mesh in the bundle.
- Character bundles with multiple skeletal parts also get an actor blueprint
  with one SkeletalMeshComponent per part.

Existing assets win: if a master material, texture, chain instance, or
blueprint already exists at the mirrored path it is reused untouched, so
hand-authored masters (pbr_std etc.) keep working across re-imports. Local
mesh material instances are the exception: their parameter values refresh on
every export. Delete an asset in Unreal to force regeneration.

Masters that do not exist yet are generated with the graph's parameters
(texture/scalar/vector) and a basic Diffuse->BaseColor, Normal->Normal,
Rough->Roughness wiring; refine them in place and later imports will pick the
refined version up automatically.
