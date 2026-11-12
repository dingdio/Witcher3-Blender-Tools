# Changelog

Notable changes to **Witcher 3 Blender Tools** are documented here.

## [Unreleased]

## [1.2.0] - 2026-11-12

### Added

#### Animation events
- Events tab: create, move and edit clip events with typed fields
- Event tracks in the viewport, with reordering, blank tracks and locks
- Auto Create Tracks by event name or class, on import or afterwards
- Recover REDkit track names for cooked game clips
- Filter events by text, class and track
- Game sound preview on the timeline
- Tracks, Sounds and Effects toggles, remembered between sessions
- Events export with the clip and can replace those in an existing set

#### Clip authoring and export
- New Clip, with a frame range and FPS
- Export Name per clip, for names longer than Blender allows
- Export summary per clip: events, sound cues and motion extraction
- Bone scale animation import and export
- Inject or replace clips in cooked or uncooked `.w2anims` sets
- Find a clip in the REDkit depot and copy its set to a project workspace, with backups
- Motion Extraction tools: generate the Trajectory path and preview the exported motion
- Humanoid (Mixamo) retargeting with auto bone mapping and live preview
- Load sets from the REDkit depot, listed in REDkit's clip order

#### Weapon effects
- Sword light trails baked from a clip's item-effect events
- Blood hit previews at `BloodTrail` markers
- Baking pauses playback and puts a sword in the hand when none is held

#### Player
- Player panel: walk, run and sprint as Geralt, Ciri, NPCs and ground monsters
- Keyboard and mouse or gamepad, third-person and free camera, collision and dangle physics
- Draw steel and silver swords, chain light attacks, dodge and roll
- Game sounds and surface-aware footsteps
- Sword trails during attacks, with optional blood hits
- R equips or puts away the steel sword, a default one if none is chosen
- REDkit reference terrain when the scene has no ground

#### Rigs
- Native `.w2rig` export, no WolvenKit needed
- Control rig, teleport and LOD data carried over from the source rig
- Rig tools resolve accessory selections to the body; Select Body Only

### Changed
- Sparse animation keys import with linear interpolation; Bezier smoothing is an option
- Legacy `.w2rig.json` export kept as an option

### Fixed
- Motion extraction export now matches CDPR's exporter (verified on 14,303 REDkit clips)
- Turns past 180 degrees and clips with an animated Root exported wrong motion
- Motion export picked up other NLA tracks; location-only bones exported wrong
- Additive clips exported as normal clips
- Clips in an Export Set picked up the pose of the action on the rig
- Bone scale tracks without a compression flag decoded as garbage
- Animation sets from older editor builds failed to list
- Sound-event string arrays lost entries on export
- Mixamo retarget with Root Motion made the character slide backwards in game
- FK/IK baking changed limb lengths, joint offsets and unrelated curve interpolation
- Character import left accessory armatures selected for baking
- Player spawned on cloud domes above the reference terrain
- AO power turned materials black at 1.0 and over-darkened tint-mask materials
- Rig export ignored the rotate-90 display fix and wrote a rolled rest pose
- Rig export crashed with no armature selected and left a stray `_OLD_FORMAT.json`
- Native writer wrote `StringAnsi` values one character short
- Preview environment list was empty without a REDkit depot

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

[Unreleased]: https://github.com/dingdio/Witcher3_Blender_Tools/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/dingdio/Witcher3_Blender_Tools/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/dingdio/Witcher3_Blender_Tools/compare/v1.0.1...v1.1.0
