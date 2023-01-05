# Witcher 3 Blender Tools
Blender addon for working with Witcher 3 files.

## Features

- Mesh importer (.w2mesh)
- Rig import/export (.w2rig, .json)
- Animation import/export (.w2anims, .json)
- Map importer (.w2l, .w2w)
- Basic map layer definition export for [radish tools](https://www.nexusmods.com/witcher3/mods/3620) (.yml)
- Characters/Entity definition (.w2ent) importer
- Lipsync Animation (.cr2w)

## Changelog

### 02-Jan-2023 (v0.5):
#### **Changes**:
* Mesh import (.w2mesh)
* animation export (.json)
* improverments to map and entity import

## Requirements
- Blender ~3.4.1

- [ArdCarraigh Blender_APX_Addon](https://github.com/ArdCarraigh/Blender_APX_Addon) - The APX addon is needed to load any redcloth items. You will have to export or download all apx from the game and add the Redcloth repo in addon settings. [Download all vanilla apx here.](https://mega.nz/file/CchGVCSb#ahDgIhxvicClEA9VHecPu6S95iT8ih2Q5kRMUHoY5ec)

    ### **Animation**:
    The current best way to work with the exported animation json is to use this specific version of Wolvenkit 0.6.1 compiled by nikich340 until Wolvenkit-7 is updated. Also the utility tool for modifying the exported files.
- [0.6.1-YML-W2ANIMS-APRIL04](https://github.com/nikich340/WolvenKit/releases/tag/0.6.1-YML-W2ANIMS-APRIL04)
- [W3-Maya-AnimUtil](https://github.com/nikich340/W3-Maya-AnimUtil) 
- [Some Wiki Notes](https://github.com/dingdio/Witcher3_Blender_Tools/wiki/Exporting-Animation-Notes)

## Recommended 

- [FBX Import plugin for blender](https://www.nexusmods.com/witcher3/mods/6118) - For working with any fbx files.

- [BlenderNormalGroups](https://github.com/theoldben/BlenderNormalGroups) Can switch normal map nodes to a faster custom node for better animation playback.

- [Prolog World Terrain Setup](https://mega.nz/file/WNZzCQQR#KICtWteq_OxwU_YKj4LU09kdJlBMqzzwIJd8DVGil4Q) - Not really required but has an example of how I set up basic terrain for the prolog world. It won't match how it looks in game but good enough to place game objects in blender. video - https://www.youtube.com/watch?v=qlRfUGMCyvQ

## Installation
Put "io_import_w2l" folder into your blender addons folder
Launch Blender and activate the addon in Blender Preferences

in the addon settings add your own paths to:
- uncook_path = main repo where you exported all the game bundles with wcclite.exe .w2mesh files, .w2mi files, .w2ent etc.
- tex_uncook_path = repo folder with ALL exported .tga from the game
- apx_uncook_path = repo folder with ALL exported .apx from the game

All these folders can be the same repo.

## Optional Setup

### Speech / Lipsync repo settings
- Extracted Lipsync repo = folder with .cr2w files
- Converted wem files = folder with (.ogg)s 
- [Wiki about speech](https://github.com/dingdio/Witcher3_Blender_Tools/wiki/Speech-Notes)

### FBX repo settings

- fbx_uncook_path = repo folder with ALL exported .fbx from the game

Since wcclite.exe has trouble exporting many fbx from the Witcher 3 game I have uploaded my collection of .fbx files along with Redcloth items in .apx format. Find them in this folder: [Folder Link](https://mega.nz/folder/GIR3AZBY#I4EEwkl4tjgnIv07f10n0A) These files are now optional for map/character loader since .w2mesh is now read directly. There is a toggle in settings to use fbx.

## Links
- https://github.com/WolvenKit/WolvenKit-7
- https://github.com/ArdCarraigh/Blender_APX_Addon
- https://github.com/nikich340/W3-Maya-AnimUtil
- https://github.com/nikich340/WolvenKit/releases/tag/0.6.1-YML-W2ANIMS-APRIL04
- https://www.nexusmods.com/witcher3/mods/3620
- https://jlouisb.users.sourceforge.net/
- https://github.com/Mets3D/batch_import_witcher3_fbx