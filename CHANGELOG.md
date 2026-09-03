# Changelog

Notable changes to **Witcher 3 Blender Tools** are documented here.

## [Unreleased]

- Unreal bridge: `SKM_Manny_Simple` is the retarget source mannequin for `man_base` (Quinn for `woman_base`, the other as fallback)
- Unreal export: textures converted at import (large detail maps) now stage from their uncook source instead of failing to resolve
- Cutscene tabs for Actors, Clips, Camera, Events, Dialogue, and Export
- Transactional bake, validation, and export workflows
- Editable dialogue timing, viewport subtitles, and three voice tiers
- Dialogue markers in `.w2cutscene` with matching companion `.w2scene` lines
- Game-voice search, preview, aliases, and REDkit or Radish string IDs
- Cutscene and Speech linking with two-way text sync and reference-safe export
- Unreal bridge: plugin README now states the UE 5.8 requirement; wiki tutorial 'Geralt to Unreal and Back'
- Custom WAV hand-off and optional external TTS commands
- Improved actor casting, entity management, prop export, and Witcher 2 retargeting
- W2/W3 clip browsing with stable IDs, mute state, re-imports, and isolated promoted clips
- Camera shots, cut conversion, automatic rig rebakes, and compact shot controls
- Multipart actor export from shot boundaries with shared boundary keys
- Copyable validation reports with go-to controls
- Clear track naming, bake status, and non-cutscene track warnings
- Frame-zero cutscene setup with configurable length and FPS
- Fixed face-animation detection for `lipsync` and `_mimic_` parent folders
- Preserved actor Root placement and yaw across cutscene round-trips
- Full-length NLA strips for static cutscene parts and separate face rigs
- Shot change tracking for parents, constraints, and timeline scrubbing
- Safer failed Speech hand-offs and atomic standalone strings CSV replacement

## [1.1.0] - 2026-06-23

1.1.0 expands Witcher 2 support, scene and cutscene authoring tools, lipsync
and facial-animation tools, browsing and equipment workflows, CR2W loading, and
the new Unreal Engine bridge. This release touches core import, export, cache,
and UI paths.

### Added

#### Unreal Engine bridge
- New bridge for sending meshes, materials, entities, characters, animations, and world data from Blender to Unreal Engine.
- Included an Unreal plugin with import commandlets, runtime support, material/texture import, mesh buffers, placements, foliage, terrain, SpeedTree import, and retarget setup.
- Added Unreal project setup UI, socket communication, resend support, import manifests, and staged bundle export.
- Added world and layer tools for `.w2l` placements, asset-browser send actions, "send layers around camera", placement visibility, and Blender-side `.w2l` loading.
- Added character and animation tools for modular-character blueprints, rig skeletons, W2/W3 roots, target preview meshes, and retarget previews.
- Added terrain and foliage export for full-map terrain bakes, blend materials, holes, terrain objects, tint textures, collision/lights, SpeedTrees, and `.flyr` foliage.

#### More Witcher 2 support
- Expanded Witcher 2 path handling and bundle (`.dzip`) loading.
- Expanded Witcher 2 entity and layer imports with cooked-layer packed meshes, embedded `CMesh` components, proxy entities, and clearer import failures.
- Improved Witcher 2 material and texture resolution, XBM/DDS previews, and material compatibility.
- Expanded Witcher 2 animation browsing/import, animation retargeting, retarget basis/direction handling, and weapon-bone rebake support.
- Expanded Witcher 2 mimic and facial-animation support with mimic animations, face-morph track names.
- Added Witcher 2 `.w2cutscene` parsing/import.
- Expanded Witcher 2 subtitles, voicelines, scene dialogue metadata, W2 speech support, `.dat` speech import, and W2 strings browsing.
- Expanded Witcher 2 ragdoll hierarchy, scabbard, bound-item equipment, and game-aware equipment import support.

#### Cutscene & scene authoring
- Added cutscene authoring tools for actors, animations, actor events, camera rigs, shot markers, FOV/DOF tracks, baking, and cut editing.
- Added scene and cutscene event display/editing, including class-field displays for imported CR2W data and UI for adding cutscene events per actor.
- Added Witcher 2 RTTI-backed cutscene events and cutscene item events.
- Added `.w2scene` section tools, choices, next-section buttons, subtitle-per-section handling, and utilities for switching scene sections.
- Added prop and animated-prop loading for scenes.
- Added look-at events and improved look-at handling.
- Added motion accumulation for `.w2scene` import and preview auto-motion for individual animation imports.
- Added in-viewport subtitle display and a reworked dialogue tab.
- Added multi-language scene/cutscene handling and fallback string lookup from REDkit databases.
- Added cutscene retargeting to Witcher 3, actor replacement, multipart retarget/import support, and W2 mimic-neck suppression for cutscene animation.

#### Lipsync & facial animation
- Added Radish lipsync generation tools.
- Added Wwise WEM generation, WAV transcription, external-tool setup helpers, phoneme file handling, and REDkit project helpers.
- Added Live Link Face import and preview.
- Added ARKit/FACS import paths for W2 and W3.
- Improved morph, mimic, and facial-animation UI.

