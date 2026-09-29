# Changelog

Notable changes to **Witcher 3 Blender Tools** are documented here.

## [Unreleased]

## [1.1.0] - 2026-09-29

Everything since 1.0.1 (24 April): 177 commits. Ready for The Witcher 3 Remastered.

### Highlights
- Support for The Witcher 3 Remastered
- Unreal Engine bridge
- Cutscene authoring revamp
- Much wider Witcher 2 support
- Map import revamp, with sky, weather and water
- Physics and particle effects
- New animation tools: Auto IK rig, retargeting, Radish lipsync and Live Link Face
- Equipment and inventory presets for Geralt
- Blender 5.2 support

### Added

#### Unreal Engine bridge
- Send meshes, characters, animations and worlds to Unreal (UE 5.8 plugin included)
- Project setup panel with plugin install and update
- Layers, terrain, SpeedTree and `.flyr` foliage
- Character blueprints and retarget setup
- Send retargeted animations back to Blender

#### Cutscenes & dialogue
- New Cutscene panel: Actors, Clips, Camera, Events, Dialogue and Export
- Build cutscenes from scratch: cast actors, add clips, props and events
- Camera shots with FOV and depth-of-field tracks
- Guided bake, validate and export to `.w2cutscene`
- Dialogue lines with viewport subtitles, game voices or custom WAV
- Witcher 2 cutscenes retargeted to Witcher 3 on import

#### Scenes (`.w2scene`)
- Sections, choices and per-section subtitles
- Props, look-at events and motion accumulation

#### Witcher 2
- `.dzip` bundles, entities, layers, terrain, materials and textures
- Animations, mimics and retargeting to Witcher 3
- `.w2cutscene` import
- Subtitles, voice lines and strings browser
- Ragdoll hierarchy and bound equipment

#### Animation tools
- IK rig with FK/IK snap and bake
- PoseKey and retargeting panels
- Radish lipsync generation with WAV transcription and WEM output
- Live Link Face and ARKit/FACS import
- Reworked morph and mimic panels
- Animation-set export and DLC animation sets
- Characters import as one unified armature
- Smoother playback of compressed animations

#### Physics
- New Physics panel
- In-scene wind force
- User presets

#### Characters & equipment
- Equipment and inventory presets for Geralt
- Scarlet Crest armor preset (Remastered)
- Item picker with thumbnails
- Inventory list with per-item visibility
- Extra appearances from DLC mounters
- Entity Builder panel with native `.w2ent` export

#### Asset browser & UI
- New layout with grid view and ranked search
- Locations browser with preview images
- Sound and voice preview
- W3 and W2 strings and dialogue browsers

#### Worlds, terrain & environment
- Load layers and `.flyr` foliage around the camera
- Terrain View LOD with materials from the game's texture arrays
- Mesh instancing, collision, lights and `.redapex`
- Environment panel: sky, time of day and weather
- World water with foam, ripples and wind

#### Materials & mesh export
- Material chain workflow and reworked material panel
- More exact shader groups; better eye and skin shading
- Mesh export splits by material instead of a vertex limit
- Tangent space and handedness options

### Changed
- The Witcher 3 Remastered: v5 bundles, v7 texture cache, v11 collision cache
- Blender 5.2 support; 4.5 LTS stays the minimum
- Caches rebuild when the game files change
- Faster CR2W, material, entity, layer and cutscene loading
- One unified entity import path
- Works in background (headless) Blender
- Python 3.13 wheels and updated bundled libraries
- 664 unit tests and 39 Blender tests

### Fixed
- Import Entity doing nothing (#13)
- Equipment thumbnails, catalog and item mounting
- Entity import losing appearance equipment
- Inventory preset picker not applying after using the Geralt quick-import preset
- Fire and water effects missing on directly imported props
- Long Windows paths in import and export
- First layer import crashing on a fresh install
- Mesh export tangents, normals, vertex colours and material order
- Cutscene multipart export, root placement and camera values
- Face-animation detection and mimic lookup
- Blender 4.5 audio playback
- Texture arrays, collision import and terrain tint maps
- REDkit rigs, uncooked entities and embedded redcloth
- Witcher 2 meshes, subtitles, materials and cutscene retargeting
- Unreal terrain, layer and texture export

### Known issues
- Cutscenes: an actor snaps to T-pose wherever no clip is playing; leave no gaps between clips

[Unreleased]: https://github.com/dingdio/Witcher3_Blender_Tools/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/dingdio/Witcher3_Blender_Tools/compare/v1.0.1...v1.1.0