#### Rigging, animation & controls
- Added IK rig tools, including bake and snap FK/IK operations.
- Improved the IK control rig and added a PoseKey control-rig panel.
- Moved retargeting controls into a dedicated panel.
- Added animation-set export UI.
- Added DLC animset support.
- Added targeting `component_name` support for clips such as scabbards.

#### Equipment & inventory
- Added equipment and inventory presets for Geralt imports.
- Added equipment initializer reading, switching, and default item selection.
- Added an item picker that uses the asset browser icon system.
- Added actual inventory display with per-item visibility toggles.
- Added equipment catalog data and shipped inventory presets.
- Added persistent equipment thumbnail caching and background thumbnail cache warm-up.

#### Asset browsing, previews & UI
- Added a new asset-browser layout and grid-view search results.
- Added browser thumbnail helpers shared by equipment and other browser-like UI.
- Added generated placeholder and error icons for browser/equipment previews.
- Added direct browser actions for Unreal send workflows.
- Added sound and voice previews used by string and dialogue browsing.
- Added shared filtered-list and string-browser UI helpers.

#### Strings, dialogue & data caches
- Added scene/dialogue cache databases for W2 and W3, including voicetags, associated scenes, associated entities, and speaker metadata.
- Added dialogue language helpers and browser UI.
- Added W3 and W2 strings browser panels with speaker search, filters, associated-file actions, copy actions, and voice preview.
- Added data generation helpers for scene dialogue indexes, Witcher 2 actor animations, and W3 voice-tag entities.
- Added a cached DLC manager.
- Added DLC mounter reading for extra appearances.

#### Maps, worlds & terrain
- Added "load layers around camera" for world scanning.
- Added `.flyr` foliage loading around the camera with instanced source meshes.
- Added repeated mesh instancing and visibility controls for W2L/W2W layer imports.
- Added collision cache pose data reading.
- Added `.redapex` import.
- Added terrain preview image generation and expanded W2 terrain import support.
- Added REDkit depot/uncooked path resolution helpers used by scene, entity, mesh, and world imports.

#### Tests & documentation
- Added unit tests for stream reading, material caching, repo path resolution, retargeting, terrain tintmaps, mod bundle discovery, REDkit world layer groups, Unreal manifests, Unreal placements, Unreal terrain holes, Unreal foliage, and W2 material compatibility.
- Added Radish lipsync documentation and updated project documentation.

### Changed
- Reworked CR2W reading with in-memory streams, buffer peek fast paths, faster `.w2l` reads, and scene-loading speed improvements.
- Added map-import logging and profiling.
- Reworked material UI and added a material-chain workflow.
- Updated `witcher3_materials.blend`.
- Replaced the mesh export vertex-count limit with material-based auto-chunking.
- Reworked DDS handling and texture conversion.
- Added Python 3.13 wheels and removed the old bundled bitstruct wheel.
- Updated wheels for requests, urllib3, certifi, charset-normalizer, idna, tqdm, colorama, cramjam, numpy, h5py, platformdirs, and pywhispercpp.
- Reworked `repo_file` path handling into a `repo_paths` module with source-game awareness and REDkit dual-depot resolution.
- Improved mod and DLC bundle cache loading.
- Improved material import performance, constraint performance, and CR2W read performance.
- Reduced console spam in scene and cutscene workflows.
- Updated developer and cache panels used to rebuild caches and inspect generated data.

### Fixed
- Fixed Windows long-path failures during bulk export.
- Fixed Blender 4.5 audio playback.
- Fixed tangent/binormal basis consistency, normal regressions, and bad chunk sorting.
- Fixed some issues with vertex-color export.
- Fixed `.w2mesh` material order and mesh export metadata.
- Fixed Witcher 2 mesh duplicate vertex-group imports.
- Fixed Witcher 2 embedded `CMesh` entity imports.
- Fixed Witcher 2 subtitle and voiceline imports.
- Fixed Witcher 2 proxy entity handling and made scene imports more game-aware.
- Fixed Witcher 2 material compatibility and stopped applying material validation to Blender-only parameters.
- Fixed texarray loading.
- Fixed NXS collision transforms and NXS extension stripping.
- Fixed collision import issues.
- Fixed cutscene multipart import/export, face tracks, mimic pose weight, timeline handling, idle animation blending, quaternion normalization, root/orientation handling, camera bugs, and DOF value conversion.
- Fixed lookup of mimics.
- Fixed animation regressions from W2 skeleton changes.
- Fixed REDkit terrain and cached layer component imports.
- Fixed Unreal terrain/layer export issues, DX10 DDS previews, socket response handling, imported normals, and Unreal map/character import issues.
- Fixed terrain tintmap detection for raw RGBA versus BC1 colormap encoding.

[Unreleased]: https://github.com/dingdio/Witcher3_Blender_Tools/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/dingdio/Witcher3_Blender_Tools/compare/v1.0.1...v1.1.0
